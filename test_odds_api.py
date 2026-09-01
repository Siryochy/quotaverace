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


def test_copertura_mondiale_completa():
    """SPORTS_MAP copre TUTTE le competizioni di calcio the-odds-api
    (66 chiavi soccer_, verificate su the-odds-api.com/sports-apis)."""
    assert len(SPORTS_MAP) >= 60
    for league, key in SPORTS_MAP.items():
        assert key.startswith("soccer_"), f"{league}: chiave non soccer: {key}"


def test_lega_con_roster_ha_dati():
    """Le leghe con roster in ALL_LEAGUES devono averlo non vuoto."""
    for league in ALL_LEAGUES:
        if league in SPORTS_MAP:
            assert ALL_LEAGUES[league], f"{league} con roster vuoto"


def test_rotazione_crediti():
    """Ogni lega ha un intervallo esplicito e il costo mensile sta nel
    piano free the-odds-api (500 crediti/mese)."""
    from odds_api import interval_for_sport, SPORTS_INTERVAL_DAYS
    assert interval_for_sport("soccer_epl") == 2
    assert interval_for_sport("soccer_turkey_super_league") == 7
    assert interval_for_sport("soccer_uefa_nations_league") == 14
    # ogni lega in SPORTS_MAP deve avere un intervallo ESPLICITO
    # (niente default silenziosi: prima "Chile Primera" finiva a 1 = 30/mese)
    for league, key in SPORTS_MAP.items():
        assert league in SPORTS_INTERVAL_DAYS, f"{league} senza intervallo"
        assert interval_for_sport(key) == SPORTS_INTERVAL_DAYS[league]


def test_budget_mensile_piano_free():
    """Costo mensile totale della rotazione <= 460 crediti (500 del piano
    free, con margine per /scores e trigger manuali)."""
    from odds_api import interval_for_sport
    cost = sum(30.0 / interval_for_sport(key) for key in SPORTS_MAP.values())
    assert cost <= 460, f"costo mensile {cost:.0f} oltre il budget free"


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


def test_match_team_fallback_nome_api():
    """Squadra fuori roster -> si usa il nome API (la partita non sparisce)."""
    from fixture_engine import _match_team
    assert _match_team("Galatasaray", "Turkey Super Lig") == "Galatasaray"
    assert _match_team("Sconosciuta FC", "Serie A") == "Sconosciuta FC"


def test_expected_goals_con_squadre_sconosciute():
    """expected_goals non alza piu' errori: profilo di lega di default."""
    from poisson_engine import expected_goals
    lam_h, lam_a = expected_goals("Sconosciuta FC", "Altra FC")
    assert lam_h > 0 and lam_a > 0
    # mischiata con una squadra conosciuta funziona comunque
    lam_h2, lam_a2 = expected_goals("Inter", "Sconosciuta FC")
    assert lam_h2 > 0 and lam_a2 > 0


def test_match_team_copre_nuove_leghe():
    """Il matching riconosce le squadre dei campionati appena aggiunti
    (West Ham/Wolves in Championship, Young Boys/Zurigo in Svizzera)."""
    from fixture_engine import _match_team
    assert _match_team("West Ham", "EFL Championship") == "West Ham"
    assert _match_team("Wolves", "EFL Championship") == "Wolves"
    # l'API the-odds-api usa i nomi completi: alias obbligatori
    assert _match_team("West Ham United", "EFL Championship") == "West Ham"
    assert _match_team("Wolverhampton Wanderers", "EFL Championship") == "Wolves"
    assert _match_team("Blackburn Rovers", "EFL Championship") == "Blackburn"
    assert _match_team("Southampton", "EFL Championship") == "Southampton"
    assert _match_team("Bolton Wanderers", "EFL Championship") == "Bolton Wanderers"
    assert _match_team("Lincoln City", "EFL Championship") == "Lincoln City"
    assert _match_team("Birmingham City", "EFL Championship") == "Birmingham City"
    assert _match_team("Young Boys", "Swiss Super League") == "Young Boys"
    assert _match_team("FC Zurich", "Swiss Super League") == "FC Zurich"
    # l'API puo' usare l'umlaut o la forma corta: entrambe devono matchare
    assert _match_team("Zürich", "Swiss Super League") == "Zürich"
    assert _match_team("Zurich", "Swiss Super League") == "FC Zurich"


def test_fetch_analizza_anche_squadre_sconosciute(monkeypatch, tmp_path):
    """Con la copertura mondiale NESSUNA partita viene piu' saltata:
    anche le squadre fuori roster vengono analizzate (profilo di default)."""
    import tracker
    import fixture_engine
    import odds_api
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "t.db")
    tracker.init_db()
    monkeypatch.setattr(fixture_engine, "DATA_DIR", tmp_path)
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)  # cache vuota -> tutte dovute
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
    assert total == 2          # ENTRAMBE analizzate (anche le sconosciute)
    assert skipped == []       # niente partite perse in silenzio


def test_budget_giornaliero_cap(monkeypatch, tmp_path):
    """Il tetto giornaliero limita le chiamate API: con budget 1 viene
    interrogata solo la lega piu' prioritaria (Serie A, intervallo minore)."""
    import tracker
    import fixture_engine
    import odds_api
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "t.db")
    tracker.init_db()
    monkeypatch.setattr(fixture_engine, "DATA_DIR", tmp_path)
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fixture_engine, "DAILY_QUERY_BUDGET", 1)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    calls = []

    def fake_fetch(sport=None, **kw):
        calls.append(sport)
        return []
    monkeypatch.setattr(fixture_engine, "fetch_odds", fake_fetch)

    fixture_engine.fetch_and_analyze_today()
    assert len(calls) == 1
    assert calls[0] == "soccer_italy_serie_a"  # prima per priorita'
