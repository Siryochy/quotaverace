"""Test di osservabilita' + gateway + dispatcher — OFFLINE.

Coprono il secondo e il terzo pezzo della rifattorizzazione del 15/09:

- **middleware**: log JSON strutturati con request/trace/span id e config hash,
  verso un sink configurabile, senza mai sollevare;
- **gateway + dispatcher**: gli effetti escono dal motore e vengono instradati.
  I gateway "veri" che toccano il mondo (ordini, Telegram, ledger) si testano
  con **finti iniettati in memoria** (nessuna fixture su disco, nessuna rete):
  e' la modalita' di sviluppo offline che non consuma crediti API.
"""

import json
import threading
from pathlib import Path

import pytest

from decision.commands import CommandKind, notify_command, persist_decision_command
from decision.dispatcher import Dispatcher, GatewayError
from decision.gateways import (
    BaseGateway, CommandResult, LedgerGateway, NotifyGateway, PlaceOrderGateway,
    ShadowGateway,
)
from decision.limits import RiskLimits
from decision.middleware import (
    JsonlSink, ListSink, NullSink, Observability, TraceContext, config_hash,
    default_sink_path, redact, sink_from_env,
)
from decision.models import DecisionRecord, KillSwitchStatus, ReasonCode, risk_approve
from test_decision_commands import make_signal, plan_for


@pytest.fixture
def limits():
    return RiskLimits.from_env()


# ---------------------------------------------------------------------------
# Middleware: trace, span, config hash
# ---------------------------------------------------------------------------

class TestTraceESpan:
    def test_span_emette_start_e_end_correlati(self):
        sink = ListSink()
        obs = Observability(sink=sink, component="test")
        root = obs.new_trace(request_id="richiesta-1")
        with obs.span("risk", ctx=root, stage="risk") as scope:
            assert scope.trace_id == root.trace_id
            assert scope.parent_span_id == root.span_id
            assert scope.request_id == "richiesta-1"

        start, end = sink.of("span.start")[0], sink.of("span.end")[0]
        assert start["span_name"] == "risk"
        assert start["span_id"] == end["span_id"] == scope.span_id
        assert start["trace_id"] == end["trace_id"] == root.trace_id
        assert end["outcome"] == "ok"
        assert isinstance(end["duration_ms"], float)

    def test_span_senza_contesto_apre_una_trace(self):
        sink = ListSink()
        obs = Observability(sink=sink)
        with obs.span("guards"):
            pass
        start = sink.of("span.start")[0]
        assert start["request_id"] and start["trace_id"] and start["span_id"]
        assert start["parent_span_id"] == ""

    def test_span_error_ri_solleva_e_traccia(self):
        sink = ListSink()
        obs = Observability(sink=sink)
        with pytest.raises(ValueError):
            with obs.span("decide"):
                raise ValueError("gate incoerente")
        error = sink.of("span.error")[0]
        assert "ValueError: gate incoerente" in error["error"]
        assert error["outcome"] == "error"

    def test_ctx_child_condivide_request_e_trace(self):
        parent = TraceContext(request_id="r", trace_id="t")
        child = parent.child()
        assert (child.request_id, child.trace_id) == ("r", "t")
        assert child.parent_span_id == parent.span_id != child.span_id


class TestConfigHash:
    def test_stabile_fra_istanze(self):
        assert config_hash(RiskLimits.from_env()) == config_hash(RiskLimits.from_env())

    def test_cambia_se_cambia_una_soglia(self):
        base = RiskLimits.from_env()
        changed = base.model_copy(update={"odds_max": base.odds_max + 0.15})
        assert config_hash(base) != config_hash(changed)

    def test_finisce_in_ogni_evento(self):
        sink = ListSink()
        obs = Observability(sink=sink)
        obs.event("qualcosa")
        assert sink.events[0]["config_hash"] == obs.config_fingerprint

    def test_extra_entra_nell_impronta(self):
        base = RiskLimits.from_env()
        assert config_hash(base, extra={"shadow": True}) != config_hash(base, extra={"shadow": False})


