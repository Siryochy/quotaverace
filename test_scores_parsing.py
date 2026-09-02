"""Regression test: punteggi Casa/Fuori nella refertazione 1X2.

BUG 02/09 (FC Machida Zelvia vs Kawasaki Frontale): _update_results
assumeva che scores[0] di the-odds-api fosse la squadra di casa, ma
l'array NON ha ordine garantito. Con l'away in prima posizione i gol
arrivavano invertiti: vittoria casa reale (1-0) salvata come 1-2 →
bet sul "2" pagata come VINTA (+15.00 invece di -5.00).

Questi test coprono:
  1. match_scores_by_name: associazione gol→squadra per NOME (mai per posizione);
  2. il flusso reale _update_results con payload away-first;
  3. la verifica 1X2 di _prediction_outcome (casa/fuori non invertiti);
  4. repair_scores: rilevazione e riparazione dei punteggi gia' salvati male.
"""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tracker
import odds_api
import bot
import repair_scores


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        monkeypatch.setattr(odds_api, "CACHE_DIR", Path(td))
        tracker.init_db()
        yield db_path


# --- 1. Parsing per nome -------------------------------------------------

MACHIDA_AWAY_FIRST = {
    "id": "machida",
    "home_team": "FC Machida Zelvia",
    "away_team": "Kawasaki Frontale",
    "scores": [{"name": "Kawasaki Frontale", "score": 0},
               {"name": "FC Machida Zelvia", "score": 1}],
}


class TestMatchScoresByName:
    def test_home_first(self):
        m = {"home_team": "Inter", "away_team": "Napoli",
             "scores": [{"name": "Inter", "score": 2},
                        {"name": "Napoli", "score": 1}]}
        assert odds_api.match_scores_by_name(m) == (2, 1)

    def test_away_first_non_inverte(self):
        """REGRESSION: away in prima posizione NON deve invertire i gol."""
        assert odds_api.match_scores_by_name(MACHIDA_AWAY_FIRST) == (1, 0)

    def test_match_per_key(self):
        m = {"home_team": "Inter", "away_team": "Napoli",
             "scores": [{"key": "napoli", "score": 0},
                        {"key": "inter", "score": 3}]}
        assert odds_api.match_scores_by_name(m) == (3, 0)

    def test_punteggio_mancante_ritorna_none(self):
        m = {"home_team": "Inter", "away_team": "Napoli",
             "scores": [{"name": "Inter", "score": 2}]}
        assert odds_api.match_scores_by_name(m) is None

    def test_squadra_non_riconosciuta_ritorna_none(self):
        m = {"home_team": "Inter", "away_team": "Napoli",
             "scores": [{"name": "Juventus", "score": 2},
                        {"name": "Roma", "score": 1}]}
        assert odds_api.match_scores_by_name(m) is None

    def test_score_non_numerico_ritorna_none(self):
        m = {"home_team": "Inter", "away_team": "Napoli",
             "scores": [{"name": "Inter", "score": None},
                        {"name": "Napoli", "score": 1}]}
        assert odds_api.match_scores_by_name(m) is None

    def test_squadre_mancanti_ritornano_none(self):
        m = {"scores": [{"name": "Inter", "score": 2}]}
        assert odds_api.match_scores_by_name(m) is None


# --- 2. Flusso reale _update_results --------------------------------------

def _fake_context():
    return MagicMock()


def _patch_send(monkeypatch):
    calls = []

    async def fake_send(context, text):
        calls.append(text)

    monkeypatch.setattr(bot, "_send_bet_settlements", fake_send)
    return calls


def _patch_fetch_scores(monkeypatch, payload_per_sport):
    import sys

    def fake_fetch(sport=None, days_from=2):
        return payload_per_sport.get(sport, [])

    monkeypatch.setattr(odds_api, "fetch_scores", fake_fetch)
    monkeypatch.setitem(sys.modules, "odds_api", odds_api)


def _patch_ratings(monkeypatch):
    import sys
    import rating_engine
    monkeypatch.setattr(rating_engine, "compute_ratings", lambda: None)
    monkeypatch.setitem(sys.modules, "rating_engine", rating_engine)


def _patch_admin(monkeypatch):
    monkeypatch.setattr(bot, "_admin_chat_ids", lambda: [])
    monkeypatch.setattr(tracker, "get_subscribers", lambda *a, **k: [])


