"""Test della shadow mode (`decision/shadow.py`) — OFFLINE, zero crediti API.

La shadow mode collega la catena nuova ad `auto_bet` senza eseguire nulla:
gli stessi segnali producono comandi che vengono SOLO registrati. Qui si
verifica che:

1. con un blocco attivo non si faccia nulla (fail fast, nemmeno una query);
2. i comandi finiscano nel registro shadow e NON sul ledger `decisions`
   (il job gira ogni 60s: il ledger reale non si duplica);
3. nessuna rete venga toccata (nessun credito speso);
4. gli errori restino dentro: il giro puntate non si rompe.
"""

import json
import sqlite3

import pytest

import tracker
from decision import KillSwitchStatus
from decision.middleware import ListSink, Observability
from decision.shadow import (
    SHADOW_ENABLED_ENV, format_report, iter_shadow_commands, run_shadow,
    shadow_enabled, shadow_summary,
)
from test_decision_commands import make_signal

ALLOWED_LEAGUE = "Premier League"


def live():
    return KillSwitchStatus(mode="live", env_mode="live", provider_ready=True)


@pytest.fixture
def db(monkeypatch, tmp_path):
    """Ledger temporaneo con lo schema di produzione + un segnale aperto."""
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "shadow.db")
    conn = tracker._get_conn()
    conn.close()
    return tmp_path


