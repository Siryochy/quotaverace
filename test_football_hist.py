"""
Test unitari per la sincronizzazione risultati storici (API-Football).
"""

import pytest

import tracker
import football_hist as fh


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "hist.db")
    tracker.init_db()
    yield


def _fixture(status="FT", home="Roma", away="Empoli", gh=2, ga=0, fxid="1"):
    return {
        "fixture": {"id": fxid, "date": "2026-08-30T18:00:00Z",
                    "status": {"short": status}},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": gh, "away": ga},
    }


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
    def raise_for_status(self):
        return None
    def json(self):
        return self._payload


def _stub_requests(monkeypatch, page_payloads):
    """Installa uno stub di requests.get: page params['page'] -> payload."""
    def fake_get(url, params=None, headers=None, timeout=None):
        page = (params or {}).get("page", 1)
        p = page_payloads.get(page)
        return _Resp({"results": len(p) if p else 0, "response": p or []})
    import requests as real_requests
    monkeypatch.setattr(fh.requests, "get", fake_get)


class TestMatchDbName:
    def test_match_esatto(self):
        assert fh._match_db_name("Roma", "Serie A") == "Roma"

    def test_squadra_assente(self):
        assert fh._match_db_name("Squadra Inventata", "Serie A") is None


class TestParseFixture:
    def test_fixture_valida(self):
        home, away, sh, sa, date = fh._parse_fixture(_fixture(), "Serie A")
        assert home == "Roma" and away == "Empoli"
        assert (sh, sa) == (2, 0)

    def test_senza_goal_ritorna_none(self):
        fx = _fixture(); fx["goals"] = {"home": None, "away": None}
        assert fh._parse_fixture(fx, "Serie A") is None

    def test_squadre_non_allineate(self):
        fx = _fixture(home="Sconosciuta", away="Ignota")
        assert fh._parse_fixture(fx, "Serie A") is None


class TestSyncHistory:
    def test_niente_key_restituisce_errore(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        assert res == {"error": "API_FOOTBALL_KEY mancante"}

    def test_salva_risultato(self, monkeypatch, tmp_path):
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        _stub_requests(monkeypatch, {1: [_fixture()]})  # Roma 2-0, FT

        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        assert res["_total"] == 1

        conn = tracker._get_conn()
        cnt = conn.cursor().execute("SELECT COUNT(*) FROM match_results").fetchone()[0]
        conn.close()
        assert cnt == 1

    def test_ignora_partite_non_finite(self, monkeypatch):
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        _stub_requests(monkeypatch, {1: [_fixture(status="NS")]})
        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        assert res["_total"] == 0
        assert res.get("Serie A") == 0

    def test_salva_piu_pagine(self, monkeypatch):
        monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
        # pagina1 piena (20), pagina2 con 1, pagina3 vuota -> 21 salvate
        page1 = [_fixture(fxid=str(i), gh=1, ga=0) for i in range(20)]
        _stub_requests(monkeypatch, {1: page1, 2: [_fixture(fxid="A", gh=0, ga=1)], 3: []})
        res = fh.sync_history(seasons=1, leagues=["Serie A"])
        assert res["_total"] == 21


class TestRunSync:
    def test_senza_key(self, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        text = fh.run_sync()
        assert "mancante" in text.lower()