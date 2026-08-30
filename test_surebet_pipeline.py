"""
Test unitari per surebet_pipeline.py (dati reali Betfair + the-odds-api)
e per odds_api.oddsapi_to_records / get_live_odds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import daily_scan_job
import surebet_pipeline
from surebet_pipeline import (
    _normalize_event,
    find_surebets,
    format_alert,
    merge_records,
    run_surebet_alert,
)
from odds_api import oddsapi_to_records


# ---------------------------------------------------------------------------
# odds_api: convertitore payload v4 -> contratto normalizzato
# ---------------------------------------------------------------------------

ODDSAPI_PAYLOAD = [
    {
        "id": "m1", "commence_time": "2026-08-31T13:00:00Z",
        "home_team": "Arsenal", "away_team": "Chelsea",
        "bookmakers": [
            {"key": "bet365", "title": "Bet365", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Arsenal", "price": 2.10},
                    {"name": "Draw", "price": 3.40},
                    {"name": "Chelsea", "price": 3.90},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over 2.5", "price": 1.85},
                    {"name": "Under 2.5", "price": 2.00},
                ]},
            ]},
        ],
    },
    # match senza bookmaker: non produce righe
    {"id": "m2", "home_team": "Roma", "away_team": "Napoli", "bookmakers": []},
]


class TestOddsapiToRecords:

    def test_mappatura_h2h(self):
        rows = [r for r in oddsapi_to_records(ODDSAPI_PAYLOAD) if r["esito"] in ("1", "X", "2")]
        esiti = {(r["esito"], r["quota_decimale"]) for r in rows}
        assert ("1", 2.10) in esiti
        assert ("X", 3.40) in esiti
        assert ("2", 3.90) in esiti

    def test_mappatura_totals(self):
        rows = [r for r in oddsapi_to_records(ODDSAPI_PAYLOAD) if r["esito"].lower().startswith(("over", "under"))]
        esiti = {r["esito"] for r in rows}
        assert esiti == {"Over 2.5", "Under 2.5"}

    def test_evento_senza_campionato(self):
        rows = oddsapi_to_records(ODDSAPI_PAYLOAD)
        assert all(r["evento"] == "Arsenal vs Chelsea" for r in rows)

    def test_contratto_completo(self):
        rows = oddsapi_to_records(ODDSAPI_PAYLOAD)
        for r in rows:
            assert set(r) == {"bookmaker", "evento", "sport", "esito",
                              "quota_decimale", "timestamp"}
            assert r["quota_decimale"] > 1.0
        assert all(r["bookmaker"] == "Bet365" for r in rows)

    def test_match_senza_bookmaker_ignorato(self):
        rows = oddsapi_to_records(ODDSAPI_PAYLOAD)
        assert all("Roma" not in r["evento"] for r in rows)

    def test_prezzo_invalido_scartato(self):
        bad = [{"home_team": "A", "away_team": "B", "bookmakers": [
            {"title": "X", "markets": [{"key": "h2h", "outcomes": [
                {"name": "A", "price": 0.95}]}]}]}]
        assert oddsapi_to_records(bad) == []


# ---------------------------------------------------------------------------
# pipeline: normalizzazione eventi e merge
# ---------------------------------------------------------------------------

class TestNormalizeEvent:

    def test_formati_noti(self):
        assert _normalize_event("Roma vs Empoli") == "roma vs empoli"
        assert _normalize_event("Roma v Empoli") == "roma vs empoli"
        assert _normalize_event("Serie A – Roma vs Empoli") == "roma vs empoli"
        assert _normalize_event("Premier League - Arsenal v Chelsea") == "arsenal vs chelsea"

    def test_casi_invalidi(self):
        assert _normalize_event("") is None
        assert _normalize_event(None) is None
        assert _normalize_event("Roma Empoli") is None


class TestMergeRecords:

    def test_matching_tra_fonti(self):
        betfair = [{"bookmaker": "Betfair Exchange", "evento": "Roma v Empoli",
                    "esito": "1", "quota_decimale": 1.80, "timestamp": "t"}]
        oddsapi = [{"bookmaker": "Snai", "evento": "Serie A – Roma vs Empoli",
                    "esito": "1", "quota_decimale": 2.20, "timestamp": "t"}]
        merged = merge_records(betfair, oddsapi)
        assert len(merged) == 2
        assert merged[0]["evento"] == merged[1]["evento"] == "roma vs empoli"

    def test_evento_irregolare_scartato(self):
        merged = merge_records([{"bookmaker": "X", "evento": "solo uno",
                                 "esito": "1", "quota_decimale": 2.0}])
        assert merged == []


# ---------------------------------------------------------------------------
# find_surebets + alert
# ---------------------------------------------------------------------------

BETFAIR_ROWS = [
    {"bookmaker": "Betfair Exchange", "evento": "Roma v Empoli", "esito": "1",
     "quota_decimale": 1.80, "timestamp": "2026-08-31T13:00:00Z"},
    {"bookmaker": "Betfair Exchange", "evento": "Roma v Empoli", "esito": "X",
     "quota_decimale": 3.10, "timestamp": "2026-08-31T13:00:00Z"},
    {"bookmaker": "Betfair Exchange", "evento": "Roma v Empoli", "esito": "2",
     "quota_decimale": 4.20, "timestamp": "2026-08-31T13:00:00Z"},
]
SECOND_SOURCE = [
    {"bookmaker": "Snai", "evento": "Serie A – Roma vs Empoli", "esito": "1",
     "quota_decimale": 2.20, "timestamp": "2026-08-31T13:00:00Z"},
    {"bookmaker": "Pinnacle", "evento": "Serie A – Roma vs Empoli", "esito": "X",
     "quota_decimale": 3.60, "timestamp": "2026-08-31T13:00:00Z"},
    {"bookmaker": "Bet365", "evento": "Serie A – Roma vs Empoli", "esito": "2",
     "quota_decimale": 5.00, "timestamp": "2026-08-31T13:00:00Z"},
]


class TestFindSurebets:

    def test_due_fonti_trovano_surebet(self):
        opps = find_surebets(BETFAIR_ROWS, SECOND_SOURCE)
        assert len(opps) == 1
        assert opps[0].rendimento_atteso > 0

    def test_singola_fonte_nessun_falso_positivo(self):
        # un solo bookmaker: mai surebet, onesto []
        opps = find_surebets(BETFAIR_ROWS)
        assert opps == []

    def test_nessuna_fonte(self):
        assert find_surebets([]) == []


class TestFormatAlert:

    def test_vuoto(self):
        assert format_alert([]) == ""

    def test_contiene_dettagli(self):
        opps = find_surebets(BETFAIR_ROWS, SECOND_SOURCE)
        text = format_alert(opps)
        assert "SUREBET" in text
        assert "roma vs empoli" in text.lower()
        assert "2.20" in text
        assert "Gioca responsabilmente" in text


class TestRunSurebetAlert:

    @pytest.fixture(autouse=True)
    def _tmp_dirs(self, monkeypatch, tmp_path):
        scan_dir = tmp_path / "data"
        scan_dir.mkdir()
        monkeypatch.setattr(daily_scan_job, "SCAN_DIR", scan_dir)
        monkeypatch.setattr(surebet_pipeline, "SUREBET_DB", tmp_path / "surebet_log.jsonl")
        yield scan_dir

    def _write_scan(self, scan_dir: Path):
        scan = {
            "day": "2026-08-31", "events": 1, "markets": 1,
            "opportunities": [
                {"event_id": "311", "event_name": "Roma v Empoli",
                 "market_id": "1.100", "market_type": "MATCH_ODDS",
                 "selection_id": 101, "selection_name": "Roma", "side": "BACK",
                 "price": 1.80, "price_size": 100.0,
                 "start_time": "2026-08-31T13:00:00.000Z"},
                {"event_id": "311", "event_name": "Roma v Empoli",
                 "market_id": "1.100", "market_type": "MATCH_ODDS",
                 "selection_id": 102, "selection_name": "Empoli", "side": "BACK",
                 "price": 4.20, "price_size": 80.0,
                 "start_time": "2026-08-31T13:00:00.000Z"},
                {"event_id": "311", "event_name": "Roma v Empoli",
                 "market_id": "1.100", "market_type": "MATCH_ODDS",
                 "selection_id": 103, "selection_name": "The Draw", "side": "BACK",
                 "price": 3.10, "price_size": 60.0,
                 "start_time": "2026-08-31T13:00:00.000Z"},
            ],
        }
        (scan_dir / "scan_2026-08-31.json").write_text(
            __import__("json").dumps(scan), encoding="utf-8")

    def test_senza_catalogo_betfair(self):
        assert run_surebet_alert() == []

    def test_senza_seconda_fonte(self, monkeypatch, tmp_path):
        self._write_scan(tmp_path / "data")
        import odds_api
        monkeypatch.setenv("ODDS_API_KEY", "")  # get_live_odds -> []
        ops = run_surebet_alert()
        assert ops == []

    def test_alert_completo_e_log(self, monkeypatch, tmp_path):
        self._write_scan(tmp_path / "data")
        import odds_api
        # la pipeline importa get_live_odds dentro run_surebet_alert:
        # la patch a livello modulo odds_api e' quella che conta
        monkeypatch.setattr(odds_api, "get_live_odds", lambda: SECOND_SOURCE)
        ops = run_surebet_alert()
        assert len(ops) == 1
        log = (tmp_path / "surebet_log.jsonl").read_text(encoding="utf-8").strip()
        assert "reale (Betfair Exchange + the-odds-api)" in log

    def test_fonte_senza_edge_nessun_alert(self, monkeypatch, tmp_path):
        # seconda fonte con quote uguali a Betfair: nessun arbitraggio
        self._write_scan(tmp_path / "data")
        same = [{"bookmaker": "Snai", "evento": "Roma vs Empoli",
                 "esito": r["esito"], "quota_decimale": r["quota_decimale"],
                 "timestamp": "2026-08-31T13:00:00Z"} for r in BETFAIR_ROWS]
        import odds_api
        monkeypatch.setattr(odds_api, "get_live_odds", lambda: same)
        assert run_surebet_alert() == []
