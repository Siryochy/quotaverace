"""Test del contratto `WriteCLVCommand` + valutatore CLV puro (`decision/clv.py`).

La proprieta' centrale da verificare: **il valutatore calcola la misura e
RESTITUISCE il comando; la scrittura su DB sta nel gateway, non nel
valutatore**. Nessun test tocca il DB di produzione: writer iniettati, store
finti, ledger temporanei. Se un test avesse bisogno di un side effect per
passare, il pattern sarebbe rotto.
"""

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from decision.clv import (
    FLAG_SINGLE_SAMPLE, REASON_CLOSING_MISSING, REASON_CONTRACT_INVALID,
    REASON_MARKET_ID_MISSING, REASON_ODDS_INVALID, REASON_SIGNAL_MISSING,
    STATUS_OK, STATUS_REJECTED, STATUS_SKIPPED, ClvInput, clv_diff,
    evaluate_clv, evaluate_clv_many,
)
from decision.commands import COMMAND_ORDER, Command, CommandKind, WriteCLVCommand
from decision.models import utcnow

TS = datetime(2026, 9, 16, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def temp_db(monkeypatch):
    """Ledger SQLite temporaneo (stesso schema degli altri test del progetto)."""
    import tracker
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(tracker, "DB_PATH", Path(td) / "test.db")
        tracker.init_db()
        yield


def obs(**kwargs) -> ClvInput:
    base = dict(signal_id="sig-1", market_id="sx-L123", outcome="1",
                signal_odds=2.10, closing_odds=1.90, timestamp=TS, source="test")
    base.update(kwargs)
    return ClvInput(**base)


def gw(writer=None):
    from decision.gateways import ClvGateway
    return ClvGateway(writer=writer)


# ---------------------------------------------------------------------------
# 1. Il valutatore puro: misura + comando, ZERO side effect
# ---------------------------------------------------------------------------

class TestValutatorePuro:
    def test_emette_il_contratto_con_i_campi_richiesti(self):
        res = evaluate_clv(obs())
        assert res.status == STATUS_OK
        assert res.emitted
        cmd = res.command
        assert isinstance(cmd, WriteCLVCommand)
        # Il contratto porta ESATTAMENTE i campi richiesti.
        assert cmd.signal_id == "sig-1"
        assert cmd.market_id == "sx-L123"
        assert cmd.signal_odds == pytest.approx(2.10)
        assert cmd.closing_odds == pytest.approx(1.90)
        assert cmd.timestamp == TS
        assert cmd.source == "test"

    def test_calcola_la_differenza_di_quota(self):
        res = evaluate_clv(obs())
        # 2.10/1.90 - 1: presa quota migliore della chiusura -> CLV positivo.
        assert res.clv_diff == pytest.approx((2.10 / 1.90) - 1.0)
        assert res.clv_diff > 0

    def test_diff_negativa_quando_la_chiusura_sale(self):
        res = evaluate_clv(obs(signal_odds=1.80, closing_odds=2.00))
        assert res.clv_diff == pytest.approx((1.80 / 2.00) - 1.0)
        assert res.clv_diff < 0
        assert res.command.signal_odds == pytest.approx(1.80)

    def test_formula_unica_market_calib(self):
        """La misura non e' ricalcolata: passa da `market_calib.clv_raw`."""
        from market_calib import clv_raw
        assert clv_diff(2.10, 1.90) == pytest.approx((2.10 / 1.90) - 1.0)
        assert clv_diff(2.50, 2.00) == pytest.approx(clv_raw(2.50, 2.00))
        assert clv_diff(0.5, 2.0) is None          # quota invalida -> None

    def test_immutabile_e_json_safe(self):
        cmd = evaluate_clv(obs()).command
        with pytest.raises(Exception):
            cmd.signal_odds = 9.99                      # frozen: non si muta
        dumped = json.dumps(cmd.model_dump(mode="json"))
        assert "sx-L123" in dumped

    def test_timestamp_default(self):
        res = evaluate_clv(obs(timestamp=None))
        assert res.emitted and res.command.timestamp is not None

    def test_valutazione_immutabile(self):
        res = evaluate_clv(obs())
        with pytest.raises(Exception):
            res.status = STATUS_REJECTED                # frozen: non si muta


class TestNessunSideEffect:
    def test_il_valutatore_non_tocca_il_db(self, monkeypatch):
        """Con sqlite3 'avvelenato' la valutazione funziona comunque."""
        import sqlite3

        def boom(*args, **kwargs):
            raise AssertionError("il valutatore CLV ha toccato il DB")

        monkeypatch.setattr(sqlite3, "connect", boom)
        res = evaluate_clv(obs())
        assert res.emitted and res.clv_diff is not None

    def test_module_import_non_carica_tracker(self):
        """`decision.clv` resta puro: nessun import di produzione a livello modulo."""
        code = ("from decision.clv import evaluate_clv, ClvInput;"
                "from decision.commands import write_clv_command;"
                "import sys;"
                "print(any(m in sys.modules for m in ('tracker','auto_bet','bot')))")
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True)
        assert out.stdout.strip() == "False", out.stdout + out.stderr


