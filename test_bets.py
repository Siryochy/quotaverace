"""Test puntate automatiche (tabella `bets`)."""
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

import tracker


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _result(mid, home, away, sh, sa):
    tracker.save_result(mid, "Serie A", home, away, sh, sa,
                        datetime.now().isoformat())


def test_save_bet_and_settle_profit_euro(temp_db):
    _result("m1", "Inter", "Napoli", 2, 1)   # margine +1
    tracker.save_bet("m1", "1X2", "1", "1.100", 101, 2.10, 10.0)
    tracker.save_bet("m1", "OU", "Over 2.5", "1.101", 201, 1.90, 10.0)   # vinta 3 gol
    tracker.save_bet("m1", "AH", "Home -1.0", "1.102", 301, 2.05, 10.0)  # push
    settled, pushes = tracker.settle_bets()
    assert settled == 3 and pushes == 1
    rows = {r["esito"]: r for r in tracker.get_bets(closed=True)}
    assert rows["1"]["esito_finale"] == "won"
    assert rows["1"]["profit"] == 11.0          # 10 * (2.10-1)
    assert rows["Over 2.5"]["esito_finale"] == "won"
    assert rows["Over 2.5"]["profit"] == 9.0    # 10 * 0.90
    assert rows["Home -1.0"]["esito_finale"] == "push"
    assert rows["Home -1.0"]["profit"] == 0.0


def test_settle_bets_lost(temp_db):
    _result("m2", "Roma", "Empoli", 0, 1)
    tracker.save_bet("m2", "1X2", "1", "1.200", 102, 2.0, 5.0)
    assert tracker.settle_bets() == (1, 0)
    rows = tracker.get_bets()
    assert rows[0]["esito_finale"] == "lost"
    assert rows[0]["profit"] == -5.0


def test_settle_bets_idempotent(temp_db):
    _result("m3", "Atalanta", "Milan", 3, 1)
    tracker.save_bet("m3", "OU", "Over 2.5", "1.300", 203, 2.0, 10.0)
    assert tracker.settle_bets() == (1, 0)
    assert tracker.settle_bets() == (0, 0)


def test_bet_exists_open(temp_db):
    tracker.save_bet("m4", "1X2", "2", "1.400", 104, 3.0, 5.0)
    assert tracker.bet_exists_open("m4", "2") is True
    assert tracker.bet_exists_open("m4", "1") is False
    # dopo la chiusura non e' piu' "aperta"
    _result("m4", "Inter", "Napoli", 0, 2)
    tracker.settle_bets()
    assert tracker.bet_exists_open("m4", "2") is False


def test_bets_period(temp_db):
    _result("m5", "Inter", "Napoli", 2, 1)
    tracker.save_bet("m5", "1X2", "1", "1.500", 105, 2.0, 10.0)
    tracker.save_bet("m5", "OU", "Under 2.5", "1.501", 205, 1.85, 5.0)  # persa (3 gol)
    tracker.settle_bets()
    p = tracker.bets_period("2000-01-01")
    assert p["piazzate"] == 2
    assert p["stake_totale"] == 15.0
    assert p["vinti"] == 1 and p["persi"] == 1
    assert p["profit"] == 5.0      # +10 - 5
    # da domani: nessuna puntata
    p2 = tracker.bets_period("2099-01-01")
    assert p2["piazzate"] == 0