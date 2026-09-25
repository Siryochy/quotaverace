"""Test OFFLINE della corsia multi-mercato OU/AH (`multi_market.py`).

Nessuna rete, nessun provider reale, nessuna credenziale, **zero crediti**:
SQLite temporaneo (monkeypatch di `tracker.DB_PATH`), order book FINTO e
provider FINTO. Copre:

1. i **formati**: linea, esito di ledger ('Over 2.5' / 'Home -0.75'), inversa
   per l'ordine (`sx_line_of_esito`) e bersaglio d'ordine (`order_target`);
2. il **modello push-aware**: OU con push sulle linee intere, AH quarter,
   e il settlement di `tracker` che legge la LINEA dall'esito;
3. l'**analisi** (`analyze_fixture`): devig, blend, EV esatto, scelta del lato
   favorito, rifiuto per libro sottile;
4. il **ledger**: le righe del contratto finiscono in `market_quotes` e i
   candidati in `predictions`;
5. la **corsia ordini** (`live_picks`): AH live / OU shadow, gate di lega,
   fascia quota, fail-closed su lega assente;
6. il **resolver a linea** (`execution_engine.resolve_market_for`): linea
   giusta/linea sbagliata, evento ambiguo, provider non supportato;
7. i **tripwire**: soglie allineate a sx_signals, `ENABLE_LIVE_OU` spento di
   default, nessuna scrittura d'ordine dal modulo.
"""

import sqlite3

import pytest

import multi_market as mm
import tracker
from poisson_engine import ah_outcome_probs, ou_outcome_probs

FIXTURE = "sx-L20067612"
HOME, AWAY = "Cagliari", "Genoa"


@pytest.fixture
def db(monkeypatch, tmp_path):
    """DB temporaneo con lo schema di produzione (creato da tracker)."""
    path = tmp_path / "ledger.db"
    monkeypatch.setattr(tracker, "DB_PATH", path)
    conn = tracker._get_conn()
    conn.close()
    return path


def quote_row(market_type, selection, line, price, *, depth=120.0,
              main_line=True, fixture=FIXTURE):
    """Riga come la restituisce `tracker.get_market_quotes` (dict del ledger)."""
    return {
        "fixture_id": fixture, "market_type": market_type,
        "line_key": mm.line_key(line), "line": line, "selection": selection,
        "selection_label": mm.ledger_esito(market_type, selection, line),
        "ledger_esito": mm.ledger_esito(market_type, selection, line),
        "price": price, "liquidity": depth, "main_line": main_line,
        "origin": "native", "home": HOME, "away": AWAY,
        "kickoff": "2030-01-01T20:00:00Z", "league": "Premier League",
    }


class FakeProvider:
    """Provider SX finto: catalogo iniettabile, nessuna rete."""

    name = "sxbet"

    def __init__(self, markets):
        self._markets = markets
        self.calls = []

    def list_market_catalogue(self, event_type_ids=("5",), market_type="1X2",
                              max_results=20, market_type_ids=None):
        self.calls.append({"types": market_type_ids, "max": max_results})
        return [m for m in self._markets
                if str(m.get("market_type_id")) in tuple(market_type_ids or ())]


class FakeBook:
    """Order book finto nel formato di `sx_signals._book`."""

    def __init__(self, one, two, depth=120.0):
        self._books = {1: {"best": {"price": one}, "depth": depth},
                       2: {"best": {"price": two}, "depth": depth}}

    def get(self, key, default=None):
        return self._books.get(key, default)


# ---------------------------------------------------------------------------
# 1. Formati
# ---------------------------------------------------------------------------

