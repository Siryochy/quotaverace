"""The Odds API - client per quote reali"""

import os
import requests
from typing import List, Dict, Any

API_KEY = os.getenv("ODDS_API_KEY", "")
BASE_URL = "https://api.the-odds-api.com/v4"


# === MAPPING COMPLETO: 31 COMPETIZIONI ===
SPORTS_MAP = {
    # Top 5 Europee
    "Serie A": "soccer_italy_serie_a",
    "Premier League": "soccer_epl",
    "La Liga": "soccer_spain_la_liga",
    "Bundesliga": "soccer_germany_bundesliga",
    "Ligue 1": "soccer_france_ligue_one",
    
    # Altre Europee
    "EFL Championship": "soccer_efl_champ",
    "Serie B": "soccer_italy_serie_b",
    "Eredivisie": "soccer_netherlands_eredivisie",
    "Primeira Liga": "soccer_portugal_primeira_liga",
    
    # Coppe Europee
    "Champions League": "soccer_uefa_champs_league",
    "Europa League": "soccer_uefa_europa_league",
    "Conference League": "soccer_uefa_europa_conference_league",
    
    # Coppe Nazionali
    "Coppa Italia": "soccer_italy_coppa_italia",
    "Copa del Rey": "soccer_spain_copa_del_rey",
    "Coupe de France": "soccer_france_coupe_de_france",
    "DFB Pokal": "soccer_germany_dfb_pokal",
    
    # Nord America
    "Liga MX": "soccer_mexico_ligamx",
    "MLS": "soccer_usa_mls",
    
    # Medio Oriente
    "Saudi Pro League": "soccer_saudi_arabia_pro_league",
    
    # Nordici
    "Allsvenskan": "soccer_sweden_allsvenskan",
    "Eliteserien": "soccer_norway_eliteserien",
    "Superliga Danimarca": "soccer_denmark_superliga",
    "Veikkausliiga": "soccer_finland_veikkausliiga",
    
    # Asia
    "J1 League": "soccer_japan_j_league",
    "K League 1": "soccer_south_korea_k_league",
    "A-League": "soccer_australia_aleague",
    
    # Sud America
    "Brasileirao": "soccer_brazil_campeonato",
    "Argentina Primera": "soccer_argentina_primera_division",
    "Colombia Primera": "soccer_colombia_primera_a",
    "Chile Primera": "soccer_chile_primera_division",
    
    # Africa
    "Egyptian Premier League": "soccer_egypt_premier_league",
}


def fetch_odds(sport: str, regions: str = "eu", markets: str = "h2h,totals",
               commence_time_from: str = None, commence_time_to: str = None) -> List[Dict[str, Any]]:
    """Scarica le quote da The Odds API"""
    if not API_KEY:
        raise ValueError("ODDS_API_KEY non configurata")
    
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
    
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_live_odds() -> List[Dict[str, Any]]:
    """Scarica quote live da tutti gli sport attivi"""
    if not API_KEY:
        return []
    
    all_odds = []
    for league_name, sport_key in SPORTS_MAP.items():
        try:
            odds = fetch_odds(sport=sport_key)
            for match in odds:
                match["_league"] = league_name
            all_odds.extend(odds)
        except Exception as e:
            print(f"⚠️  Skip {league_name}: {e}")
    
    return all_odds


def get_available_sports() -> List[Dict[str, Any]]:
    """Lista sport disponibili sul tuo piano API"""
    if not API_KEY:
        return []
    
    url = f"{BASE_URL}/sports"
    resp = requests.get(url, params={"apiKey": API_KEY}, timeout=15)
    resp.raise_for_status()
    return resp.json()
