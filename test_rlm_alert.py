"""Test RLM alert (rlm_alert.py)."""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tracker
import rlm_alert
import line_movement


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


class TestCheckRlmForSignal:
    def test_nessun_rlm_se_pochi_snapshot(self, temp_db):
        _seed_signal("m1")
        _seed_snapshots("m1", "1", [2.10, 2.05])
        sig = {"match_id": "m1", "esito": "1", "quota": 2.10,
               "ev": 0.10, "market_edge": 0.03, "status": "value"}
        alert = rlm_alert.check_rlm_for_signal(sig)
        assert alert is None

    def test_rlm_movimento_grande(self, temp_db):
        _seed_signal("m2")
        _seed_snapshots("m2", "1", [2.10, 1.95, 1.85, 1.80])
        sig = {"match_id": "m2", "esito": "1", "quota": 2.10,
               "ev": 0.10, "market_edge": 0.03, "status": "value",
               "home": "Inter", "away": "Napoli", "league": "Serie A"}
        alert = rlm_alert.check_rlm_for_signal(sig)
        assert alert is not None
        # -14% dal primo snapshot: crollo quota (urgent), non semplice RLM
        assert alert["alert_type"] in ("rlm", "steam", "crash")
        assert alert["total_move_pct"] < 0

    def test_crollo_quota_5pct_con_2_snapshot(self, temp_db):
        """Crollo >= 5%: alert URGENTE anche con soli 2 snapshot (velocita')."""
        _seed_signal("m4")
        _seed_snapshots("m4", "1", [2.10, 1.98])   # -5.7%
        sig = {"match_id": "m4", "esito": "1", "quota": 2.10,
               "ev": 0.10, "market_edge": 0.03, "status": "value"}
        alert = rlm_alert.check_rlm_for_signal(sig)
        assert alert is not None
        assert alert["alert_type"] == "crash"
        assert alert["severity"] == "urgent"
        assert alert["total_move_pct"] <= rlm_alert.CRASH_ALERT_THRESHOLD

    def test_crollo_format_include_label(self, temp_db):
        _seed_signal("m5")
        _seed_snapshots("m5", "X", [3.40, 3.20, 3.10])   # -8.8%
        sig = {"match_id": "m5", "esito": "X", "quota": 3.40,
               "ev": 0.08, "market_edge": 0.02, "status": "value"}
        alert = rlm_alert.check_rlm_for_signal(sig)
        assert alert is not None and alert["alert_type"] == "crash"
        text = rlm_alert.format_rlm_alert(alert)
        assert "CROLLO QUOTA" in text
        assert "Edge in erosione" in text

    def test_sotto_soglia_crollo_nessun_alert(self, temp_db):
        """-4.9% non e' crollo: resta nel flusso RLM standard (>=3 snapshot)."""
        _seed_signal("m6")
        _seed_snapshots("m6", "1", [2.05, 2.02, 1.96])   # -4.4% < 5%
        sig = {"match_id": "m6", "esito": "1", "quota": 2.05,
               "ev": 0.05, "market_edge": 0.02, "status": "value"}
        alert = rlm_alert.check_rlm_for_signal(sig)
        # Con 3 snapshot puo' essere rlm (>=3% movimento) ma NON crash
        assert alert is None or alert["alert_type"] != "crash"

    def test_nessun_alert_se_movimento_piccolo(self, temp_db):
        _seed_signal("m3")
        _seed_snapshots("m3", "1", [2.10, 2.09, 2.08])
        sig = {"match_id": "m3", "esito": "1", "quota": 2.10,
               "ev": 0.10, "market_edge": 0.03, "status": "value",
               "home": "Roma", "away": "Lazio", "league": "Serie A"}
        alert = rlm_alert.check_rlm_for_signal(sig)
        assert alert is None


class TestFormatRlmAlert:
    def test_format_steam(self):
        alert = {"evento": "Inter vs Napoli", "league": "Serie A",
                 "esito": "1", "quota": 2.10, "ev": 0.10,
                 "market_edge": 0.03, "status": "value",
                 "alert_type": "steam", "severity": "urgent",
                 "total_move_pct": -7.5, "direction": "↘ DISCESA",
                 "sharp_move": True, "first_price": 2.10,
                 "last_price": 1.95, "n_snapshots": 4}
        text = rlm_alert.format_rlm_alert(alert)
        assert "ALERT STEAM" in text
        assert "Inter vs Napoli" in text
        assert "↘ DISCESA" in text

    def test_format_rlm(self):
        alert = {"evento": "Roma vs Lazio", "league": "Serie A",
                 "esito": "X", "quota": 3.40, "ev": 0.05,
                 "market_edge": 0.02, "status": "value",
                 "alert_type": "rlm", "severity": "warning",
                 "total_move_pct": -4.2, "direction": "↘ DISCESA",
                 "sharp_move": True, "first_price": 3.40,
                 "last_price": 3.25, "n_snapshots": 5}
        text = rlm_alert.format_rlm_alert(alert)
        assert "ALERT RLM" in text
        assert "Roma vs Lazio" in text


class TestCheckAllAlerts:
    def test_nessun_segnale(self, temp_db):
        alerts = rlm_alert.check_all_alerts()
        assert alerts == []

    def test_segnale_senza_rlm(self, temp_db):
        _seed_signal("m1")
        _seed_snapshots("m1", "1", [2.10, 2.09, 2.08])
        alerts = rlm_alert.check_all_alerts()
        assert len(alerts) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