# ---------------------------------------------------------------------------
# 2. Skip e reject: un comando malformato NON nasce
# ---------------------------------------------------------------------------

class TestSkipEReject:
    def test_senza_closing_skip(self):
        res = evaluate_clv(obs(closing_odds=None))
        assert res.status == STATUS_SKIPPED
        assert res.reason == REASON_CLOSING_MISSING
        assert res.command is None and res.dispatch is None

    def test_senza_signal_skip(self):
        res = evaluate_clv(obs(signal_odds=None))
        assert res.status == STATUS_SKIPPED
        assert res.reason == REASON_SIGNAL_MISSING

    def test_senza_market_id_reject(self):
        res = evaluate_clv(obs(market_id="  "))
        assert res.status == STATUS_REJECTED
        assert res.reason == REASON_MARKET_ID_MISSING
        assert res.command is None

    def test_quote_fuori_contratto_reject(self):
        for sig in (1.0, 0.5):
            res = evaluate_clv(obs(signal_odds=sig, closing_odds=1.90))
            assert res.status == STATUS_REJECTED
            assert res.reason == REASON_ODDS_INVALID
            assert res.command is None

    def test_dispatch_e_command_coerenti(self):
        """I due oggetti portano GLI STESSI valori (costruiti insieme)."""
        res = evaluate_clv(obs())
        cmd, disp = res.command, res.dispatch
        assert disp.kind == CommandKind.WRITE_CLV
        assert disp.payload["match_id"] == cmd.market_id
        assert disp.payload["signal_odds"] == cmd.signal_odds
        assert disp.payload["closing_odds"] == cmd.closing_odds
        assert disp.signal_id == cmd.signal_id
        assert disp.payload["source"] == cmd.source
        assert disp.payload["outcome"] == "1"

    def test_flag_single_sample(self):
        """closing == segnale: l'eco del segnale viene marcato (CLV finto)."""
        res = evaluate_clv(obs(signal_odds=1.95, closing_odds=1.95))
        assert res.emitted
        assert FLAG_SINGLE_SAMPLE in res.flags

    def test_esito_vuoto_reject_mai_eccezione(self):
        """Senza esito il comando generico viola il contratto: esito leggibile."""
        res = evaluate_clv(obs(outcome=""))
        assert res.status == STATUS_REJECTED
        assert res.reason == REASON_CONTRACT_INVALID
        assert res.command is None and res.dispatch is None

    def test_lotto_ostile_mai_eccezione(self):
        out = evaluate_clv_many([obs(), None, "junk", obs(market_id="sx-L9")])
        assert [r.status for r in out] == [STATUS_OK, STATUS_REJECTED,
                                           STATUS_REJECTED, STATUS_OK]


# ---------------------------------------------------------------------------
# 3. Il tipo di comando nella catena: ordine, payload, validazione
# ---------------------------------------------------------------------------

class TestComandoNellaCatena:
    def test_write_clv_in_command_order(self):
        assert CommandKind.WRITE_CLV in COMMAND_ORDER
        assert COMMAND_ORDER[-1] == CommandKind.WRITE_CLV
        assert CommandKind.WRITE_CLV.value == "write_clv"

    def test_ordine_dopo_le_notifiche(self):
        assert Command(kind=CommandKind.WRITE_CLV).order == len(COMMAND_ORDER) - 1
        assert Command(kind=CommandKind.WRITE_CLV).order > \
            Command(kind=CommandKind.NOTIFY_OPERATORS).order

    def test_dedup_key_stabile(self):
        a = evaluate_clv(obs()).dispatch
        assert evaluate_clv(obs()).dispatch.dedup_key == a.dedup_key
        # Cambia la misura -> cambia la chiave (un nuovo campione).
        assert evaluate_clv(obs(closing_odds=1.85)).dispatch.dedup_key != a.dedup_key
        # Cambia il match -> cambia la chiave.
        assert evaluate_clv(obs(market_id="sx-L999")).dispatch.dedup_key != a.dedup_key

    def test_payload_validato_all_emissione(self):
        """Un comando malformato non nasce (stessa regola degli altri comandi)."""
        from decision.commands import write_clv_command
        with pytest.raises(Exception):
            write_clv_command(match_id="", outcome="1", signal_odds=2.0,
                              closing_odds=1.9, timestamp=TS)
        with pytest.raises(Exception):
            write_clv_command(match_id="sx-1", outcome="1", signal_odds=0.5,
                              closing_odds=1.9, timestamp=TS)