class TestSink:
    def test_sink_da_env(self, monkeypatch):
        assert isinstance(sink_from_env("off"), NullSink)
        assert isinstance(sink_from_env("null"), NullSink)
        assert isinstance(sink_from_env("stdout"), JsonlSink)
        assert sink_from_env("stdout").path is None
        assert str(sink_from_env("/tmp/eventi.jsonl").path) == "/tmp/eventi.jsonl"

    def test_sink_default_sul_volume(self):
        sink = sink_from_env("")
        assert isinstance(sink, JsonlSink)
        assert sink.path == default_sink_path()
        assert sink.path.name == "events.jsonl"

    def test_jsonl_scritto_e_rileggibile(self, tmp_path):
        path = tmp_path / "eventi.jsonl"
        sink = JsonlSink(path)
        sink.write({"event": "a", "n": 1})
        sink.write({"event": "b", "n": 2})
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert [line["event"] for line in lines] == ["a", "b"]

    def test_osservabilita_spenta(self):
        sink = ListSink()
        obs = Observability(sink=sink, enabled=False)
        obs.event("qualcosa")
        assert sink.events == []

    def test_rotazione_del_sink(self, tmp_path):
        """Il sink sul volume non cresce senza limite (job ogni 60s)."""
        from decision.middleware import JsonlSink
        path = tmp_path / "events.jsonl"
        sink = JsonlSink(path, max_bytes=200, generations=2)
        for index in range(12):
            sink.write({"event": "e", "i": index, "pad": "x" * 40})

        assert Path(f"{path}.1").exists()          # almeno una rotazione
        assert path.stat().st_size < 400            # il corrente resta piccolo
        # ogni riga di ogni generazione resta JSON valido
        for candidate in (path, Path(f"{path}.1")):
            for line in candidate.read_text().splitlines():
                assert json.loads(line)["event"] == "e"
        assert json.loads(path.read_text().splitlines()[-1])["i"] == 11

    def test_sink_iniettato_vince_sull_env(self, monkeypatch):
        """Con `DECISION_LOG_SINK=off` un sink passato dall'esterno scrive."""
        monkeypatch.setenv("DECISION_LOG_SINK", "off")
        injected = ListSink()
        Observability(sink=injected).event("scrive")
        assert [e["event"] for e in injected.events] == ["scrive"]

        # ... mentre il middleware senza sink resta spento
        assert Observability().enabled is False

    def test_sink_rotto_non_solleva(self, tmp_path):
        class Rotto:
            name = "rotto"

            def write(self, event):
                raise OSError("disco pieno")

        obs = Observability(sink=Rotto())
        obs.event("qualcosa")            # nessuna eccezione
        with obs.span("x"):
            pass
        assert obs.enabled is True       # non si spegne da solo

    def test_redact_maschera_i_segreti(self):
        out = redact({"api_key": "abc", "nested": {"password": "x", "ok": 1},
                      "text": "ciao"})
        assert out["api_key"] == "***"
        assert out["nested"]["password"] == "***"
        assert out["nested"]["ok"] == 1 and out["text"] == "ciao"

    def test_evento_con_dettagli_sensibili_non_li_scrive(self):
        sink = ListSink()
        Observability(sink=sink).event("x", details={"token": "segreto-vero"})
        assert "segreto-vero" not in json.dumps(sink.events[0])


# ---------------------------------------------------------------------------
# Gateway + dispatcher (dati mock in memoria)
# ---------------------------------------------------------------------------

class FakeGateway(BaseGateway):
    """Gateway finto in memoria: e' il "mock" usato in sviluppo/test."""

    name = "fake"
    kinds = (CommandKind.PERSIST_DECISION,)

    def __init__(self) -> None:
        self.commands = []

    def _run(self, command, *, ctx, obs):
        self.commands.append(command)
        return CommandResult(kind=command.kind, ok=True, status="executed",
                             detail="eseguito dal finto", data={"ok": True})


class BrokenGateway(BaseGateway):
    name = "broken"
    kinds = tuple(CommandKind)

    def _run(self, command, *, ctx, obs):
        raise RuntimeError("gateway esploso")


