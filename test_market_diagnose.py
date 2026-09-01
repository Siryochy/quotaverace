"""Test diagnosi calibrazione per mercato (market_diagnose.py).

Copre: soglie di campione (totale e per mercato), flag ROI < EV,
overconfidence (hit rate vs prob media), ordinamento mercati critici e
integrazione su DB reale tramite analyze_db().
"""
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

import tracker
import market_diagnose


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _entry(n, roi, avg_ev, hit_rate=50.0, avg_prob=0.50, push=0, edge=0.02):
    """Costruisce una voce di predictions_summary per un mercato."""
    won = int(round((n - push) * hit_rate / 100.0))
    lost = (n - push) - won
    return {
        "n": n, "won": won, "lost": lost, "push": push,
        "hit_rate": hit_rate, "roi": roi, "avg_ev": avg_ev,
        "gap": round(roi - avg_ev, 2), "avg_prob": avg_prob,
        "avg_market_edge": edge,
    }


# --- Soglie e flag ---

def test_vuoto_campione_insufficiente_nessuna_azione():
    res = market_diagnose.diagnose({})
    assert res["sufficiente"] is False
    assert res["critici"] == [] and res["azioni"] == []
    assert res["totals"]["n"] == 0


def test_campione_totale_insufficiente_blocca_le_azioni():
    """Mercato sottozero ma totale < 100: si vede, ma niente raccomandazioni."""
    by = {"1X2": _entry(40, -6.0, 1.5, hit_rate=40.0, avg_prob=0.55)}
    res = market_diagnose.diagnose(by)
    assert res["sufficiente"] is False
    # il mercato e' comunque segnalato come critico nella tabella...
    assert [m["mercato"] for m in res["critici"]] == ["1X2"]
    # ...ma le azioni restano per quando il campione matura
    assert res["azioni"] == []


def test_mercato_critico_con_campione_sufficiente():
    by = {
        "OU": _entry(60, 2.0, 1.5, hit_rate=55.0, avg_prob=0.52),
        "1X2": _entry(60, -6.0, 1.5, hit_rate=40.0, avg_prob=0.55),
    }
    res = market_diagnose.diagnose(by)  # totale 120 >= 100
    assert res["sufficiente"] is True
    assert [a["mercato"] for a in res["azioni"]] == ["1X2"]
    # critici ordinati per ROI crescente
    assert res["critici"][0]["mercato"] == "1X2"


def test_mercato_sano_non_flagato():
    by = {"1X2": _entry(120, 3.0, 2.0, hit_rate=56.0, avg_prob=0.54)}
    res = market_diagnose.diagnose(by)
    assert res["sufficiente"] is True
    assert res["critici"] == [] and res["azioni"] == []


def test_gap_dentro_tolleranza_non_flagato():
    """ROI negativo ma entro 3pp dall'EV: ancora rumore, nessun intervento."""
    by = {"1X2": _entry(120, -1.0, 1.0, avg_prob=0.50)}  # gap -2.0
    res = market_diagnose.diagnose(by)
    assert res["critici"] == []


def test_campione_per_mercato_minimo_rispetta_soglia():
    by = {
        "OU": _entry(120, 1.0, 1.0, avg_prob=0.50),
        "AH": _entry(5, -20.0, 2.0, hit_rate=10.0, avg_prob=0.55),  # n troppo piccolo
    }
    res = market_diagnose.diagnose(by)
    assert [m["mercato"] for m in res["critici"]] == []


def test_overconfidence_e_un_segnale_anche_se_roi_positivo():
    by = {"1X2": _entry(120, 1.0, 1.0, hit_rate=42.0, avg_prob=0.60)}
    res = market_diagnose.diagnose(by)
    assert res["critici"] == []  # roi positivo -> nessuna azione
    m = res["markets"][0]
    assert any("overconfidence" in s for s in m["segnali"])


def test_azioni_suggeriscono_blend_e_devig_con_edge_basso():
    by = {"1X2": _entry(120, -8.0, 1.0, hit_rate=40.0, avg_prob=0.55, edge=0.01)}
    a = market_diagnose.diagnose(by)["azioni"][0]
    any_blend = any("blend" in x for x in a["azioni_da_fare"])
    any_devig = any("devig" in x for x in a["azioni_da_fare"])
    assert any_blend and any_devig


# --- Integrazione su DB reale ---

def test_analyze_db_su_db_reale(temp_db):
    """12 previsioni 1X2 chiuse (10 perse): analyze_db segnala il mercato."""
    for i in range(12):
        mid = f"m{i}"
        home, away = f"Home{i}", f"Away{i}"
        won = i < 2
        sh, sa = (2, 0) if won else (0, 2)
        tracker.save_result(mid, "Serie A", home, away, sh, sa,
                            datetime.now().isoformat())
        tracker.save_prediction(mid, "1X2", home, 2.00, 0.55, 0.05)
    assert tracker.settle_predictions() == (12, 0)

    res = market_diagnose.analyze_db(min_total=10, min_per_market=5)
    assert res["sufficiente"] is True
    assert [a["mercato"] for a in res["azioni"]] == ["1X2"]
    r = res["markets"][0]
    assert r["n"] == 12 and r["won"] == 2 and r["lost"] == 10
    # pnl = 2 vinte (2*+1.0) - 10 perse (10*-1.0) = -8 su 12
    assert r["roi"] == round(-8.0 / 12 * 100, 2)
    assert r["roi"] < 0 and r["gap"] <= -3.0