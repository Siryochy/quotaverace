"""Test della sezione Audit dataset ML dentro format_daily_report (bot.py)."""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tracker
import bot


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


TODAY = datetime.now().strftime("%Y-%m-%d")


def _seed_closed_match(mid="ml1", esito="1", quota=2.10, mercato="1X2",
                       prob=0.55, settled_at=None):
    """Un match chiuso con analisi + previsione saldata (won 2-1)."""
    tracker.save_match(mid, "Serie A", "Inter", "Napoli",
                       f"{TODAY}T13:00:00Z")
    tracker.save_analysis(mid, 1.74, 1.01, 0.53, 0.27, 0.20, 0.52, 0.15, "1",
                          quota, "Pinnacle", "value", market_prob=0.45,
                          market_edge=0.10)
    tracker.save_result(mid, "Serie A", "Inter", "Napoli", 2, 1,
                        datetime.now().isoformat())
    tracker.save_prediction(mid, mercato, esito, quota, prob, 0.15,
                            market_prob=0.45, market_edge=0.10, status="value")
    tracker.settle_predictions()
    if settled_at:
        conn = tracker._get_conn(); c = conn.cursor()
        c.execute("UPDATE predictions SET settled_at=? WHERE match_id=?",
                  (settled_at, mid))
        conn.commit(); conn.close()


def test_report_pulisce_senza_problemi(temp_db):
    _seed_closed_match()
    text = bot.format_daily_report(TODAY, "TEST")
    assert "Audit dataset ML" not in text  # pulito: nessuna sezione


def test_report_segnala_quota_invalida(temp_db):
    _seed_closed_match(quota=1.0)  # quota <= 1.0: non e' una quota di scommessa
    text = bot.format_daily_report(TODAY, "TEST")
    assert "🔎 *Audit dataset ML:* ⚠️" in text
    assert "quota_invalida" in text


def test_report_segnala_prob_fuori_range(temp_db):
    _seed_closed_match(prob=1.5)  # prob > 1: fuori range
    text = bot.format_daily_report(TODAY, "TEST")
    assert "🔎 *Audit dataset ML:* ⚠️" in text
    assert "prob_fuori_range" in text


def test_report_filtra_per_periodo(temp_db):
    """Le righe chiuse FUORI dal periodo non generano problemi nel report."""
    _seed_closed_match(mid="ml1", esito="1")
    # previsione di ieri con quota invalida: fuori dal since di oggi
    old = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT20:00:00")
    _seed_closed_match(mid="ml2", esito="X", quota=1.0, prob=0.55,
                       settled_at=old)
    text = bot.format_daily_report(TODAY, "TEST")
    # il problema di ml2 e' di ieri: non deve comparire nel report di oggi
    assert "Audit dataset ML" not in text
