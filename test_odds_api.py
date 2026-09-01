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
    "EFL Championship": "soccer_efl_champ",
    "Swiss Super League": "soccer_switzerland_superleague",
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
    # regressione: le squadre di Serie B giocano la Coppa Italia
    # (Parma-Cremonese 1/9/2026 veniva persa: roster solo Serie A)
    assert "Parma" in ALL_LEAGUES["Coppa Italia"]
    assert "Cremonese" in ALL_LEAGUES["Coppa Italia"]
    assert "Real Madrid" in ALL_LEAGUES["Copa del Rey"]
    assert "Paris Saint-Germain" in ALL_LEAGUES["Coupe de France"]
    assert "Bayern Munich" in ALL_LEAGUES["DFB Pokal"]
    assert "Inter" in ALL_LEAGUES["Champions League"]
    # coppe internazionali: roster = merge dei campionati d'origine
    assert "Manchester City" in ALL_LEAGUES["FA Cup"]
    assert "Leeds United" in ALL_LEAGUES["EFL Cup"]
    assert "Flamengo" in ALL_LEAGUES["Copa Libertadores"]
    assert "River Plate" in ALL_LEAGUES["Copa Libertadores"]
    # regressione: West Ham e Wolves retrocesse giocano in Championship
    # (West Ham vs Wolves 1/9/2026 veniva persa: lega non interrogata)
    assert "West Ham" in ALL_LEAGUES["EFL Championship"]
    assert "Wolves" in ALL_LEAGUES["EFL Championship"]
    # regressione: Super League svizzera (Zurigo vs Young Boys 1/9/2026)
    assert "Young Boys" in ALL_LEAGUES["Swiss Super League"]
    assert "FC Zurich" in ALL_LEAGUES["Swiss Super League"]


def test_match_team_copre_nuove_leghe():
    """Il matching riconosce le squadre dei campionati appena aggiunti
    (West Ham/Wolves in Championship, Young Boys/Zurigo in Svizzera)."""
    from fixture_engine import _match_team
    assert _match_team("West Ham", "EFL Championship") == "West Ham"
    assert _match_team("Wolves", "EFL Championship") == "Wolves"
    assert _match_team("Young Boys", "Swiss Super League") == "Young Boys"
    assert _match_team("FC Zurich", "Swiss Super League") == "FC Zurich"
    # l'API puo' usare l'umlaut o la forma corta: entrambe devono matchare
    assert _match_team("Zürich", "Swiss Super League") == "Zürich"
    assert _match_team("Zurich", "Swiss Super League") == "FC Zurich"


def test_fetch_traccia_partite_saltate(monkeypatch, tmp_path):
    """fetch_and_analyze_today NON perde partite in silenzio: quelle con
    squadre fuori roster finiscono in `skipped` e su saltate.json."""
    import tracker
    import fixture_engine
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "t.db")
    tracker.init_db()
    monkeypatch.setattr(fixture_engine, "DATA_DIR", tmp_path)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    payload = [
        {"id": "m1", "home_team": "Inter", "away_team": "Napoli",
         "commence_time": "2026-09-01T18:00:00Z", "bookmakers": [
             {"title": "Pinnacle", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Inter", "price": 1.90},
                 {"name": "Napoli", "price": 4.20},
                 {"name": "Draw", "price": 3.40},
             ]}]},
         ]},
        {"id": "m2", "home_team": "Sconosciuta FC", "away_team": "Altra FC",
         "commence_time": "2026-09-01T19:00:00Z", "bookmakers": []},
    ]

    def fake_fetch(sport=None, **kw):
        return payload if sport == "soccer_italy_serie_a" else []
    monkeypatch.setattr(fixture_engine, "fetch_odds", fake_fetch)

    total, value, skipped = fixture_engine.fetch_and_analyze_today()
    assert total == 1          # solo Inter-Napoli (squadre coperte)
    assert len(skipped) == 1   # Sconosciuta FC vs Altra FC tracciata
    assert skipped[0]["home"] == "Sconosciuta FC"
    assert set(skipped[0]["non_coperte"]) == {"Sconosciuta FC", "Altra FC"}
    # persistita per il report del mattino
    persisted = fixture_engine.get_skipped_matches()
    assert persisted and persisted[0]["home"] == "Sconosciuta FC"
