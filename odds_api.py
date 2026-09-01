import json, os, time, logging, requests
from pathlib import Path
from config import DATA_DIR, load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR
ODDS_TTL = 86400          # cache 24h = 1 chiamata/giorno per lega
MIN_REMAINING = 20        # stop sotto 20 crediti

SPORTS_MAP = {
    "Serie A": "soccer_italy_serie_a", "Premier League": "soccer_epl",
    "La Liga": "soccer_spain_la_liga", "Bundesliga": "soccer_germany_bundesliga",
    "Ligue 1": "soccer_france_ligue_one", "Eredivisie": "soccer_netherlands_eredivisie",
    "MLS": "soccer_usa_mls", "Brasileirao": "soccer_brazil_serie_a",
    # Coppe (chiavi ufficiali the-odds-api, verificate su the-odds-api.com)
    "Champions League": "soccer_uefa_champs_league",
    "Europa League": "soccer_uefa_europa_league",
    "Conference League": "soccer_uefa_europa_conference_league",
    "Coppa Italia": "soccer_italy_coppa_italia",
    "Copa del Rey": "soccer_spain_copa_del_rey",
    "Coupe de France": "soccer_france_coupe_de_france",
    "DFB Pokal": "soccer_germany_dfb_pokal",
    "FA Cup": "soccer_fa_cup",
    "EFL Cup": "soccer_england_efl_cup",
    "Copa Libertadores": "soccer_conmebol_copa_libertadores",
    "EFL Championship": "soccer_efl_champ",
    "Swiss Super League": "soccer_switzerland_superleague",
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

def oddsapi_to_records(payload, sport="calcio"):
    """Converte il payload v4 the-odds-api nel contratto normalizzato di odds_ingest.

    Righe {bookmaker, evento, sport, esito, quota_decimale, timestamp}:
    - h2h: outcome nome squadra -> "1"/"2", "Draw" -> "X";
    - totals: "Over X.5"/"Under X.5" lasciati com' sono;
    - evento = f"{home} vs {away}" (senza campionato: per il merging
      con Betfair l'accoppiata squadre e' la chiave).
    """
    rows = []
    for match in payload:
        home = (match.get("home_team") or "").strip()
        away = (match.get("away_team") or "").strip()
        if not home or not away:
            continue
        commence = match.get("commence_time") or ""
        for bm in match.get("bookmakers", []):
            bookmaker = bm.get("title") or bm.get("key") or "unknown"
            for mkt in bm.get("markets", []):
                key = mkt.get("key")
                for out in mkt.get("outcomes", []):
                    name = (out.get("name") or "").strip()
                    price = out.get("price")
                    if not name or price is None or float(price) <= 1.0:
                        continue
                    if key == "h2h":
                        if name == home:
                            esito = "1"
                        elif name == away:
                            esito = "2"
                        elif name.lower() in ("draw", "pareggio"):
                            esito = "X"
                        else:
                            continue
                    elif key == "totals":
                        esito = name  # "Over 2.5" / "Under 2.5"
                    else:
                        continue
                    rows.append({
                        "bookmaker": bookmaker,
                        "evento": f"{home} vs {away}",
                        "sport": sport,
                        "esito": esito,
                        "quota_decimale": float(price),
                        "timestamp": commence,
                    })
    return rows


def get_live_odds():
    """Quote reali oggi per tutte le leghe, come lista di righe normalizzate.

    Usa la cache 24h per lega: dopo il job mattutino 6:00 (fetch_and_analyze
    today) le chiamate successive costano zero crediti. Serve ODDS_API_KEY.
    """
    if not _env("ODDS_API_KEY"):
        return []
    from datetime import datetime, timedelta
    frm = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (datetime.utcnow() + timedelta(hours=28)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for sport_key in SPORTS_MAP.values():
        try:
            payload = fetch_odds(sport=sport_key, commence_time_from=frm,
                                 commence_time_to=to)
        except Exception as e:
            logger.warning(f"get_live_odds {sport_key}: {e}")
            continue
        if payload:
            rows.extend(oddsapi_to_records(payload))
    logger.info(f"get_live_odds: {len(rows)} quote normalizzate")
    return rows

def get_quota():
    """Crediti residui dall'ultimo scan (dalle cache, costo zero)."""
    remaining = []
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("toa_*.json"):
            try:
                d = json.loads(f.read_text())
                if d.get("remaining") is not None:
                    remaining.append(int(d["remaining"]))
            except Exception:
                continue
    if not remaining:
        return None
    return min(remaining), len(remaining)
