"""
Test unitari per rating_engine (coefficienti normalizzati + conteggio match reali).
"""

import sqlite3

import pytest

import rating_engine as re_mod
import tracker
from rating_engine import compute_ratings, get_rating


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    db = tmp_path / "rating.db"
    monkeypatch.setattr(tracker, "DB_PATH", db)
    monkeypatch.setattr(re_mod, "DB_PATH", db)
    # crea schema minimo match_results
    conn = sqlite3.connect(str(db))
    conn.execute('''CREATE TABLE IF NOT EXISTS match_results (
        match_id TEXT PRIMARY KEY, league TEXT, home_team TEXT, away_team TEXT,
        score_home INTEGER, score_away INTEGER, result TEXT, settled_at TEXT)''')
    conn.commit(); conn.close()
    yield db


def _seed(home_count, away_count, gh, ga, ts="2024-05-01T18:00:00Z"):
    """Semina partite di Inter in casa (vs Napoli) e in trasferta (vs Napoli)."""
    conn = sqlite3.connect(str(tracker.DB_PATH))
    rows = []
    for i in range(home_count):
        rows.append((f"h{i}", "Serie A", "Inter", "Napoli", gh, ga,
                     "1" if gh > ga else ("2" if gh < ga else "X"), ts))
    for i in range(away_count):
        # in trasferta segna ga e ne subisce gh
        rows.append((f"a{i}", "Serie A", "Napoli", "Inter", ga, gh,
                     "2" if gh > ga else ("1" if gh < ga else "X"), ts))
    conn.executemany('''INSERT OR REPLACE INTO match_results VALUES (?,?,?,?,?,?,?,?)''', rows)
    conn.commit(); conn.close()


class TestRatingCoeff:
    def test_coefficienti_attorno_a_uno(self, _tmp_db):
        # Inter segna molto (2-1) sia in casa che in trasferta
        _seed(10, 10, 2, 1, "2024-05-01T18:00:00Z")
        compute_ratings()
        r = get_rating("Inter")
        assert r is not None
        assert r["attack_home"] > 1.0
        assert r["defense_home"] < 1.0  # subisce poco -> difesa migliore

    def test_conteggio_match_non_azzerato_da_time_decay(self, _tmp_db):
        # partite vecchie di mesi: prima del fix il peso troncava n a 0 -> None
        _seed(5, 5, 2, 1, "2024-01-01T18:00:00Z")
        compute_ratings()
        r = get_rating("Inter")
        assert r is not None

    def test_nessun_dato_nessun_rating(self, _tmp_db):
        compute_ratings()
        assert get_rating("Inter") is None

    def test_campione_minimo_sotto_soglia_usa_statico(self, _tmp_db):
        # 1-2 partite estreme NON devono ribaltare il modello: sotto la
        # soglia MIN_MATCHES il rating dinamico non viene usato (None ->
        # expected_goals ricade sul rating statico curato).
        _seed(2, 1, 4, 0, "2026-08-20T18:00:00Z")  # Inter vince 4-0 due volte
        compute_ratings()
        r = get_rating("Inter")
        assert r is None or (r["attack_home"] + r["attack_away"]) < 2.0