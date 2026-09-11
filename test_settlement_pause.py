"""Test della PAUSA SETTLEMENT (11/09/2026).

Durante il cambio di strategia il proprietario ha chiesto di fermare anche
le chiusure automatiche: nessun verdetto emesso, nessun risultato scaricato
(quindi zero crediti the-odds-api bruciati). L'override e' persistente sul
volume (data/execution/settlement_paused.json) e si attiva anche via env
`SETTLEMENT_PAUSED=1`.
"""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tracker


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    # Il file di pausa punta su tmp: i test non toccano mai il volume reale.
    monkeypatch.setattr(tracker, "SETTLEMENT_PAUSE_FILE",
                        tmp_path / "settlement_paused.json")
    monkeypatch.delenv("SETTLEMENT_PAUSED", raising=False)
    tracker.init_db()
    yield db_path


@pytest.fixture()
def open_rows(temp_db):
    """Un match con risultato + bet/previsione/cassa aperte sullo stesso match."""
    tracker.save_match("p1", "Serie A", "Inter", "Napoli",
                       (datetime.now(timezone.utc) - timedelta(hours=3))
                       .isoformat().replace("+00:00", "Z"))
    tracker.save_bet("p1", "1X2", "1", "1.100", 101, 1.70, 10.0)
    tracker.save_prediction("p1", "1X2", "1", 1.70, 0.62, 0.05)
    tracker.save_result("p1", "Serie A", "Inter", "Napoli", 2, 1,
                        datetime.now().isoformat())
    conn = tracker._get_conn()
    conn.execute("INSERT INTO cassa (partita, esito, quota, importo) "
                 "VALUES ('Serie A – Inter vs Napoli', '1', 1.70, 10.0)")
    conn.commit()
    conn.close()
    return "p1"


class TestFlag:
    def test_default_attivo(self, temp_db):
        assert tracker.settlement_paused() is False

    def test_set_e_reset(self, temp_db):
        tracker.set_settlement_paused(True)
        assert tracker.settlement_paused() is True
        st = tracker.settlement_pause_status()
        assert st["paused"] is True and st["file"].endswith("settlement_paused.json")
        tracker.set_settlement_paused(False)
        assert tracker.settlement_paused() is False

    def test_env_forza_la_pausa(self, temp_db, monkeypatch):
        monkeypatch.setenv("SETTLEMENT_PAUSED", "1")
        assert tracker.settlement_paused() is True
        assert tracker.settlement_pause_status()["env"] == "1"


class TestSettleBloccato:
    def test_settle_bets_non_chiude(self, temp_db, open_rows):
        tracker.set_settlement_paused(True)
        assert tracker.settle_bets() == (0, 0)
        assert tracker.settle_bets(return_details=True) == (0, 0, [])
        rows = tracker.get_bets(closed=False)
        assert len(rows) == 1 and rows[0]["esito_finale"] is None

    def test_settle_predictions_non_chiude(self, temp_db, open_rows):
        tracker.set_settlement_paused(True)
        assert tracker.settle_predictions() == (0, 0)
        conn = tracker._get_conn()
        n = conn.execute("SELECT COUNT(*) FROM predictions "
                         "WHERE esito_finale IS NOT NULL").fetchone()[0]
        conn.close()
        assert n == 0

    def test_settle_cassa_non_chiude(self, temp_db, open_rows):
        tracker.set_settlement_paused(True)
        assert tracker.settle_cassa() == 0
        conn = tracker._get_conn()
        n = conn.execute("SELECT COUNT(*) FROM cassa "
                         "WHERE esito_finale IS NOT NULL").fetchone()[0]
        conn.close()
        assert n == 0

    def test_dopo_la_riattivazione_salda(self, temp_db, open_rows):
        """La pausa non corrompe lo stato: rimossa, il settlement riprende
        e chiude regolarmente le righe rimaste aperte."""
        tracker.set_settlement_paused(True)
        assert tracker.settle_bets() == (0, 0)
        tracker.set_settlement_paused(False)
        settled, _ = tracker.settle_bets()
        assert settled == 1
        assert tracker.get_bets(closed=True)[0]["esito_finale"] == "won"


class TestSxSettleGate:
    def test_sx_settle_non_scarica_punteggi_in_pausa(self, temp_db, monkeypatch):
        """settle_sx_bets esce prima delle fonti punteggi: zero crediti."""
        import sx_signals

        def _boom(*a, **k):
            raise AssertionError("nessun download punteggi in pausa")

        monkeypatch.setattr(sx_signals, "_sx_open_matches", _boom)
        tracker.set_settlement_paused(True)
        res = sx_signals.settle_sx_bets()
        assert res.get("paused") is True and res["settled"] == 0


class TestUpdateResultsGate:
    def test_nessun_download_risultati_in_pausa(self, temp_db, monkeypatch):
        """_update_results esce PRIMA di fetch_scores: zero crediti bruciati."""
        import bot
        import odds_api

        def _boom(*a, **k):
            raise AssertionError("fetch_scores non deve essere chiamato in pausa")

        monkeypatch.setattr(odds_api, "fetch_scores", _boom)
        tracker.set_settlement_paused(True)
        updated, stats, settlements, sanity = bot._update_results()
        assert updated == 0
        assert settlements == [] and sanity == []
        assert isinstance(stats, dict)
