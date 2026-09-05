"""Test settlement_watchdog_job: self-healing delle pendenze.

Scenario 01/09: bet piazzate alle 16:40, redeploy alle 23:13 salta il
results_job delle 21:30 -> le bet restano aperte. Il watchdog (ogni 4h)
deve scaricare i risultati, saldare le bet e inviare i verdetti senza
intervento manuale.

Refertazione MIRATA (risparmio crediti the-odds-api): fetch_scores viene
chiamato SOLO per le leghe con scommesse attive (o chiuse da <48h) su
partite già iniziate — zero righe aperte = zero chiamate per quella lega.

I test usano il FLUSSO REALE di _update_results (nessun mock della logica
di saldatura): si monkeypatcha solo il confine esterno (fetch_scores di
odds_api e compute_ratings di rating_engine).
"""

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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


def _patch_ratings(monkeypatch):
    import rating_engine
    monkeypatch.setattr(rating_engine, "compute_ratings", lambda: None)
    monkeypatch.setitem(sys.modules, "rating_engine", rating_engine)


def _patch_admin(monkeypatch):
    monkeypatch.setattr(bot, "_admin_chat_ids", lambda: [])
    import tracker as t
    monkeypatch.setattr(t, "get_subscribers", lambda *a, **k: [])


def _recent_iso(days_ago: int = 1, hours_ago: int = 3) -> str:
    """commence_time recente (entro la finestra di get_leagues_with_open_rows):
    date hardcoded tipo 2026-09-01 restano valide solo il giorno in cui il
    test e' scritto e poi escono dalla finestra (settlement mai eseguito →
    bet aperte per sempre)."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago,
                                                   hours=hours_ago)).isoformat()


MATCH = {"id": "mX", "home_team": "Inter", "away_team": "Napoli",
         "scores": [{"name": "Inter", "score": "2"},
                    {"name": "Napoli", "score": "1"}],
         "last_update": ""}


def test_watchdog_salda_bet_pendente_flusso_reale(temp_db, monkeypatch):
    """Bet aperta + risultato scaricato -> saldatura automatica completa."""
    tracker.save_match("mX", "Serie A", "Inter", "Napoli", _recent_iso())
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
    tracker.save_match("mX", "Serie A", "Inter", "Napoli", _recent_iso())
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
    tracker.save_match("mZ", "Serie A", "Inter", "Napoli", _recent_iso())
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


# ---------------------------------------------------------------------------
# Refertazione MIRATA: fetch_scores SOLO per leghe con scommesse attive
# ---------------------------------------------------------------------------

def _recording_fetch(monkeypatch, payload_per_sport):
    """fetch_scores finto che registra gli sport interrogati."""
    requested = []

    def fake_fetch(sport=None, days_from=2):
        requested.append(sport)
        return payload_per_sport.get(sport, [])

    monkeypatch.setattr(odds_api, "fetch_scores", fake_fetch)
    monkeypatch.setitem(sys.modules, "odds_api", odds_api)
    return requested


def test_watchdog_fetch_solo_leghe_con_righe_aperte(temp_db, monkeypatch):
    """Leghe con SOLO un segnale value (nessuna scommessa aperta) NON
    vengono interrogate: zero righe attive = zero chiamate fetch_scores."""
    # Serie A: bet APERTA su partita appena iniziata -> va refertata
    tracker.save_match("mSerieA", "Serie A", "Inter", "Napoli", _recent_iso())
    tracker.save_bet("mSerieA", "1X2", "1", "1.100", 101, 2.10, 10.0)
    # J1 League: segnale value ma NESSUNA prediction/bet -> NON va refertata
    tracker.save_match("mJ1", "J1 League", "FC Machida Zelvia",
                       "Kawasaki Frontale", _recent_iso())
    tracker.save_analysis("mJ1", 1.2, 1.6, 0.28, 0.26, 0.46, 0.52,
                          0.14, "2", 4.0, "Pinnacle", "value")

    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)
    requested = _recording_fetch(
        monkeypatch, {"soccer_italy_serie_a": [dict(MATCH, id="mSerieA")]})

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    # Solo la lega con la scommessa attiva è stata interrogata
    assert requested == ["soccer_italy_serie_a"]
    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] == "won"   # la bet aperta è stata saldata


def test_watchdog_niente_fetch_per_partite_non_iniziate(temp_db, monkeypatch):
    """Bet aperta su partita FUTURA (non ancora iniziata): non c'è alcun
    risultato da scaricare -> zero chiamate API per quella lega."""
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    tracker.save_match("mFut", "La Liga", "Osasuna", "Getafe", future)
    tracker.save_bet("mFut", "1X2", "1", None, None, 2.1, 10.0)

    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)
    requested = _recording_fetch(monkeypatch, {})

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    assert requested == []
    # La bet resta aperta (il match non è ancora iniziato)
    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] is None


def test_tracker_get_leagues_with_open_rows_finestra(temp_db):
    """Helper: solo leghe con righe aperte/chiuse-da-poco su partite
    già iniziate (mai partite future né leghe senza scommesse)."""
    # aperta su partita iniziata -> inclusa
    tracker.save_match("m1", "Serie A", "Inter", "Napoli", _recent_iso())
    tracker.save_bet("m1", "1X2", "1", None, None, 2.1, 10.0)
    # futura -> esclusa
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    tracker.save_match("m2", "La Liga", "Osasuna", "Getafe", future)
    tracker.save_bet("m2", "1X2", "1", None, None, 2.1, 10.0)
    # nessuna scommessa -> esclusa
    tracker.save_match("m3", "J1 League", "A", "B", _recent_iso())

    leagues = tracker.get_leagues_with_open_rows()
    assert leagues == ["Serie A"]
