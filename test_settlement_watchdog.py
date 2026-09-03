"""Test settlement_watchdog_job: self-healing delle pendenze.

Scenario 01/09: bet piazzate alle 16:40, redeploy alle 23:13 salta il
results_job delle 21:30 -> le bet restano aperte. Il watchdog (ogni 2h)
deve scaricare i risultati, saldare le bet e inviare i verdetti senza
intervento manuale.

Dal 04/09 la refertazione usa ESCLUSIVAMENTE API-Football
(settlement_apifootball): i test monkeypatchano il confine esterno
(_api_get di settlement_apifootball con formato fixture API-Football).
"""

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tracker
import bot
import settlement_apifootball
from football_hist import FINISHED_STATUSES
from settlement_apifootball import SETTLEMENT_LEAGUE_IDS


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _fake_context():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = MagicMock()
    return ctx


def _patch_send(monkeypatch):
    """Cattura l'invio Telegram dei verdetti."""
    calls = []

    async def fake_send(context, text):
        calls.append(text)

    monkeypatch.setattr(bot, "_send_bet_settlements", fake_send)
    return calls


def _patch_api(monkeypatch, fixtures_per_league):
    """_api_get finto: ritorna il body API-Football per la lega richiesta.

    `fixtures_per_league` e' {nome_lega: [fixture API-Football]} — le
    fixture devono avere status FINITO per essere considerate dal settlement.
    """
    by_id = {}
    for lg, fixtures in (fixtures_per_league or {}).items():
        lid = SETTLEMENT_LEAGUE_IDS.get(lg)
        if lid:
            by_id[lid] = fixtures

    def fake_api_get(path, params):
        fixtures = by_id.get(params.get("league"), [])
        return {"results": len(fixtures), "response": fixtures}

    def fake_finished(lid, season, d_from, d_to):
        return [fx for fx in by_id.get(lid, [])
                if (((fx.get("fixture") or {}).get("status") or {}).get("short") or "")
                in FINISHED_STATUSES]

    monkeypatch.setattr(settlement_apifootball, "_api_get", fake_api_get)
    monkeypatch.setattr(settlement_apifootball, "_fixtures_finished", fake_finished)


def _patch_ratings(monkeypatch):
    import rating_engine
    monkeypatch.setattr(rating_engine, "compute_ratings", lambda: None)


def _patch_admin(monkeypatch):
    monkeypatch.setattr(bot, "_admin_chat_ids", lambda: [])
    import tracker as t
    monkeypatch.setattr(t, "get_subscribers", lambda *a, **k: [])


def _fixture(mid, home, away, sh, sa, status="FT"):
    """Fixture API-Football finita (stesso formato di football_hist)."""
    return {
        "fixture": {"id": mid, "date": "2026-09-01T20:00:00Z",
                    "status": {"short": status}},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": sh, "away": sa},
    }


def test_watchdog_salda_bet_pendente_flusso_reale(temp_db, monkeypatch):
    """Bet aperta + risultato scaricato -> saldatura automatica completa."""
    tracker.save_match("mX", "Serie A", "Inter", "Napoli",
                       "2026-09-01T19:00:00Z")
    tracker.save_analysis("mX", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55,
                          0.10, "1", 2.10, "Pinnacle", "value")
    tracker.save_bet("mX", "1X2", "1", "1.100", 101, 2.10, 10.0)

    _patch_send(monkeypatch)
    _patch_api(monkeypatch, {"Serie A": [_fixture("mX", "Inter", "Napoli", 2, 1)]})
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))

    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] == "won"
    assert rows[0]["profit"] == pytest.approx(11.0)  # 10 * (2.10 - 1)


def test_watchdog_notifica_verdetti_nuovi(temp_db, monkeypatch):
    """Quando il watchdog stesso chiude una bet, il verdetto va a iscritti+admin."""
    tracker.save_match("mX", "Serie A", "Inter", "Napoli",
                       "2026-09-01T19:00:00Z")
    tracker.save_analysis("mX", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55,
                          0.10, "1", 2.10, "Pinnacle", "value")
    tracker.save_bet("mX", "1X2", "1", "1.100", 101, 2.10, 10.0)

    calls = _patch_send(monkeypatch)
    _patch_api(monkeypatch, {"Serie A": [_fixture("mX", "Inter", "Napoli", 2, 1)]})
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    assert len(calls) == 1
    # _send_bet_settlements riceve la lista settlements: il testo formattato
    # (che arriva davvero a Telegram) contiene il verdetto leggibile.
    text = bot.format_bet_verdicts(calls[0])
    assert "ESITO PUNTATE" in text
    assert "Inter" in text and "VINTA" in text


def test_watchdog_silenzioso_senza_pendenze(temp_db, monkeypatch):
    """Niente bet aperte, niente risultati: nessuna notifica, nessun errore."""
    calls = _patch_send(monkeypatch)
    _patch_api(monkeypatch, {})
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    assert calls == []


def test_watchdog_sobrevive_errore_update(temp_db, monkeypatch):
    """Se il fetch esplode, il job non propaga l'eccezione."""
    def boom(*a, **k):
        raise RuntimeError("API giu'")
    monkeypatch.setattr(settlement_apifootball, "_api_get", boom)
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))  # no raise


def test_watchdog_saldatura_differita(temp_db, monkeypatch):
    """Scenario completo 01/09: bet piazzata, risultato NON ancora
    disponibile -> resta aperta; al giro successivo (risultato presente,
    anche a 12h di distanza) viene saldata senza intervento manuale."""
    tracker.save_match("mZ", "Serie A", "Inter", "Napoli",
                       "2026-09-01T19:00:00Z")
    tracker.save_analysis("mZ", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55,
                          0.10, "1", 2.10, "Pinnacle", "value")
    tracker.save_bet("mZ", "1X2", "1", "1.100", 101, 2.10, 10.0)

    calls = _patch_send(monkeypatch)
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    # Giro 1: l'API non ha ancora il risultato (fixture in corso, non FT)
    _patch_api(monkeypatch,
               {"Serie A": [_fixture("mZ", "Inter", "Napoli", 0, 0,
                                     status="1H")]})
    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] is None
    assert calls == []

    # Giro 2 (2h dopo): risultato arrivato -> auto-settlement + verdetto
    _patch_api(monkeypatch, {"Serie A": [_fixture("mZ", "Inter", "Napoli", 2, 1)]})
    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] == "won"
    assert len(calls) == 1