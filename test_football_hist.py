"""
Test unitari per la sincronizzazione risultati storici (API-Football).
"""

import sqlite3

import pytest

import tracker
import football_hist as fh


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "hist.db")
    tracker.init_db()
    # Il memo in-process delle stagioni accessibili non deve attraversare i
    # test (ogni test parte dall'anno corrente).
    fh.reset_sync_state()
    # Throttle del rate limit DISATTIVATO nei test: altrimenti ogni chiamata
    # dopo la prima aspetterebbe 6.5s reali (il default di produzione).
    monkeypatch.setenv("API_FOOTBALL_MIN_INTERVAL", "0")
    fh.reset_throttle()
    yield
    fh.reset_sync_state()
    fh.reset_throttle()


def _fixture(status="FT", home="Roma", away="Empoli", gh=2, ga=0, fxid=1001,
             api_league=None, api_country=None):
    fx = {
        "fixture": {"id": fxid, "date": "2026-08-30T18:00:00Z",
                    "status": {"short": status}},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": gh, "away": ga},
    }
    if api_league is not None:
        fx["league"] = {"name": api_league, "country": api_country or ""}
    return fx


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
    def raise_for_status(self):
        return None
    def json(self):
        return self._payload


def _stub_requests(monkeypatch, payload):
    """Installa uno stub di requests.get che ritorna il payload dato."""
    def fake_get(url, params=None, headers=None, timeout=None):
        return _Resp({"results": len(payload), "response": payload})
    monkeypatch.setattr(fh.requests, "get", fake_get)


class TestMatchDbName:
    def test_match_esatto(self):
        assert fh._match_db_name("Roma", "Serie A") == "Roma"

    def test_squadra_assente(self):
        assert fh._match_db_name("Squadra Inventata", "Serie A") is None

    def test_fallback_globale_squadra_promossa(self):
        # Parma vive in Serie B nel DB, ma API la restituisce in Serie A
        assert fh._match_db_name("Parma", "Serie A") == "Parma"

    def test_alias_da_api_minuscolo(self):
        assert fh._match_db_name("hellas verona", "Serie A") == "Verona"


class TestParseFixture:
    def test_fixture_valida(self):
        mid, home, away, sh, sa, date = fh._parse_fixture(_fixture(), "Serie A")
        assert home == "Roma" and away == "Empoli"
        assert (sh, sa) == (2, 0)
        assert mid is not None

    def test_match_id_da_fixture_non_top_level(self):
        fx = _fixture()
        fx["id"] = None  # id top-level assente: deve prendere fixture.id
        mid, *_ = fh._parse_fixture(fx, "Serie A")
        assert mid == 1001

    def test_senza_goal_ritorna_none(self):
        fx = _fixture(); fx["goals"] = {"home": None, "away": None}
        assert fh._parse_fixture(fx, "Serie A") is None

    def test_squadre_non_allineate(self):
        fx = _fixture(home="Sconosciuta", away="Ignota")
        assert fh._parse_fixture(fx, "Serie A") is None


class TestSeasonStatus:
    def test_ok_senza_errori(self):
        assert fh._season_status({"errors": [], "response": [1]}) == "ok"

    def test_skip_errore_plan(self):
        assert fh._season_status({"errors": {"plan": "Free plans do not have access"}}) == "skip"

    def test_retry_body_none(self):
        assert fh._season_status(None) == "retry"

    def test_retry_altri_errori(self):
        # errore di pagina o altro: transitorio, va ritentato
        assert fh._season_status({"errors": {"page": "The Page field do not exist"}}) == "retry"


