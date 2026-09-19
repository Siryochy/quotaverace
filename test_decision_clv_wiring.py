"""Wiring del percorso CLV nella shadow mode (`decision/shadow.py`) — OFFLINE.

Cosa si verifica (direttiva 17/09: wiring in parallelo + smoke + tracciabilita'):

1. **In parallelo, mai al posto**: `evaluate_clv` -> `WriteCLVCommand` ->
   `ClvGateway` gira accanto alla catena e il writer e' quello SHADOW
   (evento `clv.shadow_sample`): il ledger `clv_history` resta di
   `fixture_engine` e NON riceve una riga.
2. **Esiti tracciati**: ok / skipped / rejected contati nel riepilogo
   (`out["clv"]`) e negli eventi di osservabilita' (`clv.lateral`),
   con request/trace/span id.
3. **Fail-safe**: il percorso CLV non aggiunge errori al giro shadow e non
   rompe mai il job (osservazione ostile = `rejected`, non traceback).
4. **Interruttore**: `DECISION_CLV_SHADOW=0` (o `clv_enabled=False`) spegne
   tutto, senza toccare il resto del giro.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import tracker
from decision import KillSwitchStatus
from decision.clv import REASON_CLOSING_MISSING
from decision.feeds import DEFAULT_GATEWAY_ID, FeedSnapshot, StaticSource
from decision.middleware import ListSink, Observability
from decision.shadow import (
    CLV_ENV, clv_shadow_enabled, run_shadow,
)
from decision.shadow import shadow_summary
from test_decision_commands import make_signal
from test_decision_feed import valid_row, validate as feed_validate

ALLOWED_LEAGUE = "Premier League"


def live():
    return KillSwitchStatus(mode="live", env_mode="live", provider_ready=True)


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "wiring.db")
    conn = tracker._get_conn()
    conn.close()
    return tmp_path


def run(*args, **kwargs):
    """`run_shadow` con `path` come alias di `shadow_path` (concisione nei test)."""
    if "path" in kwargs:
        kwargs["shadow_path"] = kwargs.pop("path")
    return run_shadow(*args, **kwargs)


# ---------------------------------------------------------------------------
# 1. Il percorso gira in parallelo, con writer shadow
# ---------------------------------------------------------------------------

class TestPercorsoLaterale:
    def test_gira_accanto_alla_catena_e_non_scrive_su_clv_history(self, db, tmp_path):
        """Un giro normale: la catena produce i suoi comandi e il CLV aggiunge
        il suo campione SHADOW — zero righe su `clv_history`."""
        path = tmp_path / "shadow.jsonl"
        # Closing dal feed: senza, la valutazione e' `skipped` (onesto) e non
        # c'e' nessun comando da registrare.
        feed = _validated_feed(valid_row(event_id="sx-1", selection="1", odds=1.50))
        out = run(signals=[make_signal()], kills=live(), bankroll=1000.0,
                  mode="live", path=path, feed=feed)
        # La catena non e' cambiata.
        assert out["by_command"] == {"persist_decision": 1, "place_order": 1}
        # Il percorso CLV ha girato: comando emesso, eseguito sul gateway.
        assert out["clv"]["enabled"] is True
        assert out["clv"]["ok"] == 1
        assert out["clv"]["dispatched"] == 1
        assert out["clv"]["avg_diff"] == pytest.approx((1.60 / 1.50) - 1.0, abs=1e-3)
        # ...e nessuna scrittura sul ledger reale del CLV.
        conn = sqlite3.connect(str(db / "wiring.db"))
        rows = conn.execute("SELECT COUNT(*) FROM clv_history").fetchone()[0]
        conn.close()
        assert rows == 0

    def test_il_campione_shadow_finisce_nel_registro(self, db, tmp_path):
        path = tmp_path / "shadow.jsonl"
        feed = _validated_feed(valid_row(event_id="sx-1", selection="1", odds=1.50))
        out = run(signals=[make_signal()], kills=live(), bankroll=1000.0,
                  mode="live", path=path, feed=feed)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        kinds = [entry["command"]["kind"] for entry in lines]
        # persist + place_order della catena, write_clv del percorso laterale.
        assert sorted(kinds) == ["persist_decision", "place_order", "write_clv"]
        clv_entry = next(e for e in lines if e["command"]["kind"] == "write_clv")
        payload = clv_entry["command"]["payload"]
        assert payload["match_id"] == "sx-1"
        assert payload["outcome"] == "1"
        assert payload["signal_odds"] == pytest.approx(1.60)
        assert clv_entry["command"]["signal_id"]
        assert out["clv"]["errors"] == 0

    def test_chiusura_assente_nel_blocco_ferm_resto_skip(self, db, tmp_path):
        """Con le puntate ferme il percorso CLV non gira affatto (fail-fast
        della shadow resta la prima autorita')."""
        out = run(signals=[make_signal()], kills=KillSwitchStatus(mode="off"),
                         path=tmp_path / "s.jsonl")
        assert out["blocked"] is not None
        assert out["clv"]["ok"] == 0 and out["clv"]["skipped"] == 0
        assert out["clv"]["enabled"] is False


# ---------------------------------------------------------------------------
# 2. Tracciabilita' degli esiti (ok / skipped / rejected)
# ---------------------------------------------------------------------------

class TestEsiti:
    def test_ok_con_closing_dal_feed(self, db, tmp_path):
        """Chiusura disponibile dal feed del giro: esito ok + differenza calcolata."""
        # Riga conforme per l'evento sx-1 (make_signal usa match_id="sx-1").
        row = valid_row(event_id="sx-1", selection="1", odds=1.50)
        feed = _validated_feed(row)
        sink = ListSink()
        out = run(signals=[make_signal()], kills=live(), bankroll=1000.0,
                         mode="live", path=tmp_path / "s.jsonl", feed=feed,
                         observability=Observability(sink=sink))
        assert out["clv"]["ok"] == 1 and out["clv"]["dispatched"] == 1
        # closing = 1.50 (feed), segnale = 1.60 -> CLV positivo.
        assert out["clv"]["avg_diff"] == pytest.approx((1.60 / 1.50) - 1.0, abs=1e-4)
        lateral = [e for e in sink.events if e["event"] == "clv.lateral"]
        assert lateral and lateral[0]["outcome"] == "ok"
        assert lateral[0]["clv_diff"] == pytest.approx((1.60 / 1.50) - 1.0, abs=1e-4)

    def test_skipped_senza_closing(self, db, tmp_path):
        """Senza feed il closing non c'e': skip onesto, non errore."""
        sink = ListSink()
        out = run(signals=[make_signal()], kills=live(), bankroll=1000.0,
                         mode="live", path=tmp_path / "s.jsonl",
                         observability=Observability(sink=sink))
        assert out["clv"]["skipped"] == 1 and out["clv"]["ok"] == 0
        lateral = [e for e in sink.events if e["event"] == "clv.lateral"]
        assert lateral and lateral[0]["outcome"] == "skipped"
        assert lateral[0]["reason"] == REASON_CLOSING_MISSING
        assert out["clv"]["avg_diff"] is None

    def test_eventi_con_tracciabilita(self, db, tmp_path):
        """Gli eventi CLV portano trace_id e lo stesso request_id del giro."""
        sink = ListSink()
        out = run(signals=[make_signal()], kills=live(), bankroll=1000.0,
                         mode="live", path=tmp_path / "s.jsonl",
                         observability=Observability(sink=sink),
                         request_id="giro-test")
        events = [e for e in sink.events if e["event"].startswith("clv.")]
        assert events, "almeno un evento CLV atteso (skip)"
        assert all(e["trace_id"] for e in events)
        assert {e["request_id"] for e in events} == {"giro-test"}

    def test_segnale_ostile_rejected_mai_traceback(self, db, tmp_path, monkeypatch):
        """Un'eccezione dentro la valutazione diventa `rejected` + errore
        contati: il giro shadow e il job di auto_bet non si rompono."""
        monkeypatch.setattr("decision.clv.evaluate_clv",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        out = run(signals=[make_signal()], kills=live(), bankroll=1000.0,
                         mode="live", path=tmp_path / "s.jsonl")
        assert out["clv"]["rejected"] == 1 and out["clv"]["errors"] == 1
        assert out["evaluated"] == 1                    # il giro e' andato avanti
        assert not any("boom" in err for err in out["errors"])  # errori CLV contenuti


# ---------------------------------------------------------------------------
# 3. Interruttore e writer reale iniettato (solo esplicito)
# ---------------------------------------------------------------------------

class TestInterruttore:
    def test_default_attivo(self):
        assert clv_shadow_enabled("") is True
        assert clv_shadow_enabled(None) is True

    def test_si_spegne_da_env(self, monkeypatch):
        for value in ("0", "false", "off", "no"):
            assert clv_shadow_enabled(value) is False
        monkeypatch.setenv(CLV_ENV, "0")
        assert clv_shadow_enabled() is False

    def test_spento_non_aggiunge_niente(self, db, tmp_path):
        out = run(signals=[make_signal()], kills=live(), bankroll=1000.0,
                         mode="live", clv_enabled=False,
                         path=tmp_path / "s.jsonl")
        assert out["clv"]["enabled"] is False
        assert out["clv"]["ok"] == 0 and out["clv"]["skipped"] == 0
        assert out["clv"]["dispatched"] == 0

    def test_env_spenta_non_aggiunge_niente(self, monkeypatch, db, tmp_path):
        monkeypatch.setenv(CLV_ENV, "0")
        out = run(signals=[make_signal()], kills=live(), bankroll=1000.0,
                         mode="live", path=tmp_path / "s.jsonl")
        assert out["clv"]["enabled"] is False and out["clv"]["dispatched"] == 0

    def test_writer_reale_solo_iniettato_explicitamente(self, db, tmp_path):
        """Il writer di produzione (tracker.save_clv) NON e' il default: chi lo
        passa esplicitamente sa di scrivere sul ledger (es. test dedicati)."""
        calls = []
        feed = _validated_feed(valid_row(event_id="sx-1", selection="1", odds=1.50))
        out = run(signals=[make_signal()], kills=live(), bankroll=1000.0,
                  mode="live", path=tmp_path / "s.jsonl", feed=feed,
                  clv_writer=lambda p: (calls.append(p) or {"saved": True}))
        assert calls and calls[0]["match_id"] == "sx-1"
        assert out["clv"]["dispatched"] == 1
        # Il registro shadow non cambia: il writer iniettato sostituisce
        # l'evento shadow (e' la via per collaudare la scrittura vera offline).
        assert out["clv"]["ok"] == 1


# ---------------------------------------------------------------------------
# 4. Riepilogo e registro
# ---------------------------------------------------------------------------

class TestRiepilogo:
    def test_summary_del_registro_conta_write_clv(self, db, tmp_path):
        path = tmp_path / "shadow.jsonl"
        feed = _validated_feed(valid_row(event_id="sx-1", selection="1", odds=1.50))
        for _ in range(2):
            run(signals=[make_signal()], kills=live(), bankroll=1000.0,
                mode="live", path=path, feed=feed)
        summary = shadow_summary(path)
        assert summary["by_kind"]["write_clv"] == 1      # dedup: un campione
        assert summary["entries"] == 3                   # persist + order + clv

    def test_media_clv_diff_su_piu_segnali(self, db, tmp_path):
        signals = [make_signal(match_id="sx-1"),
                   make_signal(match_id="sx-2", price=1.60)]
        row1 = valid_row(event_id="sx-1", selection="1", odds=1.50)
        row2 = valid_row(event_id="sx-2", selection="1", odds=2.00)
        feed = _validated_feed(row1, row2)
        out = run(signals=signals, kills=live(), bankroll=1000.0,
                         mode="live", path=tmp_path / "s.jsonl", feed=feed)
        assert out["clv"]["ok"] == 2
        diffs = ((1.60 / 1.50) - 1.0, (1.60 / 2.00) - 1.0)
        assert out["clv"]["avg_diff"] == pytest.approx(sum(diffs) / 2, abs=1e-4)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _validated_feed(*rows, tmp_state=None):
    """Un `FeedSnapshot` validato per l'evento richiesto (sorgente statica)."""
    from decision.feeds import MarketFeed
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        feed = MarketFeed([StaticSource(list(rows), name="static", source_id="static")],
                          state_path=Path(td) / "state.json",
                          observability=Observability(sink=ListSink()))
        return feed_validate(feed)
