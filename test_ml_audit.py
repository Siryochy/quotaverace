"""Test audit qualita' dataset ML (ml_audit.py)."""
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

import tracker
import ml_dataset
import ml_audit


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _good_row(n=1, **overrides):
    row = {
        "match_id": f"m{n}", "league": "Serie A", "mercato": "1X2",
        "esito": "1", "lam_h": 1.7, "lam_a": 1.1,
        "prob_1": 0.53, "prob_X": 0.27, "prob_2": 0.20, "prob_over": 0.58,
        "quota": 2.10, "prob": 0.55, "ev": 0.15,
        "market_prob": 0.45, "market_edge": 0.10,
        "status": "value", "esito_finale": "won", "profit": 1.10,
        "label_ml": 1, "settled_at": "2026-09-01T20:00:00",
    }
    row.update(overrides)
    return row


def test_dataset_pulito_nessun_problema():
    rows = [_good_row(1), _good_row(2, match_id="m2", esito="2",
                                     esito_finale="lost", profit=-1.0, label_ml=0)]
    assert ml_audit.audit_training_rows(rows) == []


def test_label_incoerente_con_esito_finale():
    rows = [_good_row(1, esito_finale="won", label_ml=0)]  # won -> label 1
    problems = ml_audit.audit_training_rows(rows)
    assert any(p["tipo"] == "label_incoerente" for p in problems)


def test_quota_invalida():
    rows = [_good_row(1, quota=1.0)]
    problems = ml_audit.audit_training_rows(rows)
    assert any(p["tipo"] == "quota_invalida" for p in problems)


def test_quota_mancante():
    rows = [_good_row(1, quota=None)]
    problems = ml_audit.audit_training_rows(rows)
    assert any(p["tipo"] == "quota_mancante" for p in problems)


def test_esito_finale_invalido():
    rows = [_good_row(1, esito_finale="pending", label_ml=0)]
    problems = ml_audit.audit_training_rows(rows)
    assert any(p["tipo"] == "esito_finale_invalido" for p in problems)


def test_esito_non_canonico_per_mercato():
    # mercato OU ma esito "1": non e' un esito Over/Under
    rows = [_good_row(1, mercato="OU", esito="1")]
    problems = ml_audit.audit_training_rows(rows)
    assert any(p["tipo"] == "esito_non_canonico" for p in problems)


def test_esito_nome_squadra_1x2_ok():
    # best_esito della schedina puo' essere un nome squadra: NON e' un errore
    rows = [_good_row(1, mercato="1X2", esito="Osasuna")]
    assert ml_audit.audit_training_rows(rows) == []


def test_ah_esito_prefisso_casa_ok():
    rows = [_good_row(1, mercato="AH", esito="Home -1.0", quota=2.0)]
    assert ml_audit.audit_training_rows(rows) == []


def test_profit_incoerente_col_segno():
    # won con profit negativo
    rows = [_good_row(1, esito_finale="won", profit=-1.0, label_ml=1)]
    problems = ml_audit.audit_training_rows(rows)
    assert any(p["tipo"] == "profit_incoerente" for p in problems)


def test_duplicati_rilevati():
    rows = [_good_row(1), _good_row(2, match_id="m1")]  # stesso (match, mercato, esito)
    problems = ml_audit.audit_training_rows(rows)
    dups = [p for p in problems if p["tipo"] == "duplicato"]
    assert len(dups) == 1


def test_prob_fuori_range():
    rows = [_good_row(1, prob=1.5)]
    problems = ml_audit.audit_training_rows(rows)
    assert any(p["tipo"] == "prob_fuori_range" for p in problems)


def test_audit_su_dataset_reale_db(temp_db):
    """Audit end-to-end: dal DB popolato, il dataset deve risultare pulito."""
    tracker.save_match("ml1", "Serie A", "Inter", "Napoli", "2026-09-01T13:00:00Z")
    tracker.save_analysis("ml1", 1.74, 1.01, 0.53, 0.27, 0.20, 0.52, 0.15, "1",
                          2.10, "Pinnacle", "value", market_prob=0.45,
                          market_edge=0.10)
    tracker.save_result("ml1", "Serie A", "Inter", "Napoli", 2, 1,
                        datetime.now().isoformat())
    tracker.save_prediction("ml1", "1X2", "1", 2.10, 0.55, 0.15,
                            market_prob=0.45, market_edge=0.10, status="value")
    tracker.settle_predictions()

    rows = ml_dataset.build_training_rows(source="predictions")
    assert rows  # il seed produce almeno una riga
    assert ml_audit.audit_training_rows(rows) == []


def test_cli_ritorna_0_su_dataset_vuoto(monkeypatch):
    monkeypatch.setattr(ml_audit, "build_training_rows", lambda **kw: [])
    assert ml_audit.main(["--source", "predictions"]) == 0