def add_open_signal(tmp_path, *, match_id="sx-1", status="strong_value",
                    ratings=True):
    """Riga di ledger pronta per l'adapter (con o senza rating delle squadre)."""
    conn = sqlite3.connect(str(tmp_path / "shadow.db"))
    conn.execute("INSERT OR REPLACE INTO matches (id, league, home_team, away_team, "
                 "commence_time, status) VALUES (?,?,?,?,?,'scheduled')",
                 (match_id, ALLOWED_LEAGUE, "Inter", "Cagliari", "2026-09-15T18:45:00Z"))
    conn.execute("INSERT OR REPLACE INTO predictions (match_id, mercato, esito, quota, "
                 "prob, ev, market_prob, market_edge, status) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (match_id, "1X2", "1", 1.60, 0.65, 0.04, 0.58, 0.07, status))
    conn.execute("INSERT OR REPLACE INTO match_analysis (match_id, lam_h, lam_a, "
                 "prob_1, prob_X, prob_2, status) VALUES (?,?,?,?,?,?,'analyzed')",
                 (match_id, 1.6, 0.9, 0.62, 0.22, 0.16))
    if ratings:
        # `team_ratings` la crea il rating engine (non tracker): lo schema di
        # produzione qui va riprodotto a mano.
        conn.execute("CREATE TABLE IF NOT EXISTS team_ratings (team TEXT PRIMARY KEY, "
                     "attack_home REAL, defense_home REAL, attack_away REAL, "
                     "defense_away REAL, n_home INTEGER, n_away INTEGER)")
        for team in ("Inter", "Cagliari"):
            conn.execute("INSERT OR REPLACE INTO team_ratings (team, attack_home, "
                         "defense_home, attack_away, defense_away, n_home, n_away) "
                         "VALUES (?,?,?,?,?,?,?)", (team, 1.2, 0.9, 1.1, 1.0, 12, 10))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. Interruttore e fail fast
# ---------------------------------------------------------------------------

class TestInterruttore:
    def test_attiva_di_default(self):
        assert shadow_enabled("") is True
        assert shadow_enabled("1") is True

    def test_si_spegne_da_env(self, monkeypatch):
        for value in ("0", "false", "off", "no"):
            assert shadow_enabled(value) is False
        monkeypatch.setenv(SHADOW_ENABLED_ENV, "0")
        assert shadow_enabled() is False


class TestFailFast:
    def test_blocco_attivo_ferma_tutto(self, tmp_path):
        """Nessuna query, nessun registro: con le puntate ferme non c'e' nulla da confrontare."""
        path = tmp_path / "shadow.jsonl"
        out = run_shadow(signals=[make_signal()], kills=KillSwitchStatus(mode="off"),
                         shadow_path=path)
        assert out["blocked"]["name"] == "manual"
        assert out["evaluated"] == 0 and out["plans"] == []
        assert not path.exists()

    def test_stop_loss_ferma_tutto(self, tmp_path):
        out = run_shadow(signals=[make_signal()],
                         kills=live().model_copy(update={"daily_stop_active": True,
                                                         "daily_stop_detail": "-6%"}),
                         shadow_path=tmp_path / "s.jsonl")
        assert out["blocked"]["name"] == "daily_stop"
        assert out["evaluated"] == 0

    def test_pausa_settlement_non_ferma_la_shadow(self, tmp_path):
        """La pausa ferma il referto, non la valutazione: resta un avviso."""
        out = run_shadow(signals=[make_signal()],
                         kills=live().model_copy(update={"settlement_paused": True}),
                         shadow_path=tmp_path / "s.jsonl")
        assert out["blocked"] is None and out["evaluated"] == 1


# ---------------------------------------------------------------------------
# 2. Comandi registrati, nessun effetto
# ---------------------------------------------------------------------------

class TestRegistrazione:
    def test_comandi_registrati_senza_esecuzione(self, db, tmp_path):
        path = tmp_path / "shadow.jsonl"
        out = run_shadow(signals=[make_signal()], kills=live(), bankroll=1000.0,
                         mode="live", shadow_path=path)
        assert out["evaluated"] == 1
        assert out["by_verdict"] == {"approve": 1}
        assert out["by_command"] == {"persist_decision": 1, "place_order": 1}
        assert out["plans"][0]["would_order"] is True
        assert out["shadow"] is True

        lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert sorted(entry["command"]["kind"] for entry in lines) == [
            "persist_decision", "place_order"]
        assert all(entry["config_hash"] for entry in lines)
        assert lines[0]["trace"]["trace_id"]

    def test_il_ledger_delle_decisioni_resta_vuoto(self, db, tmp_path):
        """Il ledger reale non si riempie coi giri della shadow (job ogni 60s)."""
        run_shadow(signals=[make_signal()], kills=live(), bankroll=1000.0,
                   mode="live", shadow_path=tmp_path / "shadow.jsonl")
        assert tracker.get_decisions(limit=100) == []

    def test_dedup_fra_giri(self, db, tmp_path):
        path = tmp_path / "shadow.jsonl"
        for _ in range(3):
            run_shadow(signals=[make_signal()], kills=live(), bankroll=1000.0,
                       mode="live", shadow_path=path)
        assert len(path.read_text().splitlines()) == 2      # persist + order
        out = run_shadow(signals=[make_signal()], kills=live(), bankroll=1000.0,
                         mode="live", shadow_path=path)
        assert out["plans"][0]["duplicated"] == 2

    def test_rifiutato_registra_solo_audit(self, db, tmp_path):
        path = tmp_path / "shadow.jsonl"
        out = run_shadow(signals=[make_signal(price=2.35, market_prob=0.42,
                                             blended_prob=0.50, tier="value")],
                         kills=live(), bankroll=1000.0, mode="live", shadow_path=path)
        assert out["plans"][0]["would_order"] is False
        kinds = [json.loads(line)["command"]["kind"] for line in path.read_text().splitlines()]
        assert kinds == ["persist_decision"]

    def test_sim_non_produce_ordini(self, db, tmp_path):
        out = run_shadow(signals=[make_signal()], kills=live(), bankroll=1000.0,
                         mode="sim", shadow_path=tmp_path / "shadow.jsonl")
        assert out["by_command"] == {"persist_decision": 1}

    def test_nessun_segnale_non_scrive_eventi(self, db, tmp_path):
        """Il giro ogni 60s non deve riempire il volume di "nessun segnale"."""
        sink = ListSink()
        out = run_shadow(signals=[], kills=live(),
                         observability=Observability(sink=sink),
                         shadow_path=tmp_path / "shadow.jsonl")
        assert out["evaluated"] == 0 and out["no_signals"] is True
        assert sink.events == []

    def test_limite_al_numero_di_segnali(self, db, tmp_path):
        signals = [make_signal(match_id=f"sx-{i}") for i in range(5)]
        out = run_shadow(signals=signals, kills=live(), bankroll=1000.0, mode="live",
                         limit=2, shadow_path=tmp_path / "shadow.jsonl")
        assert out["evaluated"] == 2


# ---------------------------------------------------------------------------
# 3. Percorso reale (adapter sul ledger) e rete
# ---------------------------------------------------------------------------

class TestPercorsoReale:
    def test_legge_il_ledger_e_valuta(self, db, tmp_path):
        add_open_signal(db)
        out = run_shadow(kills=live(), bankroll=1000.0, mode="live",
                         shadow_path=tmp_path / "shadow.jsonl")
        assert out["evaluated"] == 1
        assert out["plans"][0]["match_id"] == "sx-1"
        assert out["plans"][0]["verdict"] == "approve"
        assert out["plans"][0]["would_order"] is True

    def test_modello_cieco_va_in_review(self, db, tmp_path):
        """Senza rating il modello usa il profilo neutro: decide un umano.

        E' il gate di qualita' dei dati: la shadow lo mostra sui dati veri,
        senza doverlo dedurre dal codice.
        """
        add_open_signal(db, ratings=False)
        out = run_shadow(kills=live(), bankroll=1000.0, mode="live",
                         shadow_path=tmp_path / "shadow.jsonl")
        assert out["plans"][0]["verdict"] == "review"
        assert out["plans"][0]["reason"] == "data_quality_low"
        assert out["plans"][0]["would_order"] is False

    def test_nessuna_rete_toccata(self, db, tmp_path, monkeypatch):
        """Tripwire crediti: la shadow non deve fare chiamate in uscita."""
        import socket
        import requests

        def boom(*args, **kwargs):
            raise AssertionError("la shadow ha toccato la rete")

        monkeypatch.setattr(socket, "create_connection", boom)
        monkeypatch.setattr(requests, "post", boom)
        monkeypatch.setattr(requests, "get", boom)
        add_open_signal(db)
        out = run_shadow(kills=live(), bankroll=1000.0, mode="live",
                         shadow_path=tmp_path / "shadow.jsonl")
        assert out["evaluated"] == 1 and out["errors"] == []

    def test_errore_dell_adapter_non_solleva(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("ledger chiuso")

        monkeypatch.setattr("decision.adapters.iter_signals", boom)
        out = run_shadow(kills=live(), shadow_path=tmp_path / "s.jsonl")
        assert out["evaluated"] == 0
        assert any("ledger chiuso" in err for err in out["errors"])

    def test_eventi_di_osservabilita_emessi(self, tmp_path):
        sink = ListSink()
        run_shadow(signals=[make_signal()], kills=live(), bankroll=1000.0, mode="live",
                   observability=Observability(sink=sink, component="test"),
                   shadow_path=tmp_path / "s.jsonl")
        events = [e["event"] for e in sink.events]
        assert "shadow.start" in events and "shadow.end" in events
        assert "plan.dispatched" in events
        assert all(e["trace_id"] for e in sink.events)


# ---------------------------------------------------------------------------
# 4. Registro e report
# ---------------------------------------------------------------------------

class TestRegistro:
    def test_lettura_e_riepilogo(self, db, tmp_path):
        path = tmp_path / "shadow.jsonl"
        run_shadow(signals=[make_signal(match_id="sx-1"), make_signal(match_id="sx-2")],
                   kills=live(), bankroll=1000.0, mode="live", shadow_path=path)

        entries = iter_shadow_commands(path)
        assert len(entries) == 4                      # 2 segnali x (persist + order)
        # una trace per DECISIONE, stesso request_id per tutto il giro
        traces = {entry["trace"]["trace_id"] for entry in entries}
        requests = {entry["trace"]["request_id"] for entry in entries}
        assert len(traces) == 2 and len(requests) == 1

        summary = shadow_summary(path)
        assert summary["entries"] == 4
        assert summary["by_kind"] == {"persist_decision": 2, "place_order": 2}
        assert summary["would_order"] == 2
        assert summary["distinct_signals"] == 2
        assert summary["last_ts"]

    def test_report_leggibile(self, db, tmp_path):
        path = tmp_path / "shadow.jsonl"
        run_shadow(signals=[make_signal()], kills=live(), bankroll=1000.0,
                   mode="live", shadow_path=path)
        text = format_report(path)
        assert "Shadow mode" in text and "nessuna esecuzione reale" in text
        assert "ordini che SAREBBERO partiti: 1" in text

    def test_registro_assente_o_corrotto(self, tmp_path):
        assert iter_shadow_commands(tmp_path / "nope.jsonl") == []
        broken = tmp_path / "broken.jsonl"
        broken.write_text("non-json\n")
        assert iter_shadow_commands(broken) == []

    def test_path_da_env(self, monkeypatch, tmp_path):
        from decision.gateways import SHADOW_LOG_ENV, shadow_log_path
        monkeypatch.setenv(SHADOW_LOG_ENV, str(tmp_path / "custom.jsonl"))
        assert shadow_log_path() == tmp_path / "custom.jsonl"

    def test_report_su_registro_vuoto(self, tmp_path):
        text = format_report(tmp_path / "nope.jsonl")
        assert "comandi registrati: 0" in text
