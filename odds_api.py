import os
import logging
import requests

logger = logging.getLogger(__name__)

API_KEY = os.getenv("ODDS_API_KEY", "")
BASE_URL = "https://api.the-odds-api.com/v4"

SPORTS_MAP = {
    "Serie A": "soccer_italy_serie_a",
    "Premier League": "soccer_epl",
    "La Liga": "soccer_spain_la_liga",
    "Bundesliga": "soccer_germany_bundesliga",
    "Ligue 1": "soccer_france_ligue_one",
}

def fetch_odds(sport="soccer_italy_serie_a", regions="eu", markets="h2h,totals", commence_time_from=None, commence_time_to=None):
    if not API_KEY:
        raise ValueError("Imposta ODDS_API_KEY")
    url = f"{BASE_URL}/sports/{sport}/odds"
    params = {
        "apiKey": API_KEY,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "decimal",
    }
    if commence_time_from:
        params["commenceTimeFrom"] = commence_time_from
    if commence_time_to:
        params["commenceTimeTo"] = commence_time_to
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()

def normalize_odds(raw_data, league_name="Serie A"):
    normalized = []
    for match in raw_data:
        event_name = f"{league_name} – {match['home_team']} vs {match['away_team']}"
        commence = match.get("commence_time", "")
        for bookmaker in match.get("bookmakers", []):
            bm_name = bookmaker["title"]
            for market in bookmaker.get("markets", []):
                for outcome in market.get("outcomes", []):
                    esito = outcome["name"]
                    quota = outcome["price"]
                    if market["key"] == "h2h":
                        if esito == match["home_team"]: esito = "1"
                        elif esito == match["away_team"]: esito = "2"
                        else: esito = "X"
                    elif market["key"] == "totals":
                        point = outcome.get("point", "2.5")
                        esito = f"{outcome['name']} {point}"
                    normalized.append({
                        "evento": event_name,
                        "esito": esito,
                        "quota_decimale": quota,
                        "bookmaker": bm_name,
                        "probabilita": 0.0,
                        "match_id": match.get("id", ""),
                        "commence_time": commence,
                        "home_team": match["home_team"],
                        "away_team": match["away_team"],
                    })
    return normalized

def get_live_odds():
    if not API_KEY:
        logger.warning("ODDS_API_KEY non impostata, uso quote statiche")
        return []
    all_odds = []
    for league, sport_key in SPORTS_MAP.items():
        try:
            raw = fetch_odds(sport=sport_key)
            normalized = normalize_odds(raw, league_name=league)
            all_odds.extend(normalized)
            logger.info(f"{league}: {len(normalized)} quote caricate")
        except Exception as e:
            logger.warning(f"Errore fetch {league}: {e}")
    return all_odds
