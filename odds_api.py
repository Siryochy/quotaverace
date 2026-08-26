"""
QuotaVerace – Fetch quote reali da The Odds API
Registrati gratis: https://the-odds-api.com
"""
import os
import requests
from typing import List, Dict, Any

API_KEY = os.getenv("ODDS_API_KEY", "")
BASE_URL = "https://api.the-odds-api.com/v4"

def fetch_odds(sport="soccer_italy_serie_a", regions="eu", markets="h2h,totals"):
    if not API_KEY:
        raise ValueError("Imposta ODDS_API_KEY")
    url = f"{BASE_URL}/sports/{sport}/odds"
    params = {
        "apiKey": API_KEY,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "decimal",
    }
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()

def normalize_odds(raw_data):
    normalized = []
    for match in raw_data:
        event_name = f"Serie A – {match['home_team']} vs {match['away_team']}"
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
                    })
    return normalized

def get_live_odds():
    raw = fetch_odds()
    return normalize_odds(raw)
