"""
Raccolta risultati storici da API-Football (api-sports.io, v3).

Popola la tabella match_results con le partite reali gia' giocate: alimenta i
rating dinamici (rating_engine) e il backtest dei segnali. Uso previsto:
    python football_hist.py sync --seasons 2
Il free plan di API-Football concede 100 richieste/giorno su tutti gli endpoint.
"""

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from config import load_dotenv
from leagues_data import ALL_LEAGUES
from rating_engine import compute_ratings

# garantisce che .env sia caricato anche quando questo modulo e' eseguito
# direttamente come CLI (python football_hist.py sync)
load_dotenv()

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


MAX_RETRIES = 3        # tentativi per errore transitorio (rate-limit/rete)
RETRY_BACKOFF = 3      # secondi tra i tentativi (x2 a ogni retry)


def _api_get(path: str, params: Dict) -> Optional[dict]:
    """GET con header x-apisports-key. Ritorna il body json (o None).

    Gli errori transitori (429 rate-limit, 503, eccezioni di rete) vengono
    ritentati con backoff esponenziale, cosi' una corsa lunga /sync non perde
    intere leghe per un blocco temporaneo del free plan.
    """
    key = _env("API_FOOTBALL_KEY")
    if not key:
        logger.warning("API_FOOTBALL_KEY mancante")
        return None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(f"{BASE_URL}/{path}", headers={"x-apisports-key": key},
                             params=params, timeout=30)
            if r.status_code in (401, 403):
                logger.warning(f"API-Football bloccata (codice {r.status_code}), key non valida o non attiva")
                return None
            if r.status_code in (429, 503):
                wait = RETRY_BACKOFF * (2 ** attempt)
                logger.info(f"Rate-limit API-Football ({r.status_code}), retry {attempt+1}/{MAX_RETRIES} tra {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"Errore rete API-Football {path}: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
            return None
        except Exception as e:
            logger.warning(f"Errore API-Football {path}: {e}")
            return None
    logger.warning(f"API-Football {path}: rate-limit persistente dopo {MAX_RETRIES} tentativi")
    return None


def _match_db_name(api_name: str, league: str) -> Optional[str]:
    """Allinea il nome squadra API-Football ai nomi nel database locale.

    Prima cerca nella lega indicata; se non trova (es. squadra promossa o
    retrocessa che nel DB vive in un'altra lega), fa un fallback globale su
    tutte le leghe con priorita' al match esatto.
    """
    a = api_name.strip()
    al = a.lower()
    if not al:
        return None

    def _search(teams):
        # prima il match esatto, poi il contenimento bilaterale
        for team in teams:
            if team.lower() == al:
                return team
        for team in teams:
            tl = team.lower()
            if tl in al or al in tl:
                return team
        return None

    found = _search(ALL_LEAGUES.get(league, {}))
    if found:
        return found
    # fallback globale: esatto su tutte le leghe, poi contenimento
    for lname, teams in ALL_LEAGUES.items():
        for team in teams:
            if team.lower() == al:
                return team
    for lname, teams in ALL_LEAGUES.items():
        for team in teams:
            tl = team.lower()
            if tl in al or al in tl:
                return team
    return None


def fetch_fixtures(league_id: int, season: int) -> List[dict]:
    """Recupera le fixtures di una lega/season. Ritorna la lista fixture.

    Nota: su /fixtures il parametro 'page' NON esiste (l'API risponde
    'The Page field do not exist.'); l'endpoint restituisce tutte le partite
    della stagione in una sola risposta.
    """
    body = _api_get("fixtures", {"league": league_id, "season": season})
    if not body or body.get("results", 0) == 0:
        return []
    return body.get("response", [])


def _save_results_batch(rows: List[Tuple], conn=None) -> None:
    """Inserisce in blocco nella tabella match_results (una transazione)."""
    if not rows:
        return
    own_conn = conn is None
    if own_conn:
        from tracker import DB_PATH
        conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS match_results (
        match_id TEXT PRIMARY KEY, league TEXT, home_team TEXT, away_team TEXT,
        score_home INTEGER, score_away INTEGER, result TEXT, settled_at TEXT)''')
    # risultato 1/X/2 per risolvere il vincitore in /risultati
    def _res(sh, sa):
        return "1" if sh > sa else ("2" if sh < sa else "X")
    c.executemany(
        '''INSERT OR REPLACE INTO match_results VALUES (?,?,?,?,?,?,?,?)''',
        [(mid, lg, h, a, sh, sa, _res(sh, sa), ts) for mid, lg, h, a, sh, sa, ts in rows])
    conn.commit()
    if own_conn:
        conn.close()


def _parse_fixture(fx: dict, league: str) -> Optional[Tuple]:
    """
    Estrae (match_id, home, away, sh, sa, date) da una fixture finita.
    Ritorna None se non e' una vittoria regolare con punteggio valido.
    """
    fixture = fx.get("fixture") or {}
    match_id = fixture.get("id")
    if match_id is None:
        return None
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
    date = fixture.get("date") or ""
    return match_id, home, away, sh, sa, date


def _season_status(fx_body: Optional[dict]) -> str:
    """Classifica l'esito della chiamata fixtures per una stagione.

    Ritorna uno di:
      "ok"        -> stagione disponibile e con dati
      "skip"      -> stagione non esposta dal free plan (errore 'plan')
      "retry"     -> errore transitorio (rate-limit / rete): da ritentare
    """
    if fx_body is None:
        return "retry"
    errs = fx_body.get("errors")
    if not errs:
        return "ok"
    joined = " ".join(str(v).lower() for v in errs.values() if v)
    if "plan" in joined or "free" in joined:
        return "skip"
    return "retry"


def sync_history(seasons: int = DEFAULT_SEASONS, leagues: Optional[List[str]] = None) -> Dict:
    """
    Scarica i risultati storici e li salva in match_results (INSERT OR REPLACE),
    poi ricalcola i rating.

    Il free plan di API-Football espone solo le stagioni dal 2022 al 2024; le
    stagioni piu' recenti vengono saltate (l'API risponde con un errore 'plan')
    e si scende finche' non si raccolgono 'seasons' stagioni accessibili.
    Ritorna un riepilogo per lega.
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
        collected = 0
        year = current_year
        rows = []
        while collected < seasons and year >= 2018:
            body = _api_get("fixtures", {"league": lid, "season": year})
            status = _season_status(body)
            if status == "retry":
                # errore transitorio (rate-limit/rete): riprova la stessa stagione
                logger.info(f"Retry stesso anno per {league} {year} (status retry)")
                continue
            if status == "skip":
                logger.info(f"Stagione {year} non accessibile ({league}), salto")
                year -= 1
                continue
            fixtures = (body or {}).get("response", []) or []
            for fx in fixtures:
                status_short = (((fx.get("fixture") or {}).get("status") or {}).get("short")) or ""
                if status_short not in FINISHED_STATUSES:
                    continue
                parsed = _parse_fixture(fx, league)
                if not parsed:
                    continue
                mid, home, away, sh, sa, date = parsed
                rows.append((f"{league}-{mid}", league, home, away, sh, sa, date))
                done += 1
            collected += 1
            year -= 1
            time.sleep(1)  # gentilezza verso il limite di rate
        _save_results_batch(rows)
        summary[league] = done
        total += done
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