"""Test ledger previsioni e Asian Handicap.

Copre tracker: save_prediction (UPSERT idempotente), settle_predictions con
esiti 1X2/Over-Under/AH (incluso lo split-bet delle quarter line),
predictions_summary per mercato; e poisson_engine.ah_outcome_probs.
"""
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

import tracker
from poisson_engine import ah_outcome_probs


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


# --- AH: P/L (nuova logica split-bet) ---

def test_ah_pnl_quarter_home():
    # Home -0.75, vittoria di 1 gol: meta' vinta (+0.5*(q-1)), meta' push
    assert tracker.ah_pnl_units("Home -0.75", 2.0, 1) == 0.5
    # pareggio: entrambe le meta' perse
    assert tracker.ah_pnl_units("Home -0.75", 2.0, 0) == -1.0


def test_ah_pnl_away_quarter_draw():
    # Away +0.25 col pareggio: una meta' win, una push
    assert tracker.ah_pnl_units("Away +0.25", 2.0, 0) == 0.5
    # Away +0.25 con vittoria di un gol della casa (doppia linea persa)
    assert tracker.ah_pnl_units("Away +0.25", 2.0, 1) == -1.0


def test_ah_pnl_half_line_push():
    # Home -1.0 con vittoria esatta di 1 gol: push totale
    assert tracker.ah_pnl_units("Home -1.0", 2.0, 1) == 0.0
    # Home -1.0 con vittoria di 2: vinta
    assert tracker.ah_pnl_units("Home -1.0", 2.0, 2) == 1.0


def test_ah_pnl_double_quarter():
    # Home -1.25, vittoria di 1: meta' (-1.0) push, meta' (-1.5) persa
    assert tracker.ah_pnl_units("Home -1.25", 2.0, 1) == -0.5
    # Home -1.25, vittoria di 2: entrambe le meta' vinte
    assert tracker.ah_pnl_units("Home -1.25", 2.0, 2) == 1.0
    # Home -1.75, vittoria di 2: meta' (-2.0) push, meta' (-1.5) vinta
    assert tracker.ah_pnl_units("Home -1.75", 2.0, 2) == 0.5


def test_ah_pnl_unknown_side():
    assert tracker.ah_pnl_units("Magari -1", 2.0, 0) is None


# --- Poisson AH: probabilita' coerenti ---

def test_ah_probs_sum_to_one():
    p_win, p_push, p_lose = ah_outcome_probs(1.5, 1.3, -0.5, "home")
    assert abs((p_win + p_push + p_lose) - 1.0) < 1e-9
    p_win2, p_push2, p_lose2 = ah_outcome_probs(1.5, 1.3, 0.75, "away")
    assert abs((p_win2 + p_push2 + p_lose2) - 1.0) < 1e-9


def test_ah_probs_complement_away_side():
    # Scommettere Away +0.5 = complementare al Home -0.5 (push a meta' via)
    wh, ph, lh = ah_outcome_probs(1.5, 1.3, -0.5, "home")
    wa, pa, la = ah_outcome_probs(1.5, 1.3, 0.5, "away")
    assert abs(wa - lh) < 1e-9
    assert abs(la - wh) < 1e-9


def test_ah_probs_home_stronger():
    # Con casa nettamente piu' forte, Home -0.5 ha p_win > 0.5
    w, _, _ = ah_outcome_probs(2.0, 0.8, -0.5, "home")
    assert w > 0.5


def test_ah_probs_half_push_prob():
    # Home -1.0: la probabilita' di push = probabilita' di vittoria esatta 1-0
    # (margine 1), piu' eventuali 2-1, 3-2... (margine 1)
    _, push, _ = ah_outcome_probs(1.5, 1.0, -1.0, "home")
    assert 0.0 < push < 0.5


# --- Settle ledger ---

def test_settle_1x2_and_over(temp_db):
    _result("m1", "Osasuna", "Getafe", 2, 1)
    tracker.save_prediction("m1", "1X2", "Osasuna", 2.40, 0.52, 0.09)
    tracker.save_prediction("m1", "1X2", "Getafe", 3.10, 0.24, 0.02)
    tracker.save_prediction("m1", "OU", "Over 2.5", 2.10, 0.56, 0.15)
    settled, pushes = tracker.settle_predictions()
    assert settled == 3 and pushes == 0
    rows = {r["esito"]: r for r in tracker.get_predictions(closed=True)}
    assert rows["Osasuna"]["esito_finale"] == "won"
    assert rows["Osasuna"]["profit"] == 1.4
    assert rows["Getafe"]["esito_finale"] == "lost"
    assert rows["Getafe"]["profit"] == -1.0
    assert rows["Over 2.5"]["esito_finale"] == "won"
    assert rows["Over 2.5"]["profit"] == 1.1


def test_settle_ah_split_and_push(temp_db):
    _result("m2", "Inter", "Napoli", 2, 1)   # margine +1
    # Home -0.75: meta' win + meta' push -> +0.5*(q-1)
    tracker.save_prediction("m2", "AH", "Home -0.75", 2.00, 0.55, 0.08)
    # Home -1.0: push totale (vittoria esatta di 1)
    tracker.save_prediction("m2", "AH", "Home -1.0", 2.05, 0.45, 0.04)
    # Away +0.25: persa (casa vince di 1: entrambe le meta' perse) -> -1.0
    tracker.save_prediction("m2", "AH", "Away +0.25", 1.90, 0.45, 0.03)
    settled, pushes = tracker.settle_predictions()
    assert settled == 3 and pushes == 1
    rows = {r["esito"]: r for r in tracker.get_predictions(closed=True)}
    assert rows["Home -0.75"]["esito_finale"] == "won"
    assert rows["Home -0.75"]["profit"] == 0.5
    assert rows["Home -1.0"]["esito_finale"] == "push"
    assert rows["Home -1.0"]["profit"] == 0.0
    assert rows["Away +0.25"]["esito_finale"] == "lost"
    assert rows["Away +0.25"]["profit"] == -1.0


