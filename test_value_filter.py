"""
Test unitari per value_filter.py.

Verifica:
- compute_ev isolato con casi edge;
- filter_value_bets identifichi correttamente ≥3 value bet con EV > 5 %;
- scarto degli esiti con EV ≤ 5 %;
- threshold configurabile;
- gestione DataFrame vuoti.
"""

import pandas as pd
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

    def test_ev_alta_probabilità(self):
        assert compute_ev(0.80, 1.50) == pytest.approx(0.20)


class TestFilterValueBets:
    """Test del filtro value bet su dataset Serie A mock."""

    @pytest.fixture
    def odds_df(self):
        """DataFrame quote da bookmaker (input odds_ingest)."""
        return pd.DataFrame({
            "bookmaker": ["Bet365", "Snai", "Bet365", "Snai",
                          "Bet365", "Snai", "Bet365", "Snai", "Bet365"],
            "evento": [
                "Serie A – Roma vs Empoli",
                "Serie A – Roma vs Empoli",
                "Serie A – Inter vs Milan",
                "Serie A – Inter vs Milan",
                "Serie A – Atalanta vs Milan",
                "Serie A – Atalanta vs Milan",
                "Serie A – Sassuolo vs Napoli",
                "Serie A – Juventus vs Milan",
                "Serie A – Juventus vs Milan",
            ],
            "sport": ["calcio"] * 9,
            "esito": [
                "Over 2.5", "Under 2.5",
                "1", "X",
                "1", "2",
                "2",
                "X", "1",
            ],
            "quota_decimale": [2.10, 1.75, 1.90, 3.60,
                               2.00, 3.40, 1.55, 3.40, 2.00],
            "timestamp": pd.to_datetime(
                ["2024-09-17T15:00:00Z"] * 2 +
                ["2024-09-16T20:45:00Z"] * 3 +
                ["2024-09-15T18:00:00Z"] * 2 +
                ["2024-09-14T20:45:00Z"] * 2,
                utc=True,
            ),
        })

    @pytest.fixture
    def probs_df(self):
        """DataFrame probabilità stimate da poisson_engine."""
        return pd.DataFrame({
            "evento": [
                "Serie A – Roma vs Empoli",
                "Serie A – Roma vs Empoli",
                "Serie A – Inter vs Milan",
                "Serie A – Inter vs Milan",
                "Serie A – Inter vs Milan",
                "Serie A – Atalanta vs Milan",
                "Serie A – Atalanta vs Milan",
                "Serie A – Atalanta vs Milan",
                "Serie A – Sassuolo vs Napoli",
                "Serie A – Sassuolo vs Napoli",
                "Serie A – Sassuolo vs Napoli",
                "Serie A – Juventus vs Milan",
                "Serie A – Juventus vs Milan",
                "Serie A – Juventus vs Milan",
            ],
            "esito": [
                "Over 2.5", "Under 2.5",
                "1", "X", "2",
                "1", "X", "2",
                "1", "X", "2",
                "1", "X", "2",
            ],
            "probabilità": [
                0.554, 0.446,
                0.720, 0.170, 0.110,
                0.570, 0.210, 0.220,
                0.180, 0.180, 0.640,
                0.480, 0.260, 0.260,
            ],
        })

    def test_identifica_almeno_tre_value_bet(self, odds_df, probs_df):
        result = filter_value_bets(odds_df, probs_df, threshold=0.05)
        assert len(result) >= 3

    def test_value_bet_corretti(self, odds_df, probs_df):
        result = filter_value_bets(odds_df, probs_df, threshold=0.05)

        assert any(
            (result["evento"] == "Serie A – Roma vs Empoli") &
            (result["esito"] == "Over 2.5")
        )
        assert any(
            (result["evento"] == "Serie A – Inter vs Milan") &
            (result["esito"] == "1")
        )
        assert any(
            (result["evento"] == "Serie A – Atalanta vs Milan") &
            (result["esito"] == "1")
        )

    def test_scarta_ev_minore_uguale_soglia(self, odds_df, probs_df):
        result = filter_value_bets(odds_df, probs_df, threshold=0.05)

        assert not any(
            (result["evento"] == "Serie A – Sassuolo vs Napoli") &
            (result["esito"] == "2")
        )
        assert not any(
            (result["evento"] == "Serie A – Juventus vs Milan") &
            (result["esito"] == "X")
        )
        assert not any(
            (result["evento"] == "Serie A – Juventus vs Milan") &
            (result["esito"] == "1")
        )

    def test_threshold_configurabile(self, odds_df, probs_df):
        result = filter_value_bets(odds_df, probs_df, threshold=0.30)
        assert len(result) == 1
        assert result.iloc[0]["evento"] == "Serie A – Inter vs Milan"
        assert result.iloc[0]["esito"] == "1"

    def test_colonne_output_corrette(self, odds_df, probs_df):
        result = filter_value_bets(odds_df, probs_df, threshold=0.05)
        expected_cols = {"sport", "evento", "esito", "quota_decimale",
                         "probabilità", "ev", "timestamp"}
        assert set(result.columns) == expected_cols

    def test_ordinamento_ev_decrescente(self, odds_df, probs_df):
        result = filter_value_bets(odds_df, probs_df, threshold=0.05)
        evs = result["ev"].tolist()
        assert evs == sorted(evs, reverse=True)

    def test_dataframe_vuoto_se_nessun_match(self, odds_df):
        empty_probs = pd.DataFrame({
            "evento": [], "esito": [], "probabilità": []
        })
        result = filter_value_bets(odds_df, empty_probs, threshold=0.05)
        assert result.empty
        assert set(result.columns) == {
            "sport", "evento", "esito", "quota_decimale",
            "probabilità", "ev", "timestamp",
        }
