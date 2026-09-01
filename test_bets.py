"""Test puntate automatiche (tabella `bets`) e idempotenza analisi."""
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

import tracker


def test_save_analysis_idempotente(temp_db):
    """Due analisi dello stesso match NON creano doppioni: la schedina
    (JOIN matches x match_analysis) non deve mostrare pick duplicati."""
    tracker.save_match("dup1", "Serie A", "Inter", "Napoli",
                       "2026-09-01T13:00:00Z")
    tracker.save_analysis("dup1", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55,
                          0.10, "1", 2.10, "Pinnacle", "value",
                          market_prob=0.45, market_edge=0.10)
    tracker.save_analysis("dup1", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55,
                          0.12, "1", 2.20, "Pinnacle", "strong_value",
                          market_prob=0.45, market_edge=0.12)
    rows = tracker.get_analysis_for_match("dup1")
    assert rows is not None and len(rows) == 16  # una sola riga (id + 15 campi)
    # l'ultima analisi vince (status strong_value)
    assert rows[12] == "strong_value"


def test_init_db_elimina_doppioni_analisi(temp_db):
    """I doppioni gia' presenti (da analisi ripetute pre-fix) vengono
    ripuliti da init_db: resta solo l'analisi piu' recente per match."""
    tracker.save_match("dup2", "Serie A", "Inter", "Napoli",
                       "2026-09-01T13:00:00Z")
    tracker.save_analysis("dup2", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55,
                          0.10, "1", 2.10, "Pinnacle", "value")
    # riga duplicata simulata (INSERT diretto, come accadeva prima del fix)
    conn = tracker._get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO match_analysis
                 (match_id,lam_h,lam_a,prob_1,prob_X,prob_2,prob_over,best_ev,
                  best_esito,best_quota,best_bookmaker,status,timestamp)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))''',
              ("dup2", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55, 0.12, "1", 2.20,
               "Pinnacle", "strong_value"))
    conn.commit(); conn.close()
    tracker.init_db()  # ripulisce
    conn = tracker._get_conn(); c = conn.cursor()
    n = c.execute("SELECT COUNT(*) FROM match_analysis WHERE match_id=?",
                  ("dup2",)).fetchone()[0]
    conn.close()
    assert n == 1


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


def test_settle_bets_details(temp_db):
    _result("m6", "Inter", "Napoli", 2, 1)
    tracker.save_match("m6", "Serie A", "Inter", "Napoli", "2026-09-01T13:00:00Z")
    tracker.save_bet("m6", "1X2", "1", "1.600", 106, 2.10, 10.0)
    settled, pushes, details = tracker.settle_bets(return_details=True)
    assert settled == 1 and pushes == 0 and len(details) == 1
    d = details[0]
    assert d["outcome"] == "won" and d["profit"] == 11.0
    assert d["home"] == "Inter" and d["away"] == "Napoli"
    assert d["league"] == "Serie A" and d["mode"] == "dry-run"
    # idempotente: il secondo giro non emette nuovi verdetti
    _, _, details2 = tracker.settle_bets(return_details=True)
    assert details2 == []
    # default: mantiene la vecchia firma (saldate, push)
    assert tracker.settle_bets() == (0, 0)


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