class TestSyncHistory:
    def test_niente_key_restituisce_errore(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        assert res == {"error": "API_FOOTBALL_KEY mancante"}

    def test_salva_risultato(self, monkeypatch, tmp_path):
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        _stub_requests(monkeypatch, [_fixture()])  # Roma 2-0, FT

        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        assert res["_total"] == 1

        conn = tracker._get_conn()
        cnt = conn.cursor().execute("SELECT COUNT(*) FROM match_results").fetchone()[0]
        conn.close()
        assert cnt == 1

    def test_ignora_partite_non_finite(self, monkeypatch):
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        _stub_requests(monkeypatch, [_fixture(status="NS")])
        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        assert res["_total"] == 0
        assert res.get("Serie A") == 0

    def test_salva_piu_partite_una_stagione(self, monkeypatch):
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        fixtures = [_fixture(fxid=str(i), gh=1, ga=0) for i in range(20)]
        _stub_requests(monkeypatch, fixtures)
        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        assert res["_total"] == 20

    def test_retry_transitorio_poi_ok(self, monkeypatch):
        """Un paio di errori transitori sulla stessa stagione NON devono far
        saltare la stagione: si ritenta e si prosegue appena l'API risponde."""
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        monkeypatch.setattr(fh.time, "sleep", lambda *a, **k: None)
        calls = {"n": 0}

        def flaky(path, params=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                return None  # _season_status(None) == "retry"
            return {"results": 1, "response": [_fixture()]}

        monkeypatch.setattr(fh, "_api_get", flaky)
        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        assert res["_total"] == 1

    def test_retry_persistente_termina_senza_bloccare(self, monkeypatch):
        """Errore persistente (body None = 'retry' per TUTTE le stagioni): il
        loop DEVE terminare. Regressione del bug 08/09/2026: il ramo 'retry'
        faceva continue all'infinito sulla stessa stagione (Serie A 2024),
        loggando a raffica e bloccando il job. Con MAX_YEAR_RETRIES ogni
        stagione viene provata al piu' MAX_YEAR_RETRIES+1 volte e poi
        scartata passando all'anno precedente."""
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        monkeypatch.setattr(fh.time, "sleep", lambda *a, **k: None)
        calls = []

        def never_ok(path, params=None):
            calls.append(dict(params or {}))
            return None  # _season_status(None) == "retry"

        monkeypatch.setattr(fh, "_api_get", never_ok)
        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        # Nessun risultato salvato, nessuna eccezione: il job e' uscito.
        assert res["_total"] == 0
        assert res["Serie A"] == 0
        # Chiamate limitate: (retry max + 1 tentativo che fa scattare lo skip)
        # per ogni anno dal corrente fino a 2018 (incluso).
        years = fh.time.localtime().tm_year - 2018 + 1
        assert len(calls) <= years * (fh.MAX_YEAR_RETRIES + 1)
        # ... e nessuna stagione e' stata tentata piu' del previsto.
        assert all(calls.count(s) <= fh.MAX_YEAR_RETRIES + 1 for s in set(
            (c.get("league"), c.get("season")) for c in calls))


class TestRunSync:
    def test_senza_key(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        text = fh.run_sync()
        assert "mancante" in text.lower()


# ---------------------------------------------------------------------------
# Copertura leghe estesa (11/09/2026): il modello era cieco sulle leghe che
# SX Bet scansiona ma che non erano mai state sincronizzate.
# ---------------------------------------------------------------------------

class TestCoperturaLeghe:
    def test_leghe_sx_ora_coperte(self):
        for lg in ("Champions League", "Europa League", "Conference League",
                   "Primeira Liga", "Liga MX", "Serie B", "J1 League",
                   "K League 1", "Allsvenskan", "Eliteserien",
                   "Argentina Primera", "Copa Libertadores"):
            assert lg in fh.LEAGUE_IDS, f"{lg} non sincronizzata"

    def test_ogni_lega_sincronizzabile_ha_un_roster(self):
        """Tripwire (11/09/2026): sincronizzare una lega SENZA roster in
        `ALL_LEAGUES` brucia richieste API e salva 0 righe (`_match_db_name`
        non allinea nessuna squadra). Ogni id deve avere il suo roster."""
        from leagues_data import ALL_LEAGUES
        senza = [lg for lg in fh.LEAGUE_IDS if not ALL_LEAGUES.get(lg)]
        assert senza == []

    def test_leghe_dei_buchi_ora_in_all_leagues(self):
        """I roster aggiunti l'11/09 chiudono i buchi di copertura emersi dal
        report sul container (leghe SX senza elenco squadre)."""
        from leagues_data import ALL_LEAGUES
        for lg in ("Austrian Bundesliga", "Russian Premier League",
                   "Turkey Super Lig", "Belgian First Div",
                   "Scottish Premiership", "Greek Super League",
                   "Polish Ekstraklasa", "Sweden Superettan",
                   "Brazil Serie B", "Copa Sudamericana", "League One",
                   "3. Liga"):
            assert ALL_LEAGUES.get(lg), f"{lg}: roster assente"
            assert lg in fh.LEAGUE_IDS, f"{lg}: id API assente"

    def test_ogni_id_ha_una_validazione(self):
        for lg in fh.LEAGUE_IDS:
            assert lg in fh.LEAGUE_API

    def test_id_unici(self):
        ids = list(fh.LEAGUE_IDS.values())
        assert len(ids) == len(set(ids))


class TestRateLimitThrottle:
    """Il piano Free ha 10 richieste/MINUTO: senza distanziamento un burst
    (es. `--verify-ids` su 41 leghe) prende 429 a raffica e brucia richieste
    nei retry (osservato in produzione il 12/09/2026: 9/41 verificate)."""

    def test_default_e_override_env(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_MIN_INTERVAL", raising=False)
        assert fh.api_min_interval() == fh.MIN_INTERVAL_SECONDS
        monkeypatch.setenv("API_FOOTBALL_MIN_INTERVAL", "0")
        assert fh.api_min_interval() == 0.0
        monkeypatch.setenv("API_FOOTBALL_MIN_INTERVAL", "non-numerico")
        assert fh.api_min_interval() == fh.MIN_INTERVAL_SECONDS

    def test_interval_zero_non_aspetta(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(fh.time, "sleep", lambda s: sleeps.append(s))
        fh.reset_throttle()
        fh._throttle()
        fh._throttle()
        assert sleeps == []

    def test_seconda_chiamata_viene_distanziata(self, monkeypatch):
        sleeps = []
        clock = [1000.0]
        monkeypatch.setenv("API_FOOTBALL_MIN_INTERVAL", "0.5")
        monkeypatch.setattr(fh.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(fh.time, "sleep", lambda s: sleeps.append(s))
        fh.reset_throttle()
        fh._throttle()                 # prima chiamata: nessuna attesa
        fh._throttle()                 # seconda: deve aspettare l'intervallo
        assert sleeps == [0.5]

    def test_burst_verify_ids_distanziato(self, monkeypatch):
        """`_api_get` distanzia OGNI tentativo: niente raffica di 429."""
        monkeypatch.setenv("API_FOOTBALL_MIN_INTERVAL", "2")
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        sleeps = []
        clock = [500.0]
        monkeypatch.setattr(fh.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(fh.time, "sleep", lambda s: sleeps.append(s))
        _stub_requests(monkeypatch, [_fixture()])
        fh.reset_throttle()
        for _ in range(3):
            fh._api_get("fixtures", {"league": 135, "season": 2024})
        assert sleeps == [2, 2]        # 3 chiamate -> 2 attese


class TestNomiApiVerificati:
    """Attese id<->nome confermate con chiamate reali all'API il 12/09/2026.
    Un'attesa sbagliata NON e' un errore visibile: `_league_response_ok`
    scarta la lega in silenzio e la sync importa 0 righe."""

    def test_la_liga(self):
        fx = [_fixture(api_league="La Liga", api_country="Spain")]
        assert fh._league_response_ok(fx, "La Liga") is True

    def test_turkey_super_lig_con_dieresi(self):
        fx = [_fixture(api_league="S\u00fcper Lig", api_country="Turkey")]
        assert fh._league_response_ok(fx, "Turkey Super Lig") is True

    def test_argentina_liga_profesional(self):
        fx = [_fixture(api_league="Liga Profesional Argentina",
                       api_country="Argentina")]
        assert fh._league_response_ok(fx, "Argentina Primera") is True

    def test_paese_sbagliato_blocca_ancora(self):
        fx = [_fixture(api_league="Primera Divisi\u00f3n", api_country="Chile")]
        assert fh._league_response_ok(fx, "Argentina Primera") is False


class TestValidazioneId:
    def test_nome_e_paese_corretti(self):
        fx = [_fixture(api_league="Serie A", api_country="Italy")]
        assert fh._league_response_ok(fx, "Serie A") is True

    def test_nome_diverso_blocca(self):
        fx = [_fixture(api_league="Eredivisie", api_country="Netherlands")]
        assert fh._league_response_ok(fx, "Serie A") is False

    def test_paese_diverso_blocca(self):
        fx = [_fixture(api_league="Serie A", api_country="Brazil")]
        assert fh._league_response_ok(fx, "Serie A") is False

    def test_senza_metadati_non_blocca(self):
        assert fh._league_response_ok([_fixture()], "Serie A") is True
        assert fh._league_response_ok([], "Serie A") is True

    def test_lega_non_in_tabella_non_blocca(self):
        fx = [_fixture(api_league="Qualcosa", api_country="Ovunque")]
        assert fh._league_response_ok(fx, "Lega Non Mappata") is True


class TestSyncNonImportaIdSbagliato:
    def test_id_sbagliato_nessuna_riga(self, monkeypatch):
        """L'id risponde per un'altra competizione: la sync salta (0 righe),
        cosi' un id sbagliato non puo' inquinare i rating."""
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        monkeypatch.setattr(fh.time, "sleep", lambda *a, **k: None)

        def api(path, params=None):
            return {"results": 1, "response": [
                _fixture(api_league="Eredivisie", api_country="Netherlands")]}

        monkeypatch.setattr(fh, "_api_get", api)
        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        assert res["_total"] == 0
        conn = tracker._get_conn()
        try:
            n = conn.cursor().execute(
                "SELECT COUNT(*) FROM match_results").fetchone()[0]
        except sqlite3.OperationalError:
            n = 0            # tabella mai creata: nessun salvataggio
        conn.close()
        assert n == 0
        assert fh._is_synced("Serie A", 1) is False


class TestMarkerSincronizzazione:
    """Il marker (lega, stagione) evita di ri-scaricare ogni giorno decine di
    leghe: il free plan ha 100 richieste al giorno."""

    def test_marker_dopo_sync(self, monkeypatch):
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        monkeypatch.setattr(fh.time, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(fh, "_api_get",
                            lambda p, params=None:
                            {"results": 1, "response": [_fixture()]})
        assert fh._is_synced("Serie A", 1) is False
        fh.sync_history(seasons=1, leagues=["Serie A"])
        assert fh._is_synced("Serie A", 1) is True

    def test_secondo_giro_non_chiama_api(self, monkeypatch):
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        monkeypatch.setattr(fh.time, "sleep", lambda *a, **k: None)
        calls = []
        monkeypatch.setattr(fh, "_api_get",
                            lambda p, params=None: (calls.append(params),
                                                    {"results": 1,
                                                     "response": [_fixture()]})[1])
        fh.sync_history(seasons=1, leagues=["Serie A"])
        first = len(calls)
        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        assert len(calls) == first          # nessuna nuova richiesta
        assert res["_skipped"] == 1 and res["_total"] == 0

    def test_force_history_sync_ignora_i_marker(self, monkeypatch):
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        monkeypatch.setattr(fh.time, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(fh, "_api_get",
                            lambda p, params=None:
                            {"results": 1, "response": [_fixture()]})
        fh.sync_history(seasons=1, leagues=["Serie A"])
        monkeypatch.setenv("FORCE_HISTORY_SYNC", "1")
        assert fh._is_synced("Serie A", 1) is False
        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        assert res["_total"] == 1 and res["_skipped"] == 0

    def test_nessun_marker_se_la_stagione_fallisce(self, monkeypatch):
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        monkeypatch.setattr(fh.time, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(fh, "_api_get", lambda p, params=None: None)
        monkeypatch.setattr(fh, "MAX_YEAR_RETRIES", 0)
        fh.sync_history(seasons=1, leagues=["Serie A"])
        assert fh._is_synced("Serie A", 1) is False


class TestMemoStagioni:
    """Il piano free rifiuta le stagioni recenti: la prima lega scopre il
    limite e le successive partono da li' (2 richieste risparmiate/lega)."""

    def test_le_altre_leghe_partono_dal_limite(self, monkeypatch):
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        monkeypatch.setattr(fh.time, "sleep", lambda *a, **k: None)
        calls = []

        def api(path, params=None):
            calls.append(dict(params or {}))
            year = (params or {}).get("season")
            if year is not None and int(year) >= 2025:
                return {"errors": {"plan": "Free plans do not have access"}}
            return {"results": 1, "response": [_fixture()]}

        monkeypatch.setattr(fh, "_api_get", api)
        res = fh.sync_history(seasons=1, leagues=["Serie A", "Premier League"])
        assert res["_total"] == 2
        assert fh._SYNC_STATE["first_year"] == 2024
        pl_seasons = [c.get("season") for c in calls
                      if c.get("league") == fh.LEAGUE_IDS["Premier League"]]
        assert pl_seasons == [2024]        # una sola richiesta, senza sprechi


class TestVerifyLeagueIds:
    def test_ok_e_mismatch(self, monkeypatch):
        def api(path, params=None):
            lid = (params or {}).get("id")
            if lid == fh.LEAGUE_IDS["Serie A"]:
                return {"response": [{"league": {"name": "Serie A"},
                                      "country": {"name": "Italy"}}]}
            return {"response": [{"league": {"name": "Championship"},
                                  "country": {"name": "England"}}]}

        monkeypatch.setattr(fh, "_api_get", api)
        res = fh.verify_league_ids(["Serie A", "Primeira Liga"])
        assert res["Serie A"]["ok"] is True
        assert res["Primeira Liga"]["ok"] is False
        assert res["Primeira Liga"]["api_name"] == "Championship"

    def test_errore_api_segnalato(self, monkeypatch):
        monkeypatch.setattr(fh, "_api_get",
                            lambda p, params=None:
                            {"errors": {"access": "suspended"}})
        res = fh.verify_league_ids(["Serie A"])
        assert res["Serie A"]["ok"] is False
        assert "suspended" in str(res["Serie A"]["error"])


class TestRunSyncConSkip:
    def test_messaggio_con_leghe_gia_sincronizzate(self, monkeypatch):
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        monkeypatch.setattr(fh, "_is_synced", lambda lg, s: True)
        text = fh.run_sync(1, ["Serie A"])
        assert "gia' sincronizzate" in text
        assert "nessuna lega da sincronizzare" in text