class TestDispatcher:
    def test_instrada_al_gateway_giusto(self, limits):
        plan = plan_for(limits=limits)
        fake = FakeGateway()
        report = Dispatcher([fake], observability=Observability(sink=ListSink())).dispatch(plan)
        assert [c.kind for c in fake.commands] == [CommandKind.PERSIST_DECISION]
        assert report.executed == 1
        assert report.ok is False                       # place_order non gestito
        assert any("place_order" in err for err in report.errors)

    def test_comando_senza_gateway_finisce_nei_skip(self, limits):
        plan = plan_for(limits=limits, mode="sim")
        report = Dispatcher([FakeGateway()], observability=Observability(sink=ListSink())
                            ).dispatch(plan)
        assert report.ok is True and report.executed == 1 and report.skipped == 0

    def test_gateway_che_esplode_non_ferma_il_dispatch(self, limits):
        plan = plan_for(limits=limits, mode="sim")
        report = Dispatcher([BrokenGateway(), FakeGateway()],
                            observability=Observability(sink=ListSink())).dispatch(plan)
        # il primo gateway che gestisce il comando e' quello rotto
        assert report.ok is False and "gateway esploso" in " ".join(report.errors)

    def test_raise_on_error_solleva(self, limits):
        plan = plan_for(limits=limits)
        with pytest.raises(GatewayError) as excinfo:
            Dispatcher([FakeGateway()], observability=Observability(sink=ListSink()),
                       raise_on_error=True).dispatch(plan)
        # il motivo c'e' anche quando il fallimento non ha un CommandResult
        # (comando senza gateway): l'errore deve dire COSA e' mancato.
        assert any("place_order" in err for err in excinfo.value.errors)

    def test_span_per_ogni_comando(self, limits):
        sink = ListSink()
        plan = plan_for(limits=limits, mode="sim")
        Dispatcher([FakeGateway()], observability=Observability(sink=sink)).dispatch(plan)
        spans = [e["span_name"] for e in sink.of("span.start")]
        assert "command.persist_decision" in spans
        assert sink.of("plan.dispatch") and sink.of("plan.dispatched")
        assert sink.of("command.result")[0]["status"] == "executed"

    def test_shadow_riconosciuta(self, limits, tmp_path):
        plan = plan_for(limits=limits)
        dispatcher = Dispatcher([ShadowGateway(tmp_path / "shadow.jsonl")],
                                observability=Observability(sink=ListSink()))
        report = dispatcher.dispatch(plan)
        assert report.shadow is True
        assert report.duplicated == 0 and report.executed == 2
        assert json.loads((tmp_path / "shadow.jsonl").read_text().splitlines()[0])


class TestGatewayProduzione:
    def test_ledger_gateway_usa_il_writer_iniettato(self, limits):
        calls = []

        def writer(row):
            calls.append(row)
            return {"saved": True, "record_id": row.get("record_id"), "error": ""}

        plan = plan_for(limits=limits, mode="sim")
        result = LedgerGateway(persist=writer).execute(
            plan.of_kind(CommandKind.PERSIST_DECISION)[0],
            obs=Observability(sink=ListSink()))
        assert result.ok and result.status == "executed"
        assert calls[0]["verdict"] == "approve"

    def test_ledger_gateway_errore_del_writer(self, limits):
        plan = plan_for(limits=limits, mode="sim")
        result = LedgerGateway(persist=lambda row: {"saved": False, "error": "db chiuso"}
                               ).execute(plan.of_kind(CommandKind.PERSIST_DECISION)[0])
        assert result.ok is False and "db chiuso" in result.detail

    def test_place_order_dry_run_non_esegue(self, limits):
        called = []
        gateway = PlaceOrderGateway(dry_run=True,
                                    fill=lambda *a: called.append(a) or {"ok": True})
        plan = plan_for(limits=limits)
        result = gateway.execute(plan.of_kind(CommandKind.PLACE_ORDER)[0])
        assert result.status == "recorded" and result.dry_run is True
        assert called == []                      # nessuna esecuzione

    def test_place_order_in_sim_non_ordina(self, limits):
        plan = plan_for(limits=limits, mode="sim")
        command = _order_command(plan.record)
        result = PlaceOrderGateway().execute(command)
        assert result.status == "skipped" and result.ok

    def test_place_order_eseguito_col_finto(self, limits):
        plan = plan_for(limits=limits)
        gateway = PlaceOrderGateway(fill=lambda pick, stake, price: {
            "ok": True, "status": "FULLY_FILLED", "bet_id": "tx-1",
            "price": price, "stake": stake})
        result = gateway.execute(plan.of_kind(CommandKind.PLACE_ORDER)[0])
        assert result.ok and result.data["bet_id"] == "tx-1"

    def test_place_order_saltato_e_rifiutato(self, limits):
        plan = plan_for(limits=limits)
        command = plan.of_kind(CommandKind.PLACE_ORDER)[0]
        assert PlaceOrderGateway(fill=lambda *a: None).execute(command).status == "skipped"
        refused = PlaceOrderGateway(fill=lambda *a: {"ok": False, "error": "CANCELLED"}
                                    ).execute(command)
        assert refused.ok is False and "CANCELLED" in refused.detail

    def test_notify_gateway_con_sender_finto(self, limits):
        sent = []
        gateway = NotifyGateway(sender=lambda text, targets: sent.append((text, targets)) or True,
                                targets=["42"])
        command = notify_command(DecisionRecord(signal=make_signal(),
                                                risk=risk_approve(checked=[]),
                                                mode="live"),
                                 kind="review_pending", text="ciao")
        result = gateway.execute(command)
        assert result.ok and sent[0][1] == ["42"]

    def test_notify_senza_destinatari_skippa(self, limits):
        gateway = NotifyGateway(sender=lambda t, x: True, targets=[])
        command = notify_command(_record(limits), kind="blocked", text="ciao")
        assert gateway.execute(command).status == "skipped"

    def test_notify_invio_fallito_diventa_errore(self, limits):
        gateway = NotifyGateway(sender=lambda t, x: False, targets=["1"])
        command = notify_command(_record(limits), kind="blocked", text="ciao")
        result = gateway.execute(command)
        assert result.ok is False and result.status == "error"


