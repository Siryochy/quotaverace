"""Regressione bug 01/09: le 3 bet (Birmingham, Wycombe, Tranmere) piazzate
alle 16:40 non sono state mai saldate perche' results_job rileggeva la cache
dei punteggi scritta nel pomeriggio (completed=False, scores=[]).

La cache non e' attendibile quando contiene partite INIZIATE da oltre
STALE_INPLAY_HOURS ma ancora completed=False: significa che e' stata scritta
mentre la partita era in corso (una partita di calcio finisce entro ~2h).
"""

import json
import time
from datetime import datetime, timezone, timedelta

import pytest

import odds_api


def _match(mid, commence_iso, completed=False, n_scores=0):
    return {
        "id": mid,
        "home_team": "Birmingham City",
        "away_team": "Southampton",
        "commence_time": commence_iso,
        "completed": completed,
        "scores": [{"name": "Birmingham City", "score": "1"},
                   {"name": "Southampton", "score": "1"}][:n_scores],
    }


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    return tmp_path


def _write_cache(path, payload, age_s=0.0):
    path.write_text(json.dumps({"ts": time.time() - age_s, "payload": payload}))


def test_cache_fresh_con_partite_future_e_valida(cache_env, monkeypatch):
    """Partite non ancora iniziate + fresche: la cache resta valida."""
    future = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_cache(cache_env / "toa_scores_soccer_x.json",
                 [_match("m1", future, completed=False)])
    calls = []
    monkeypatch.setattr(odds_api.requests, "get",
                        lambda *a, **k: calls.append(1))
    assert odds_api.fetch_scores("soccer_x", days_from=2) != []
    assert not calls  # nessuna chiamata API: cache valida


def test_cache_stale_inplay_forza_refresh(cache_env, monkeypatch):
    """Partita iniziata da 5h con completed=False -> cache NON attendibile,
    viene richiamata l'API e i risultati veri sovrascrivono la cache."""
    f = cache_env / "toa_scores_soccer_x.json"
    past = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_cache(f, [_match("m1", past, completed=False)])

    fresh = [_match("m1", past, completed=True, n_scores=2)]
    class FakeResp:
        status_code = 200
        headers = {"x-requests-remaining": "400"}
        def raise_for_status(self): pass
        def json(self): return fresh
    calls = []
    monkeypatch.setattr(odds_api, "_env", lambda name: "fake-key")
    monkeypatch.setattr(odds_api.requests, "get",
                        lambda *a, **k: calls.append(1) or FakeResp())

    out = odds_api.fetch_scores("soccer_x", days_from=2)
    assert calls, "l'API doveva essere richiamata"
    assert out == fresh
    saved = json.loads(f.read_text())
    assert saved["payload"][0]["completed"] is True


def test_refresh_solo_completate_non_ringiovanisce_cache(cache_env, monkeypatch):
    """Se il refresh ritorna solo partite completate, la cache conserva il
    timestamp originale (non si bruciano crediti extra nelle ore dopo)."""
    f = cache_env / "toa_scores_soccer_x.json"
    past = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_ts = time.time() - odds_api.ODDS_TTL / 2  # cache a meta' TTL
    f.write_text(json.dumps({"ts": old_ts, "payload": [_match("m1", past, completed=False)]}))

    class FakeResp:
        status_code = 200
        headers = {"x-requests-remaining": "400"}
        def raise_for_status(self): pass
        def json(self): return [_match("m1", past, completed=True, n_scores=2)]
    monkeypatch.setattr(odds_api.requests, "get", lambda *a, **k: FakeResp())

    odds_api.fetch_scores("soccer_x", days_from=2)
    saved = json.loads(f.read_text())
    assert abs(saved["ts"] - old_ts) < 1.0  # timestamp originale conservato


def test_refresh_fallito_ripiega_su_cache(cache_env, monkeypatch):
    """Se l'API fallisce (es. crediti esauriti), si usa comunque la cache."""
    f = cache_env / "toa_scores_soccer_x.json"
    past = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_payload = [_match("m1", past, completed=False)]
    _write_cache(f, stale_payload)

    class FailResp:
        status_code = 429
        headers = {}
        def raise_for_status(self): raise odds_api.requests.HTTPError("429")
        def json(self): return {}
    monkeypatch.setattr(odds_api.requests, "get", lambda *a, **k: FailResp())
    monkeypatch.setattr(odds_api, "_env", lambda name: "fake-key")

    out = odds_api.fetch_scores("soccer_x", days_from=2)
    assert out == stale_payload  # fallback: cache anche se stantia


def test_senza_key_ripiega_su_cache(cache_env, monkeypatch):
    f = cache_env / "toa_scores_soccer_x.json"
    payload = [_match("m1", "2026-09-01T18:45:00Z", completed=True, n_scores=2)]
    _write_cache(f, payload)
    monkeypatch.setattr(odds_api, "_env", lambda name: None)
    assert odds_api.fetch_scores("soccer_x", days_from=2) == payload


def test_partita_in_corso_da_poco_non_e_stale(cache_env, monkeypatch):
    """Partita iniziata da 1h e ancora in corso: la cache e' normale, nessun
    refresh forzato (non si sprecano crediti durante la partita)."""
    f = cache_env / "toa_scores_soccer_x.json"
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_cache(f, [_match("m1", recent, completed=False)])
    calls = []
    monkeypatch.setattr(odds_api.requests, "get", lambda *a, **k: calls.append(1))
    odds_api.fetch_scores("soccer_x", days_from=2)
    assert not calls
