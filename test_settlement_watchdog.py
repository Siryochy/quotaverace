"""Test settlement_watchdog_job: self-healing delle pendenze.

Scenario 01/09: bet piazzate alle 16:40, redeploy alle 23:13 salta il
results_job delle 21:30 -> le bet restano aperte. Il watchdog (ogni 2h)
deve scaricare i risultati, saldare le bet e inviare i verdetti senza
intervento manuale.

I test usano il FLUSSO REALE di _update_results (nessun mock della logica
di saldatura): si monkeypatcha solo il confine esterno (fetch_scores di
odds_api e compute_ratings di rating_engine).
"""

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tracker
import odds_api
import bot


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        monkeypatch.setattr(odds_api, "CACHE_DIR", Path(td))
        tracker.init_db()
        yield db_path


def _fake_context():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = asyncio.coroutine(lambda *a, **k: None)() \
        if False else MagicMock()
    return ctx


def _patch_send(monkeypatch):
    """Cattura l'invio Telegram dei verdetti."""
    calls = []

    async def fake_send(context, text):
        calls.append(text)

    monkeypatch.setattr(bot, "_send_bet_settlements", fake_send)
    return calls


def _patch_fetch_scores(monkeypatch, payload_per_sport):
    """fetch_scores finto: ritorna il payload per lo sport richiesto."""
    def fake_fetch(sport=None, days_from=2):
        return payload_per_sport.get(sport, [])

    monkeypatch.setattr(odds_api, "fetch_scores", fake_fetch)
    # _update_results fa l'import dentro la funzione: patch del modulo
    monkeypatch.setitem(sys.modules, "odds_api", odds_api)


import sys  # noqa: E402  (usato da _patch_fetch_scores)


def _patch_ratings(monkeypatch):
    import rating_engine
    monkeypatch.setattr(rating_engine, "compute_ratings", lambda: None)
    monkeypatch.setitem(sys.modules, "rating_engine", rating_engine)


def _patch_admin(monkeypatch):
    monkeypatch.setattr(bot, "_admin_chat_ids", lambda: [])
    import tracker as t
    monkeypatch.setattr(t, "get_subscribers", lambda *a, **k: [])


MATCH = {"id": "mX", "home_team": "Inter", "away_team": "Napoli",
         "scores": [{"name": "Inter", "score": "2"},
                    {"name": "Napoli", "score": "1"}],
         "last_update": ""}


def test_watchdog_salda_bet_pendente_flusso_reale(temp_db, monkeypatch):
    """Bet aperta + risultato scaricato -> saldatura automatica completa."""
    tracker.save_match("mX", "Serie A", "Inter", "Napoli",
                       "2026-09-01T13:00:00Z")
    tracker.save_analysis("mX", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55,
                          0.10, "1", 2.10, "Pinnacle", "value")
    tracker.save_bet("mX", "1X2", "1", "1.100", 101, 2.10, 10.0)

    _patch_send(monkeypatch)
    _patch_fetch_scores(monkeypatch, {"soccer_italy_serie_a": [dict(MATCH)]})
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))

    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] == "won"
    assert rows[0]["profit"] == pytest.approx(11.0)  # 10 * (2.10 - 1)


def test_watchdog_notifica_verdetti_nuovi(temp_db, monkeypatch):
    """Quando il watchdog stesso chiude una bet, il verdetto va a iscritti+admin."""
    tracker.save_match("mX", "Serie A", "Inter", "Napoli",
                       "2026-09-01T13:00:00Z")
    tracker.save_analysis("mX", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55,
                          0.10, "1", 2.10, "Pinnacle", "value")
    tracker.save_bet("mX", "1X2", "1", "1.100", 101, 2.10, 10.0)

    calls = _patch_send(monkeypatch)
    _patch_fetch_scores(monkeypatch, {"soccer_italy_serie_a": [dict(MATCH)]})
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
    _patch_fetch_scores(monkeypatch, {})
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    assert calls == []


def test_watchdog_sobrevive_errore_update(temp_db, monkeypatch):
    """Se il fetch esplode, il job non propaga l'eccezione."""
    def boom(*a, **k):
        raise RuntimeError("API giu'")
    monkeypatch.setattr(odds_api, "fetch_scores", boom)
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))  # no raise


def test_watchdog_saldatura_differita(temp_db, monkeypatch):
    """Scenario completo 01/09: bet piazzata, risultato NON ancora
    disponibile -> resta aperta; al giro successivo (risultato presente,
    anche a 12h di distanza) viene saldata senza intervento manuale."""
    tracker.save_match("mZ", "Serie A", "Inter", "Napoli",
                       "2026-09-01T13:00:00Z")
    tracker.save_analysis("mZ", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55,
                          0.10, "1", 2.10, "Pinnacle", "value")
    tracker.save_bet("mZ", "1X2", "1", "1.100", 101, 2.10, 10.0)

    calls = _patch_send(monkeypatch)
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    # Giro 1: l'API non ha ancora il risultato (payload con completed=False)
    in_corso = [dict(MATCH, completed=False, scores=[])]
    _patch_fetch_scores(monkeypatch, {"soccer_italy_serie_a": in_corso})
    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] is None
    assert calls == []

    # Giro 2 (2h dopo): risultato arrivato -> auto-settlement + verdetto
    _patch_fetch_scores(monkeypatch,
                        {"soccer_italy_serie_a": [dict(MATCH, id="mZ",
                                                       completed=True)]})
    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] == "won"
    assert len(calls) == 1
