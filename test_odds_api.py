"""Test SPORTS_MAP: le coppe devono avere la chiave ufficiale the-odds-api
e i dati squadre in ALL_LEAGUES (senza, il matching squadre salta)."""

from odds_api import SPORTS_MAP
from leagues_data import ALL_LEAGUES

# Chiavi ufficiali the-odds-api (documentazione the-odds-api.com/sports-apis)
OFFICIAL_CUP_KEYS = {
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
}


def test_coppe_presenti_in_sports_map():
    for cup in OFFICIAL_CUP_KEYS:
        assert cup in SPORTS_MAP, f"{cup} manca da SPORTS_MAP"


def test_coppe_con_chiave_ufficiale():
    for cup, key in OFFICIAL_CUP_KEYS.items():
        assert SPORTS_MAP[cup] == key, f"{cup}: chiave {SPORTS_MAP[cup]} != ufficiale {key}"


def test_ogni_lega_ha_dati_squadre():
    """Ogni lega di SPORTS_MAP deve avere il roster in ALL_LEAGUES
    (altrimenti _match_team non riconosce le squadre e la lega salta)."""
    for league in SPORTS_MAP:
        assert league in ALL_LEAGUES, f"{league} senza dati in ALL_LEAGUES"
        assert ALL_LEAGUES[league], f"{league} con roster vuoto"


def test_roster_coppe_coprono_le_top():
    """Le coppe nazionali copiano i roster dei campionati: almeno le squadre
    principali devono essere riconosciute (es. Inter in Coppa Italia)."""
    assert "Inter" in ALL_LEAGUES["Coppa Italia"]
    assert "Real Madrid" in ALL_LEAGUES["Copa del Rey"]
    assert "Paris Saint-Germain" in ALL_LEAGUES["Coupe de France"]
    assert "Bayern Munich" in ALL_LEAGUES["DFB Pokal"]
    assert "Inter" in ALL_LEAGUES["Champions League"]
    # coppe internazionali: roster = merge dei campionati d'origine
    assert "Manchester City" in ALL_LEAGUES["FA Cup"]
    assert "Leeds United" in ALL_LEAGUES["EFL Cup"]
    assert "Flamengo" in ALL_LEAGUES["Copa Libertadores"]
    assert "River Plate" in ALL_LEAGUES["Copa Libertadores"]