class TestUpdateResultsAwayFirst:
    """Il payload away-first di Machida-Kawasaki nel flusso REALE."""

    def _setup(self, temp_db):
        tracker.save_match("machida", "J1 League", "FC Machida Zelvia",
                           "Kawasaki Frontale", "2026-09-02T05:00:00Z")
        tracker.save_analysis("machida", 1.2, 1.6, 0.28, 0.26, 0.46, 0.52,
                              0.14, "2", 4.0, "Pinnacle", "value")
        tracker.save_bet("machida", "1X2", "2", None, None, 4.0, 5.0)

    def test_bet_sul_2_perde_con_vittoria_casa(self, temp_db, monkeypatch):
        """REGRESSION: bet sul 2 + vittoria casa reale (1-0) → LOST -5.00."""
        self._setup(temp_db)
        _patch_send(monkeypatch)
        _patch_fetch_scores(monkeypatch,
                            {"soccer_japan_j_league": [dict(MACHIDA_AWAY_FIRST)]})
        _patch_ratings(monkeypatch)
        _patch_admin(monkeypatch)

        bot._update_results()  # funzione SINCRONA (non serve asyncio)

        row = tracker.get_bets(limit=10)[0]
        assert row["esito_finale"] == "lost"
        assert row["profit"] == pytest.approx(-5.0)

    def test_risultato_salvato_non_invertito(self, temp_db, monkeypatch):
        self._setup(temp_db)
        _patch_send(monkeypatch)
        _patch_fetch_scores(monkeypatch,
                            {"soccer_japan_j_league": [dict(MACHIDA_AWAY_FIRST)]})
        _patch_ratings(monkeypatch)
        _patch_admin(monkeypatch)

        bot._update_results()

        conn = tracker._get_conn()
        sh, sa = conn.execute(
            "SELECT score_home, score_away FROM match_results WHERE match_id='machida'"
        ).fetchone()
        conn.close()
        assert (sh, sa) == (1, 0)

    def test_bet_sul_1_vince_con_vittoria_casa(self, temp_db, monkeypatch):
        self._setup(temp_db)
        tracker.save_bet("machida", "1X2", "1", None, None, 2.5, 5.0)
        _patch_send(monkeypatch)
        _patch_fetch_scores(monkeypatch,
                            {"soccer_japan_j_league": [dict(MACHIDA_AWAY_FIRST)]})
        _patch_ratings(monkeypatch)
        _patch_admin(monkeypatch)

        bot._update_results()

        rows = {r["esito"]: r for r in tracker.get_bets(limit=10)}
        assert rows["1"]["esito_finale"] == "won"
        assert rows["1"]["profit"] == pytest.approx(7.5)  # 5 * (2.5 - 1)
        assert rows["2"]["esito_finale"] == "lost"


# --- 3. Verifica 1X2 diretta ----------------------------------------------

class TestPredictionOutcome1X2:
    def test_2_perde_se_vince_casa(self):
        outcome, _ = tracker._prediction_outcome("1X2", "2", 4.0, 1, 0,
                                                 "Machida", "Kawasaki")
        assert outcome == "lost"

    def test_2_vince_se_vince_fuori(self):
        outcome, _ = tracker._prediction_outcome("1X2", "2", 4.0, 0, 1,
                                                 "Machida", "Kawasaki")
        assert outcome == "won"

    def test_1_vince_se_vince_casa(self):
        outcome, _ = tracker._prediction_outcome("1X2", "1", 2.0, 2, 1,
                                                 "Machida", "Kawasaki")
        assert outcome == "won"

    def test_x_con_pareggio(self):
        outcome, _ = tracker._prediction_outcome("1X2", "X", 3.0, 1, 1,
                                                 "Machida", "Kawasaki")
        assert outcome == "won"


# --- 4. Riparazione dati esistenti ----------------------------------------

def _seed_inverted(temp_db):
    """Stato produzione del bug: risultato 1-2 (errato) + bet sul 2 vinta."""
    tracker.save_match("machida", "J1 League", "FC Machida Zelvia",
                       "Kawasaki Frontale", "2026-09-02T05:00:00Z")
    tracker.save_analysis("machida", 1.2, 1.6, 0.28, 0.26, 0.46, 0.52,
                          0.14, "2", 4.0, "Pinnacle", "value")
    tracker.save_result("machida", "J1 League", "FC Machida Zelvia",
                        "Kawasaki Frontale", 1, 2, "2026-09-02T13:53:49Z")
    tracker.save_bet("machida", "1X2", "2", None, None, 4.0, 5.0)
    tracker.save_prediction("machida", "1X2", "2", 4.0, 0.25, 0.14,
                            status="value")
    tracker.settle_bets()
    tracker.settle_predictions()
    rows = {r["esito"]: r for r in tracker.get_bets(limit=10)}
    assert rows["2"]["esito_finale"] == "won"      # stato errato iniziale
    assert rows["2"]["profit"] == pytest.approx(15.0)