class TestFormati:
    def test_linea_e_esito_ou(self):
        assert mm.ledger_esito("OU", "over", 2.5) == "Over 2.5"
        assert mm.ledger_esito("OU", "under", 3.25) == "Under 3.25"

    def test_esito_ah_dal_punto_di_vista_del_lato(self):
        # La linea SX e' quella di teamOne: l'Away la porta invertita.
        assert mm.ledger_esito("AH", "1", -0.75) == "Home -0.75"
        assert mm.ledger_esito("AH", "2", -0.75) == "Away +0.75"
        assert mm.ledger_esito("AH", "2", 0.25) == "Away -0.25"

    def test_inversa_per_l_ordine(self):
        assert mm.sx_line_of_esito("Home -0.75") == -0.75
        assert mm.sx_line_of_esito("Away +0.75") == -0.75
        assert mm.sx_line_of_esito("Away -0.25") == 0.25
        assert mm.order_target({"mercato": "AH", "esito_key": "Home -0.75"}) == {
            "market_type": "AH", "line": -0.75, "side": "home"}

    def test_order_target_ou_e_fail_closed(self):
        target = mm.order_target({"mercato": "OU", "esito_key": "Under 3.25"})
        assert target == {"market_type": "OU", "line": 3.25, "side": "under"}
        # Mercato non a linea o esito illeggibile -> nessun bersaglio.
        assert mm.order_target({"mercato": "1X2", "esito_key": "1"}) is None
        assert mm.order_target({"mercato": "OU", "esito_key": "Over"}) is None

    def test_linea_zero_e_valida_per_l_ah(self):
        assert mm.parse_line("Home 0") == 0.0
        assert mm.line_key(0.0) == "0"
        # Handicap pari: mai 'Away +-0' (visto in produzione il 19/09).
        assert mm.ledger_esito("AH", "2", 0) == "Away 0"
        assert mm.ledger_esito("AH", "1", 0) == "Home 0"
        assert mm.sx_line_of_esito("Away 0") == 0.0

    def test_outcome_sides(self):
        assert mm.outcome_sides("OU", "Over 2.5", HOME, AWAY) == ("over", "under")
        assert mm.outcome_sides("OU", "Under 2.5", HOME, AWAY) == ("under", "over")
        assert mm.outcome_sides("AH", f"{HOME} -0.75", HOME, AWAY) == ("1", "2")
        assert mm.outcome_sides("AH", AWAY, HOME, AWAY) == ("2", "1")
        assert mm.outcome_sides("OU", "Draw", HOME, AWAY) is None
        assert mm.outcome_sides("AH", "Squadra Ignota", HOME, AWAY) is None


# ---------------------------------------------------------------------------
# 2. Modello push-aware + settlement
# ---------------------------------------------------------------------------

class TestModello:
    def test_ou_senza_push_sulle_mezze_linee(self):
        p_win, p_push, p_lose = ou_outcome_probs(1.6, 1.1, 2.5, "over")
        assert p_push == 0.0
        assert p_win + p_lose == pytest.approx(1.0)

    def test_ou_push_sulla_linea_intera(self):
        p_win, p_push, p_lose = ou_outcome_probs(1.6, 1.1, 3.0, "over")
        assert p_push > 0.0
        assert p_win + p_push + p_lose == pytest.approx(1.0)

    def test_ah_quarter_somma_a_uno(self):
        for side in ("home", "away"):
            p_win, p_push, p_lose = ah_outcome_probs(1.6, 1.1, -0.75, side)
            assert p_win + p_push + p_lose == pytest.approx(1.0)
            assert p_push > 0.0

    def test_settlement_legge_la_linea_dall_esito(self):
        # Over 3.5 con 2 gol -> persa; Over 3 con 3 gol -> PUSH (linea intera).
        assert tracker._prediction_outcome("OU", "Over 3.5", 2.0, 1, 1, HOME, AWAY) \
            == ("lost", -1.0)
        assert tracker._prediction_outcome("OU", "Over 3", 2.0, 2, 1, HOME, AWAY) \
            == ("push", 0.0)
        assert tracker._prediction_outcome("OU", "Over 2.5", 2.0, 2, 1, HOME, AWAY)[0] \
            == "won"
        # Comportamento storico invariato per il 2.5.
        assert tracker._esito_won("Over 2.5", 2, 1) is True
        assert tracker._esito_won("Over 2.5", 2, 0) is False
        assert tracker._esito_won("Under 2.5", 1, 1) is True
        assert tracker._esito_possible("OU", "Over 3", 2, 1) is True


# ---------------------------------------------------------------------------
# 3. Analisi (Poisson + devig + blend + gate)
# ---------------------------------------------------------------------------

