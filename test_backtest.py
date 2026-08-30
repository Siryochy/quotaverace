"""
Test unitari per il motore di backtest (calibrazione EV atteso vs ROI).
"""

import json

import pytest

import backtest as bt_module
from backtest import backtest, _load_from_json, format_backtest, run_backtest


def _bet(won, quota=2.0, ev=0.05, evento="X vs Y", esito="1"):
    return {"evento": evento, "esito": esito, "quota": quota, "ev": ev, "won": won}


class TestBacktestMetrics:
    def test_campione_vuoto(self):
        s = backtest([])
        assert s["n"] == 0
        assert s["hit_rate"] == 0.0
        assert s["roi"] == 0.0

    def test_tutte_vinte(self):
        s = backtest([_bet(True, 2.0, 0.10) for _ in range(10)])
        assert s["n"] == 10
        assert s["hit_rate"] == 100.0
        assert s["roi"] == pytest.approx(100.0)  # +1 unita ognuna su 2.0

    def test_tutte_perse(self):
        s = backtest([_bet(False, 2.0, 0.10) for _ in range(10)])
        assert s["hit_rate"] == 0.0
        assert s["roi"] == pytest.approx(-100.0)

    def test_ev_atteso_medio(self):
        s = backtest([_bet(True, 2.0, 0.10), _bet(False, 2.0, 0.04)])
        assert s["roi_edge"] == pytest.approx(7.0)  # (10%+4%)/2

    def test_net_units_cumulate(self):
        # win su 2.0 -> +1u ; loss -> -1u
        s = backtest([_bet(True, 2.0, 0.1), _bet(False, 2.0, 0.1), _bet(True, 2.0, 0.1)])
        assert s["net_units"] == pytest.approx(1.0)

    def test_flag_campione(self):
        assert backtest([])["sufficiente"] is False
        assert backtest([_bet(True) for _ in range(bt_module.MIN_SAMPLE)])["sufficiente"] is True
        assert backtest([_bet(True) for _ in range(bt_module.MIN_WARN_SAMPLE)])["warn"] is True

    def test_ev_implicito_se_mancante(self):
        # senza ev esplicita, usa prob*quota-1
        s = backtest([{"evento": "A vs B", "esito": "1", "quota": 2.0, "won": True, "prob": 0.6}])
        assert s["roi_edge"] == pytest.approx((0.6 * 2.0 - 1.0) * 100)


class TestRequestFormat:
    def test_nessun_dato(self):
        text = format_backtest(backtest([]))
        assert "BACKTEST" in text
        assert "Gioca responsabilmente" in text

    def test_verdetto_campione_piccolo(self):
        text = format_backtest(backtest([_bet(True) for _ in range(5)]))
        assert "campione troppo piccolo" in text.lower()

    def test_verdetto_calibrato(self):
        # ROI positivo e vicino all'EV atteso
        bets = [_bet(True, 2.0, 0.05) for _ in range(bt_module.MIN_WARN_SAMPLE)]
        text = format_backtest(backtest(bets))
        assert "calibrato" in text.lower() or "coerente" in text.lower()

    def test_verdetto_edge_non_confermato(self):
        # ROI negativo nonostante EV atteso positivo
        bets = [_bet(False, 4.0, 0.30) for _ in range(bt_module.MIN_WARN_SAMPLE)]
        text = format_backtest(backtest(bets))
        assert "non confermato" in text.lower()


class TestLoadJson:
    def test_leggi_dataset(self, tmp_path):
        p = tmp_path / "sig.json"
        p.write_text(json.dumps([
            {"evento": "A vs B", "esito": "1", "quota": 2.0, "ev": 0.1, "esito_finale": "won"},
            {"evento": "C vs D", "esito": "2", "quota": 3.0, "ev": 0.2, "esito_finale": "lost"},
            {"evento": "E vs F", "esito": "X", "quota": 3.2, "esito_finale": "pending"},
        ]))
        bets = _load_from_json(str(p))
        # pending escluso
        assert len(bets) == 2

    def test_run_backtest_json(self, tmp_path):
        p = tmp_path / "sig.json"
        p.write_text(json.dumps([
            {"evento": "A vs B", "esito": "1", "quota": 2.0, "ev": 0.1, "esito_finale": "won"},
        ]))
        text = run_backtest(str(p))
        assert "BACKTEST" in text


# Test del DB viene coperto indirettamente: con DB vuoto deve tornare 0 segnali
def test_run_backtest_db_vuoto(monkeypatch, tmp_path):
    """Con tracker vuoto, run_backtest() deve gestire 0 segnali senza crash."""
    import tracker
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "empty.db")
    tracker.init_db()
    text = run_backtest()
    assert "scommesse chiuse" in text or "BACKTEST" in text
    assert "Gioca responsabilmente" in text