"""
Test unitari per surebet_scanner.py.
"""

import pytest

from surebet_scanner import (
    calculate_inverse_sum,
    calculate_stake_allocation,
    detect_surebet,
    scan_surebets,
    SurebetOpportunity,
    get_mock_odds_for_testing,
    format_telegram_notification,
)


class TestCalculateInverseSum:
    def test_surebet_valida(self):
        # 1/2.20 + 1/3.60 + 1/5.00 = 0.4545 + 0.2778 + 0.2000 = 0.9323 < 1.0
        quote = [2.20, 3.60, 5.00]
        inv_sum = calculate_inverse_sum(quote)
        assert inv_sum < 1.0
        assert inv_sum == pytest.approx(0.9323, abs=0.001)

    def test_mercato_efficiente(self):
        quote = [2.0, 2.0]
        assert calculate_inverse_sum(quote) == pytest.approx(1.0)

    def test_vantaggio_bookmaker(self):
        quote = [1.90, 1.90]
        assert calculate_inverse_sum(quote) > 1.0

    def test_quota_invalida_scartata(self):
        quote = [2.0, 0.5, 2.0]
        assert calculate_inverse_sum(quote) == pytest.approx(1.0)


class TestCalculateStakeAllocation:
    def test_somma_allocazioni_uguale_stake(self):
        quote = [2.20, 3.60, 5.00]
        alloc = calculate_stake_allocation(quote, total_stake=100.0)
        assert sum(alloc) == pytest.approx(100.0, abs=0.5)

    def test_allocazione_proporzionale(self):
        quote = [2.0, 2.0]
        alloc = calculate_stake_allocation(quote, total_stake=100.0)
        assert alloc[0] == pytest.approx(alloc[1], abs=0.01)

    def test_allocazione_surebet(self):
        quote = [2.20, 3.60, 5.00]
        alloc = calculate_stake_allocation(quote, total_stake=100.0)
        # Quota più bassa → allocazione più alta
        assert alloc[0] > alloc[1] > alloc[2]


class TestDetectSurebet:
    def test_nessuna_opportunita_con_quote_simili(self):
        odds = [
            {"bookmaker": "A", "evento": "Test", "esito": "1", "quota_decimale": 2.0},
            {"bookmaker": "B", "evento": "Test", "esito": "2", "quota_decimale": 1.90},
        ]
        result = detect_surebet(odds, min_margin=0.01)
        assert result is None

    def test_surebet_individuata(self):
        # 1/2.20 + 1/3.60 + 1/5.00 = 0.9323 < 1.0 → surebet
        odds = [
            {"bookmaker": "A", "evento": "Test", "esito": "1", "quota_decimale": 2.20},
            {"bookmaker": "B", "evento": "Test", "esito": "X", "quota_decimale": 3.60},
            {"bookmaker": "C", "evento": "Test", "esito": "2", "quota_decimale": 5.00},
        ]
        result = detect_surebet(odds, min_margin=0.001)
        assert result is not None
        assert result.margin < 0
        assert result.rendimento_atteso > 0
        assert result.mercato == "1X2"

    def test_mercato_incompleto_scartato(self):
        odds = [
            {"bookmaker": "A", "evento": "Test", "esito": "1", "quota_decimale": 2.0},
        ]
        result = detect_surebet(odds)
        assert result is None

    def test_dato_incompleto_scartato(self):
        odds = [
            {"bookmaker": "A", "evento": "Test", "esito": "", "quota_decimale": 2.0},
            {"bookmaker": "B", "evento": "Test", "esito": "2", "quota_decimale": 1.90},
        ]
        result = detect_surebet(odds)
        assert result is None

    def test_quota_minore_uguale_1_scartata(self):
        odds = [
            {"bookmaker": "A", "evento": "Test", "esito": "1", "quota_decimale": 0.95},
            {"bookmaker": "B", "evento": "Test", "esito": "2", "quota_decimale": 2.0},
        ]
        result = detect_surebet(odds)
        assert result is None

    def test_prende_quota_migliore_per_esito(self):
        odds = [
            {"bookmaker": "A", "evento": "Test", "esito": "1", "quota_decimale": 2.0},
            {"bookmaker": "B", "evento": "Test", "esito": "1", "quota_decimale": 2.50},  # migliore
            {"bookmaker": "C", "evento": "Test", "esito": "2", "quota_decimale": 3.0},
        ]
        result = detect_surebet(odds, min_margin=0.001)
        assert result is not None
        assert 2.50 in result.quote


class TestScanSurebets:
    def test_mock_data_restituisce_opportunita(self):
        mock = get_mock_odds_for_testing()
        df = pd.DataFrame(mock)
        results = scan_surebets(df, min_margin=0.001)
        assert len(results) >= 1
        # Verifica che sia la Surebet Test
        assert any(r.evento == "Serie A – Surebet Test" for r in results)

    def test_filtro_margin_troppo_alto(self):
        mock = get_mock_odds_for_testing()
        df = pd.DataFrame(mock)
        results = scan_surebets(df, min_margin=0.50)
        assert len(results) == 0

    def test_nessun_duplicato(self):
        mock = get_mock_odds_for_testing()
        df = pd.DataFrame(mock)
        results = scan_surebets(df)
        eventi = [r.evento for r in results]
        assert len(eventi) == len(set(eventi))

    def test_ordinamento_decrescente(self):
        mock = get_mock_odds_for_testing()
        df = pd.DataFrame(mock)
        results = scan_surebets(df)
        if len(results) >= 2:
            assert results[0].rendimento_atteso >= results[-1].rendimento_atteso


class TestTelegramFormat:
    def test_contiene_disclaimer(self):
        opp = SurebetOpportunity(
            timestamp="2024-01-01T00:00:00+00:00",
            evento="Test",
            mercato="1X2",
            esiti=("1", "X", "2"),
            bookmakers=("A", "B", "C"),
            quote=(2.2, 3.6, 5.0),
            margin=-0.0677,
            allocazioni=(48.7, 29.7, 21.4),
            rendimento_atteso=6.77,
            fonte_dati="mock",
            nota_limitazione="Test",
        )
        text = format_telegram_notification(opp)
        assert "Gioca responsabilmente" in text
        assert "www.adm.gov.it" in text
        assert "NON è garantita" in text.lower() or "non è garantita" in text.lower()

    def test_non_contiene_garantito(self):
        opp = SurebetOpportunity(
            timestamp="2024-01-01T00:00:00+00:00",
            evento="Test",
            mercato="1X2",
            esiti=("1", "X", "2"),
            bookmakers=("A", "B", "C"),
            quote=(2.2, 3.6, 5.0),
            margin=-0.0677,
            allocazioni=(48.7, 29.7, 21.4),
            rendimento_atteso=6.77,
            fonte_dati="mock",
            nota_limitazione="Test",
        )
        text = format_telegram_notification(opp)
        assert "priva di rischi" not in text.lower()
        assert "sicuro" not in text.lower()

    def test_contiene_allocazioni(self):
        opp = SurebetOpportunity(
            timestamp="2024-01-01T00:00:00+00:00",
            evento="Test",
            mercato="1X2",
            esiti=("1", "X", "2"),
            bookmakers=("A", "B", "C"),
            quote=(2.2, 3.6, 5.0),
            margin=-0.0677,
            allocazioni=(48.7, 29.7, 21.4),
            rendimento_atteso=6.77,
            fonte_dati="mock",
            nota_limitazione="Test",
        )
        text = format_telegram_notification(opp)
        assert "puntata" in text.lower()
        assert "48.7" in text or "48" in text