def _patch_repair_fetch(monkeypatch, true_scores):
    """Patch di repair_scores.fetch_scores (importato per nome nel modulo:
    patchare odds_api.fetch_scores non basterebbe)."""
    def fake_fetch(sport=None, days_from=2):
        return list(true_scores.values()) if sport else []

    monkeypatch.setattr(repair_scores, "fetch_scores", fake_fetch)


class TestRepairScores:
    def test_dry_run_non_tocca_il_db(self, temp_db, monkeypatch):
        _seed_inverted(temp_db)
        _patch_repair_fetch(monkeypatch,
                            {"m": dict(MACHIDA_AWAY_FIRST,
                                       completed=True)})
        rc = repair_scores.repair(apply=False, days_from=7)
        assert rc == 1  # rilevata incongruenza
        conn = tracker._get_conn()
        sh, sa = conn.execute(
            "SELECT score_home, score_away FROM match_results "
            "WHERE match_id='machida'").fetchone()
        conn.close()
        assert (sh, sa) == (1, 2)  # invariato nel dry-run

    def test_apply_corregge_risultato_e_ledger(self, temp_db, monkeypatch):
        _seed_inverted(temp_db)
        _patch_repair_fetch(monkeypatch,
                            {"m": dict(MACHIDA_AWAY_FIRST,
                                       completed=True)})
        rc = repair_scores.repair(apply=True, days_from=7)
        assert rc == 0

        conn = tracker._get_conn()
        sh, sa = conn.execute(
            "SELECT score_home, score_away FROM match_results "
            "WHERE match_id='machida'").fetchone()
        conn.close()
        assert (sh, sa) == (1, 0)  # risultato corretto

        rows = {r["esito"]: r for r in tracker.get_bets(limit=10)}
        assert rows["2"]["esito_finale"] == "lost"
        assert rows["2"]["profit"] == pytest.approx(-5.0)

    def test_apply_ri_salda_previsioni(self, temp_db, monkeypatch):
        _seed_inverted(temp_db)
        _patch_repair_fetch(monkeypatch,
                            {"m": dict(MACHIDA_AWAY_FIRST,
                                       completed=True)})
        repair_scores.repair(apply=True, days_from=7)

        conn = tracker._get_conn()
        row = conn.execute(
            "SELECT esito_finale, profit FROM predictions "
            "WHERE match_id='machida' AND esito='2'").fetchone()
        conn.close()
        assert row[0] == "lost"
        assert row[1] == pytest.approx(-1.0)  # unita': -1 per persa

    def test_apply_ri_salda_cassa(self, temp_db, monkeypatch):
        _seed_inverted(temp_db)
        tracker.save_cassa_entry("FC Machida Zelvia vs Kawasaki Frontale",
                                 "2", 4.0, 5.0)
        tracker.settle_cassa()
        conn = tracker._get_conn()
        assert conn.execute(
            "SELECT esito_finale FROM cassa WHERE id=1").fetchone()[0] == "won"
        conn.close()

        _patch_repair_fetch(monkeypatch,
                            {"m": dict(MACHIDA_AWAY_FIRST,
                                       completed=True)})
        repair_scores.repair(apply=True, days_from=7)

        conn = tracker._get_conn()
        row = conn.execute(
            "SELECT esito_finale, profit FROM cassa WHERE id=1").fetchone()
        conn.close()
        assert row[0] == "lost"
        assert row[1] == pytest.approx(-5.0)

    def test_nessuna_incongruenza_ritorna_zero(self, temp_db, monkeypatch):
        _seed_inverted(temp_db)
        # fetch ritorna il punteggio GIA' salvato (corretto): nessuna riparazione
        coerente = dict(MACHIDA_AWAY_FIRST)
        coerente["scores"] = [{"name": "FC Machida Zelvia", "score": 1},
                              {"name": "Kawasaki Frontale", "score": 2}]
        _patch_repair_fetch(monkeypatch, {"m": dict(coerente, completed=True)})
        rc = repair_scores.repair(apply=True, days_from=7)
        assert rc == 0


if __name__ == "__main__":
    import pytest as _p
    _p.main([__file__, "-v"])
