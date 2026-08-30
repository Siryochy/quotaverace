"""odds_api.py — Quote via API-Football (v3), schema compatibile the-odds-api."""
import json
import os
import re
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://v3.football.api-sports.io"
CACHE_DIR = Path(__file__).parent / "data"
FIXTURE_TTL = 4 * 3600   # cache calendario 4h
ODDS_TTL = 2 * 3600      # cache quote 2h
DAILY_BUDGET = 90        # margine sotto le 100 richieste/giorno
SEASON = 2026            # stagione 2026-27 (cambia a inizio anno)

# Nome lega (usato da fixture_engine) -> API-Football league id
SPORTS_MAP = {
    "Serie A": 135, "Premier League": 39, "La Liga": 140,
    "Bundesliga": 78, "Ligue 1": 61, "Eredivisie": 88,
    "MLS": 253, "Brasileirao": 71, "Serie B": 136,
    "EFL Championship": 40, "Primeira Liga": 94,
    "Superliga Danimarca": 119, "Allsvenskan": 113,
    "Eliteserien": 103, "Liga MX": 262, "Argentina Primera": 128,
    "Champions League": 2, "Europa League": 3,
    "Coppa Italia": 137, "Copa del Rey": 143, "DFB Pokal": 81,
    "Coupe de France": 66, "Saudi Pro League": 307,
    "Veikkausliiga": 110, "J1 League": 98, "K League 1": 292,
    "A-League": 188, "Colombia Primera": 239,
    "Chile Primera": 265, "Egyptian Premier League": 233,
}

_COUNT_FILE = CACHE_DIR / "af_requests.json"


def _env(name):
    """Legge una variabile d'ambiente tollerando newline/spazi nel nome."""
    exact = os.getenv(name)
    if exact is not None:
        return exact.strip()
    for k, v in os.environ.items():
        if k.strip() == name:
            return v.strip()
    return ""


API_KEY = _env("API_FOOTBALL_KEY")


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _load_counter():
    try:
        d = json.loads(_COUNT_FILE.read_text())
        if d.get("day") == _today():
            return d.get("count", 0)
    except Exception:
        pass
    return 0


def _inc_counter():
    CACHE_DIR.mkdir(exist_ok=True)
    d = {"day": _today(), "count": _load_counter() + 1}
    _COUNT_FILE.write_text(json.dumps(d))
    logger.info(f"API-Football richieste oggi: {d['count']}/100")


def _get_json(path, params, cache_key, ttl):
    if not API_KEY:
        logger.warning("API_FOOTBALL_KEY mancante")
        return None
    cache_file = CACHE_DIR / f"af_{cache_key}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            if time.time() - data.get("ts", 0) < ttl:
                return data.get("payload")
        except Exception:
            pass
    if _load_counter() >= DAILY_BUDGET:
        logger.warning("Budget giornaliero API-Football raggiunto (%d)", DAILY_BUDGET)
        return None
    try:
        resp = requests.get(
            BASE_URL + path,
            params=params,
            headers={"x-apisports-key": API_KEY, "host": "v3.football.api-sports.io"},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        logger.warning(f"Errore API-Football {path}: {e}")
        return None
    _inc_counter()
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps({"ts": time.time(), "payload": body}))
    return body


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _extract_point(s):
    m = re.search(r"(\d+(?:\.\d+)?)", s or "")
    return float(m.group(1)) if m else None


def _dates_from_range(frm, to):
    try:
        d0 = datetime.fromisoformat(frm.replace("Z", "+00:00")) if frm else datetime.utcnow()
        d1 = datetime.fromisoformat(to.replace("Z", "+00:00")) if to else d0 + timedelta(hours=28)
    except Exception:
        d0, d1 = datetime.utcnow(), datetime.utcnow() + timedelta(hours=28)
    dates, cur = [], d0.date()
    while cur <= d1.date() and len(dates) < 3:
        dates.append(cur.isoformat())
        cur += timedelta(days=1)
    return dates


def _in_window(ts, frm, to):
    if not ts:
        return True
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if frm and t < datetime.fromisoformat(frm.replace("Z", "+00:00")):
            return False
        if to and t > datetime.fromisoformat(to.replace("Z", "+00:00")):
            return False
    except Exception:
        pass
    return True


def _fetch_league_odds(league_id, season):
    """Una sola chiamata /odds per lega -> {fixture_id: [bookmakers]}."""
    body = _get_json("/odds", {"league": league_id, "season": season},
                     f"odds_{league_id}_{season}", ODDS_TTL)
    if not body or not body.get("response"):
        return {}
    out = {}
    for item in body["response"]:
        fid = str(item.get("fixture", ""))
        bms = []
        for bm in item.get("bookmakers", []):
            mkts = []
            for bet in bm.get("bets", []):
                bname = (bet.get("name") or "").lower()
                if "match winner" in bname or "1x2" in bname.replace(" ", ""):
                    outcomes = [{"name": v.get("value", ""), "price": _to_float(v.get("odd"))}
                                for v in bet.get("values", [])]
                    if outcomes:
                        mkts.append({"key": "h2h", "outcomes": outcomes})
                elif "over/under" in bname or "goals over/under" in bname:
                    outcomes = []
                    for v in bet.get("values", []):
                        val = v.get("value", "")
                        point = _extract_point(val) or _extract_point(bname)
                        if point is None:
                            continue
                        if "over" in val.lower():
                            outcomes.append({"name": f"Over {point}", "price": _to_float(v.get("odd")), "point": point})
                        elif "under" in val.lower():
                            outcomes.append({"name": f"Under {point}", "price": _to_float(v.get("odd")), "point": point})
                    if outcomes:
                        mkts.append({"key": "totals", "outcomes": outcomes})
            if mkts:
                bms.append({"title": bm.get("name", ""), "markets": mkts})
        if bms:
            out[fid] = bms
    return out


def fetch_odds(sport=None, commence_time_from=None, commence_time_to=None,
               league=None, season=None):
    """Compat con la vecchia firma: sport = id lega API-Football."""
    if not API_KEY:
        return []
    league_id = league or sport
    if league_id is None:
        return []
    league_name = next((n for n, i in SPORTS_MAP.items() if str(i) == str(league_id)),
                       str(league_id))
    season = season or SEASON
    matches = []
    for date in _dates_from_range(commence_time_from, commence_time_to):
        body = _get_json("/fixtures",
                         {"league": league_id, "season": season, "date": date,
                          "timezone": "Europe/Rome"},
                         f"fix_{league_id}_{date}", FIXTURE_TTL)
        if not body or not body.get("response"):
            continue
        odds_by_fixture = _fetch_league_odds(league_id, season)
        for fx in body["response"]:
            home = fx.get("teams", {}).get("home", {}).get("name", "")
            away = fx.get("teams", {}).get("away", {}).get("name", "")
            ts = fx.get("fixture", {}).get("date", "")
            if not home or not away or not _in_window(ts, commence_time_from, commence_time_to):
                continue
            fid = str(fx.get("fixture", {}).get("id", ""))
            bms = odds_by_fixture.get(fid, [])
            # Mappa Home/Draw/Away ai nomi reali (schema vecchio)
            for bm in bms:
                for mkt in bm["markets"]:
                    if mkt["key"] == "h2h":
                        for o in mkt["outcomes"]:
                            if o["name"] == "Home":
                                o["name"] = home
                            elif o["name"] == "Away":
                                o["name"] = away
            matches.append({
                "id": fid, "home_team": home, "away_team": away,
                "commence_time": ts, "league": league_name, "bookmakers": bms,
            })
    return matches
