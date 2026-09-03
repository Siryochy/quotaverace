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


class TestFormatSchedina:
    """Regressione: format_schedina con picks non deve mai crashare
    (mancava get_pro_stake importato in fixture_engine → la schedina
    delle 08:00 non sarebbe mai partita con picks presenti)."""

    def test_con_picks_non_crasha(self):
        from fixture_engine import format_schedina
        picks = [
            {"evento": "Serie A – Inter vs Napoli", "esito": "1", "quota": 2.21,
             "bookmaker": "Pinnacle", "ev": 0.061, "market_edge": 0.10},
            {"evento": "Serie A – Milan vs Juventus", "esito": "Over 2.5", "quota": 2.38,
             "bookmaker": "Bet365", "ev": 0.095, "market_edge": 0.12},
        ]
        msg = format_schedina(picks, 100.0)
        assert "SCHEDINA DEL GIORNO" in msg
        assert "Inter" in msg and "Over 2.5" in msg
        assert "MULTIPLA" in msg and "EV" in msg

    def test_senza_picks(self):
        from fixture_engine import format_schedina
        msg = format_schedina([], 100.0)
        assert "Nessuna partita" in msg


class TestFormatBetVerdicts:
    """Verdetti puntate a fine partita: notifica Telegram vinta/persa/push."""

    def test_formatta_verdetti(self):
        from bot import format_bet_verdicts
        sets = [
            {"home": "Inter", "away": "Napoli", "league": "Serie A",
             "mercato": "1X2", "esito": "1", "price": 2.10, "stake": 5.0,
             "mode": "dry-run", "outcome": "won", "profit": 5.5},
            {"home": "Milan", "away": "Juventus", "league": "Serie A",
             "mercato": "OU", "esito": "Over 2.5", "price": 1.90, "stake": 5.0,
             "mode": "dry-run", "outcome": "lost", "profit": -5.0},
        ]
        text = format_bet_verdicts(sets)
        assert "ESITO PUNTATE AUTOMATICHE" in text and "DRY-RUN" in text
        assert "✅ *VINTA*" in text and "Inter vs Napoli" in text and "+€5.50" in text
        assert "❌ *PERSA*" in text and "Over 2.5" in text and "-€5.00" in text

    def test_vuoto(self):
        from bot import format_bet_verdicts
        assert format_bet_verdicts([]) == ""

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
        assert "/scan" not in text  # rimosso dal 04/09 (Betfair fuori architettura)
        assert kwargs.get("parse_mode") == "Markdown"