class TestAnalisi:
    def test_ou_calcola_ev_e_lato_favorito(self):
        # Modello (lam 2.0/1.4 = Over prob ~0.66) contro mercato che prezza
        # l'Over a 1.75 (fair ~0.57): l'Over e' il favorito E batte il mercato.
        quotes = [quote_row("OU", "over", 2.5, 1.75),
                  quote_row("OU", "under", 2.5, 2.30)]
        cands = mm.analyze_fixture(FIXTURE, 2.0, 1.4, quotes=quotes,
                                   league="Premier League")
        assert len(cands) == 2
        by_sel = {c["selection"]: c for c in cands}
        assert by_sel["over"]["market_prob"] > 0.5
        assert by_sel["over"]["esito_key"] == "Over 2.5"
        assert by_sel["over"]["playable"] is True
        assert by_sel["under"]["playable"] is False
        # EV esatto (niente push sul 2.5).
        over = by_sel["over"]
        assert over["ev"] == pytest.approx(
            over["p_win"] * (1.75 - 1.0) - over["p_lose"])

    def test_libro_sottile_non_e_giocabile(self):
        quotes = [quote_row("OU", "over", 2.5, 1.75, depth=3.0),
                  quote_row("OU", "under", 2.5, 2.30, depth=3.0)]
        cands = mm.analyze_fixture(FIXTURE, 2.0, 1.4, quotes=quotes,
                                   league="Premier League")
        assert cands and all(c["playable"] is False for c in cands)
        assert "liquidita'" in next(c for c in cands if c["selection"] == "over")["reason"]

    def test_ah_quarter_e_linea_dal_punto_di_vista_di_teamone(self):
        # AH -0.75 di casa: la riga 'Away' corrisponde a teamTwo +0.75.
        quotes = [quote_row("AH", "1", -0.75, 1.80),
                  quote_row("AH", "2", -0.75, 2.10)]
        cands = mm.analyze_fixture(FIXTURE, 1.9, 1.1, quotes=quotes,
                                   league="Premier League")
        esiti = {c["esito_key"] for c in cands}
        assert esiti == {"Home -0.75", "Away +0.75"}
        assert all(c["market_prob"] is not None for c in cands)
        assert all(c["p_push"] > 0 for c in cands)

    def test_lega_vietata_non_e_giocabile(self):
        quotes = [quote_row("OU", "over", 2.5, 1.75),
                  quote_row("OU", "under", 2.5, 2.30)]
        cands = mm.analyze_fixture(FIXTURE, 1.9, 1.1, quotes=quotes,
                                   league="Serie A")
        assert cands and all(c["playable"] is False for c in cands)

    def test_linea_oltre_la_fascia_non_e_giocabile(self):
        quotes = [quote_row("OU", "over", 2.5, 2.60),
                  quote_row("OU", "under", 2.5, 1.55)]
        cands = mm.analyze_fixture(FIXTURE, 1.9, 1.1, quotes=quotes,
                                   league="Premier League")
        assert all(c["playable"] is False for c in cands)

    def test_senza_quote_nessun_candidato(self, db):
        assert mm.analyze_fixture(FIXTURE, 1.9, 1.1, league="Premier League") == []


# ---------------------------------------------------------------------------
# 4. Ledger: quote del contratto + previsioni
# ---------------------------------------------------------------------------

class TestLedger:
    def test_build_quote_rows_valida_col_contratto(self):
        records = [{"event_id": "20067612", "league_label": "Premier League",
                    "kickoff_ms": 1893456000000, "home": HOME, "away": AWAY,
                    "market_type": "OU", "line": 2.5, "main_line": True,
                    "market_hash": "HASH-OU", "outcome_one": "Over 2.5",
                    "outcome_two": "Under 2.5"},
                   {"event_id": "20067612", "league_label": "Premier League",
                    "kickoff_ms": 1893456000000, "home": HOME, "away": AWAY,
                    "market_type": "AH", "line": -0.75, "main_line": True,
                    "market_hash": "HASH-AH", "outcome_one": f"{HOME} -0.75",
                    "outcome_two": f"{AWAY} +0.75"}]
        books = {"HASH-OU": FakeBook(1.75, 2.30),
                 "HASH-AH": FakeBook(1.80, 2.10)}
        rows, stats = mm.build_quote_rows(records, books)
        assert stats["built"] == 4 and stats["rejected"] == 0
        by_key = {(r["market_type"], r["selection"]): r for r in rows}
        assert by_key[("OU", "over")]["line_key"] == "2.5"
        assert by_key[("OU", "over")]["odds"] == 1.75
        assert by_key[("AH", "2")]["ledger_esito"] == "Away +0.75"
        assert by_key[("AH", "1")]["line_key"] == "-0.75"

    def test_build_quote_rows_scarta_libro_assente(self):
        records = [{"event_id": "1", "league_label": "X",
                    "kickoff_ms": 1893456000000, "home": HOME, "away": AWAY,
                    "market_type": "OU", "line": 2.5, "main_line": False,
                    "market_hash": "HASH", "outcome_one": "Over 2.5",
                    "outcome_two": "Under 2.5"}]
        rows, stats = mm.build_quote_rows(records, {"HASH": {"error": "boom"}})
        assert rows == [] and stats["no_book"] == 1

    def test_ingest_salva_sul_ledger(self, db, monkeypatch):
        provider = FakeProvider([])
        monkeypatch.setattr(mm, "discover", lambda *a, **k: [{
            "event_id": "20067612", "league_label": "Premier League",
            "kickoff_ms": 1893456000000, "home": HOME, "away": AWAY,
            "market_type": "OU", "line": 2.5, "main_line": True,
            "market_hash": "HASH-OU", "outcome_one": "Over 2.5",
            "outcome_two": "Under 2.5"}])
        monkeypatch.setattr("sx_signals._books_parallel",
                            lambda prov, ids: {"HASH-OU": FakeBook(1.75, 2.30)})
        summary = mm.ingest(provider)
        assert summary["saved"] == 2 and summary["error"] is None
        assert tracker.count_market_quotes() == 2
        # Upsert: un secondo giro non duplica.
        mm.ingest(provider)
        assert tracker.count_market_quotes() == 2

    def test_scan_registra_le_previsioni(self, db, monkeypatch):
        quotes = [quote_row("OU", "over", 2.5, 1.75),
                  quote_row("OU", "under", 2.5, 2.30),
                  quote_row("AH", "1", -0.75, 1.80),
                  quote_row("AH", "2", -0.75, 2.10)]
        monkeypatch.setattr(mm, "ingest", lambda *a, **k: {"saved": 0})
        monkeypatch.setattr(mm, "_fixtures_with_quotes", lambda now: [{
            "id": FIXTURE, "home": HOME, "away": AWAY,
            "commence": "2030-01-01T20:00:00Z", "league": "Premier League"}])
        monkeypatch.setattr(mm, "_quotes_for", lambda fx: quotes)
        monkeypatch.setattr(mm, "_lam_for", lambda i, h, a: (2.0, 1.4))
        saved = mm.scan(ingest_quotes=False)
        assert saved, "attesi segnali giocabili"
        markets = {s["mercato"] for s in saved}
        assert markets <= {"OU", "AH"}
        preds = tracker.get_predictions(limit=50)
        assert preds, "il ledger deve contenere le previsioni multi-mercato"
        assert {p["mercato"] for p in preds} <= {"OU", "AH"}