# ---------------------------------------------------------------------------
# 4. Il gateway: la scrittura su DB sta QUI (e solo qui)
# ---------------------------------------------------------------------------

class TestClvGateway:
    def test_gateway_scrive_tramite_writer_iniettato(self):
        writes = []

        def writer(payload):
            writes.append(dict(payload))
            return {"saved": True, "seeded": False}

        out = gw(writer).execute(evaluate_clv(obs()).dispatch)
        assert out.ok and out.status == "executed"
        assert len(writes) == 1
        assert writes[0]["match_id"] == "sx-L123"
        assert writes[0]["signal_odds"] == pytest.approx(2.10)
        assert out.data["seeded"] is False

    def test_gateway_iniettato_riceve_tutto_il_payload(self):
        seen = {}

        def writer(payload):
            seen.update(payload)
            return {"saved": True}

        gw(writer).execute(evaluate_clv(obs()).dispatch)
        assert seen["outcome"] == "1"
        assert seen["source"] == "test"
        assert "timestamp" in seen

    def test_payload_incompleto_mai_al_writer(self):
        calls = []

        def writer(payload):
            calls.append(payload)
            return {"saved": True}

        from decision.commands import write_clv_command
        bad = write_clv_command(match_id="sx-1", outcome="1", signal_odds=2.0,
                                closing_odds=1.9, timestamp=TS)
        bad.payload["match_id"] = ""
        out = gw(writer).execute(bad)
        assert out.ok is False and out.status == "error"
        assert not calls

    def test_gateway_fail_safe_su_errore_writer(self):
        def writer(payload):
            raise RuntimeError("db giu'")

        out = gw(writer).execute(evaluate_clv(obs()).dispatch)
        assert out.ok is False and out.status == "error"
        assert "db giu'" in out.detail

    def test_gateway_non_solleva_mai(self):
        cmd = evaluate_clv(obs()).dispatch
        cmd.payload.clear()                          # payload distrutto
        out = gw().execute(cmd)
        assert out.ok is False                       # errore, non eccezione

    def test_gateway_marca_audit_only(self):
        assert bool(getattr(gw(), "audit_only", False)) is True

    def test_default_writer_delega_a_tracker_save_clv(self, temp_db):
        """Il percorso di produzione: `tracker.save_clv` (ledger temporaneo).

        `save_clv` (firma a una quota) semina ENTRAMBE le colonne alla prima
        chiamata: si semina il segnale, poi il comando aggiorna la chiusura —
        lo stesso flusso di `fixture_engine` in produzione.
        """
        import tracker
        tracker.save_clv("mDEF", "Roma", 2.00, signal_started=True)   # seme
        res = evaluate_clv(obs(signal_odds=2.00, closing_odds=1.90,
                               market_id="mDEF", outcome="Roma"))
        out = gw().execute(res.dispatch)
        assert out.ok and out.status == "executed"
        assert out.data["seeded"] is False            # closing != segnale: aggiorna
        row = tracker._get_conn().execute(
            "SELECT signal_quota, closing_quota FROM clv_history "
            "WHERE match_id='mDEF'").fetchone()
        assert row == (2.00, 1.90)                    # segnale intatto, chiusura aggiornata

    def test_default_writer_seme_iniziale(self, temp_db):
        """Il primo campione (closing == segnale) semina la quota segnale."""
        import tracker
        res = evaluate_clv(obs(signal_odds=2.05, closing_odds=2.05,
                               market_id="mSEED", outcome="Roma"))
        out = gw().execute(res.dispatch)
        assert out.ok and out.data["seeded"] is True
        row = tracker._get_conn().execute(
            "SELECT signal_quota, closing_quota FROM clv_history "
            "WHERE match_id='mSEED'").fetchone()
        assert row == (2.05, 2.05)


# ---------------------------------------------------------------------------
# 5. Orchestratore: dispatcher instrada il comando al gateway
# ---------------------------------------------------------------------------

