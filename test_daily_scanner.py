"""
Test unitari per daily_scanner.py
=================================

Copertura:
- _day_window: formato ISO UTC con "Z", finestra di 24h, data custom;
- scan_day: aggregazione catalogo per tipo mercato, estrazione miglior
  prezzo back, conteggio eventi/mercati, robustezza (book mancanti,
  runner senza ex, API in errore);
- batching di listMarketBook a blocchi di 200;
- group_same_start: raggruppamento per minuto di kickoff.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from daily_scanner import (
    BETFAIR_BOOKMAKER,
    MARKET_TYPES,
    _day_window,
    _esito_from_selection,
    _split_teams,
    group_same_start,
    scan_day,
    to_odds_records,
)
from betfair_client import BetfairClient


# ---------------------------------------------------------------------------
# Client finto (nessuna chiamata di rete)
# ---------------------------------------------------------------------------

class FakeBetfairClient:
    """Ritorna risposte pre-caricate, senza mai toccare la rete."""

    def __init__(self, catalogue=None, books=None, book_error=False):
        self._catalogue = catalogue or []
        self._books = books or {}
        self._book_error = book_error
        self.catalogue_calls = []
        self.book_calls = []

    def list_market_catalogue(self, market_filter, max_results=200, market_projection=None):
        self.catalogue_calls.append(market_filter)
        # la scan chiede un tipo di mercato per volta
        mtype = market_filter["marketTypeCodes"][0]
        return [m for m in self._catalogue if m["marketType"] == mtype]

    def list_market_book(self, market_ids, price_projection=None):
        self.book_calls.append(list(market_ids))
        if self._book_error:
            raise RuntimeError("API giu'")
        return [self._books[mid] for mid in market_ids if mid in self._books]


def _catalogue_entry(market_id, mtype, event_id="311", event_name="Roma vs Empoli",
                     start="2026-08-31T13:00:00.000Z"):
    return {
        "marketId": market_id,
        "marketType": mtype,
        "marketStartTime": start,
        "event": {"id": event_id, "name": event_name},
        "runners": [
            {"selectionId": 101, "runnerName": "Roma"},
            {"selectionId": 102, "runnerName": "Empoli"},
        ],
    }


def _book_entry(market_id, backs):
    """backs: dict selectionId -> lista di livelli back (price, size)."""
    return {
        "marketId": market_id,
        "runners": [
            {"selectionId": sel, "ex": {"availableToBack": [
                {"price": p, "size": s} for (p, s) in levels
            ]}}
            for sel, levels in backs.items()
        ],
    }


# ---------------------------------------------------------------------------
# _day_window
# ---------------------------------------------------------------------------

class TestDayWindow:

    def test_formato_con_z(self):
        start, end = _day_window("2026-08-31")
        assert start == "2026-08-31T00:00:00Z"
        assert end == "2026-09-01T00:00:00Z"

    def test_finestra_ventiquattro_ore(self):
        start, end = _day_window("2026-08-31")
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        assert (e - s).total_seconds() == 24 * 3600

    def test_default_oggi_utc(self):
        start, end = _day_window()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert start.startswith(today)
        assert end.split("T")[0] in (today, _plus_one_day(today))


def _plus_one_day(iso_date):
    d = datetime.strptime(iso_date, "%Y-%m-%d")
    from datetime import timedelta
    return (d + timedelta(days=1)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# scan_day
# ---------------------------------------------------------------------------

class TestScanDay:

    def test_estruttura_completa(self):
        cat = [
            _catalogue_entry("1.100", "MATCH_ODDS"),
            _catalogue_entry("1.200", "OVER_UNDER_25", event_id="312",
                             event_name="Inter vs Milan",
                             start="2026-08-31T20:45:00.000Z"),
        ]
        books = {
            "1.100": _book_entry("1.100", {101: [(2.5, 100.0)], 102: [(3.4, 80.0)]}),
            "1.200": _book_entry("1.200", {101: [(1.9, 200.0)], 102: [(2.1, 150.0)]}),
        }
        result = scan_day(FakeBetfairClient(catalogue=cat, books=books), "2026-08-31")

        assert result["day"] == "2026-08-31"
        assert result["markets"] == 2
        assert result["events"] == 2
        assert len(result["opportunities"]) == 4

        opp = result["opportunities"][0]
        assert opp["side"] == "BACK"
        assert opp["market_type"] == "MATCH_ODDS"
        assert opp["selection_name"] == "Roma"
        assert opp["price"] == 2.5
        assert opp["price_size"] == 100.0
        assert opp["event_id"] == "311"

    def test_una_chiamata_catalogo_per_mercato(self):
        client = FakeBetfairClient()
        scan_day(client, "2026-08-31")
        assert len(client.catalogue_calls) == len(MARKET_TYPES)
        # ogni filtro richiede UN solo tipo di mercato
        for call in client.catalogue_calls:
            assert len(call["marketTypeCodes"]) == 1

    def test_miglior_prezzo_back_primo_livello(self):
        cat = [_catalogue_entry("1.100", "MATCH_ODDS")]
        books = {"1.100": _book_entry("1.100", {101: [(2.30, 50.0), (2.20, 900.0)]})}
        result = scan_day(FakeBetfairClient(catalogue=cat, books=books), "2026-08-31")
        # il primo livello disponibileToBack e' il miglior prezzo back
        assert result["opportunities"][0]["price"] == 2.30
        assert result["opportunities"][0]["price_size"] == 50.0

    def test_market_senza_book_ignorato(self):
        cat = [
            _catalogue_entry("1.100", "MATCH_ODDS"),
            _catalogue_entry("1.200", "OVER_UNDER_25", event_id="312",
                             event_name="Inter vs Milan"),
        ]
        books = {"1.100": _book_entry("1.100", {101: [(2.5, 100.0)]})}
        result = scan_day(FakeBetfairClient(catalogue=cat, books=books), "2026-08-31")
        assert result["markets"] == 2
        assert len(result["opportunities"]) == 1

    def test_runner_senza_ex_ignorato(self):
        cat = [_catalogue_entry("1.100", "MATCH_ODDS")]
        books = {
            "1.100": {
                "marketId": "1.100",
                "runners": [
                    {"selectionId": 101, "ex": {}},            # niente ex
                    {"selectionId": 102},                       # niente ex del tutto
                    {"selectionId": 103, "ex": {"availableToBack": []}},  # vuoto
                ],
            },
        }
        result = scan_day(FakeBetfairClient(catalogue=cat, books=books), "2026-08-31")
        assert result["opportunities"] == []

    def test_errore_api_book_non_crasha(self):
        cat = [_catalogue_entry("1.100", "MATCH_ODDS")]
        client = FakeBetfairClient(catalogue=cat, book_error=True)
        result = scan_day(client, "2026-08-31")
        # il book fallisce: nessuna opportunita' ma scan completa
        assert result["markets"] == 1
        assert result["opportunities"] == []

    def test_book_batching_a_200(self):
        cat = [_catalogue_entry(f"1.{i}", "MATCH_ODDS", event_id=str(i))
               for i in range(250)]
        books = {m["marketId"]: _book_entry(m["marketId"], {101: [(2.0, 10.0)]})
                 for m in cat}
        client = FakeBetfairClient(catalogue=cat, books=books)
        result = scan_day(client, "2026-08-31")
        # 250 mercati -> due lotti: 200 + 50
        assert len(client.book_calls) == 2
        assert len(client.book_calls[0]) == 200
        assert len(client.book_calls[1]) == 50

    def test_mercati_dello_stesso_evento_contano_un_evento(self):
        cat = [
            _catalogue_entry("1.100", "MATCH_ODDS", event_id="311"),
            _catalogue_entry("1.101", "OVER_UNDER_25", event_id="311"),
        ]
        books = {
            "1.100": _book_entry("1.100", {101: [(2.5, 100.0)]}),
            "1.101": _book_entry("1.101", {101: [(1.8, 100.0)]}),
        }
        result = scan_day(FakeBetfairClient(catalogue=cat, books=books), "2026-08-31")
        assert result["markets"] == 2
        assert result["events"] == 1


# ---------------------------------------------------------------------------
# group_same_start
# ---------------------------------------------------------------------------

class TestGroupSameStart:

    def test_raggruppa_per_minuto(self):
        opps = [
            {"start_time": "2026-08-31T13:00:00Z", "price": 2.0},
            {"start_time": "2026-08-31T13:00:30Z", "price": 1.9},
            {"start_time": "2026-08-31T20:45:00Z", "price": 2.5},
        ]
        groups = group_same_start(opps)
        # 13:00:00 e 13:00:30 cadono nello stesso minuto
        assert len(groups["2026-08-31T13:00"]) == 2
        assert len(groups["2026-08-31T20:45"]) == 1
        assert sum(len(v) for v in groups.values()) == 3

    def test_start_time_mancante_gruppo_vuoto(self):
        opps = [{"start_time": None, "price": 2.0}, {"price": 1.5}]
        groups = group_same_start(opps)
        assert len(groups[""]) == 2


# ---------------------------------------------------------------------------
# Ponte verso odds_ingest / surebet_scanner
# ---------------------------------------------------------------------------

class TestSplitTeams:

    def test_separatori_nota(self):
        assert _split_teams("Roma v Empoli") == ("Roma", "Empoli")
        assert _split_teams("Roma vs Empoli") == ("Roma", "Empoli")
        assert _split_teams("Roma - Empoli") == ("Roma", "Empoli")
        assert _split_teams("Roma – Empoli") == ("Roma", "Empoli")

    def test_nessun_separatore(self):
        assert _split_teams("Roma Empoli") is None
        assert _split_teams("") is None


class TestEsitoFromSelection:

    def test_match_odds_squadre(self):
        assert _esito_from_selection("MATCH_ODDS", "Roma", "Roma v Empoli") == "1"
        assert _esito_from_selection("MATCH_ODDS", "Empoli", "Roma v Empoli") == "2"

    def test_match_odds_pareggio(self):
        for name in ("The Draw", "Draw", "Pareggio"):
            assert _esito_from_selection("MATCH_ODDS", name, "Roma v Empoli") == "X"

    def test_match_odds_case_insensitive(self):
        assert _esito_from_selection("MATCH_ODDS", "roma", "Roma v Empoli") == "1"

    def test_match_odds_nome_sconosciuto_fallback(self):
        # nome non riconosciuto: resta il nome secco del runner
        assert _esito_from_selection("MATCH_ODDS", "Roma U23", "Roma v Empoli") == "Roma U23"

    def test_over_under_pass_through(self):
        assert _esito_from_selection("OVER_UNDER_25", "Over 2.5", "Roma v Empoli") == "Over 2.5"
        assert _esito_from_selection("OVER_UNDER_15", "Under 1.5", "Roma v Empoli") == "Under 1.5"

    def test_btts_pass_through(self):
        assert _esito_from_selection("BOTH_TEAMS_TO_SCORE", "Yes", "Roma v Empoli") == "Yes"

    def test_selezione_vuota(self):
        assert _esito_from_selection("MATCH_ODDS", "", "Roma v Empoli") is None


class TestToOddsRecords:

    def _sample_opportunities(self):
        return [
            {"event_id": "311", "event_name": "Roma v Empoli",
             "market_id": "1.100", "market_type": "MATCH_ODDS",
             "selection_id": 101, "selection_name": "Roma", "side": "BACK",
             "price": 2.10, "price_size": 100.0,
             "start_time": "2026-08-31T13:00:00.000Z"},
            {"event_id": "311", "event_name": "Roma v Empoli",
             "market_id": "1.101", "market_type": "MATCH_ODDS",
             "selection_id": 103, "selection_name": "The Draw", "side": "BACK",
             "price": 3.30, "price_size": 90.0,
             "start_time": "2026-08-31T13:00:00.000Z"},
            {"event_id": "311", "event_name": "Roma v Empoli",
             "market_id": "1.102", "market_type": "OVER_UNDER_25",
             "selection_id": 201, "selection_name": "Over 2.5", "side": "BACK",
             "price": 1.85, "price_size": 70.0,
             "start_time": "2026-08-31T13:00:00.000Z"},
            # scartati: prezzo <= 1.0 e selezione vuota
            {"event_id": "312", "event_name": "Inter v Milan",
             "market_id": "1.200", "market_type": "MATCH_ODDS",
             "selection_id": 110, "selection_name": "Inter", "side": "BACK",
             "price": 0.95, "price_size": 10.0,
             "start_time": "2026-08-31T20:45:00.000Z"},
            {"event_id": "312", "event_name": "Inter v Milan",
             "market_id": "1.201", "market_type": "MATCH_ODDS",
             "selection_id": 111, "selection_name": "", "side": "BACK",
             "price": 2.0, "price_size": 10.0,
             "start_time": "2026-08-31T20:45:00.000Z"},
        ]

    def test_contratto_completo(self):
        rows = to_odds_records(self._sample_opportunities())
        assert len(rows) == 3  # 5 opps - 2 scartate
        for r in rows:
            assert set(r) == {"bookmaker", "evento", "sport", "esito",
                              "quota_decimale", "timestamp"}
            assert r["bookmaker"] == BETFAIR_BOOKMAKER
            assert r["sport"] == "calcio"
            assert r["quota_decimale"] > 1.0

    def test_mappatura_esiti(self):
        rows = to_odds_records(self._sample_opportunities())
        esiti = {r["esito"] for r in rows}
        assert esiti == {"1", "X", "Over 2.5"}

    def test_compatibile_con_odds_ingest(self):
        # il DataFrame del contratto deve avere le colonne richieste e tipi corretti
        import pandas as pd
        from odds_ingest import REQUIRED_COLS

        rows = to_odds_records(self._sample_opportunities())
        df = pd.DataFrame(rows)
        assert list(df.columns) == REQUIRED_COLS
        assert df["quota_decimale"].dtype == "float64"
        parsed = pd.to_datetime(df["timestamp"], utc=True)
        assert not parsed.isna().any()

    def test_end_to_end_con_surebet_scanner(self):
        """scan_day -> to_odds_records -> scan_surebets trova la surebet.

        Solo Betfair non puo' produrre surebet (mercato unico, somma inversi
        > 1): mescoliamo i prezzi Exchange con una seconda fonte divergente,
        come accadrebbe con piu' bookmaker nel DataFrame.
        """
        import pandas as pd
        from surebet_scanner import scan_surebets

        cat = [_catalogue_entry("1.100", "MATCH_ODDS")]
        books = {
            "1.100": _book_entry("1.100", {
                101: [(1.80, 100.0)],   # Roma
                102: [(4.20, 100.0)],   # Empoli
            }),
        }
        scan = scan_day(FakeBetfairClient(catalogue=cat, books=books), "2026-08-31")
        rows = to_odds_records(scan["opportunities"])
        # scan_day produce anche il Draw se presente nel book: qui solo 2 runner
        assert {r["esito"] for r in rows} == {"1", "2"}

        # seconda fonte con quote divergenti: 1/2.20 + 1/3.60 + 1/5.00 = 0.932 < 1
        # (stesso nome evento del catalogo Betfair: altrimenti due gruppi separati)
        second_source = [
            {"bookmaker": "Snai", "evento": "Roma vs Empoli", "sport": "calcio",
             "esito": "1", "quota_decimale": 2.20,
             "timestamp": "2026-08-31T13:00:00.000Z"},
            {"bookmaker": "Pinnacle", "evento": "Roma vs Empoli", "sport": "calcio",
             "esito": "X", "quota_decimale": 3.60,
             "timestamp": "2026-08-31T13:00:00.000Z"},
            {"bookmaker": "Bet365", "evento": "Roma vs Empoli", "sport": "calcio",
             "esito": "2", "quota_decimale": 5.00,
             "timestamp": "2026-08-31T13:00:00.000Z"},
        ]
        df = pd.DataFrame(rows + second_source)
        opps = scan_surebets(df)
        assert len(opps) == 1
        assert opps[0].evento == "Roma vs Empoli"
        assert opps[0].rendimento_atteso > 0
