"""Test backtest_mc: walk-forward (no look-ahead), Kelly dinamico, Monte Carlo.

Il backtest USA lo stesso codice di produzione (ensemble + adaptive_staking):
i test verificano walk-forward senza look-ahead, metriche sensate e
riproducibilita' (seed fisso).
"""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tracker
import backtest_mc


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _seed_rows(n=40, win_rate=0.55, quota=2.10, edge=0.08):
    """n righe chiuse con esiti pseudocasuali deterministici (win_rate)."""
    base = datetime.now(timezone.utc) - timedelta(days=2)
    for i in range(n):
        mid = f"m{i}"
        start = (base + timedelta(minutes=i)).isoformat().replace("+00:00", "Z")
        tracker.save_match(mid, "Serie A", f"H{i}", f"A{i}", start)
        tracker.save_analysis(mid, 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                              f"H{i}", quota, "Pinnacle", "value",
                              market_prob=0.45, market_edge=edge)
        tracker.save_prediction(mid, "1X2", f"H{i}", quota, 0.55, edge)
        # Spread delle vincite pseudo-casuale (moltiplicatore primo): con i
        # consecutivi il pattern `i % 100` concentra TUTTE le vincite in testa
        # (es. 60 righe a win_rate .60 = 60/60 vinte) e il fold di training
        # resta a classe singola → XGBoost non si addestra.
        won = (i * 7) % 100 < int(win_rate * 100)
        tracker.save_result(mid, "Serie A", f"H{i}", f"A{i}", 2 if won else 0, 1,
                            (base + timedelta(hours=2, minutes=i)).isoformat())
        tracker.settle_predictions()


def test_insufficient_data(temp_db):
    res = backtest_mc.run_backtest_mc()
    assert res["status"] == "insufficient_data"


def test_walk_forward_no_rows_before_window(temp_db):
    """Il training del primo giro usa SOLO le righe prima della finestra."""
    _seed_rows(12)
    rows = backtest_mc.load_bets_rows()
    assert len(rows) == 12
    # window 30 > 12: nessun training, predizioni = prob base
    preds = backtest_mc._walk_forward(rows, window=30)
    assert len(preds) == 12
    for p, r in zip(preds, rows):
        assert p["base_prob"] > 0


def test_monte_carlo_metriche_sensate(temp_db):
    """Con edge positivo: ROI base > 0, MaxDD >= ROI, percentili coerenti."""
    _seed_rows(60, win_rate=0.60)
    res = backtest_mc.run_backtest_mc(window=30, sims=200)
    assert res["status"] == "ok"
    assert res["n_bets"] > 0
    assert res["roi_base"] > 0
    assert res["maxdd_base"] >= 0
    assert res["roi_p5"] <= res["roi_mediana"] <= res["roi_p95"]
    assert res["maxdd_p95"] >= res["maxdd_mediana"]
    assert 0 <= res["bust_pct"] <= 100


def test_monte_carlo_riproducibile(temp_db):
    """Seed fisso: due run producono le stesse statistiche."""
    _seed_rows(50, win_rate=0.55)
    r1 = backtest_mc.run_backtest_mc(window=25, sims=150)
    r2 = backtest_mc.run_backtest_mc(window=25, sims=150)
    assert r1["roi_mediana"] == r2["roi_mediana"]
    assert r1["maxdd_p95"] == r2["maxdd_p95"]


def test_kelly_stake_fallback_proporzionale(temp_db, monkeypatch):
    """Senza adaptive_staking il fallback scala col bankroll e rispetta il cap."""
    monkeypatch.setitem(__import__("sys").modules, "adaptive_staking", None)
    r_small = backtest_mc._kelly_stake(0.60, 2.10, "value", 50.0, 50.0)
    r_big = backtest_mc._kelly_stake(0.60, 2.10, "value", 500.0, 500.0)
    assert r_small["stake"] < r_big["stake"]
    assert r_big["stake"] <= 500.0 * 0.03 + 0.01   # cap 3% value


def test_simulazione_perde_se_tutte_perse(temp_db):
    bets = [{"prob": 0.55, "quota": 2.10, "status": "value", "label": 0,
             "ev": 0.15, "settled_at": ""} for _ in range(10)]
    res = backtest_mc._simulate_sequence(bets, 100.0)
    assert res["pnl"] < 0
    assert res["busted"] is False          # kelly dinamico non porta a rovina
    assert res["max_dd"] > 0


def test_formato_report_contiene_maxdd(temp_db):
    _seed_rows(40, win_rate=0.58)
    res = backtest_mc.run_backtest_mc(window=20, sims=100)
    text = backtest_mc.format_backtest_report(res)
    assert "MAX DRAWDOWN" in text
    assert "ROI atteso" in text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