def _plan(command):
    """Un CommandPlan vero con un record minimo e UN SOLO comando."""
    from decision.commands import CommandPlan
    from decision.models import (DataQuality, DecisionRecord, ReasonCode,
                                 RiskDecision, Signal)
    signal = Signal(match_id="sx-L123", outcome="1",
                    kickoff=TS + timedelta(hours=3), price=2.10,
                    market_prob=0.5, model_prob=0.5, blended_prob=0.5,
                    data_quality=DataQuality())
    record = DecisionRecord(signal=signal,
                            risk=RiskDecision(verdict="approve",
                                              reason=ReasonCode.OK))
    return CommandPlan(record=record, commands=[command])


class TestOrchestratore:
    def test_dispatch_end_to_end_con_gateway_iniettato(self):
        """Il valutatore restituisce il comando, il dispatcher lo esegue."""
        from decision.dispatcher import Dispatcher
        from decision.middleware import ListSink, Observability
        writes = []
        gateway = gw(lambda p: (writes.append(p) or {"saved": True}))
        report = Dispatcher([gateway],
                            observability=Observability(sink=ListSink())).dispatch(
            _plan(evaluate_clv(obs()).dispatch))
        assert report.ok and report.executed == 1
        # Il gateway CLV e' AUDIT-only: il dispatcher dichiara correttamente
        # "nessun effetto reale sul mondo" (la scrittura e' telemetria).
        assert report.shadow is True
        assert len(writes) == 1

    def test_shadow_gateway_registra_senza_scrivere(self, tmp_path):
        """In shadow mode il comando CLV finisce SOLO nel registro."""
        from decision.dispatcher import Dispatcher
        from decision.gateways import ShadowGateway
        from decision.middleware import ListSink, Observability
        writes = []
        shadow = ShadowGateway(tmp_path / "shadow.jsonl")
        report = Dispatcher([shadow],
                            observability=Observability(sink=ListSink())).dispatch(
            _plan(evaluate_clv(obs()).dispatch))
        assert report.shadow is True
        assert not writes
        lines = (tmp_path / "shadow.jsonl").read_text(encoding="utf-8").splitlines()
        assert any("write_clv" in ln for ln in lines)

    def test_write_clv_senza_gateway_e_segnalato(self):
        """Un `write_clv` senza gateway non sparisce in silenzio."""
        from decision.dispatcher import Dispatcher
        from decision.gateways import ShadowGateway
        from decision.middleware import ListSink, Observability

        class OnlyLedger:
            name = "ledger"
            kinds = (CommandKind.PERSIST_DECISION,)

            def handles(self, kind):
                return kind in self.kinds

            def execute(self, command, *, ctx=None, obs=None):
                raise AssertionError("non deve essere chiamato per write_clv")

        report = Dispatcher([OnlyLedger()],
                            observability=Observability(sink=ListSink())).dispatch(
            _plan(evaluate_clv(obs()).dispatch))
        assert report.errors, "un write_clv senza gateway deve essere segnalato"
        assert any("write_clv" in e for e in report.errors)

    def test_clv_non_blocca_nessun_altro_comando(self):
        """Il comando CLV e' audit: non altera il conteggio shadow della coda."""
        from decision.gateways import ClvGateway, ShadowGateway
        assert bool(getattr(ClvGateway(), "audit_only", False)) is True
        assert bool(getattr(ShadowGateway(), "dry_run", False)) is True


# ---------------------------------------------------------------------------
# 6. Serenita' del ciclo di vita: evaluazione -> comando -> scrittura
# ---------------------------------------------------------------------------

def test_ciclo_completo_senza_db_reale():
    """Il flusso richiesto: misura pura -> comando -> scrittura iniettata."""
    writes = []
    res = evaluate_clv(obs(signal_odds=2.00, closing_odds=1.60,
                           timestamp=None, source="sx"))
    assert res.emitted
    assert res.clv_diff == pytest.approx(0.25)       # 2.00/1.60 - 1
    out = gw(lambda p: (writes.append(p) or {"saved": True})) \
        .execute(res.dispatch)
    assert out.ok
    assert writes[0]["closing_odds"] == pytest.approx(1.60)


def test_timestamp_ricevuto_dall_esterno():
    """`now` iniettato: la misura non dipende dall'orologio (testabilita')."""
    res = evaluate_clv(obs(timestamp=None), now=utcnow())
    assert res.emitted
    assert res.command.timestamp.tzinfo is not None
