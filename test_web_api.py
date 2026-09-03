"""
Test unitari per l'API JSON backend (web_api.py).
"""

import json

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

    def test_dashboard_bankroll_reale_da_cassa(self):
        """Con movimento in cassa, il bankroll mostrato è il SUM reale
        (colonna importo) e non il default env — bug amount/importo fixato."""
        tracker.save_cassa_entry("Inter vs Napoli", "1", 2.1, 10.0)
        tracker.save_cassa_entry("Roma vs Milan", "2", 3.0, 20.0)
        d = web_api._dashboard_json()
        assert d["bankroll"] == pytest.approx(30.0)

    def test_dashboard_include_nuove_sezioni(self):
        """La dashboard espone streak, CLV, auto_bets e per_mercato."""
        d = web_api._dashboard_json()
        assert "streaks" in d
        assert "clv" in d
        assert "auto_bets" in d
        assert "per_mercato" in d
        assert "bankroll_stats" in d

    def test_dashboard_include_market_signals(self):
        """La dashboard espone anche gli alert RLM/steam/crollo (stessi dati
        del report Telegram) per il monitor in pagina."""
        d = web_api._dashboard_json()
        assert "market_signals" in d
        assert "summary" in d["market_signals"]
        assert "signals" in d["market_signals"]
        assert d["market_signals"]["summary"]["total"] >= 0


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
                      "/api/schedina", "/api/scan", "/api/test_notify",
                      "/api/training"):
            assert route in web_api.ROUTES
        assert "/api/test_notify" in web_api.POST_ROUTES
        assert "/api/auto_bet" in web_api.POST_ROUTES


class TestScanRemoved:
    """Betfair rimosso dal 04/09: /api/scan risponde esplicitamente 503
    con errore betfair_removed, mai dati di catalogo Exchange."""

    def test_scan_rimosso_503(self):
        code, payload = web_api._scan_json()
        assert code == 503
        assert payload["error"] == "betfair_removed"

    def test_scan_con_parametri_rimosso_503(self):
        code, payload = web_api._scan_json({"live": "1"})
        assert code == 503
        assert payload["error"] == "betfair_removed"


class TestAutoBet:
    def test_nessuna_puntata(self, monkeypatch):
        monkeypatch.setattr("auto_bet.run_today_bets", lambda **kw: [])
        d = web_api._auto_bet()
        assert d["ok"] is True and d["piazzate"] == 0

    def test_piazza_e_notifica_agli_admin(self, monkeypatch):
        fake = [{"home": "Birmingham City", "away": "Southampton",
                 "esito_key": "1", "price": 2.68, "stake": 5.0,
                 "mode": "dry-run"}]
        monkeypatch.setattr("auto_bet.run_today_bets", lambda **kw: fake)
        monkeypatch.setenv("ADMIN_CHAT_ID", "111")
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "tok")
        inviati = []
        monkeypatch.setattr("web_api._telegram_send_message",
                            lambda t, c, text: inviati.append((c, text)))
        d = web_api._auto_bet()
        assert d["piazzate"] == 1
        assert len(inviati) == 1
        assert "PUNTATE AUTOMATICHE" in inviati[0][1]
        assert "DRY-RUN" in inviati[0][1]

    def test_errore_run_restituisce_500(self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("Betfair giu'")
        monkeypatch.setattr("auto_bet.run_today_bets", boom)
        code, payload = web_api._auto_bet()
        assert code == 500


class TestTraining:
    def test_endpoint_risponde(self):
        d = web_api._training_json({"limit": "10"})
        assert "n" in d and "rows" in d
        assert isinstance(d["rows"], list)

    def test_limite_non_valido_non_crasha(self):
        d = web_api._training_json({"limit": "abc"})
        assert "error" in d or "rows" in d


class TestTestNotify:
    def test_disabilitato_senza_chiave(self, monkeypatch):
        monkeypatch.delenv("TEST_NOTIFY_KEY", raising=False)
        code, payload = web_api._test_notify({})
        assert code == 403
        assert payload["error"] == "disabled"

    def test_chiave_errata(self, monkeypatch):
        monkeypatch.setenv("TEST_NOTIFY_KEY", "segreta")
        code, _ = web_api._test_notify({"key": "sbagliata"})
        assert code == 401

    def test_invia_agli_admin(self, monkeypatch):
        monkeypatch.setenv("TEST_NOTIFY_KEY", "segreta")
        monkeypatch.setenv("ADMIN_CHAT_ID", "111, 222")
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "tok-test")
        inviati = []

        def fake_send(token, chat_id, text):
            inviati.append((chat_id, text))
            return {"ok": True}

        monkeypatch.setattr(web_api, "_telegram_send_message", fake_send)
        code, payload = web_api._test_notify({"key": "segreta"})
        assert code == 200
        assert payload["ok"] is True
        assert payload["destinatari"] == [111, 222]
        assert len(inviati) == 2
        assert "Test notifica" in inviati[0][1]

    def test_errore_invio_riportato(self, monkeypatch):
        monkeypatch.setenv("TEST_NOTIFY_KEY", "segreta")
        monkeypatch.setenv("ADMIN_CHAT_ID", "111")
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "tok-test")

        def fake_send(token, chat_id, text):
            raise RuntimeError("timeout telegram")

        monkeypatch.setattr(web_api, "_telegram_send_message", fake_send)
        code, payload = web_api._test_notify({"key": "segreta"})
        assert code == 502
        assert payload["results"][0]["ok"] is False

    def test_chat_id_extra(self, monkeypatch):
        monkeypatch.setenv("TEST_NOTIFY_KEY", "segreta")
        monkeypatch.setenv("ADMIN_CHAT_ID", "111")
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "tok")
        inviati = []

        def fake_send(token, chat_id, text):
            inviati.append(chat_id)
            return {"ok": True}

        monkeypatch.setattr(web_api, "_telegram_send_message", fake_send)
        code, payload = web_api._test_notify({"key": "segreta", "chat_id": "333"})
        assert code == 200
        assert payload["destinatari"] == [111, 333]