# ---------------------------------------------------------------------------
# 5. Corsia ordini (interruttori AH live / OU shadow)
# ---------------------------------------------------------------------------

def _seed_prediction(match_id, mercato, esito, quota=1.70, status="value",
                     market_prob=0.58, edge=0.05, ev=0.08, league="Premier League",
                     hours=6):
    """Partita + previsione aperta nel ledger (helper dei test)."""
    from datetime import datetime, timedelta, timezone
    commence = (datetime.now(timezone.utc) + timedelta(hours=hours))
    tracker.save_match(match_id, league, HOME, AWAY,
                       commence.isoformat().replace("+00:00", "Z"))
    tracker.save_prediction(match_id, mercato, esito, quota, 0.6, ev,
                            market_prob=market_prob, market_edge=edge,
                            status=status)


class TestCorsiaOrdini:
    def test_ah_live_e_ou_shadow(self, db, monkeypatch):
        monkeypatch.setattr(mm, "ENABLE_LIVE_AH", True)
        monkeypatch.setattr(mm, "ENABLE_LIVE_OU", False)
        _seed_prediction(FIXTURE, "AH", "Home -0.75")
        _seed_prediction("sx-2", "OU", "Over 2.5")
        picks = mm.live_picks()
        assert [p["mercato"] for p in picks] == ["AH"]
        assert picks[0]["order_side"] == "home"
        assert picks[0]["market_line"] == -0.75

    def test_ou_live_solo_col_suo_interruttore(self, db, monkeypatch):
        monkeypatch.setattr(mm, "ENABLE_LIVE_AH", False)
        monkeypatch.setattr(mm, "ENABLE_LIVE_OU", True)
        _seed_prediction(FIXTURE, "OU", "Under 3.25")
        picks = mm.live_picks()
        assert [p["mercato"] for p in picks] == ["OU"]
        assert picks[0]["order_side"] == "under"

    def test_nessuna_corsia_accesa_nessun_pick(self, db, monkeypatch):
        monkeypatch.setattr(mm, "ENABLE_LIVE_AH", False)
        monkeypatch.setattr(mm, "ENABLE_LIVE_OU", False)
        _seed_prediction(FIXTURE, "AH", "Home -0.75")
        assert mm.live_picks() == []

    def test_gate_lega_e_fascia_ripetuti(self, db, monkeypatch):
        monkeypatch.setattr(mm, "ENABLE_LIVE_AH", True)
        monkeypatch.setattr(mm, "ENABLE_LIVE_OU", False)
        _seed_prediction(FIXTURE, "AH", "Home -0.75", league="Serie A")
        assert mm.live_picks() == []
        # Quota fuori fascia (sopra ODDS_MAX): nessun ordine.
        _seed_prediction("sx-3", "AH", "Away +0.75", quota=2.60)
        assert mm.live_picks() == []

    def test_lega_assente_fail_closed(self, db, monkeypatch):
        monkeypatch.setattr(mm, "ENABLE_LIVE_AH", True)
        monkeypatch.setattr(mm, "ENABLE_LIVE_OU", False)
        _seed_prediction(FIXTURE, "AH", "Home -0.75", league="")
        assert mm.live_picks() == []

    def test_order_target_usato_per_il_lato(self, db, monkeypatch):
        monkeypatch.setattr(mm, "ENABLE_LIVE_AH", True)
        monkeypatch.setattr(mm, "ENABLE_LIVE_OU", False)
        _seed_prediction(FIXTURE, "AH", "Away +0.75")
        pick = mm.live_picks()[0]
        # 'Away +0.75' -> linea SX -0.75 (punto di vista di teamOne).
        assert pick["market_line"] == -0.75 and pick["order_side"] == "away"


