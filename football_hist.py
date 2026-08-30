"""
Raccolta risultati storici da API-Football (api-sports.io, v3).

Popola la tabella match_results con le partite reali gia' giocate: alimenta i
rating dinamici (rating_engine) e il backtest dei segnali. Uso previsto:
    python football_hist.py sync --seasons 2
Il free plan di API-Football concede 100 richieste/giorno su tutti gli endpoint.
"""

import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from leagues_data import ALL_LEAGUES
from tracker import save_result
from rating_engine import compute_ratings

logger = logging.getLogger(__name__)

BASE_URL = "https://v3.football.api-sports.io"

# mappa lega locale -> league id API-Football (v3)
LEAGUE_IDS: Dict[str, int] = {
    "Serie A": 135,
    "Premier League": 39,
    "La Liga": 140,
    "Bundesliga": 78,
    "Ligue 1": 61,
    "Eredivisie": 88,
    "MLS": 253,
    "Brasileirao": 71,
}

# status API-Football trattati come "partita finita"
FINISHED_STATUSES = {"FT", "AET", "PEN", "FF"}

# quanti anni di stagioni sono recuperabili col free plan (indicativo)
DEFAULT_SEASONS = 2


def _env(name: str) -> str:
    exact = os.getenv(name)
    if exact is not None:
        return exact.strip()
    for k, v in os.environ.items():
        if k.strip() == name:
            return v.strip()
    return ""


def _api_get(path: str, params: Dict) -> Optional[dict]:
    """GET con header x-apisports-key. Ritorna il body json (o None)."""
    key = _env("API_FOOTBALL_KEY")
    if not key:
        logger.warning("API_FOOTBALL_KEY mancante")
        return None
    try:
        r = requests.get(f"{BASE_URL}/{path}", headers={"x-apisports-key": key},
                         params=params, timeout=30)
        if r.status_code in (401, 403):
            logger.warning(f"API-Football bloccata (codice {r.status_code}), key non valida o non attiva")
            return None
        if r.status_code in (429, 503):
            # rate limit: attende e riprova una volta
            time.sleep(2)
            r = requests.get(f"{BASE_URL}/{path}", headers={"x-apisports-key": key},
                             params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Errore API-Football {path}: {e}")
        return None


def _match_db_name(api_name: str, league: str) -> Optional[str]:
    """Allinea il nome squadra API-Football ai nomi nel database locale."""
    league_teams = ALL_LEAGUES.get(league, {})
    a = api_name.strip()
    al = a.lower()
    for team in league_teams:
        tl = team.lower()
        # match esatto, o contenimento in un senso o nell'altro (preferisce equo)
        if tl == al or tl in al or al in tl:
            return team
    return None


def fetch_fixtures(league_id: int, season: int, page: int = 1) -> List[dict]:
    """Recupera le fixtures di una lega/season. Ritorna la lista fixture."""
    body = _api_get("fixtures", {"league": league_id, "season": season, "page": page})
    if not body or body.get("results", 0) == 0:
        return []
    return body.get("response", [])


def _parse_fixture(fx: dict, league: str) -> Optional[Tuple]:
    """
    Estrae (home, away, sh, sa, date) da una fixture finita.
    Ritorna None se non e' una vittoria regolare con punteggio valido.
    """
    goals = fx.get("goals") or {}
    sh, sa = goals.get("home"), goals.get("away")
    if sh is None or sa is None:
        return None
    try:
        sh = int(sh); sa = int(sa)
    except (TypeError, ValueError):
        return None
    home_api = ((fx.get("teams") or {}).get("home") or {}).get("name", "")
    away_api = ((fx.get("teams") or {}).get("away") or {}).get("name", "")
    home = _match_db_name(home_api, league)
    away = _match_db_name(away_api, league)
    if not home or not away or home == away:
        return None
    date = (fx.get("fixture") or {}).get("date") or ""
    return home, away, sh, sa, date


def sync_history(seasons: int = DEFAULT_SEASONS, leagues: Optional[List[str]] = None) -> Dict:
    """
    Scarica i risultati storici e li salva in match_results (INSERT OR REPLACE),
    poi ricalcola i rating. Ritorna un riepilogo per lega.
    """
    if not _env("API_FOOTBALL_KEY"):
        return {"error": "API_FOOTBALL_KEY mancante"}
    target = leagues or list(LEAGUE_IDS.keys())
    current_year = time.localtime().tm_year
    summary = {}
    total = 0
    for league in target:
        lid = LEAGUE_IDS.get(league)
        if not lid:
            continue
        done = 0
        for season in range(current_year, current_year - seasons, -1):
            # itera le pagine (free plan: 100 req/giorno -> max ~3-4 leghe x 2 season)
            for page in (1, 2, 3):
                fixtures = fetch_fixtures(lid, season, page)
                if not fixtures:
                    break
                for fx in fixtures:
                    status_short = (((fx.get("fixture") or {}).get("status") or {}).get("short")) or ""
                    if status_short not in FINISHED_STATUSES:
                        continue
                    parsed = _parse_fixture(fx, league)
                    if not parsed:
                        continue
                    home, away, sh, sa, date = parsed
                    save_result(f"{league}-{season}-{fx.get('id')}",
                                league, home, away, sh, sa, date)
                    done += 1
                # se la pagina era piena, prova quella dopo; altrimenti chiudi
                if len(fixtures) < 20:
                    break
        summary[league] = done
        total += done
        time.sleep(1)  # gentilezza verso il limite di rate
    if total:
        try:
            compute_ratings()
        except Exception as e:
            logger.warning(f"Errore ricalcolo rating: {e}")
    return {**summary, "_total": total}


def run_sync(seasons: int = DEFAULT_SEASONS, leagues: Optional[List[str]] = None) -> str:
    """Endpoint CLI/Telegram: esegue la sincronizzazione e produce un riepilogo."""
    if not _env("API_FOOTBALL_KEY"):
        return "❌ *API_FOOTBALL_KEY mancante.* Imposta la variabile per sincronizzare i risultati storici.\n\n`/sync` popola il database con le partite giocate: alimenta i rating dinamici e il backtest."
    result = sync_history(seasons, leagues)
    if isinstance(result, dict) and result.get("error"):
        return f"❌ *Errore:* {result['error']}"
    lines = []
    for k, v in result.items():
        if k == "_total":
            continue
        lines.append(f"• *{k}*: {v} partite salvate")
    total = result.get("_total", 0)
    return (
        "🔄 *SINCRONIZZAZIONE RISULTATI STORICI*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines)
        + f"\n\n✅ Totale: {total} partite | Rating ricalcolati.\n"
        + "Usa `/backtest` e `/risultati` per vedere i dati."
    )


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, default=DEFAULT_SEASONS)
    ap.add_argument("--league", nargs="*", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    print(run_sync(args.seasons, args.league))