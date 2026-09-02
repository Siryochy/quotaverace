"""Stand-by Betfair (BETFAIR_ENABLED=0) e confine di refertazione.

Garanzie verificate:
1. Con BETFAIR_ENABLED=0 l'integrazione Betfair e' SOSPESA senza rumore
   (get_client -> None, endpoint /api/scan -> 503 esplicito, surebet
   silenzioso, health mostra il flag).
2. La REFERTAZIONE (risultati + saldaggio bet/previsioni/cassa) NON tocca
   MAI Betfair: usa solo the-odds-api (fetch_scores). Il test pianta una
   tripwire sui client Betfair: se il flusso di saldaggio tenta qualunque
   accesso Exchange, il test fallisce.
"""
import sys
import tempfile
from pathlib import Path

import pytest

import tracker
import betfair_client
import surebet_pipeline


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture()
def bf_disabled(monkeypatch):
    monkeypatch.setenv("BETFAIR_ENABLED", "0")


@pytest.fixture()
def bf_enabled(monkeypatch):
    monkeypatch.setenv("BETFAIR_ENABLED", "1")


# --- 1. Switch master -------------------------------------------------------


def test_enabled_default_true(monkeypatch):
    monkeypatch.delenv("BETFAIR_ENABLED", raising=False)
    assert betfair_client.enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "off", "no"])
def test_enabled_false_values(monkeypatch, val):
    monkeypatch.setenv("BETFAIR_ENABLED", val)
    assert betfair_client.enabled() is False


def test_get_client_none_quando_disabled(bf_disabled, monkeypatch):
    """Anche CON le credenziali impostate, il client non viene creato."""
    monkeypatch.setenv("BETFAIR_APP_KEY", "fake-key")
    monkeypatch.setenv("BETFAIR_USERNAME", "u")
    monkeypatch.setenv("BETFAIR_PASSWORD", "p")
    monkeypatch.setenv("BETFAIR_CERT_PATH", "/tmp/cert.pem")
    assert betfair_client.get_client() is None


def test_get_client_ok_quando_enabled(bf_enabled, monkeypatch):
    monkeypatch.setenv("BETFAIR_APP_KEY", "fake-key")
    monkeypatch.setenv("BETFAIR_USERNAME", "u")
    monkeypatch.setenv("BETFAIR_PASSWORD", "p")
    monkeypatch.setenv("BETFAIR_CERT_PATH", "/tmp/cert.pem")
    assert betfair_client.get_client() is not None


def test_get_client_none_senza_credenziali(bf_enabled, monkeypatch):
    monkeypatch.delenv("BETFAIR_APP_KEY", raising=False)
    assert betfair_client.get_client() is None


# --- 2. auto_bet degenera in SIM, non in errore ------------------------------


def test_auto_bet_sim_con_betfair_disabled(bf_disabled, temp_db):
    """Con lo switch a 0 auto_bet NON tenta Exchange: mode='sim' (come se
    le credenziali mancassero) — le puntate paper per il ledger ML proseguono."""
    import auto_bet
    from datetime import datetime, timedelta, timezone
    start = (datetime.now(timezone.utc) + timedelta(hours=3)) \
        .isoformat().replace("+00:00", "Z")
    tracker.save_match("m1", "Serie A", "Osasuna", "Getafe", start)
    tracker.save_analysis("m1", 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                          "Osasuna", 2.20, "Pinnacle", "value",
                          market_prob=0.45, market_edge=0.07)
    placed = auto_bet.run_today_bets(allow_sim=True, stake_eur=5.0)
    assert len(placed) == 1
    assert placed[0]["mode"] == "sim"
    assert placed[0]["price"] == 2.20


# --- 3. /api/scan esplicito, surebet silenzioso -------------------------------


def test_api_scan_503_betfair_disabled(bf_disabled):
    import web_api
    status, payload = web_api._scan_json({"live": "1"})
    assert status == 503
    assert payload["error"] == "betfair_disabled"


def test_surebet_silenzioso_con_disabled(bf_disabled, monkeypatch, caplog):
    """BETFAIR_ENABLED=0 senza catalogo: NESSUN log (skip voluto, non un
    avviso). Con lo switch a 1 il log informativo resta."""
    import logging
    monkeypatch.setattr(surebet_pipeline, "load_latest_scan", lambda: None)
    with caplog.at_level(logging.INFO):
        assert surebet_pipeline.run_surebet_alert() == []
    assert "nessun catalogo Betfair in cache" not in caplog.text

    monkeypatch.setenv("BETFAIR_ENABLED", "1")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert surebet_pipeline.run_surebet_alert() == []
    assert "nessun catalogo Betfair in cache" in caplog.text


def test_health_flag_betfair_enabled(bf_disabled):
    import web_api
    payload = web_api._health_json()
    assert payload["betfair_enabled"] is False


# --- 4. Tripwire: la refertazione non tocca MAI Betfair -----------------------


class _BetfairTripwire:
    """Qualsiasi uso del client Betfair nel flusso di saldaggio fallisce."""

    def __getattr__(self, name):
        raise AssertionError(
            f"la refertazione NON deve toccare Betfair (chiamata: {name})")


def test_update_results_mai_betfair(temp_db, monkeypatch):
    """Tripwire: _update_results usa SOLO fetch_scores (the-odds-api).

    Registra il confine come contratto testato: se in futuro qualcuno
    introducesse una dipendenza da Betfair nel saldaggio, il test rompe.
    """
    import bot
    import odds_api
    from datetime import datetime, timedelta, timezone

    # Betfair non configurato E tripwire: qualunque tentativo Exchange
    # (anche solo costruire il client) fallisce il test.
    monkeypatch.setattr(betfair_client, "get_client",
                        lambda *a, **k: _BetfairTripwire())
    monkeypatch.setenv("BETFAIR_APP_KEY", "tripwire")

    # Risultato reale che arriva da the-odds-api
    start = (datetime.now(timezone.utc) - timedelta(hours=4)) \
        .isoformat().replace("+00:00", "Z")
    tracker.save_match("mX", "Serie A", "Inter", "Napoli", start)
    tracker.save_analysis("mX", 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                          "Inter", 2.10, "Pinnacle", "value",
                          market_prob=0.45, market_edge=0.07)
    tracker.save_bet(match_id="mX", mercato="1X2", esito="1",
                     market_id=None, selection_id=None, price=2.10,
                     stake=5.0, mode="sim", status="SUCCESS")

    payload = {"soccer_italy_serie_a": [{
        "id": "mX", "home_team": "Inter", "away_team": "Napoli",
        "last_update": datetime.now(timezone.utc).isoformat(),
        "completed": True,
        "scores": [{"name": "Inter", "score": 2}, {"name": "Napoli", "score": 1}],
    }]}
    monkeypatch.setattr(odds_api, "fetch_scores",
                        lambda sport, days_from=2: payload.get(sport, []))

    updated, stats, settlements, sanity = bot._update_results()

    assert updated == 1
    bets = tracker.get_bets()
    assert len(bets) == 1
    assert bets[0]["esito_finale"] == "won"  # Inter 2-1, segno "1"
    assert bets[0]["profit"] == pytest.approx(5.5)  # 5.0 * (2.10 - 1)
