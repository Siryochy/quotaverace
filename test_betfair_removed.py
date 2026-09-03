"""Tripwire: Betfair RIMOSSO dall'architettura (04/09) + settlement API-Football.

Garanzie verificate:
1. I moduli Betfair NON esistono piu' nel repo (betfair_client, daily_scanner,
   daily_scan_job, surebet_pipeline): qualunque reintroduzione rompe il test.
2. Nessun import/stringa BETFAIR nei moduli attivi (bot, web_api, auto_bet,
   tracker, repair_scores, fixture_engine) — solo commenti/docstring storici.
3. La REFERTAZIONE (risultati + saldaggio bet/previsioni/cassa) usa
   ESCLUSIVAMENTE API-Football: bot._update_results chiama
   settlement_apifootball.settle_results_from_apifootball e NON importa
   piu' odds_api.fetch_scores (the-odds-api resta solo per quote/CLV).
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


def test_update_results_usa_settlement_apifootball():
    """La refertazione passa SOLO da settlement_apifootball: il vecchio
    confine (odds_api.fetch_scores + match_scores_by_name) non deve piu'
    comparire in _update_results."""
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert "settle_results_from_apifootball" in src
    # il flusso _update_results non deve piu' importare fetch_scores
    assert "from odds_api import SPORTS_MAP, fetch_scores" not in src


def test_scan_endpoint_risponde_rimosso():
    code, payload = web_api._scan_json({})
    assert code == 503
    assert payload["error"] == "betfair_removed"