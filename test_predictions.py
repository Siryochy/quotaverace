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
    # Giornata senza partite = "completata" (niente da aspettare): serve al
    # report EOD in bot.py che invia quando day_completed ritorna True.
    assert tracker.day_completed("2020-01-01") is True


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


# --- Colonna `league` sul ledger previsioni (22/09) ---
# La lega deve vivere SULLA RIGA del segnale: prima si ricavava solo dalla
# JOIN con `matches`, che non copre le righe senza partita (il 65% del ledger
# restava non attribuibile) — cosi' la strategia per lega non era misurabile.

def test_save_prediction_salva_la_lega(temp_db):
    tracker.save_prediction("lg1", "1X2", "Inter", 1.65, 0.62, 0.05,
                            market_prob=0.60, market_edge=0.02,
                            status="value", league="Serie A")
    row = tracker.get_predictions()[0]
    assert row["league"] == "Serie A"


def test_lega_sopravvive_a_una_chiamata_senza_lega(temp_db):
    """Rianalisi senza `league`: il valore registrato NON si cancella.

    Le rianalisi dello stesso match possono arrivare da percorsi diversi (uno
    passa la lega, uno no): perdere l'attribuzione a ogni giro renderebbe la
    strategia per lega di nuovo non misurabile.
    """
    tracker.save_prediction("lg2", "1X2", "Inter", 1.65, 0.62, 0.05,
                            status="value", league="Serie A")
    tracker.save_prediction("lg2", "1X2", "Inter", 1.70, 0.62, 0.06,
                            status="value")            # senza lega
    row = tracker.get_predictions()[0]
    assert row["league"] == "Serie A"
    assert row["quota"] == 1.70                        # il resto si aggiorna
    tracker.save_prediction("lg2", "1X2", "Inter", 1.71, 0.62, 0.06,
                            status="value", league="")  # lega vuota
    assert tracker.get_predictions()[0]["league"] == "Serie A"


def test_lega_assente_resta_none(temp_db):
    tracker.save_prediction("lg3", "1X2", "Inter", 1.65, 0.62, 0.05)
    assert tracker.get_predictions()[0]["league"] is None


