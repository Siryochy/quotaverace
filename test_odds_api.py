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
    # Profilo SETTEMBRE 2026 sotto-budget: top campionati a 3gg, il resto
    # (es. Turkey Super Lig, UEFA Nations League) a 30gg = dormiente.
    assert interval_for_sport("soccer_epl") == 3
    assert interval_for_sport("soccer_turkey_super_league") == 30
    assert interval_for_sport("soccer_uefa_nations_league") == 30
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


# ---------------------------------------------------------------------------
# Stagger rotazione (10/09): le leghe core a 3gg non devono sincronizzarsi
# tutte nello stesso giorno, altrimenti ci sono 2 giorni su 3 senza analisi.
# ---------------------------------------------------------------------------


def _write_cache(tmp_path, sport_key, ts):
    (tmp_path / f"toa_{sport_key}.json").write_text(
        __import__("json").dumps({"ts": ts, "payload": [], "remaining": 500}))


def test_stagger_spalma_le_leghe_core():
    """Le leghe core (intervallo 3) hanno fasi diverse: non scadono tutte
    lo stesso giorno."""
    import odds_api
    core = [k for k in SPORTS_MAP.values() if odds_api.interval_for_sport(k) == 3]
    assert len(core) >= 6
    fasi = {odds_api._rotation_phase(k, 3) for k in core}
    assert len(fasi) >= 2, f"tutte le leghe core sincronizzate: fasi {fasi}"


def test_stagger_scadenza_sul_giorno_di_fase(monkeypatch, tmp_path):
    """Con cache vecchia di 1 giorno, una lega core e' dovuta SOLO sul suo
    giorno di fase (e non sui giorni vicini)."""
    import time as _t
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    now = 1_800_000_000.0  # giorno fisso: day = now // 86400
    monkeypatch.setattr(odds_api.time, "time", lambda: now)
    day = int(now // 86400)

    key = "soccer_italy_serie_a"
    interval = odds_api.interval_for_sport(key)
    phase = odds_api._rotation_phase(key, interval)
    # cache vecchia di 1 giorno (eta' < ttl di 3 giorni)
    _write_cache(tmp_path, key, now - 86400)
    assert odds_api.is_sport_due(key) == (day % interval == phase)

    # cache vecchia di 2 giorni: ancora dentro il ttl, stessa regola di fase
    _write_cache(tmp_path, key, now - 2 * 86400)
    assert odds_api.is_sport_due(key) == (day % interval == phase)


def test_stagger_non_anticipa_le_leghe_30gg(monkeypatch, tmp_path):
    """Le leghe a 30gg (dormienti) NON vengono anticipate dal giorno di
    fase: restano dovute solo a scadenza intervallo (zero costi extra)."""
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    now = 1_800_000_000.0
    monkeypatch.setattr(odds_api.time, "time", lambda: now)

    key = "soccer_turkey_super_league"
    assert odds_api.interval_for_sport(key) == 30
    _write_cache(tmp_path, key, now - 2 * 86400)  # eta' 2 giorni
    assert odds_api.is_sport_due(key) is False
    # anche sul giorno di fase (se cadesse oggi) non scatta: niente anticipo
    phase = odds_api._rotation_phase(key, 30)
    day = int(now // 86400)
    if day % 30 == phase:
        _write_cache(tmp_path, key, now - 86400)
        assert odds_api.is_sport_due(key) is False


def test_stagger_scadenza_per_intervallo_invariata(monkeypatch, tmp_path):
    """A scadenza intervallo la lega e' dovuta comunque, qualunque sia la
    fase (la regola di stagger NON allunga mai l'intervallo)."""
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    now = 1_800_000_000.0
    monkeypatch.setattr(odds_api.time, "time", lambda: now)

    key = "soccer_italy_serie_a"
    interval = odds_api.interval_for_sport(key)
    _write_cache(tmp_path, key, now - interval * 86400 - 1)
    assert odds_api.is_sport_due(key) is True


def test_scores_cache_persiste_i_crediti(monkeypatch, tmp_path):
    """Bug 12/09: `fetch_scores` non salvava `remaining` nella cache dei
    punteggi, quindi `get_remaining()`/`get_quota()` (guardia proattiva +
    credit watchdog) vedevano solo il consumo della rotazione quote: il
    contatore restava fermo a 58 mentre l'API ne riportava 6 -> nessun
    throttle, nessun alert, crediti bruciati dal settlement fino a zero.
    La cache dei punteggi deve rendere visibile il credito residuo
    restituito dall'header `x-requests-remaining`."""
    import json
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    class _Resp:
        status_code = 200
        headers = {"x-requests-remaining": "6"}

        def raise_for_status(self):
            return None

        def json(self):
            return [{"id": "m1", "home_team": "A", "away_team": "B",
                     "completed": True,
                     "scores": [{"name": "A", "score": "1"}]}]

    monkeypatch.setattr(odds_api.requests, "get", lambda *a, **k: _Resp())
    odds_api.fetch_scores("soccer_italy_serie_a")

    cached = json.loads(
        (tmp_path / "toa_scores_soccer_italy_serie_a.json").read_text())
    assert cached["remaining"] == 6
    assert odds_api.get_remaining() == 6
    assert odds_api.get_quota() == (6, 1)


def test_get_remaining_usa_la_lettura_piu_recente(monkeypatch, tmp_path):
    """Il contatore non deve restare inchiodato a un valore STANTIO.

    Caso reale del 12/09: chiave nuova con 452 crediti, ma le cache quote
    scritte con la chiave vecchia portavano ancora `remaining: 58` -> con il
    MINIMO tra le cache la lettura restava 58 per settimane (le cache quote
    si rinnovano ogni 3-30 giorni) e la rotazione veniva throttled a vuoto.
    Vale la lettura piu' recente; con `ts` preservato in `fetch_scores` e'
    `remaining_ts` a datare il valore del credito.
    """
    import json
    import time
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    now = time.time()
    # cache QUOTE vecchia (chiave precedente): 58 crediti, letta 2 giorni fa
    (tmp_path / "toa_soccer_italy_serie_a.json").write_text(json.dumps(
        {"ts": now - 2 * 86400, "payload": [], "remaining": 58,
         "remaining_ts": now - 2 * 86400}))
    # cache PUNTEGGI fresca (chiave nuova): 452 crediti, letta ora;
    # attenzione: `ts` puo' essere quello vecchio, `remaining_ts` e' ora
    (tmp_path / "toa_scores_soccer_italy_serie_a.json").write_text(json.dumps(
        {"ts": now - 86400, "payload": [], "remaining": 452,
         "remaining_ts": now}))
    assert odds_api.get_remaining() == 452
    # anche get_quota (usato da /api/health) deve riportare la lettura fresca
    assert odds_api.get_quota() == (452, 2)


def test_get_remaining_senza_remaining_ts_usa_ts(monkeypatch, tmp_path):
    """Ripiego: cache di formato vecchio (senza `remaining_ts`) ordinate
    per `ts`, per non perdere la telemetria dei crediti."""
    import json
    import time
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    now = time.time()
    (tmp_path / "toa_a.json").write_text(json.dumps(
        {"ts": now - 3600, "payload": [], "remaining": 10}))
    (tmp_path / "toa_b.json").write_text(json.dumps(
        {"ts": now, "payload": [], "remaining": 33}))
    assert odds_api.get_remaining() == 33