# ---------------------------------------------------------------------------
# 6. Resolver a linea (execute su SX reale) — provider FINTO
# ---------------------------------------------------------------------------

def catalogue(market_type_id, line, outcome_one, *, home=HOME, away=AWAY,
             open_date="2030-01-01T20:00:00", market_id="M1"):
    return {"market_id": market_id, "market_type_id": market_type_id,
            "line": line, "outcome_one_name": outcome_one,
            "team_one_name": home, "team_two_name": away,
            "open_date": open_date}


class TestResolverALinea:
    def test_ah_lato_home_e_away(self):
        import execution_engine as ee
        prov = FakeProvider([catalogue("3", -0.75, f"{HOME} -0.75")])
        kick = "2030-01-01T20:00:00Z"
        m = ee.resolve_market_for(prov, HOME, AWAY, "AH", -0.75, "home", kick)
        assert m and m["selection_id"] == 1 and m["line"] == -0.75
        m = ee.resolve_market_for(prov, HOME, AWAY, "AH", -0.75, "away", kick)
        assert m and m["selection_id"] == 2

    def test_ah_linea_sbagliata_fail_closed(self):
        import execution_engine as ee
        prov = FakeProvider([catalogue("3", -0.75, f"{HOME} -0.75")])
        assert ee.resolve_market_for(prov, HOME, AWAY, "AH", -1.25, "home",
                                     "2030-01-01T20:00:00Z") is None

    def test_ou_over_e_under(self):
        import execution_engine as ee
        prov = FakeProvider([catalogue("2", 2.5, "Over 2.5")])
        kick = "2030-01-01T20:00:00Z"
        assert ee.resolve_market_for(prov, HOME, AWAY, "OU", 2.5, "over",
                                     kick)["selection_id"] == 1
        assert ee.resolve_market_for(prov, HOME, AWAY, "OU", 2.5, "under",
                                     kick)["selection_id"] == 2

    def test_evento_ambiguo_fail_closed(self):
        import execution_engine as ee
        prov = FakeProvider([
            catalogue("2", 2.5, "Over 2.5", open_date="2030-01-01T20:00:00", market_id="A"),
            catalogue("2", 2.5, "Over 2.5", open_date="2030-01-02T20:00:00", market_id="B")])
        # Due eventi con le stesse squadre ma kickoff diversi: ambiguo.
        prov._markets[0]["line"] = 2.5
        prov._markets[0]["outcome_one_name"] = "Over 2.5"
        assert ee.resolve_market_for(prov, HOME, AWAY, "OU", 2.5, "over",
                                     "2030-01-01T20:00:00Z", window_hours=96) is None

    def test_provider_non_supportato(self):
        import execution_engine as ee

        class Altro:
            name = "smarkets"

        assert ee.resolve_market_for(Altro(), HOME, AWAY, "AH", -0.75, "home") is None


# ---------------------------------------------------------------------------
# 7. Tripwire
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_soglie_allineate_a_sx_signals(self):
        import sx_signals
        assert mm.MIN_DEPTH_USDC == sx_signals.MIN_DEPTH_USDC
        assert mm.MIN_LEG_DEPTH_USDC == sx_signals.MIN_LEG_DEPTH_USDC
        assert mm.MIN_EXEC_DEPTH_USDC == sx_signals.MIN_EXEC_DEPTH_USDC

    def test_ou_spento_di_default(self):
        assert mm._env_flag("ENABLE_LIVE_OU", False) is False
        assert mm._env_flag("ENABLE_LIVE_AH", True) is True

    def test_etichetta_lega_risolta_in_discovery(self):
        """`discover` deve risolvere l'etichetta SX nella chiave della
        strategia: `Major League Soccer` -> `MLS`.

        Il difetto del 24/09/2026: questa corsia salvava l'etichetta GREZZA
        del provider, quindi il gate di lega la leggeva come vietata e
        scartava candidati con EV +52% ed edge +9.5pp con "ROI negativo" —
        un falso divieto su una lega in PROBATION (il resolver esisteva gia'
        ed era corretto: non veniva usato qui).
        """
        import value_filter as vf
        assert mm._resolve_league_label("Major League Soccer") == "MLS"
        assert vf.league_allowed(mm._resolve_league_label("Major League Soccer"))
        # lega sconosciuta: resta se stessa (mai un nome inventato)
        assert mm._resolve_league_label("Lega Fantasma") == "Lega Fantasma"
        assert mm._resolve_league_label("") == ""
        # lega vietata: nessuna scorciatoia del resolver
        assert not vf.league_allowed(mm._resolve_league_label("Serie A"))

    def test_discover_usa_la_risoluzione_della_lega(self):
        """Tripwire sul percorso REALE: la risoluzione sta in `discover`."""
        import pathlib
        src = pathlib.Path(mm.__file__).read_text()
        assert '"league_label": _resolve_league_label(' in src
        assert 'm.get("leagueLabel") or ""' not in src

    def test_nessuna_scrittura_sql_diretta(self):
        import pathlib
        src = pathlib.Path(mm.__file__).read_text()
        for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
            assert verb not in src, f"scrittura SQL diretta: {verb}"

    def test_auto_bet_usa_il_resolver_a_linea(self):
        import pathlib
        import auto_bet
        src = pathlib.Path(auto_bet.__file__).read_text()
        assert "resolve_market_for" in src
        assert "_multi_market_picks" in src


