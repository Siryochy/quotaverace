"""
Test unitari per l'API JSON backend (web_api.py).
"""

import json

import pytest

import tracker
import web_api
import daily_scan_job


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "webapi.db")
    monkeypatch.setattr(daily_scan_job, "SCAN_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
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
        for route in ("/api/health", "/api/dashboard", "/api/storico", "/api/value",
                      "/api/schedina", "/api/scan"):
            assert route in web_api.ROUTES


class TestScanEndpoint:

    def test_no_cache_ritorna_503(self):
        code, payload = web_api._scan_json()
        assert code == 503
        assert payload["error"] == "no_scan_cache"
        assert "/scan" in payload["message"]

    def test_cache_presente_ritorna_200(self, tmp_path):
        scan_dir = tmp_path / "data"
        (scan_dir / "scan_2026-08-31.json").write_text(json.dumps({
            "day": "2026-08-31", "events": 2, "markets": 3,
            "opportunities": [{"price": 2.1, "event_name": "Roma v Empoli"}],
            "generated_at": "2026-08-31T08:45:00+00:00",
        }), encoding="utf-8")
        code, payload = web_api._scan_json()
        assert code == 200
        assert payload["day"] == "2026-08-31"
        assert payload["opportunities"][0]["price"] == 2.1

    def test_live_senza_credenziali_503(self, monkeypatch):
        monkeypatch.setattr(daily_scan_job, "get_client", lambda: None)
        code, payload = web_api._scan_json({"live": "1"})
        assert code == 503
        assert payload["error"] == "betfair_not_configured"
        assert "BETFAIR_APP_KEY" in payload["message"]

    def test_live_con_client_finto_200(self, monkeypatch, tmp_path):
        class FakeClient:
            def list_market_catalogue(self, mf, max_results=200, market_projection=None):
                mtype = mf["marketTypeCodes"][0]
                if mtype != "MATCH_ODDS":
                    return []
                return [{
                    "marketId": "1.100", "marketType": "MATCH_ODDS",
                    "marketStartTime": "2026-08-31T13:00:00.000Z",
                    "event": {"id": "311", "name": "Roma v Empoli"},
                    "runners": [{"selectionId": 101, "runnerName": "Roma"}],
                }]

            def list_market_book(self, market_ids, price_projection=None):
                return [{"marketId": "1.100", "runners": [
                    {"selectionId": 101, "ex": {"availableToBack": [
                        {"price": 2.1, "size": 100.0}]}},
                ]} for mid in market_ids if mid == "1.100"]

        monkeypatch.setattr(daily_scan_job, "get_client", lambda: FakeClient())
        code, payload = web_api._scan_json({"live": "1", "date": "2026-08-31"})
        assert code == 200
        assert payload["day"] == "2026-08-31"
        # la scansione live ha salvato la cache per le prossime richieste
        assert (tmp_path / "data" / "scan_2026-08-31.json").exists()

    def test_params_vuoti_senza_live_usa_cache(self):
        code, _ = web_api._scan_json({})
        assert code == 503  # tmp dir vuota: nessuna cache