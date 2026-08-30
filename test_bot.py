"""
Test unitari per QuotaVerace Bot (integrato con Poisson Engine).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from bot import (
    compute_ev,
    format_segnale_pronto,
    prob_1x2,
    prob_over_under,
    cmd_test_segnale,
    cmd_segnale,
    cmd_help,
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
        assert kwargs.get("parse_mode") == "Markdown"