# ---------------------------------------------------------------------------
# 8. Report shadow: split giocabili / scartati
# ---------------------------------------------------------------------------

def _seed(path, market, status, verdicts, *, profit=None):
    """Salva previsioni per `market` con `status` e le chiude coi verdetti dati.

    `verdicts`: lista di "won"/"lost"/"push" (chiuse) o None (ancora aperta).
    `profit`: P/L per unita' di stake sulle chiuse; default +0.75 / -1.0 / 0.0.
    """
    defaults = {"won": 0.75, "lost": -1.0, "push": 0.0}
    ids = [f"{market}-{status}-{i}" for i in range(1, len(verdicts) + 1)]
    # Fase 1: `tracker` apre e chiude la PROPRIA connessione a ogni salvataggio,
    # quindi la nostra non deve restare in transazione mentre lui scrive.
    for mid in ids:
        tracker.save_prediction(mid, market, "Over 2.5", 1.75, 0.6, 0.05,
                                status=status)
    # Fase 2: chiusura dei verdetti, in una sola transazione.
    conn = sqlite3.connect(path)
    for mid, verdict in zip(ids, verdicts):
        if verdict is None:
            continue
        pl = defaults.get(verdict, 0.0) if profit is None else profit
        conn.execute("UPDATE predictions SET esito_finale=?, profit=? "
                     "WHERE match_id=? AND mercato=?", (verdict, pl, mid, market))
    conn.commit()
    conn.close()


