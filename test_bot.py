"""
Test unitari per QuotaVerace Bot (integrato con Poisson Engine).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot import (
    compute_ev,
    format_segnale_pronto,
    prob_1x2,
    prob_over_under,
    cmd_test_segnale,
    cmd_segnale,
    cmd_help,
    cmd_scan,
    format_scan_result,
)


class TestComputeEv:
    def test_ev_positivo(self):
        assert compute_ev(0.50, 2.20) == pytest.approx(0.10)

    def test_ev_negativo(self):
        assert compute_ev(0.30, 2.00) == pytest.approx(-0.40)

    def test_ev_zero(self):
        assert compute_ev(0.50, 2.00) == pytest.approx(0.0)


class TestProbabilitaPoisson:
    def test_prob_1x2_somma_a_1(self):
        p1, px, p2 = prob_1x2(1.5, 1.0)
        assert abs((p1 + px + p2) - 1.0) < 0.01

    def test_prob_over_under_somma_a_1(self):
        p_over, p_under = prob_over_under(1.5, 1.0)
        assert abs((p_over + p_under) - 1.0) < 0.01

    def test_favorito_casa_con_lambda_maggiore(self):
        p1, px, p2 = prob_1x2(2.5, 0.8)
        assert p1 > p2


class TestFormatSegnalePronto:
    def test_contiene_expected_goals(self):
        text = format_segnale_pronto("Roma", "Empoli", 2.28, 0.63)
        assert "Expected Goals" in text
        assert "Roma" in text
        assert "Empoli" in text

    def test_contiene_prob_1x2(self):
        text = format_segnale_pronto("Roma", "Empoli", 2.28, 0.63)
        assert "1:" in text
        assert "X:" in text
        assert "2:" in text

    def test_contiene_over_under(self):
        text = format_segnale_pronto("Roma", "Empoli", 2.28, 0.63)
        assert "Over 2.5" in text
        assert "Under 2.5" in text

    def test_contiene_header_segnale(self):
        text = format_segnale_pronto("Roma", "Empoli", 2.28, 0.63)
        assert "SEGNALE PRONTO" in text

    def test_contiene_disclaimer(self):
        text = format_segnale_pronto("Roma", "Empoli", 2.28, 0.63)
        assert "Gioca responsabilmente" in text
        assert "www.adm.gov.it" in text


class TestHandlerTelegram:
    def _make_update(self, text="/test_segnale"):
        update = MagicMock()
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        update.message.text = text
        return update

    @pytest.mark.asyncio
    async def test_cmd_test_segnale_invia_messaggio_corretto(self):
        update = self._make_update()
        context = MagicMock()

        await cmd_test_segnale(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.await_args
        text = args[0] if args else kwargs.get("text")
        parse_mode = kwargs.get("parse_mode")

        assert "SEGNALE PRONTO" in text
        assert "Inter" in text
        assert "Expected Goals" in text
        assert "Gioca responsabilmente" in text
        assert "www.adm.gov.it" in text
        assert parse_mode == "Markdown"

    @pytest.mark.asyncio
    async def test_cmd_segnale_partita_valida(self):
        update = self._make_update("/segnale Roma Empoli")
        context = MagicMock()
        context.args = ["Roma", "Empoli"]

        await cmd_segnale(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.await_args
        text = args[0] if args else kwargs.get("text")

        assert "Roma" in text
        assert "Empoli" in text
        assert "SEGNALE PRONTO" in text
        assert "Gioca responsabilmente" in text
        assert kwargs.get("parse_mode") == "Markdown"

    @pytest.mark.asyncio
    async def test_cmd_segnale_squadra_non_trovata(self):
        update = self._make_update("/segnale SquadraInventata Altra")
        context = MagicMock()
        context.args = ["SquadraInventata", "Altra"]

        await cmd_segnale(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.await_args
        text = args[0] if args else kwargs.get("text")

        assert "❌" in text
        assert "SquadraInventata" in text
        assert kwargs.get("parse_mode") == "Markdown"

    @pytest.mark.asyncio
    async def test_cmd_segnale_argomenti_mancanti(self):
        update = self._make_update("/segnale")
        context = MagicMock()
        context.args = []

        await cmd_segnale(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.await_args
        text = args[0] if args else kwargs.get("text")

        assert "❌" in text
        assert "Errore" in text
        assert kwargs.get("parse_mode") == "Markdown"

    @pytest.mark.asyncio
    async def test_cmd_help_mostra_comandi(self):
        update = self._make_update("/help")
        context = MagicMock()

        await cmd_help(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.await_args
        text = args[0] if args else kwargs.get("text")

        assert "QuotaVerace Pro" in text
        assert "Comandi" in text
        assert "/segnale" in text
        assert "/scan" in text
        assert kwargs.get("parse_mode") == "Markdown"


# ---------------------------------------------------------------------------
# /scan — scansione Betfair su richiesta
# ---------------------------------------------------------------------------

class FakeBetfairClient:
    """Client Betfair finto per /scan: nessuna rete."""

    def __init__(self, catalogue=None, books=None):
        self._catalogue = catalogue or []
        self._books = books or {}

    def list_market_catalogue(self, market_filter, max_results=200, market_projection=None):
        mtype = market_filter["marketTypeCodes"][0]
        return [m for m in self._catalogue if m["marketType"] == mtype]

    def list_market_book(self, market_ids, price_projection=None):
        return [self._books[mid] for mid in market_ids if mid in self._books]


def _fake_catalogue():
    return [
        {
            "marketId": "1.100", "marketType": "MATCH_ODDS",
            "marketStartTime": "2026-08-31T13:00:00.000Z",
            "event": {"id": "311", "name": "Roma v Empoli"},
            "runners": [
                {"selectionId": 101, "runnerName": "Roma"},
                {"selectionId": 102, "runnerName": "Empoli"},
            ],
        },
        {
            "marketId": "1.200", "marketType": "OVER_UNDER_25",
            "marketStartTime": "2026-08-31T20:45:00.000Z",
            "event": {"id": "312", "name": "Inter v Milan"},
            "runners": [{"selectionId": 201, "runnerName": "Over 2.5"}],
        },
    ]


def _fake_books():
    return {
        "1.100": {"marketId": "1.100", "runners": [
            {"selectionId": 101, "ex": {"availableToBack": [{"price": 2.10, "size": 100.0}]}},
            {"selectionId": 102, "ex": {"availableToBack": [{"price": 3.40, "size": 80.0}]}},
        ]},
        "1.200": {"marketId": "1.200", "runners": [
            {"selectionId": 201, "ex": {"availableToBack": [{"price": 1.85, "size": 70.0}]}},
        ]},
    }


class TestCmdScan:
    def _make_update(self, text="/scan"):
        update = MagicMock()
        update.message = MagicMock()
        note = MagicMock()  # il messaggio di progresso restituito da reply_text
        note.edit_text = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=note)
        update._scan_note = note  # riferimento per gli assert nel test
        update.message.text = text
        return update

    @pytest.mark.asyncio
    async def test_non_configurato_mostra_env_richieste(self):
        update = self._make_update()
        context = MagicMock()
        context.args = []
        with patch("bot.get_betfair_client", return_value=None):
            await cmd_scan(update, context)
        text = update.message.reply_text.await_args.args[0]
        assert "Betfair non configurato" in text
        assert "BETFAIR_APP_KEY" in text

    @pytest.mark.asyncio
    async def test_data_non_valida(self):
        update = self._make_update()
        context = MagicMock()
        context.args = ["31/08/2026"]
        with patch("bot.get_betfair_client", return_value=FakeBetfairClient()):
            await cmd_scan(update, context)
        text = update.message.reply_text.await_args.args[0]
        assert "Data non valida" in text

    @pytest.mark.asyncio
    async def test_scan_completa_con_client_finto(self):
        update = self._make_update()
        context = MagicMock()
        context.args = ["2026-08-31"]
        client = FakeBetfairClient(catalogue=_fake_catalogue(), books=_fake_books())
        with patch("bot.get_betfair_client", return_value=client):
            await cmd_scan(update, context)
        # prima nota di progresso, poi risultato via edit_text
        progress = update.message.reply_text.await_args.args[0]
        assert "Scansione" in progress
        text = update._scan_note.edit_text.await_args.args[0]
        assert "SCANSIONE BETFAIR" in text
        assert "Roma v Empoli" in text
        assert "Inter v Milan" in text
        assert "Gioca responsabilmente" in text

    @pytest.mark.asyncio
    async def test_scan_errore_api_notifica_fallimento(self):
        class BrokenClient(FakeBetfairClient):
            def list_market_catalogue(self, *a, **k):
                raise RuntimeError("API giu'")

        update = self._make_update()
        context = MagicMock()
        context.args = []
        with patch("bot.get_betfair_client", return_value=BrokenClient()):
            await cmd_scan(update, context)
        text = update._scan_note.edit_text.await_args.args[0]
        assert "❌" in text
        assert "fallita" in text


class TestFormatScanResult:

    def _result(self, opps, events=2, markets=3):
        return {"day": "2026-08-31", "events": events, "markets": markets,
                "opportunities": opps}

    def test_header_con_conteggi(self):
        text = format_scan_result(self._result([]))
        assert "2026-08-31" in text
        assert "Eventi: 2" in text
        assert "Mercati: 3"

    def test_vuoto_mostra_messaggio(self):
        text = format_scan_result(self._result([]))
        assert "Nessun prezzo disponibile" in text
        assert "Gioca responsabilmente" in text

    def test_mostra_prezzi_ordinati_per_evento(self):
        opps = [
            {"event_name": "Roma v Empoli", "start_time": "2026-08-31T13:00:00.000Z",
             "market_type": "MATCH_ODDS", "selection_name": "Roma", "price": 2.10},
            {"event_name": "Roma v Empoli", "start_time": "2026-08-31T13:00:00.000Z",
             "market_type": "OVER_UNDER_25", "selection_name": "Over 2.5", "price": 1.85},
        ]
        text = format_scan_result(self._result(opps))
        assert "Roma v Empoli" in text
        assert "@ 2.10" in text
        assert "@ 1.85" in text
        assert "MATCH_ODDS" in text

    def test_tronca_al_limite_telegram(self):
        opps = [
            {"event_name": f"Squadra{i} v Avversaria{i}",
             "start_time": f"2026-08-31T{i % 24:02d}:00:00.000Z",
             "market_type": "MATCH_ODDS", "selection_name": f"Squadra{i}",
             "price": 2.0}
            for i in range(200)
        ]
        text = format_scan_result(self._result(opps, events=200, markets=200))
        assert len(text) <= 4096

    def test_nota_eventi_omessi(self):
        opps = [
            {"event_name": f"Partita{i} v Altra{i}",
             "start_time": f"2026-08-31T{i:02d}:00:00.000Z",
             "market_type": "MATCH_ODDS", "selection_name": f"Partita{i}",
             "price": 2.0}
            for i in range(12)
        ]
        text = format_scan_result(self._result(opps, events=12, markets=12))
        assert "altri" in text
