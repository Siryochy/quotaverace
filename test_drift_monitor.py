"""Test del monitor concept drift (drift_monitor.py).

Verifica che il Brier/LogLoss rolling sulle ultime previsioni chiuse venga
confrontato con la baseline storica e che lo stato passi a 'drift' quando
le probabilita' stanno perdendo calibrazione (sintomo di concept drift e
bisogno di retraining).
"""
import tempfile
from pathlib import Path

import pytest

import tracker
import drift_monitor


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _seed_prediction(mid, prob, won: bool, mercato="1X2", esito="1"):
    """Inserisce una previsione con esito finale gia' saldato."""
    tracker.save_prediction(mid, mercato, esito, 2.0, prob, 0.05,
                            market_prob=0.45, market_edge=0.03, status="value")
    conn = tracker._get_conn()
    conn.execute("UPDATE predictions SET esito_finale=?, profit=? "
                 "WHERE match_id=? AND mercato=? AND esito=?",
                 ("won" if won else "lost",
                  1.0 if won else -1.0, mid, mercato, esito))
    conn.commit()
    conn.close()


class TestCheckDrift:
    def test_insufficiente_senza_dati(self, temp_db):
        d = drift_monitor.check_drift()
        assert d["status"] == "insufficient"
        assert d["n"] == 0

    def test_ok_se_calibrato(self, temp_db):
        """Prob 0.8 e vincono il 80% delle volte: Brier ~0.16 costante,
        nessun drift."""
        for i in range(40):
            _seed_prediction(f"ok{i}", 0.8, won=(i % 5 != 0), mercato="1X2",
                             esito="1")
        d = drift_monitor.check_drift()
        assert d["status"] == "ok"
        assert d["brier_rolling"] is not None
        assert d["brier_rolling"] <= d["brier_baseline"] * 1.3

    def test_drift_quando_le_recenti_peggiorano(self, temp_db):
        """Baseline buona (0.8, vincono), ultime 20 pessime (0.8 ma perdono):
        il Brier rolling schizza verso 0.8 vs baseline ~0.16 -> drift."""
        for i in range(20):
            _seed_prediction(f"base{i}", 0.8, won=(i % 5 != 0), mercato="1X2",
                             esito="1")
        for i in range(20):
            _seed_prediction(f"recent{i}", 0.8, won=False, mercato="1X2",
                             esito="1")
        d = drift_monitor.check_drift()
        assert d["status"] == "drift"
        assert d["brier_rolling"] > d["brier_baseline"] * 1.3
        assert "retraining" in d["recommendation"].lower() or \
               "RETRAINING" in d["recommendation"]

    def test_formato_report(self, temp_db):
        for i in range(30):
            _seed_prediction(f"f{i}", 0.7, won=True, mercato="1X2", esito="1")
        d = drift_monitor.check_drift()
        lines = drift_monitor.format_drift_report(d)
        assert len(lines) >= 2
        assert any("Drift modello" in l for l in lines)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))