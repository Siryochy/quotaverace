import json, os, time, logging, requests
from pathlib import Path
from config import DATA_DIR, load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR
ODDS_TTL = 86400          # cache 24h = 1 chiamata/giorno per lega
MIN_REMAINING = 20        # stop sotto 20 crediti

# TUTTE le competizioni di calcio coperte da the-odds-api (chiavi ufficiali
# verificate su the-odds-api.com/sports-apis). Le squadre senza roster in
# leagues_data usano il profilo di lega di default (expected_goals).
SPORTS_MAP = {
    # Campionati top + serie B
    "Serie A": "soccer_italy_serie_a", "Serie B": "soccer_italy_serie_b",
    "Premier League": "soccer_epl", "EFL Championship": "soccer_efl_champ",
    "League One": "soccer_england_league1", "League Two": "soccer_england_league2",
    "La Liga": "soccer_spain_la_liga", "La Liga 2": "soccer_spain_segunda_division",
    "Bundesliga": "soccer_germany_bundesliga", "Bundesliga 2": "soccer_germany_bundesliga2",
    "3. Liga": "soccer_germany_liga3", "Frauen-Bundesliga": "soccer_germany_bundesliga_women",
    "Ligue 1": "soccer_france_ligue_one", "Ligue 2": "soccer_france_ligue_two",
    "Eredivisie": "soccer_netherlands_eredivisie", "Primeira Liga": "soccer_portugal_primeira_liga",
    "Scottish Premiership": "soccer_spl", "Austrian Bundesliga": "soccer_austria_bundesliga",
    "Belgian First Div": "soccer_belgium_first_div", "Greek Super League": "soccer_greece_super_league",
    "Polish Ekstraklasa": "soccer_poland_ekstraklasa", "Russian Premier League": "soccer_russia_premier_league",
    "Turkey Super Lig": "soccer_turkey_super_league", "Swiss Super League": "soccer_switzerland_superleague",
    "Superliga Danimarca": "soccer_denmark_superliga", "Allsvenskan": "soccer_sweden_allsvenskan",
    "Sweden Superettan": "soccer_sweden_superettan", "Eliteserien": "soccer_norway_eliteserien",
    "Veikkausliiga": "soccer_finland_veikkausliiga", "League of Ireland": "soccer_league_of_ireland",
    "China Super League": "soccer_china_superleague", "J1 League": "soccer_japan_j_league",
    "K League 1": "soccer_korea_kleague1", "A-League": "soccer_australia_aleague",
    # Americhe
    "MLS": "soccer_usa_mls", "Brasileirao": "soccer_brazil_campeonato",
    "Brazil Serie B": "soccer_brazil_serie_b", "Liga MX": "soccer_mexico_ligamx",
    "Saudi Pro League": "soccer_saudi_arabia_pro_league",
    "Argentina Primera": "soccer_argentina_primera_division",
    "Chile Primera": "soccer_chile_campeonato",
    # Coppe europee
    "Champions League": "soccer_uefa_champs_league",
    "Europa League": "soccer_uefa_europa_league",
    "Conference League": "soccer_uefa_europa_conference_league",
    "UCL Qualification": "soccer_uefa_champs_league_qualification",
    "UEFA Women's Champions League": "soccer_uefa_champs_league_women",
    "Coppa Italia": "soccer_italy_coppa_italia",
    "Copa del Rey": "soccer_spain_copa_del_rey",
    "Coupe de France": "soccer_france_coupe_de_france",
    "DFB Pokal": "soccer_germany_dfb_pokal",
    "FA Cup": "soccer_fa_cup", "EFL Cup": "soccer_england_efl_cup",
    # Coppe internazionali + nazionali
    "Copa Libertadores": "soccer_conmebol_copa_libertadores",
    "Copa Sudamericana": "soccer_conmebol_copa_sudamericana",
    "Copa America": "soccer_conmebol_copa_america",
    "CONCACAF Gold Cup": "soccer_concacaf_gold_cup",
    "CONCACAF Leagues Cup": "soccer_concacaf_leagues_cup",
    "Africa Cup of Nations": "soccer_africa_cup_of_nations",
    "FIFA Club World Cup": "soccer_fifa_club_world_cup",
    "FIFA World Cup": "soccer_fifa_world_cup",
    "FIFA World Cup Qualifiers Europe": "soccer_fifa_world_cup_qualifiers_europe",
    "FIFA World Cup Qualifiers S.America": "soccer_fifa_world_cup_qualifiers_south_america",
    "FIFA Women's World Cup": "soccer_fifa_world_cup_womens",
    "UEFA Euro": "soccer_uefa_european_championship",
    "UEFA Euro Qualifiers": "soccer_uefa_euro_qualification",
    "UEFA Nations League": "soccer_uefa_nations_league",
}

