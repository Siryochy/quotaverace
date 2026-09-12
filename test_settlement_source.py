"""Guardia sul settlement: la refertazione (risultati + saldaggio) usa
ESCLUSIVAMENTE the-odds-api (odds_api.fetch_scores + match_scores_by_name).

Vincolo architetturale indipendente da Betfair (verificato 04/09): il piano
free di API-Football copre solo le stagioni 2022-2024, quindi NON può saldare
le partite correnti del 2026. Il settlement DEVE restare su the-odds-api
(la stessa chiave delle quote restituisce i risultati FINITI della stagione
corrente). Il tripwire Betfair è stato rimosso il 06/09 (esecuzione via
aggregatore), ma questo guard rimane: non spostare il settlement su
settlement_apifootball / fetch_true_scores da API-Football.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent

BANNED_SETTLEMENT_REFS = ("settlement_apifootball", "settle_results_from_apifootball")


def test_update_results_usa_fetch_scores_the_odds_api():
    """La refertazione passa SOLO da odds_api.fetch_scores (the-odds-api):
    nessuna dipendenza da settlement_apifootball in _update_results."""
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    body = src.split("def _update_results")[1].split("def _admin_chat_ids")[0]
    # L'import puo' essere su piu' righe (dal 12/09 include SCORES_DAYS_FROM):
    # conta che fetch_scores venga importato PROPRIO da odds_api.
    assert "from odds_api import" in body
    assert "fetch_scores" in body
    assert "match_scores_by_name" in body
    for ref in BANNED_SETTLEMENT_REFS:
        assert ref not in body, \
            f"_update_results non deve usare {ref} (settlement = the-odds-api)"


def test_repair_scores_usa_fetch_scores_the_odds_api():
    """repair_scores riscarica i punteggi veri da the-odds-api, mai da
    settlement_apifootball. NB: repair_scores definisce UNA PROPRIA funzione
    fetch_true_scores (che usa odds_api.fetch_scores): il bando riguarda il
    modulo settlement_apifootball, non il nome della funzione locale."""
    src = (ROOT / "repair_scores.py").read_text(encoding="utf-8")
    assert "from odds_api import SPORTS_MAP, fetch_scores" in src
    assert "settlement_apifootball" not in src \
        and "settle_results_from_apifootball" not in src
    # la fetch_true_scores locale deve chiamare fetch_scores (the-odds-api)
    assert "fetch_scores(sport" in src