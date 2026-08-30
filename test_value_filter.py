"""
Test unitari per value_filter.py (API attuale: lista di dict).

Verifica:
- compute_ev isolato con casi edge;
- filter_value_bets identifichi value bet con EV >= soglia e filtri di sanita';
- threshold configurabile;
- gestione lista vuota.
"""

import pytest

from value_filter import compute_ev, filter_value_bets


class TestComputeEv:
    """Test della funzione pura compute_ev."""

    def test_ev_positivo(self):
        assert compute_ev(0.50, 2.20) == pytest.approx(0.10)

    def test_ev_negativo(self):
        assert compute_ev(0.30, 2.00) == pytest.approx(-0.40)

    def test_ev_zero(self):
        assert compute_ev(0.50, 2.00) == pytest.approx(0.0)

    def test_ev_alta_probabilita(self):
        assert compute_ev(0.80, 1.50) == pytest.approx(0.20)


class TestFilterValueBets:
    """Test del filtro value bet su dataset Serie A mock (API dict)."""

    @pytest.fixture
    def odds_data(self):
        """Quote con probabilita' gia' stimate dal modello."""
        return [
            {"bookmaker": "Bet365", "evento": "Serie A – Roma vs Empoli", "sport": "calcio",
             "esito": "Over 2.5", "quota_decimale": 2.00, "probabilita": 0.554,
             "timestamp": "2024-09-17T15:00:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Roma vs Empoli", "sport": "calcio",
             "esito": "Under 2.5", "quota_decimale": 1.75, "probabilita": 0.446,
             "timestamp": "2024-09-17T15:00:00Z"},
            {"bookmaker": "Bet365", "evento": "Serie A – Inter vs Milan", "sport": "calcio",
             "esito": "1", "quota_decimale": 1.55, "probabilita": 0.720,
             "timestamp": "2024-09-16T20:45:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Inter vs Milan", "sport": "calcio",
             "esito": "X", "quota_decimale": 3.60, "probabilita": 0.170,
             "timestamp": "2024-09-16T20:45:00Z"},
            {"bookmaker": "Bet365", "evento": "Serie A – Atalanta vs Milan", "sport": "calcio",
             "esito": "1", "quota_decimale": 2.00, "probabilita": 0.570,
             "timestamp": "2024-09-15T18:00:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Atalanta vs Milan", "sport": "calcio",
             "esito": "2", "quota_decimale": 3.40, "probabilita": 0.220,
             "timestamp": "2024-09-15T18:00:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Sassuolo vs Napoli", "sport": "calcio",
             "esito": "2", "quota_decimale": 1.55, "probabilita": 0.640,
             "timestamp": "2024-09-14T20:45:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Juventus vs Milan", "sport": "calcio",
             "esito": "X", "quota_decimale": 3.40, "probabilita": 0.260,
             "timestamp": "2024-09-14T20:45:00Z"},
            {"bookmaker": "Bet365", "evento": "Serie A – Juventus vs Milan", "sport": "calcio",
             "esito": "1", "quota_decimale": 2.00, "probabilita": 0.480,
             "timestamp": "2024-09-14T20:45:00Z"},
        ]

    def _has(self, result, evento, esito):
        return any(r.get("evento") == evento and r.get("esito") == esito for r in result)

    def test_identifica_almeno_tre_value_bet(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.05)
        assert len(result) >= 3

    def test_value_bet_corretti(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.05)
        assert self._has(result, "Serie A – Roma vs Empoli", "Over 2.5")
        assert self._has(result, "Serie A – Inter vs Milan", "1")
        assert self._has(result, "Serie A – Atalanta vs Milan", "1")

    def test_scarta_ev_minore_uguale_soglia(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.05)
        assert not self._has(result, "Serie A – Sassuolo vs Napoli", "2")
        assert not self._has(result, "Serie A – Juventus vs Milan", "X")
        assert not self._has(result, "Serie A – Juventus vs Milan", "1")

    def test_threshold_configurabile(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.12)
        assert len(result) == 1
        assert result[0]["evento"] == "Serie A – Atalanta vs Milan"
        assert result[0]["esito"] == "1"

    def test_campi_output_corretti(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.05)
        required = {"sport", "evento", "esito", "quota_decimale",
                    "probabilita", "ev", "timestamp"}
        assert result
        for r in result:
            assert required.issubset(set(r.keys()))

    def test_ordinamento_ev_decrescente(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.05)
        evs = [r["ev"] for r in result]
        assert evs == sorted(evs, reverse=True)

    def test_lista_vuota_se_nessun_match(self):
        result = filter_value_bets([], ev_threshold=0.05)
        assert result == []
