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


def _seed_signal(match_id, status="value", esito="1"):
    """Inserisce un segnale value nel DB."""
    tracker.save_match(match_id, "Serie A", "Inter", "Napoli",
                       "2099-01-01T20:00:00Z")
    tracker.save_analysis(match_id, 1.5, 1.2, 0.45, 0.25, 0.30,
                          0.52, 0.10, esito, 2.10, "Bet365", status,
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


class TestRecordSnapshotsForActiveSignals:
    """Il job RLM deve registrare snapshot freschi dalle cache odds ogni ciclo
    (5 minuti): senza, la serie di prezzi intraday non esiste e steam/RLM/
    crollo non scattano mai. Legge SOLO la cache (costo zero crediti)."""

    def _mock_odds(self, monkeypatch, payload):
        import odds_api
        monkeypatch.setattr(odds_api, "fetch_odds", lambda **kw: payload)

    def _get_price(self, match_id, esito):
        conn = tracker._get_conn()
        line_movement._ensure_table(conn)
        rows = conn.execute(
            "SELECT price FROM price_snapshots WHERE match_id=? AND esito=? "
            "ORDER BY recorded_at", (match_id, esito)).fetchall()
        conn.close()
        return [r[0] for r in rows]

    def test_registra_snapshot_dalla_cache_odds(self, temp_db, monkeypatch):
        _seed_signal("m10")
        self._mock_odds(monkeypatch, [{
            "id": "m10", "home_team": "FC Inter", "away_team": "Napoli",
            "commence_time": "2099-01-01T20:00:00Z",
            "bookmakers": [{
                "key": "bet365", "title": "Bet365",
                "markets": [{
                    "key": "h2h",
                    "outcomes": [
                        {"name": "FC Inter", "price": 2.10},
                        {"name": "Napoli", "price": 3.40},
                        {"name": "Draw", "price": 3.20},
                    ],
                }],
            }],
        }])
        n = rlm_alert.record_snapshots_for_active_signals()
        assert n == 1
        assert self._get_price("m10", "1") == [2.10]

    def test_job_accumula_serie_di_prezzi_intraday(self, temp_db, monkeypatch):
        """Due cicli del job con prezzi diversi: la serie cresce e il crollo
        scatta (era il bug: senza snapshot freschi il crollo non partiva)."""
        _seed_signal("m11")

        def _payload(price):
            return [{
                "id": "m11", "home_team": "Inter", "away_team": "Napoli",
                "commence_time": "2099-01-01T20:00:00Z",
                "bookmakers": [{
                    "key": "bet365", "title": "Bet365",
                    "markets": [{
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Inter", "price": price},
                            {"name": "Napoli", "price": 3.40},
                            {"name": "Draw", "price": 3.20},
                        ],
                    }],
                }],
            }]

        # Ciclo 1: quota 2.10 (snapshot 1)
        self._mock_odds(monkeypatch, _payload(2.10))
        assert rlm_alert.record_snapshots_for_active_signals() == 1
        # Ciclo 2: quota 1.95 (-7.1% dal primo snapshot)
        self._mock_odds(monkeypatch, _payload(1.95))
        assert rlm_alert.record_snapshots_for_active_signals() == 1

        prices = self._get_price("m11", "1")
        assert prices == [2.10, 1.95]
        # Con 2 snapshot e calo >= 5% -> crollo quota URGENTE
        sig = {"match_id": "m11", "esito": "1", "quota": 2.10,
               "ev": 0.10, "market_edge": 0.03, "status": "value",
               "home": "Inter", "away": "Napoli", "league": "Serie A"}
        alert = rlm_alert.check_rlm_for_signal(sig)
        assert alert is not None
        assert alert["alert_type"] == "crash"

    def test_nessun_snapshot_se_la_quota_manca(self, temp_db, monkeypatch):
        """Esito non presente nella cache (es. Over 2.5 non offerto): nessun
        snapshot, nessun crash del job."""
        _seed_signal("m12", esito="Over 2.5")
        self._mock_odds(monkeypatch, [{
            "id": "m12", "home_team": "Inter", "away_team": "Napoli",
            "commence_time": "2099-01-01T20:00:00Z",
            "bookmakers": [{
                "key": "bet365", "title": "Bet365",
                "markets": [{
                    "key": "totals",
                    "outcomes": [
                        {"name": "Over", "point": 1.5, "price": 1.70},
                        {"name": "Under", "point": 1.5, "price": 2.05},
                    ],
                }],
            }],
        }])
        assert rlm_alert.record_snapshots_for_active_signals() == 0
        assert self._get_price("m12", "Over 2.5") == []

    def test_match_non_trovato_nessun_snapshot(self, temp_db, monkeypatch):
        """Partita non piu' in cache (iniziata): nessun snapshot, nessun errore."""
        _seed_signal("m13")
        self._mock_odds(monkeypatch, [])
        assert rlm_alert.record_snapshots_for_active_signals() == 0


class TestCurrentPricesFromOdds:
    def test_normalizza_prefissi_squadre(self, temp_db, monkeypatch):
        """'FC Inter' (API) == 'Inter' (DB): i prefissi club non bloccano
        il matching (stesso problema del repair cassa 'CA Osasuna')."""
        import odds_api
        monkeypatch.setattr(odds_api, "fetch_odds", lambda **kw: [{
            "id": "m20", "home_team": "FC Machida Zelvia",
            "away_team": "Kawasaki Frontale",
            "commence_time": "2099-01-01T10:00:00Z",
            "bookmakers": [{
                "key": "pinnacle", "title": "Pinnacle",
                "markets": [{
                    "key": "h2h",
                    "outcomes": [
                        {"name": "FC Machida Zelvia", "price": 3.10},
                        {"name": "Kawasaki Frontale", "price": 4.00},
                        {"name": "Draw", "price": 3.30},
                    ],
                }],
            }],
        }])
        price, bm = rlm_alert._current_prices_from_odds(
            "m20", "Serie A", "Machida Zelvia", "Kawasaki Frontale", "2")
        assert price == 4.00
        assert bm == "Pinnacle"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
