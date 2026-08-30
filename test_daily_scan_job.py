"""
Test unitari per daily_scan_job.py
==================================

Copertura:
- run_daily_scan: None se Betfair non configurato;
- persistenza su data/scan_<giorno>.json con struttura completa;
- load_latest_scan: file piu' recente, vuoto, file corrotto ignorato.
"""

from __future__ import annotations

import json

import pytest

import daily_scan_job
from daily_scan_job import load_latest_scan, run_daily_scan


class FakeBetfairClient:
    def __init__(self, catalogue=None, books=None):
        self._catalogue = catalogue or []
        self._books = books or {}

    def list_market_catalogue(self, market_filter, max_results=200, market_projection=None):
        mtype = market_filter["marketTypeCodes"][0]
        return [m for m in self._catalogue if m["marketType"] == mtype]

    def list_market_book(self, market_ids, price_projection=None):
        return [self._books[mid] for mid in market_ids if mid in self._books]


CATALOGUE = [
    {
        "marketId": "1.100", "marketType": "MATCH_ODDS",
        "marketStartTime": "2026-08-31T13:00:00.000Z",
        "event": {"id": "311", "name": "Roma v Empoli"},
        "runners": [{"selectionId": 101, "runnerName": "Roma"}],
    },
]
BOOKS = {
    "1.100": {"marketId": "1.100", "runners": [
        {"selectionId": 101, "ex": {"availableToBack": [{"price": 2.1, "size": 100.0}]}},
    ]},
}


@pytest.fixture(autouse=True)
def _tmp_scan_dir(monkeypatch, tmp_path):
    scan_dir = tmp_path / "data"
    scan_dir.mkdir()
    monkeypatch.setattr(daily_scan_job, "SCAN_DIR", scan_dir)
    yield scan_dir


def _fake_configured(monkeypatch, client=None):
    fake = client or FakeBetfairClient(catalogue=CATALOGUE, books=BOOKS)
    monkeypatch.setattr(daily_scan_job, "get_client", lambda: fake)


class TestRunDailyScan:

    def test_non_configurato_ritorna_none(self, monkeypatch):
        monkeypatch.setattr(daily_scan_job, "get_client", lambda: None)
        assert run_daily_scan("2026-08-31") is None

    def test_salva_file_con_struttura(self, monkeypatch, _tmp_scan_dir):
        _fake_configured(monkeypatch)
        payload = run_daily_scan("2026-08-31")
        assert payload is not None
        path = _tmp_scan_dir / "scan_2026-08-31.json"
        assert path.exists()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["day"] == "2026-08-31"
        assert on_disk["generated_at"]
        assert on_disk["opportunities"][0]["price"] == 2.1
        assert payload["markets"] == 1

    def test_default_oggi_se_senza_data(self, monkeypatch, _tmp_scan_dir):
        from datetime import datetime, timezone
        _fake_configured(monkeypatch)
        run_daily_scan()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert (_tmp_scan_dir / f"scan_{today}.json").exists()


class TestLoadLatestScan:

    def test_vuoto(self):
        assert load_latest_scan() is None

    def test_prende_il_piu_recente(self, _tmp_scan_dir):
        (_tmp_scan_dir / "scan_2026-08-30.json").write_text(
            json.dumps({"day": "2026-08-30", "opportunities": []}), encoding="utf-8")
        (_tmp_scan_dir / "scan_2026-08-31.json").write_text(
            json.dumps({"day": "2026-08-31", "opportunities": [1, 2]}), encoding="utf-8")
        latest = load_latest_scan()
        assert latest["day"] == "2026-08-31"

    def test_file_corrotto_ignorato(self, _tmp_scan_dir):
        (_tmp_scan_dir / "scan_2026-08-30.json").write_text("{non json", encoding="utf-8")
        (_tmp_scan_dir / "scan_2026-08-31.json").write_text(
            json.dumps({"day": "2026-08-31", "opportunities": []}), encoding="utf-8")
        latest = load_latest_scan()
        assert latest["day"] == "2026-08-31"

    def test_solo_file_corrotti(self, _tmp_scan_dir):
        (_tmp_scan_dir / "scan_2026-08-30.json").write_text("{{{", encoding="utf-8")
        assert load_latest_scan() is None
