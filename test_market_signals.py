"""Test aggregatore segnali di mercato (market_signals.py).

Verifica che la classificazione passi dai VERI rilevatori
(rlm_alert.check_rlm_for_signal) e che report Telegram, endpoint web
e CLI concordino.
"""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tracker
import rlm_alert
import line_movement
import market_signals


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _seed_signal(match_id, status="value"):
    """Inserisce un segnale value nel DB."""
    tracker.save_match(match_id, "Serie A", "Inter", "Napoli",
                       "2099-01-01T20:00:00Z")
    tracker.save_analysis(match_id, 1.5, 1.2, 0.45, 0.25, 0.30,
                          0.52, 0.10, "1", 2.10, "Bet365", status,
                          market_prob=0.42, market_edge=0.03)


def _seed_snapshots(match_id, esito, prices):
    """Inserisce price snapshots con timestamp diversi."""
    base = datetime.now() - timedelta(minutes=30)
    conn = tracker._get_conn()
    line_movement._ensure_table(conn)
    for i, p in enumerate(prices):
        ts = (base + timedelta(minutes=i * 10)).isoformat()
        conn.execute(
            "INSERT INTO price_snapshots "
            "(match_id, esito, price, bookmaker, market_prob, recorded_at) "
            "VALUES (?,?,?,?,?,?)",
            (match_id, esito, p, "", None, ts))
    conn.commit(); conn.close()


class TestCollectMarketSignals:
    def test_nessun_segnale(self, temp_db):
        assert market_signals.collect_market_signals() == []

    def test_segnale_senza_movimento(self, temp_db):
        _seed_signal("m1")
        _seed_snapshots("m1", "1", [2.10, 2.09, 2.08])
        assert market_signals.collect_market_signals() == []

    def test_crollo_classificato(self, temp_db):
        """Crollo >= 5%: alert urgent 'crash' dall'aggregatore."""
        _seed_signal("m2")
        _seed_snapshots("m2", "1", [2.10, 1.98])  # -5.7%
        alerts = market_signals.collect_market_signals()
        assert len(alerts) == 1
        a = alerts[0]
        assert a["alert_type"] == "crash"
        assert a["severity"] == "urgent"
        assert a["total_move_pct"] <= -5.0

    def test_ordine_urgent_prima(self, temp_db):
        """Un crollo (urgent) precede un RLM (warning) nell'ordine."""
        _seed_signal("m3")                       # crollo: -5.7%, 2 snap
        _seed_snapshots("m3", "1", [2.10, 1.98])
        _seed_signal("m4")                       # RLM warning: +4.8%, 3 step
        _seed_snapshots("m4", "1", [2.10, 2.18, 2.12, 2.20])
        alerts = market_signals.collect_market_signals()
        assert len(alerts) == 2
        assert alerts[0]["match_id"] == "m3"
        assert alerts[0]["severity"] == "urgent"
        assert alerts[1]["match_id"] == "m4"
        assert alerts[1]["severity"] == "warning"
        assert alerts[1]["alert_type"] == "rlm"

    def test_eccezione_singolo_segnale_non_blocca(self, temp_db, monkeypatch):
        """Un segnale che esplode non blocca gli altri."""
        _seed_signal("m5")
        _seed_snapshots("m5", "1", [2.10, 1.98])
        _seed_signal("m6")
        _seed_snapshots("m6", "1", [2.10, 1.95])

        orig = rlm_alert.check_rlm_for_signal
        calls = {"n": 0}

        def flaky(sig):
            calls["n"] += 1
            if sig["match_id"] == "m5":
                raise RuntimeError("boom")
            return orig(sig)

        monkeypatch.setattr(rlm_alert, "check_rlm_for_signal", flaky)
        alerts = market_signals.collect_market_signals()
        assert calls["n"] == 2
        assert len(alerts) == 1
        assert alerts[0]["match_id"] == "m6"


class TestSummarize:
    def test_conteggi(self):
        alerts = [
            {"alert_type": "steam", "severity": "urgent"},
            {"alert_type": "crash", "severity": "urgent"},
            {"alert_type": "rlm", "severity": "warning"},
        ]
        s = market_signals.summarize_market_signals(alerts)
        assert s["total"] == 3
        assert s["urgent"] == 2
        assert s["by_type"] == {"steam": 1, "crash": 1, "rlm": 1}

    def test_vuoto(self):
        s = market_signals.summarize_market_signals([])
        assert s["total"] == 0 and s["urgent"] == 0


class TestFormatReport:
    def test_vuoto_ritorna_lista_vuota(self):
        assert market_signals.format_market_signals_report([]) == []

    def test_righe_con_segnali(self):
        alerts = [
            {"evento": "Inter vs Napoli", "esito": "1", "quota": 2.10,
             "alert_type": "crash", "severity": "urgent",
             "total_move_pct": -5.7, "n_snapshots": 2},
        ]
        lines = market_signals.format_market_signals_report(alerts)
        assert len(lines) >= 2
        assert "Line Movement" in lines[0]
        assert "🚨 Crollo: 1" in lines[1]
        assert "Inter vs Napoli" in lines[2]
        assert "-5.7%" in lines[2]


class TestWebEndpoint:
    def test_endpoint_struttura(self, temp_db):
        import web_api
        d = web_api._market_signals_json()
        assert "summary" in d and "signals" in d
        assert d["summary"]["total"] == len(d["signals"])

    def test_endpoint_con_crollo(self, temp_db):
        import web_api
        _seed_signal("m7")
        _seed_snapshots("m7", "1", [2.10, 1.98])
        d = web_api._market_signals_json()
        assert d["summary"]["by_type"]["crash"] == 1
        assert d["signals"][0]["alert_type"] == "crash"

    def test_errore_gestito(self, temp_db, monkeypatch):
        import web_api
        def boom(max_matches=20):
            raise RuntimeError("db giu'")
        monkeypatch.setattr("market_signals.collect_market_signals", boom)
        d = web_api._market_signals_json()
        assert d["signals"] == []
        assert "error" in d

    def test_rotta_registrata(self):
        import web_api
        assert "/api/market_signals" in web_api.ROUTES


class TestReportTelegram:
    def test_report_senza_sezione_se_vuoto(self, temp_db):
        import bot
        text = bot.format_daily_report(datetime.now().strftime("%Y-%m-%d"),
                                       "TEST")
        assert "Line Movement" not in text

    def test_report_mostra_crollo(self, temp_db):
        import bot
        _seed_signal("m8")
        _seed_snapshots("m8", "1", [2.10, 1.98])
        text = bot.format_daily_report(datetime.now().strftime("%Y-%m-%d"),
                                       "TEST")
        assert "📊 *Line Movement (24h):*" in text
        assert "🚨 Crollo: 1" in text
        assert "Inter vs Napoli" in text


class TestCli:
    def test_cli_json(self, temp_db, capsys):
        _seed_signal("m9")
        _seed_snapshots("m9", "1", [2.10, 1.98])
        rc = market_signals.main(["--json"])
        out = capsys.readouterr().out
        assert rc == 0
        assert '"crash": 1' in out

    def test_cli_vuoto(self, temp_db, capsys):
        rc = market_signals.main([])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Nessun movimento" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