class TestSplitReport:
    """Il P/L separato per stato: giocabili e scartati NON si sommano.

    Sommare le due popolazioni produce un ROI che non corrisponde a nessuna
    strategia (mescola cio' che sarebbe stato giocato con cio' che i gate
    hanno rifiutato): qui il comportamento e' fissato da un test.
    """

    def test_giocabili_e_scartati_separati(self, db):
        _seed(db, "OU", "rejected", ["won"] * 10 + ["lost"] * 10)
        _seed(db, "OU", "value", ["won", "won", "lost"])
        ou = mm.shadow_report()["markets"]["OU"]
        assert ou["playable"]["closed"] == 3
        assert ou["playable"]["won"] == 2 and ou["playable"]["lost"] == 1
        assert ou["playable"]["profit"] == pytest.approx(0.5, abs=1e-6)
        assert ou["playable"]["roi"] == pytest.approx(0.5 / 3, abs=1e-4)
        assert ou["rejected"]["closed"] == 20
        assert ou["rejected"]["profit"] == pytest.approx(-2.5, abs=1e-6)
        assert ou["rejected"]["roi"] == pytest.approx(-0.125, abs=1e-4)

    def test_il_totale_non_e_una_strategia(self, db):
        _seed(db, "OU", "rejected", ["lost"] * 20)
        _seed(db, "OU", "value", ["won"] * 3)
        ou = mm.shadow_report()["markets"]["OU"]
        assert ou["closed"] == 23
        assert ou["roi"] != ou["playable"]["roi"]
        assert ou["roi"] < 0 < ou["playable"]["roi"]

    def test_i_tre_tier_giocabili_confluiscono(self, db):
        _seed(db, "OU", "value", ["won"])
        _seed(db, "OU", "strong_value", ["won"])
        _seed(db, "OU", "moderate", ["won"])
        ou = mm.shadow_report()["markets"]["OU"]
        assert ou["playable"]["closed"] == 3
        assert set(ou["by_status"]) >= {"value", "strong_value", "moderate"}

    def test_dettaglio_per_singolo_tier(self, db):
        _seed(db, "AH", "strong_value", ["won", "won"])
        _seed(db, "AH", "value", ["lost"])
        by_status = mm.shadow_report()["markets"]["AH"]["by_status"]
        assert by_status["strong_value"]["closed"] == 2
        assert by_status["strong_value"]["roi"] == pytest.approx(0.75, abs=1e-4)
        assert by_status["value"]["roi"] == pytest.approx(-1.0, abs=1e-6)

    def test_aperte_fuori_dal_roi(self, db):
        _seed(db, "OU", "value", ["won", None, None])
        play = mm.shadow_report()["markets"]["OU"]["playable"]
        assert play["open"] == 2
        assert play["closed"] == 1
        assert play["roi"] == pytest.approx(0.75, abs=1e-4)

    def test_stato_ignoto_non_sparisce(self, db):
        _seed(db, "OU", "stato_boh", ["won"])
        ou = mm.shadow_report()["markets"]["OU"]
        assert ou["unclassified"]["closed"] == 1
        assert ou["playable"]["closed"] == 0
        assert ou["rejected"]["closed"] == 0
        assert ou["closed"] == 1        # il totale resta completo

    def test_verdetto_inatteso_contato_a_parte(self, db):
        _seed(db, "OU", "value", ["won", "annullata"])
        play = mm.shadow_report()["markets"]["OU"]["playable"]
        assert play["closed"] == 2
        assert play["other"] == 1
        assert play["won"] + play["lost"] + play["push"] == 1

    def test_campione_piccolo_e_dichiarato_rumore(self, db):
        _seed(db, "OU", "value", ["won"] * (mm.MIN_RELIABLE_CLOSED - 1))
        ou = mm.shadow_report()["markets"]["OU"]
        assert ou["playable"]["reliable"] is False
        assert f"campione < {mm.MIN_RELIABLE_CLOSED}" in mm.format_report(
            mm.shadow_report())

    def test_campione_pieno_e_dichiarato_affidabile(self, db):
        _seed(db, "OU", "value", ["won"] * mm.MIN_RELIABLE_CLOSED)
        play = mm.shadow_report()["markets"]["OU"]["playable"]
        assert play["reliable"] is True
        assert play["closed"] == mm.MIN_RELIABLE_CLOSED

    def test_soglia_affidabilita_coerente_con_il_progetto(self):
        # Stessa soglia di league_gate_impact: il progetto non deve avere due
        # idee diverse di "campione affidabile".
        import league_gate_impact as lgi
        assert mm.MIN_RELIABLE_CLOSED == lgi.MIN_RELIABLE_CLOSED

    def test_tripla_dei_giocabili_una_sola_definizione(self):
        """La tripla vive in `value_filter`: qui e' solo un alias.

        Ricopiarla e' il modo silenzioso di far divergere due misure (un tier
        nuovo conterebbe come giocabile nel report e non nell'ordine).
        """
        import pathlib
        from value_filter import PLAYABLE_TIERS
        assert mm.PLAYABLE_STATUSES == ("value", "strong_value", "moderate")
        assert mm.PLAYABLE_STATUSES is PLAYABLE_TIERS      # alias, non copia
        # Nessuna delle due fonti ricopia la tupla a mano.
        for mod in (mm, __import__("value_filter")):
            src = pathlib.Path(mod.__file__).read_text()
            assert src.count('"value", "strong_value"') <= 1

    def test_report_non_scrive_nel_ledger(self, db):
        _seed(db, "OU", "value", ["won"])
        conn = sqlite3.connect(db)
        before = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        conn.close()
        mm.shadow_report()
        mm.format_report()
        conn = sqlite3.connect(db)
        after = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        conn.close()
        assert before == after

    def test_db_assente_non_solleva(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "assente" / "x.db")
        rep = mm.shadow_report()
        assert rep["markets"]["OU"]["closed"] == 0
        assert rep["markets"]["AH"]["playable"]["roi"] is None

    def test_format_mai_un_eccezione_su_input_strano(self, db):
        assert "MULTI-MERCATO" in mm.format_report("spazzatura")
        assert "MULTI-MERCATO" in mm.format_report({"markets": {}, "live_markets": []})
        assert "non leggibili" in mm.format_report(
            {"markets": {"OU": "spazzatura"}, "live_markets": None})


# ---------------------------------------------------------------------------
# 9. Sorveglianza GRATUITA del mercato BTTS (type 17) — 25/09/2026
# ---------------------------------------------------------------------------

