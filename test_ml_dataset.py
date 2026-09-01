"""Test dataset ML: feature+label dalle previsioni chiuse e puntate saldate."""
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

import tracker
import ml_dataset


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _seed():
    """Un match chiuso (Inter 2-1 Napoli) con analisi + previsioni + risultato."""
    tracker.save_match("ml1", "Serie A", "Inter", "Napoli", "2026-09-01T13:00:00Z")
    tracker.save_analysis("ml1", 1.74, 1.01, 0.53, 0.27, 0.20, 0.52, 0.15, "1",
                          2.10, "Pinnacle", "value", market_prob=0.45,
                          market_edge=0.10)
    tracker.save_result("ml1", "Serie A", "Inter", "Napoli", 2, 1,
                        datetime.now().isoformat())
    tracker.save_prediction("ml1", "1X2", "1", 2.10, 0.55, 0.15,
                            market_prob=0.45, market_edge=0.10, status="value")
    tracker.save_prediction("ml1", "OU", "Over 2.5", 1.95, 0.55, 0.07,
                            market_prob=0.52, market_edge=0.03, status="value")
    tracker.settle_predictions()


def test_predictions_rows_con_feature_e_label(temp_db):
    _seed()
    rows = ml_dataset.build_training_rows(source="predictions")
    assert len(rows) == 2
    row = next(r for r in rows if r["mercato"] == "1X2")
    assert row["esito"] == "1" and row["league"] == "Serie A"
    assert row["lam_h"] == pytest.approx(1.74)
    assert row["prob_1"] == pytest.approx(0.53)
    assert row["market_edge"] == pytest.approx(0.10)
    assert row["esito_finale"] == "won" and row["label_ml"] == 1
    assert row["profit"] is not None


def test_solo_chiuse(temp_db):
    _seed()
    # previsione ancora aperta: NON deve comparire nel dataset
    tracker.save_prediction("ml2", "1X2", "X", 3.2, 0.28, 0.02,
                            market_prob=0.29, market_edge=-0.01, status="no_value")
    rows = ml_dataset.build_training_rows(source="predictions")
    assert all(r["match_id"] == "ml1" for r in rows)
    assert len(rows) == 2


def test_bets_rows(temp_db):
    _seed()
    tracker.save_bet("ml1", "1X2", "1", "1.700", 107, 2.10, 10.0)
    tracker.settle_bets()
    rows = ml_dataset.build_training_rows(source="bets")
    assert len(rows) == 1
    assert rows[0]["label_ml"] == 1 and rows[0]["profit"] == 11.0
    assert rows[0]["league"] == "Serie A"


def test_source_all_unisce(temp_db):
    _seed()
    tracker.save_bet("ml1", "1X2", "1", "1.700", 107, 2.10, 10.0)
    tracker.settle_bets()
    rows = ml_dataset.build_training_rows()
    mercati = {r["mercato"] for r in rows}
    assert mercati == {"1X2", "OU"}


def test_export_csv(temp_db, tmp_path):
    _seed()
    out = tmp_path / "train.csv"
    n = ml_dataset.export_csv(out)
    assert n >= 2 and out.exists()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert "label_ml" in lines[0] and "lam_h" in lines[0]
    assert len(lines) == n + 1  # header + righe