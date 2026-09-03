"""Tripwire: Betfair RIMOSSO dall'architettura (04/09) + settlement the-odds-api.

Garanzie verificate:
1. I moduli Betfair NON esistono piu' nel repo (betfair_client, daily_scanner,
   daily_scan_job, surebet_pipeline): qualunque reintroduzione rompe il test.
2. Nessun import/stringa BETFAIR nei moduli attivi (bot, web_api, auto_bet,
   tracker, repair_scores, fixture_engine) — solo commenti/docstring storici.
3. La REFERTAZIONE (risultati + saldaggio bet/previsioni/cassa) usa
   ESCLUSIVAMENTE the-odds-api (odds_api.fetch_scores + match_scores_by_name):
   bot._update_results e repair_scores NON devono dipendere da
   settlement_apifootball (verificato 04/09: il piano free API-Football copre
   solo le stagioni 2022-2024, quindi la refertazione corrente resta su
   the-odds-api; API-Football serve solo allo storico ratings in football_hist).
4. /api/scan risponde esplicitamente "betfair_removed" (503).
"""

from pathlib import Path

import bot
import web_api

# Moduli Betfair rimossi il 04/09: devono NON esistere.
REMOVED_MODULES = ("betfair_client.py", "daily_scanner.py",
                   "daily_scan_job.py", "surebet_pipeline.py")

# Moduli attivi che NON devono importare/nominare Betfair nel codice
# (i commenti storici in docstring sono ammessi: si cerca solo il codice).
ACTIVE_MODULES = ("bot.py", "web_api.py", "auto_bet.py", "tracker.py",
                  "repair_scores.py", "fixture_engine.py", "run_all.py")

# Il settlement NON deve passare da API-Football (settlement_apifootball):
# il piano free copre solo 2022-2024 e bloccherebbe il saldo delle partite
# correnti. Resta esclusivamente the-odds-api.
BANNED_SETTLEMENT_REFS = ("settlement_apifootball", "settle_results_from_apifootball",
                          "fetch_true_scores")

ROOT = Path(__file__).resolve().parent


def test_moduli_betfair_non_esistono():
    for mod in REMOVED_MODULES:
        assert not (ROOT / mod).exists(), \
            f"{mod} non deve esistere: Betfair rimosso dall'architettura (04/09)"


def test_nessun_referimento_betfair_nei_moduli_attivi():
    for mod in ACTIVE_MODULES:
        path = ROOT / mod
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Import/assegnazioni reali: 'betfair' come identificatore di codice
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            lower = line.lower()
            if "betfair" in lower and any(k in lower for k in (
                    "import ", "from ", "get_client", "enabled(", "run_daily_scan",
                    "run_surebet_alert", "load_latest_scan", "scan_day")):
                raise AssertionError(
                    f"{mod}:{i}: riferimento Betfair nel codice: {line.strip()}")


def test_update_results_usa_fetch_scores_the_odds_api():
    """La refertazione passa SOLO da odds_api.fetch_scores (the-odds-api):
    nessuna dipendenza da settlement_apifootball in _update_results."""
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    body = src.split("def _update_results")[1].split("def _admin_chat_ids")[0]
    assert "from odds_api import SPORTS_MAP, fetch_scores" in body
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
    assert "settlement_apifootball" not in src and "settle_results_from_apifootball" not in src
    # la fetch_true_scores locale deve chiamare fetch_scores (the-odds-api)
    assert "fetch_scores(sport" in src


def test_scan_endpoint_risponde_rimosso():
    code, payload = web_api._scan_json({})
    assert code == 503
    assert payload["error"] == "betfair_removed"