class FakeProbeProvider:
    """Provider minimo per il probe: SOLO `_get`, nessuna rete.

    Registra le chiamate cosi' un test puo' dimostrare che il probe legge
    l'endpoint PUBBLICO e non porta con se' chiavi (quindi: zero crediti e
    zero credenziali).
    """

    name = "sxbet"

    def __init__(self, markets=None, exc=None, payload=None):
        self._markets = list(markets or [])
        self._exc = exc
        self._payload = payload
        self.calls = []

    def _get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if self._exc is not None:
            raise self._exc
        if self._payload is not None:
            return self._payload
        return {"data": {"markets": list(self._markets), "nextKey": None}}


def _sx_market(**over):
    m = {"sportXeventId": "E1", "marketHash": "H1", "teamOneName": "Roma",
         "teamTwoName": "Lazio", "outcomeOneName": "Yes",
         "outcomeTwoName": "No", "leagueLabel": "Serie A",
         "gameTime": 1900000000000}
    m.update(over)
    return m


class TestSorveglianzaBTTS:
    """Il backlog BTTS e' congelato: qui si fissa SOLO il campanello.

    Nessun feed a pagamento, nessuno dei 10 punti di refactoring: il type 17
    non e' pubblicato su SX (0 mercati), quindi la sorveglianza deve essere
    gratuita, silenziosa e fail-safe.
    """

    def test_zero_mercati_non_disponibile(self):
        p = FakeProbeProvider(markets=[])
        out = mm.probe_market_type("17", provider=p)
        assert out["available"] is False
        assert out["markets"] == 0
        assert out["error"] is None

    def test_mercati_presenti_disponibile(self):
        p = FakeProbeProvider(markets=[_sx_market(), _sx_market()])
        out = mm.probe_market_type("17", provider=p)
        assert out["available"] is True
        assert out["markets"] == 2
        assert out["example"]["event"] == "Roma - Lazio"
        assert out["example"]["outcome_one"] == "Yes"

    def test_legge_solo_lendpoint_pubblico_senza_chiavi(self):
        """Gratuito per costruzione: `/markets/active` e nessun `apiKey`."""
        p = FakeProbeProvider(markets=[_sx_market()])
        mm.probe_market_type("17", provider=p)
        assert p.calls, "il probe non ha interrogato SX"
        path, params = p.calls[0]
        assert path == "markets/active"
        assert "apiKey" not in params and "key" not in params
        assert params.get("type") == "17"

    def test_fail_safe_su_errore_di_rete(self):
        p = FakeProbeProvider(exc=RuntimeError("boom"))
        out = mm.probe_market_type("17", provider=p)      # mai un'eccezione
        assert out["available"] is False
        assert "boom" in out["error"]

    def test_fail_safe_su_payload_ostile(self):
        for payload in ({"data": 5}, 5, None, {"data": {"markets": "x"}}):
            p = FakeProbeProvider(payload=payload)
            out = mm.probe_market_type("17", provider=p)
            assert out["available"] is False          # mai un'eccezione
            assert isinstance(out["markets"], int)

    def test_btts_congelato_non_e_un_mercato_del_modulo(self):
        """Il probe NON deve riaprire il backlog: BTTS resta fuori da MARKETS."""
        assert mm.WATCHED_TYPES == {"BTTS": "17"}
        assert "BTTS" not in mm.MARKETS
        assert "BTTS" not in mm.SX_TYPE_IDS
        assert "BTTS" not in mm.live_markets()

    def test_tipo_sorvegliato_coerente_col_registro_del_contratto(self):
        """Il type id sorvegliato e' quello ufficiale del registro SX."""
        from decision.market import MarketType, SX_TYPE_IDS
        assert SX_TYPE_IDS[17] is MarketType.BOTH_TEAMS_TO_SCORE

    def test_probe_watched_e_format(self):
        probes = mm.probe_watched_markets(
            provider=FakeProbeProvider(markets=[_sx_market()]))
        assert len(probes) == 1 and probes[0]["market"] == "BTTS"
        assert "DISPONIBILE" in mm.format_probe(probes)
        vuoto = mm.probe_watched_markets(provider=FakeProbeProvider())
        assert "non disponibile" in mm.format_probe(vuoto)
        assert mm.format_probe([]) == "nessun mercato sorvegliato"

    def test_nessun_credito_the_odds_api(self):
        """Zero costi: il modulo non chiama MAI il settlement/quote a pagamento."""
        import pathlib
        src = pathlib.Path(mm.__file__).read_text()
        assert "fetch_scores" not in src
        assert "odds_api" not in src

    def test_job_schedulato_in_bot(self):
        import pathlib
        src = pathlib.Path("bot.py").read_text(encoding="utf-8")
        assert "async def btts_watch_job" in src
        assert "run_repeating(btts_watch_job" in src
        assert "probe_watched_markets" in src

    def test_cli_espone_il_probe(self):
        import pathlib
        src = pathlib.Path(mm.__file__).read_text()
        assert '"btts"' in src
