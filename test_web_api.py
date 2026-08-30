"""
Test unitari per l'API JSON backend (web_api.py).
"""

import pytest

import tracker
import web_api


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "webapi.db")
    tracker.init_db()
    yield


def _seed_signal(monkeypatch):
    # inserisce un segnale chiuso + uno pendente direttamente nel DB
    tracker.log_signal(123, "Inter vs Napoli", "Over 2.5", 2.10, 0.55, 0.157)
    conn = tracker._get_conn(); c = conn.cursor()
    c.execute("UPDATE signals SET esito_finale='won', profit=1.10 WHERE id=1")
    conn.commit(); conn.close()


class TestHealth:
    def test_health_ok(self):
        h = web_api._health_json()
        assert h["status"] == "ok"

    def test_api_football_key_flag(self, monkeypatch):
        monkeypatch.setenv("API_FOOTBALL_KEY", "x")
        assert web_api._health_json()["api_football_key"] is True
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        assert web_api._health_json()["api_football_key"] is False


class TestDashboard:
    def test_dashboard_vuoto(self):
        d = web_api._dashboard_json()
        assert d["bankroll"] == pytest.approx(100.0)
        assert d["ultime_value"] == []

    def test_dashboard_con_segnale(self):
        tracker.log_signal(123, "X vs Y", "1", 2.0, 0.55, 0.10)
        d = web_api._dashboard_json()
        # il segnale pendente appare tra le ultime value bet
        assert len(d["ultime_value"]) >= 1


class TestStorico:
    def test_storico_vuoto(self, monkeypatch):
        s = web_api._storico_json()
        assert s["segnali"] == []
        assert s["summary"]["closed"] == 0

    def test_storico_con_segnali(self):
        tracker.log_signal(123, "Inter vs Napoli", "1", 2.0, 0.5, 0.1)
        s = web_api._storico_json()
        assert len(s["segnali"]) >= 1
        assert s["segnali"][0]["evento"] == "Inter vs Napoli"


class TestSchedina:
    def test_schedina_vuoto(self):
        s = web_api._schedina_json()
        assert s["picks"] == []
        assert s["multipla"] is None
        assert s["bankroll"] == pytest.approx(100.0)


class TestRoutes:
    def test_rotte_esistono(self):
        for route in ("/api/health", "/api/dashboard", "/api/storico", "/api/value", "/api/schedina"):
            assert route in web_api.ROUTES