# ---------------------------------------------------------------------------
# Shadow gateway
# ---------------------------------------------------------------------------

class TestShadowGateway:
    def test_registra_senza_eseguire(self, limits, tmp_path):
        path = tmp_path / "shadow.jsonl"
        gateway = ShadowGateway(path)
        plan = plan_for(limits=limits)
        result = gateway.execute(plan.of_kind(CommandKind.PLACE_ORDER)[0],
                                 obs=Observability(sink=ListSink()))
        assert result.status == "recorded" and result.dry_run
        entry = json.loads(path.read_text().splitlines()[0])
        assert entry["command"]["kind"] == "place_order"
        assert entry["config_hash"]

    def test_dedup_key_evita_duplicati(self, limits, tmp_path):
        path = tmp_path / "shadow.jsonl"
        gateway = ShadowGateway(path)
        command = plan_for(limits=limits).of_kind(CommandKind.PLACE_ORDER)[0]
        assert gateway.execute(command).status == "recorded"
        assert gateway.execute(command).status == "duplicate"
        assert len(path.read_text().splitlines()) == 1

    def test_dedup_sopravvive_al_riavvio(self, limits, tmp_path):
        path = tmp_path / "shadow.jsonl"
        command = plan_for(limits=limits).of_kind(CommandKind.PLACE_ORDER)[0]
        ShadowGateway(path).execute(command)
        again = ShadowGateway(path)                 # nuovo processo
        assert again.execute(command).status == "duplicate"

    def test_registro_corrotto_non_blocca(self, limits, tmp_path):
        path = tmp_path / "shadow.jsonl"
        path.write_text("{non-json\n" + json.dumps({"dedup_key": "x"}) + "\n")
        gateway = ShadowGateway(path)
        command = plan_for(limits=limits).of_kind(CommandKind.PLACE_ORDER)[0]
        assert gateway.execute(command).status == "recorded"

    def test_dedup_senza_memoria_registra_sempre(self, limits, tmp_path):
        path = tmp_path / "shadow.jsonl"
        command = plan_for(limits=limits).of_kind(CommandKind.PLACE_ORDER)[0]
        gateway = ShadowGateway(path, remember=False)
        assert gateway.execute(command).status == "recorded"
        assert gateway.execute(command).status == "recorded"


# ---------------------------------------------------------------------------
# Tripwire
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_shadow_non_importa_il_percorso_di_esecuzione(self, limits, tmp_path):
        """Il gateway shadow non deve toccare auto_bet (nessun ordine possibile)."""
        import subprocess
        import sys
        code = (
            "from decision.gateways import ShadowGateway;"
            "from decision.commands import Command, CommandKind;"
            "import sys, tempfile, os;"
            "path = os.path.join(tempfile.mkdtemp(), 's.jsonl');"
            "gw = ShadowGateway(path);"
            "print(gw.execute(Command(kind=CommandKind.PLACE_ORDER, payload={"
            "'mode': 'live', 'price': 1.6, 'stake': 1.0})).status,"
            "'auto_bet' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.stdout.strip() == "recorded False", out.stdout + out.stderr

    def test_comandi_in_parallelo_non_si_pestano(self, limits, tmp_path):
        """Il registro shadow non corrompe le righe con scritture concorrenti."""
        path = tmp_path / "shadow.jsonl"
        plan = plan_for(limits=limits)
        command = plan.of_kind(CommandKind.PLACE_ORDER)[0]

        def worker():
            ShadowGateway(path, remember=False).execute(command)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        lines = path.read_text().splitlines()
        assert len(lines) == 5
        for line in lines:
            assert json.loads(line)["command"]["kind"] == "place_order"


def _record(limits):
    from decision import decide
    return decide(make_signal(), kills=KillSwitchStatus(mode="live", provider_ready=True),
                  limits=limits, bankroll=1000.0)


def _order_command(record):
    from decision.commands import place_order_command
    return place_order_command(record)