def test_migrazione_league_su_db_vecchio(temp_db):
    """Un DB creato PRIMA del 22/09 prende la colonna senza perdere righe.

    La migrazione vive in `_get_conn` (all'avvio del bot): deve essere
    idempotente e non toccare i dati esistenti.
    """
    import sqlite3
    db = Path(temp_db)
    db.unlink()
    conn = sqlite3.connect(db)
    conn.execute('''CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT, mercato TEXT, esito TEXT,
        quota REAL, prob REAL, ev REAL,
        market_prob REAL, market_edge REAL,
        status TEXT, esito_finale TEXT, profit REAL,
        created_at TEXT, settled_at TEXT,
        UNIQUE(match_id, mercato, esito))''')
    conn.execute("INSERT INTO predictions (match_id, mercato, esito, quota, "
                 "prob, ev, status) VALUES ('old', '1X2', '1', 1.7, 0.6, 0.05, 'value')")
    conn.commit(); conn.close()

    tracker.init_db()                       # deve aggiungere la colonna
    tracker.init_db()                       # idempotente
    rows = tracker.get_predictions()
    assert len(rows) == 1                   # nessuna riga persa
    assert rows[0]["league"] is None        # valore mai inventato
    tracker.save_prediction("old", "1X2", "1", 1.71, 0.6, 0.05,
                            status="value", league="Bundesliga")
    assert tracker.get_predictions()[0]["league"] == "Bundesliga"
    cols = [r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(predictions)")]
    assert cols.count("league") == 1


def test_predictions_summary_filtra_per_status(temp_db):
    """`statuses` separa i giocabili dagli scartati (misura vs telemetria).

    Senza filtro si sommano due popolazioni diverse: il filtro e' cio' che
    rende la diagnosi una misura della strategia invece di un numero che
    mescola cio' che sarebbe stato giocato con cio' che i gate hanno tagliato.
    """
    _result("s1", "Inter", "Napoli", 2, 1)
    _result("s2", "Roma", "Empoli", 0, 2)
    _result("s3", "Lazio", "Torino", 1, 0)
    tracker.save_prediction("s1", "1X2", "Inter", 1.70, 0.62, 0.05,
                            status="value")
    tracker.save_prediction("s2", "1X2", "Roma", 1.75, 0.60, 0.04,
                            status="strong_value")
    # Scartato = la trasferta (Lazio vince 1-0): perde, come un gate che
    # taglia un segnale sbagliato.
    tracker.save_prediction("s3", "1X2", "Torino", 3.50, 0.30, -0.10,
                            status="rejected")
    tracker.settle_predictions()

    tutto = tracker.predictions_summary()
    assert tutto["1X2"]["n"] == 3
    giocabili = tracker.predictions_summary(statuses=("value", "strong_value",
                                                      "moderate"))
    assert giocabili["1X2"]["n"] == 2
    assert giocabili["1X2"]["won"] == 1 and giocabili["1X2"]["lost"] == 1
    scartati = tracker.predictions_summary(statuses=("rejected",))
    assert scartati["1X2"]["n"] == 1
    assert scartati["1X2"]["lost"] == 1
    # il filtro e' case-insensitive e accetta anche stati mai visti
    assert tracker.predictions_summary(statuses=("REJECTED",))["1X2"]["n"] == 1
    assert tracker.predictions_summary(statuses=("inesistente",)) == {}
    # una tupla vuota non equivale a "nessun filtro"
    assert tracker.predictions_summary(statuses=()) == {}


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


# --- Filtro d'ERA e di FASCIA QUOTA (25/09/2026) ----------------------------
# Una sola definizione nel ledger (`filter_predictions`), riusata dal report
# shadow multi-mercato e dalla diagnosi per mercato: il 25/09 il ROI Over/Under
# risultava +21.21% solo perche' 22 delle 30 righe giocabili erano di una
# pipeline ritirata (quota media ~2.25).

def _set_born(mid, stamp):
    """Riscrive la data di NASCITA di una riga (l'era e' quella, non il saldo)."""
    conn = tracker._get_conn()
    conn.execute("UPDATE predictions SET created_at=? WHERE match_id=?",
                 (stamp, mid))
    conn.commit()
    conn.close()


def test_filter_predictions_since_confine_inclusivo():
    rows = [{"created_at": "2026-09-18T23:59:59", "quota": 2.0},
            {"created_at": "2026-09-19T00:00:00", "quota": 1.5},
            {"created_at": "2026-09-19T23:59:59Z", "quota": 1.7},
            {"created_at": "2026-09-25T10:00:00+00:00", "quota": 1.6}]
    out = tracker.filter_predictions(rows, created_since="2026-09-19")
    assert [r["created_at"] for r in out] == ["2026-09-19T00:00:00",
                                             "2026-09-19T23:59:59Z",
                                             "2026-09-25T10:00:00+00:00"]


def test_filter_predictions_formati_di_data_reali():
    """'T', 'Z', offset, microsecondi e spazio: tutti confrontabili.

    E' la lezione del 17/09 (date del ledger ISO con 'T' contro date SQLite
    con lo spazio): la normalizzazione sta in un solo posto, in Python.
    """
    for stamp in ("2026-09-19T00:00:00", "2026-09-19 00:00:00",
                  "2026-09-19T00:00:00Z", "2026-09-19T00:00:00+02:00",
                  "2026-09-19T00:00:00.123456"):
        rows = [{"created_at": stamp, "quota": 1.5}]
        assert len(tracker.filter_predictions(
            rows, created_since="2026-09-19")) == 1, stamp
    assert tracker.filter_predictions(
        [{"created_at": "2026-09-18T23:59:59", "quota": 1.5}],
        created_since="2026-09-19") == []


def test_filter_predictions_fascia_quota_inclusiva():
    rows = [{"created_at": "2026-09-20T01:00:00", "quota": q}
            for q in (1.29, 1.30, 1.80, 1.81)]
    out = tracker.filter_predictions(rows, odds_min=1.30, odds_max=1.80)
    assert [r["quota"] for r in out] == [1.30, 1.80]


def test_filter_predictions_fail_closed_su_dato_mancante():
    """Con un filtro attivo, una riga NON dimostrabile viene esclusa.

    I due filtri sono indipendenti: manca la data -> fuori dal filtro d'era;
    manca la quota -> fuori dal filtro di fascia. Una riga con la data valida
    non deve sparire solo perche' le manca la quota.
    """
    senza_data = [{"created_at": "", "quota": 1.5},
                  {"created_at": None, "quota": 1.5},
                  {"quota": 1.5}]
    assert tracker.filter_predictions(senza_data,
                                      created_since="2026-09-19") == []

    senza_quota = [{"created_at": "2026-09-20T01:00:00"},
                   {"created_at": "2026-09-20T01:00:00",
                    "quota": "non-numerica"},
                   {"created_at": "2026-09-20T01:00:00", "quota": None}]
    assert tracker.filter_predictions(senza_quota, odds_min=1.30) == []
    assert tracker.filter_predictions(senza_quota, odds_max=1.80) == []
    # senza il filtro di fascia restano (la data di nascita e' valida)
    assert len(tracker.filter_predictions(
        senza_quota, created_since="2026-09-19")) == 3


def test_filter_predictions_righe_ostili_non_sollevano():
    assert tracker.filter_predictions([None, "x", 5],
                                      created_since="2026-09-19") == []
    assert tracker.filter_predictions(None, created_since="2026-09-19") == []


def test_filter_predictions_senza_filtro_non_tocca_nulla():
    rows = [{"created_at": "", "quota": None}, {"created_at": "x"}]
    assert tracker.filter_predictions(rows) == rows


def test_get_predictions_filtro_era_e_fascia(temp_db):
    tracker.save_prediction("vecchio", "OU", "Over 2.5", 2.40, 0.5, 0.10,
                            status="value")
    tracker.save_prediction("nuovo", "OU", "Over 2.5", 1.60, 0.6, 0.05,
                            status="value")
    _set_born("vecchio", "2026-09-05T10:00:00")
    _set_born("nuovo", "2026-09-20T10:00:00")

    assert len(tracker.get_predictions(mercato="OU")) == 2
    era = tracker.get_predictions(mercato="OU", created_since="2026-09-19")
    assert [r["match_id"] for r in era] == ["nuovo"]
    banda = tracker.get_predictions(mercato="OU", odds_max=1.80)
    assert [r["match_id"] for r in banda] == ["nuovo"]
    assert tracker.get_predictions(mercato="OU", created_since="2026-09-19",
                                   odds_max=1.80)[0]["quota"] == 1.60


def test_predictions_summary_filtro_era(temp_db):
    for mid, home, quota in (("v", "Vecchia", 2.40), ("n", "Nuova", 1.60)):
        _result(mid, home, f"{home}Away", 2, 0)
        tracker.save_prediction(mid, "1X2", home, quota, 0.6, 0.05,
                                status="value")
    tracker.settle_predictions()
    _set_born("v", "2026-09-05T10:00:00")
    _set_born("n", "2026-09-20T10:00:00")

    tutto = tracker.predictions_summary(statuses=("value",))
    assert tutto["1X2"]["n"] == 2
    era = tracker.predictions_summary(statuses=("value",),
                                      created_since="2026-09-19")
    assert era["1X2"]["n"] == 1 and era["1X2"]["won"] == 1
    banda = tracker.predictions_summary(statuses=("value",), odds_max=1.80)
    assert banda["1X2"]["n"] == 1
    # era e fascia insieme: il filtro e' additivo, non alternativo
    insieme = tracker.predictions_summary(statuses=("value",),
                                          created_since="2026-09-19",
                                          odds_min=1.30, odds_max=1.80)
    assert insieme["1X2"]["n"] == 1


def test_created_since_e_settled_since_restano_distinti(temp_db):
    """Nascita e saldo sono due domande diverse: nessuna delle due sovrascrive."""
    _result("m", "Alfa", "Beta", 2, 0)
    tracker.save_prediction("m", "1X2", "Alfa", 1.6, 0.6, 0.05, status="value")
    tracker.settle_predictions()
    _set_born("m", "2026-09-01T10:00:00")     # nata PRIMA dell'era...
    assert tracker.predictions_summary(statuses=("value",),
                                       created_since="2026-09-19") == {}
    # ...ma saldata DOPO: i due filtri non sono lo stesso filtro
    assert tracker.predictions_summary(statuses=("value",),
                                       settled_since="2026-09-19")["1X2"]["n"] == 1