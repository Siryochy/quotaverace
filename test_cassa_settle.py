"""Test saldamento automatico della cassa.

Copre tracker.settle_cassa: il match delle scommesse aperte della cassa con
i risultati reali (match_results), il calcolo del profitto, l'idempotenza e
i totali realizzati (cassa_totals).
"""
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

import tracker


@pytest.fixture()
def temp_db(monkeypatch):
    """DB SQLite temporaneo per isolare i test."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _result(mid, home, away, sh, sa):
    tracker.save_result(mid, "Serie A", home, away, sh, sa,
                        datetime.now().isoformat())


def test_settle_matches_and_computes_profit(temp_db):
    _result("m1", "Osasuna", "Getafe", 2, 1)  # 3 gol: Over vinto, BTTS vinto, 1 vinto
    tracker.save_cassa_entry("Osasuna vs Getafe", "Over 2.5", 3.05, 20)
    tracker.save_cassa_entry("Osasuna vs Getafe", "Under 2.5", 1.85, 10)
    tracker.save_cassa_entry("Osasuna vs Getafe", "1", 2.0, 5)

    settled = tracker.settle_cassa()

    assert settled == 3
    rows = {r["esito"]: r for r in tracker.get_cassa()}
    assert rows["Over 2.5"]["esito_finale"] == "won"
    assert rows["Over 2.5"]["profit"] == 41.0          # (3.05-1)*20
    assert rows["Under 2.5"]["esito_finale"] == "lost"
    assert rows["Under 2.5"]["profit"] == -10.0
    assert rows["1"]["esito_finale"] == "won"
    assert rows["1"]["profit"] == 5.0


def test_settle_leaves_pending_without_result(temp_db):
    tracker.save_cassa_entry("Roma vs Empoli", "Over 2.5", 2.10, 10)
    settled = tracker.settle_cassa()
    assert settled == 0
    rows = tracker.get_cassa()
    assert rows[0]["esito_finale"] is None
    assert rows[0]["profit"] is None


def test_settle_expired_game_settles_later(temp_db):
    tracker.save_cassa_entry("Roma vs Empoli", "Over 2.5", 2.10, 10)
    assert tracker.settle_cassa() == 0
    _result("m2", "Roma", "Empoli", 0, 0)  # 0 gol: Over perso
    assert tracker.settle_cassa() == 1
    rows = tracker.get_cassa()
    assert rows[0]["esito_finale"] == "lost"
    assert rows[0]["profit"] == -10.0


def test_settle_is_idempotent(temp_db):
    _result("m3", "Inter", "Napoli", 3, 1)
    tracker.save_cassa_entry("Inter vs Napoli", "Over 2.5", 2.00, 20)
    assert tracker.settle_cassa() == 1
    assert tracker.settle_cassa() == 0   # seconda chiamata: nessuna nuova riga
    rows = tracker.get_cassa()
    assert rows[0]["esito_finale"] == "won"
    assert rows[0]["profit"] == 20.0


def test_settle_normalizes_team_names(temp_db):
    _result("m4", "FC Barcelona", "Real Madrid", 1, 0)
    tracker.save_cassa_entry("Barcelona vs Real Madrid", "2", 3.5, 10)
    assert tracker.settle_cassa() == 1
    rows = tracker.get_cassa()
    assert rows[0]["esito_finale"] == "lost"
    assert rows[0]["profit"] == -10.0


def test_settle_btts_variants(temp_db):
    _result("m5", "Atalanta", "Milan", 1, 1)
    tracker.save_cassa_entry("Atalanta vs Milan", "Gol Gol (BTTS)", 1.90, 20)
    tracker.save_cassa_entry("Atalanta vs Milan", "Under 2.5", 1.80, 20)
    assert tracker.settle_cassa() == 2
    rows = {r["esito"]: r for r in tracker.get_cassa()}
    assert rows["Gol Gol (BTTS)"]["esito_finale"] == "won"   # BTTS 1-1
    assert rows["Under 2.5"]["esito_finale"] == "won"        # 2 gol <= 2


def test_cassa_totals_realizzati(temp_db):
    _result("m6", "Osasuna", "Getafe", 2, 1)
    tracker.save_cassa_entry("Osasuna vs Getafe", "Over 2.5", 3.05, 20)   # vinta +41
    tracker.save_cassa_entry("Osasuna vs Getafe", "Under 2.5", 1.85, 10)  # persa -10
    tracker.save_cassa_entry("Roma vs Empoli", "Over 2.5", 2.10, 10)      # in gioco
    tracker.settle_cassa()
    t = tracker.cassa_totals()
    assert t["chiusi"] == 2
    assert t["vinti"] == 1
    assert t["persi"] == 1
    assert t["in_gioco"] == 1
    assert t["speso_chiuso"] == 30.0
    assert t["profit_realizzato"] == 31.0          # 41 - 10
    assert t["roi"] == round(31.0 / 30.0 * 100, 2)