# Rotazione interrogazioni (giorni) per rispettare i 500 crediti/mese del
# piano free (1 chiamata/lega/giorno): le leghe core ogni giorno, le altre
# a rotazione. Il TTL di cache effettivo diventa interval*24h.
SPORTS_INTERVAL_DAYS = {
    # ogni giorno (le leghe con roster + i mercati principali)
    "Serie A": 1, "Premier League": 1, "La Liga": 1, "Bundesliga": 1,
    "Ligue 1": 1, "Eredivisie": 1, "Champions League": 1,
    "Europa League": 1, "Conference League": 1, "Coppa Italia": 1,
    "Copa del Rey": 1, "Coupe de France": 1, "DFB Pokal": 1,
    "FA Cup": 1, "EFL Cup": 1, "EFL Championship": 1, "Serie B": 1,
    "Swiss Super League": 1, "MLS": 1, "Brasileirao": 1,
    "Liga MX": 1, "Saudi Pro League": 1,
    # ogni 2 giorni
    "Primeira Liga": 2, "Allsvenskan": 2, "Eliteserien": 2,
    "Superliga Danimarca": 2, "Veikkausliiga": 2, "J1 League": 2,
    "K League 1": 2, "A-League": 2, "Argentina Primera": 2,
    "Copa Libertadores": 2, "Copa Sudamericana": 2, "Ligue 2": 2,
    # ogni 3 giorni
    "Bundesliga 2": 3, "La Liga 2": 3, "League One": 3, "League Two": 3,
    "3. Liga": 3, "Scottish Premiership": 3, "Austrian Bundesliga": 3,
    "Belgian First Div": 3, "Greek Super League": 3, "Polish Ekstraklasa": 3,
    "Russian Premier League": 3, "Turkey Super Lig": 3,
    # ogni 4 giorni
    "Brazil Serie B": 4, "Sweden Superettan": 4, "China Super League": 4,
    "League of Ireland": 4, "Frauen-Bundesliga": 4, "FIFA Club World Cup": 4,
    "UCL Qualification": 4, "UEFA Women's Champions League": 4,
    # settimanali (nazionali e tornei rari)
    "FIFA World Cup": 7, "FIFA World Cup Qualifiers Europe": 7,
    "FIFA World Cup Qualifiers S.America": 7, "FIFA Women's World Cup": 7,
    "UEFA Euro": 7, "UEFA Euro Qualifiers": 7, "UEFA Nations League": 7,
    "Copa America": 7, "CONCACAF Gold Cup": 7, "CONCACAF Leagues Cup": 7,
    "Africa Cup of Nations": 7,
}


def interval_for_sport(sport_key: str) -> int:
    """Giorni tra un'interrogazione e l'altra per una sport key."""
    for lg, key in SPORTS_MAP.items():
        if key == sport_key:
            return SPORTS_INTERVAL_DAYS.get(lg, 1)
    return 1

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
            # TTL effettivo = intervallo di rotazione della lega (giorni*24h):
            # le leghe core ogni giorno, le altre meno spesso (risparmio crediti).
            ttl = interval_for_sport(sport) * 86400
            if time.time() - data.get("ts", 0) < ttl:
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
