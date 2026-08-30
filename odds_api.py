import json, os, time, logging, requests
from pathlib import Path
logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / "data"
ODDS_TTL = 86400          # cache 24h = 1 chiamata/giorno per lega
MIN_REMAINING = 20        # stop sotto 20 crediti

SPORTS_MAP = {
    "Serie A": "soccer_italy_serie_a", "Premier League": "soccer_epl",
    "La Liga": "soccer_spain_la_liga", "Bundesliga": "soccer_germany_bundesliga",
    "Ligue 1": "soccer_france_ligue_one", "Eredivisie": "soccer_netherlands_eredivisie",
    "MLS": "soccer_usa_mls", "Brasileirao": "soccer_brazil_serie_a",
}

def _env(name):
    exact = os.getenv(name)
    if exact is not None: return exact.strip()
    for k, v in os.environ.items():
        if k.strip() == name: return v.strip()
    return ""

def _get_odds(sport, frm, to):
    cache_file = CACHE_DIR / f"toa_{sport}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            if time.time() - data.get("ts", 0) < ODDS_TTL:
                return data.get("payload", []), data.get("remaining", 999)
        except Exception: pass
    key = _env("ODDS_API_KEY")
    if not key: return [], 999
    try:
        r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sport}/odds", params={
            "apiKey": key, "regions": "eu", "markets": "h2h,totals",
            "oddsFormat": "decimal", "commenceTimeFrom": frm, "commenceTimeTo": to,
        }, timeout=30)
        remaining = int(r.headers.get("x-requests-remaining", 999))
        if r.status_code in (401, 429):
            logger.warning(f"the-odds-api bloccata (codice {r.status_code})")
            return [], 0
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.warning(f"Errore the-odds-api {sport}: {e}")
        return [], 999
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps({"ts": time.time(), "payload": payload, "remaining": remaining}))
    logger.info(f"the-odds-api {sport}: {len(payload)} match | crediti residui: {remaining}")
    return payload, remaining

def fetch_odds(sport=None, commence_time_from=None, commence_time_to=None, **kwargs):
    if not sport: return []
    payload, remaining = _get_odds(sport, commence_time_from, commence_time_to)
    if remaining < MIN_REMAINING:
        logger.warning(f"Crediti esauriti ({remaining}), nessuna quota scaricata")
        return []
    return payload

def fetch_scores(sport=None, days_from=2):
    """Risultati finali (stessa chiave, ~1 credito/call, cache 24h)."""
    if not sport:
        return []
    cache_file = CACHE_DIR / f"toa_scores_{sport}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            if time.time() - data.get("ts", 0) < ODDS_TTL:
                return data.get("payload", [])
        except Exception:
            pass
    key = _env("ODDS_API_KEY")
    if not key:
        return []
    try:
        r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sport}/scores",
                         params={"apiKey": key, "daysFrom": days_from}, timeout=30)
        remaining = int(r.headers.get("x-requests-remaining", 999))
        if r.status_code in (401, 429):
            logger.warning(f"Scores bloccati ({r.status_code})")
            return []
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.warning(f"Errore scores {sport}: {e}")
        return []
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps({"ts": time.time(), "payload": payload}))
    logger.info(f"the-odds-api scores {sport}: {len(payload)} | crediti residui: {remaining}")
    return payload
