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
    assert res["excluded"]["n"] == 0      # tutte le righe sono giocabili


# --- Split giocabili / scartati (22/09) -------------------------------------
# La direttiva: la diagnosi, i gap e le conseguenti raccomandazioni si
# calcolano ESCLUSIVAMENTE sui segnali giocabili. Gli scartati sono il costo
# (o il risparmio) dei gate, non la performance di una strategia: sommarli
# faceva sembrare il campione piu' maturo di quanto sia e poteva far
# raccomandare cambi di blend/soglie per colpa di righe mai giocate.


def test_diagnose_ignora_gli_scartati_nei_giudizi():
    by = {"1X2": _entry(120, 3.0, 2.0, hit_rate=56.0, avg_prob=0.54)}
    skipped = {"1X2": {"n": 900, "won": 100, "lost": 800, "push": 0,
                        "roi": -88.0}}
    res = market_diagnose.diagnose(by, skipped)
    # il campione giocabile e' sano: nessuna azione, nonostante 900 scartati
    assert res["critici"] == [] and res["azioni"] == []
    assert res["totals"]["n"] == 120
    assert res["excluded"]["n"] == 900
    assert res["excluded"]["roi"] == -88.0
    assert res["excluded"]["by_market"][0]["mercato"] == "1X2"


def test_sufficiente_non_contagia_il_campione_coi_rejected():
    """90 giocabili + 500 scartati NON fanno un campione maturo.

    Prima dello split il totale aggregato faceva scattare le raccomandazioni:
    e' esattamente il falso segnale che la direttiva vuole chiudere.
    """
    by = {"1X2": _entry(90, -8.0, 1.0, hit_rate=40.0, avg_prob=0.55)}
    skipped = {"1X2": {"n": 500, "won": 50, "lost": 450, "push": 0,
                        "roi": -80.0}}
    res = market_diagnose.diagnose(by, skipped)
    assert res["totals"]["n"] == 90
    assert res["sufficiente"] is False
    assert res["azioni"] == []
    assert "escluse" in res["note"]


def test_excluded_vuoto_non_aggiunge_rumore():
    by = {"1X2": _entry(120, 3.0, 2.0)}
    res = market_diagnose.diagnose(by, {})
    assert res["excluded"]["n"] == 0
    assert res["excluded"]["by_market"] == []
    assert "escluse" not in res["note"]


def test_excluded_tollera_voci_malformate():
    by = {"1X2": _entry(120, 3.0, 2.0)}
    res = market_diagnose.diagnose(by, {"X": "spazzatura", "Y": {"n": 0}})
    assert res["excluded"]["n"] == 0      # nessuna eccezione, niente righe finte


def test_report_dichiara_gli_esclusi_e_i_calcoli():
    by = {"1X2": _entry(120, -6.0, 1.5, hit_rate=40.0, avg_prob=0.55)}
    skipped = {"1X2": {"n": 300, "won": 30, "lost": 270, "push": 0,
                        "roi": -90.0}}
    out = market_diagnose._report(market_diagnose.diagnose(by, skipped))
    assert "SOLO segnali giocabili" in out
    assert "Fuori dai calcoli: 300" in out
    assert "NON performance" in out


def test_report_in_modalita_confronto_non_si_chiama_giocabile():
    """Con `--all-statuses` il campione e' mescolato: etichettarlo
    "giocabile" sarebbe una bugia letta dall'operatore."""
    by = {"1X2": _entry(120, -6.0, 1.5, hit_rate=40.0, avg_prob=0.55)}
    out = market_diagnose._report(market_diagnose.diagnose(by),
                                  all_statuses=True)
    assert "TUTTO il ledger" in out and "CONFRONTO" in out
    assert "Campione MESCOLATO" in out
    assert "SOLO segnali giocabili" not in out


def test_analyze_db_esclude_gli_scartati(temp_db):
    """Integrazione: il DB reale ha entrambe le popolazioni."""
    for i in range(12):                       # giocabili: 2 vinte, 10 perse
        mid = f"p{i}"
        home = f"Play{i}"                     # nomi distinti: il referto aggancia
        sh, sa = (2, 0) if i < 2 else (0, 2)  # anche per coppia di squadre
        tracker.save_result(mid, "Serie A", home, f"PAway{i}", sh, sa,
                            datetime.now().isoformat())
        tracker.save_prediction(mid, "1X2", home, 1.7, 0.6, 0.05,
                                status="value")
    for i in range(40):                       # scartati: tutti persi
        mid = f"r{i}"
        home = f"Rej{i}"
        tracker.save_result(mid, "Serie A", home, f"RAway{i}", 0, 2,
                            datetime.now().isoformat())
        tracker.save_prediction(mid, "1X2", home, 3.5, 0.3, -0.1,
                                status="rejected")
    tracker.settle_predictions()

    res = market_diagnose.analyze_db(min_total=10, min_per_market=5)
    assert res["markets"][0]["n"] == 12          # solo i giocabili
    assert res["markets"][0]["lost"] == 10
    assert res["excluded"]["n"] == 40
    assert res["excluded"]["roi"] == -100.0      # i 40 rejected sono tutti persi
    assert res["excluded"]["by_market"][0]["mercato"] == "1X2"

    old = market_diagnose.analyze_db(all_statuses=True, min_total=10,
                                     min_per_market=5)
    assert old["markets"][0]["n"] == 52           # comportamento pre-22/09


def test_usa_la_definizione_condivisa_dei_tier():
    """Tripwire: nessuna copia della tripla dentro market_diagnose.

    Un tier nuovo deve contare come giocabile in un posto solo: se la tripla
    fosse ricopiata qui, il report e la corsia ordini misurerebbero due
    insiemi diversi senza che nessun test lo dica.
    """
    from pathlib import Path
    import value_filter
    assert value_filter.PLAYABLE_TIERS == ("value", "strong_value", "moderate")
    src = Path(market_diagnose.__file__).read_text(encoding="utf-8")
    assert "PLAYABLE_TIERS" in src
    assert '"value", "strong_value"' not in src