def test_settle_pending_without_result(temp_db):
    tracker.save_prediction("mX", "OU", "Over 2.5", 2.0, 0.55, 0.10)
    assert tracker.settle_predictions() == (0, 0)
    rows = tracker.get_predictions()
    assert rows[0]["esito_finale"] is None


def test_settle_is_idempotent(temp_db):
    _result("m3", "Roma", "Empoli", 0, 0)
    tracker.save_prediction("m3", "1X2", "Roma", 2.0, 0.5, 0.05)
    assert tracker.settle_predictions() == (1, 0)
    assert tracker.settle_predictions() == (0, 0)
    rows = tracker.get_predictions()
    assert rows[0]["esito_finale"] == "lost"


def test_save_prediction_upsert_keeps_settled(temp_db):
    """Rianalisi dopo la chiusura: la previsione saldata non si tocca."""
    _result("m4", "Atalanta", "Milan", 3, 0)
    tracker.save_prediction("m4", "1X2", "Atalanta", 2.0, 0.5, 0.05)
    tracker.settle_predictions()
    # nuova analisi con prezzo diverso
    tracker.save_prediction("m4", "1X2", "Atalanta", 1.95, 0.5, 0.03)
    rows = tracker.get_predictions()
    assert rows[0]["esito_finale"] == "won"
    assert rows[0]["quota"] == 2.0          # non sovrascritta
    assert rows[0]["profit"] == 1.0


def test_summary_settled_since(temp_db):
    """predictions_summary filtra per data di saldo (report giornaliero)."""
    _result("m7", "Inter", "Napoli", 2, 1)
    tracker.save_prediction("m7", "1X2", "Inter", 2.0, 0.5, 0.10)
    tracker.settle_predictions()
    assert tracker.predictions_summary()["1X2"]["n"] == 1
    # da domani in poi: nessuna previsione saldata
    assert tracker.predictions_summary(settled_since="2099-01-01") == {}
    # da ieri: presente
    assert tracker.predictions_summary(settled_since="2000-01-01")["1X2"]["n"] == 1


def test_day_completed_no_matches(temp_db):
    assert tracker.day_completed("2020-01-01") is False


def test_day_completed_waits_for_result(temp_db):
    """Partita del giorno iniziata senza risultato: giornata non chiusa."""
    tracker.save_match("m1", "Lega", "A", "B", "2020-01-01T10:00:00Z")
    assert tracker.day_completed("2020-01-01") is False
    tracker.save_result("m1", "Lega", "A", "B", 1, 0,
                        datetime.now().isoformat())
    assert tracker.day_completed("2020-01-01") is True


def test_day_completed_all_future_returns_true(temp_db):
    """Partite non ancora iniziate non bloccano la chiusura giornata."""
    tracker.save_match("m1", "Lega", "A", "B", "2100-01-01T10:00:00Z")
    assert tracker.day_completed("2100-01-01") is True


def test_cassa_period_filters_settled(temp_db):
    """cassa_period conta solo le puntate saldate da una certa data."""
    _result("m8", "Osasuna", "Getafe", 2, 1)
    tracker.save_cassa_entry("Osasuna vs Getafe", "Over 2.5", 3.05, 20)
    tracker.save_cassa_entry("Osasuna vs Getafe", "Under 2.5", 1.85, 10)
    tracker.settle_cassa()
    p = tracker.cassa_period("2000-01-01")
    assert p["chiusi"] == 2 and p["vinti"] == 1 and p["persi"] == 1
    assert p["profit"] == 31.0
    # da domani: nessuna
    p2 = tracker.cassa_period("2099-01-01")
    assert p2["chiusi"] == 0 and p2["profit"] == 0.0


def test_predictions_summary_per_mercato(temp_db):
    _result("m5", "Inter", "Napoli", 2, 1)
    _result("m6", "Roma", "Empoli", 1, 0)
    tracker.save_prediction("m5", "1X2", "Inter", 2.0, 0.5, 0.10)
    tracker.save_prediction("m5", "AH", "Home -0.5", 1.95, 0.55, 0.07)
    tracker.save_prediction("m6", "AH", "Home -1.0", 2.05, 0.5, 0.05)  # push
    tracker.settle_predictions()
    s = tracker.predictions_summary()
    assert set(s) == {"1X2", "AH"}
    assert s["1X2"]["n"] == 1 and s["1X2"]["roi"] == 100.0
    assert s["AH"]["n"] == 2
    assert s["AH"]["push"] == 1
    assert s["AH"]["won"] == 1
    # profitti: Home -0.5 vinta => +0.95 unita'; Home -1.0 push => 0
    # roi = pnl / n = 0.95 / 2 = 0.475 -> 47.5%
    assert s["AH"]["roi"] == round(0.95 / 2 * 100, 2)