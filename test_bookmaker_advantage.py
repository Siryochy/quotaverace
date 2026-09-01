"""Test multi-bookmaker price advantage (bookmaker_advantage.py)."""
import pytest

from bookmaker_advantage import (
    identify_sharp_prices, detect_soft_book_lag,
    find_best_prices_with_advantage, analyze_match_bookmakers,
)


def _make_bookmaker(name, outcomes_h2h=None, outcomes_totals=None):
    """Helper per creare un bookmaker fittizio."""
    markets = []
    if outcomes_h2h:
        markets.append({"key": "h2h", "outcomes": outcomes_h2h})
    if outcomes_totals:
        markets.append({"key": "totals", "outcomes": outcomes_totals})
    return {"title": name, "markets": markets}


def _home_away_outcomes(home_price, draw_price, away_price,
                        home_name="Inter", away_name="Juventus"):
    return [
        {"name": home_name, "price": home_price},
        {"name": "Draw", "price": draw_price},
        {"name": away_name, "price": away_price},
    ]


def _over_under_outcomes(over_price, under_price):
    return [
        {"name": "Over 2.5", "price": over_price, "point": 2.5},
        {"name": "Under 2.5", "price": under_price, "point": 2.5},
    ]


# --- identify_sharp_prices ---

class TestIdentifySharpPrices:
    def test_pinnacle_rilevato(self):
        bms = [
            _make_bookmaker("Pinnacle", _home_away_outcomes(1.90, 3.40, 3.95)),
            _make_bookmaker("Bet365", _home_away_outcomes(1.85, 3.30, 3.80)),
        ]
        sharp = identify_sharp_prices(bms, "inter", "juventus")
        assert sharp["1"] == 1.90
        assert sharp["X"] == 3.40
        assert sharp["2"] == 3.95

    def test_nessun_pinnacle(self):
        bms = [_make_bookmaker("Bet365", _home_away_outcomes(1.85, 3.30, 3.80))]
        sharp = identify_sharp_prices(bms, "inter", "juventus")
        assert sharp == {}

    def test_pinnacle_con_over_under(self):
        bms = [
            _make_bookmaker("Pinnacle",
                           _home_away_outcomes(1.90, 3.40, 3.95),
                           _over_under_outcomes(1.88, 1.96)),
        ]
        sharp = identify_sharp_prices(bms, "inter", "juventus")
        assert sharp["Over 2.5"] == 1.88
        assert sharp["Under 2.5"] == 1.96


# --- detect_soft_book_lag ---

class TestDetectSoftBookLag:
    def test_lag_rilevato(self):
        """Bet365 offre 2.15 vs Pinnacle 1.90 → lag di ~13%."""
        bms = [
            _make_bookmaker("Pinnacle", _home_away_outcomes(1.90, 3.40, 3.95)),
            _make_bookmaker("Bet365", [
                {"name": "Inter", "price": 2.15},  # molto più alto di Pinnacle
                {"name": "Draw", "price": 3.40},
                {"name": "Juventus", "price": 3.95},
            ]),
        ]
        lags = detect_soft_book_lag(bms, "inter", "juventus")
        assert len(lags) >= 1
        assert lags[0]["esito"] == "1"
        assert lags[0]["bookmaker"] == "Bet365"
        assert lags[0]["advantage_pct"] > 10

    def test_nessun_lag_se_allineati(self):
        """Tutti i bookmaker sono allineati → nessun lag."""
        bms = [
            _make_bookmaker("Pinnacle", _home_away_outcomes(1.90, 3.40, 3.95)),
            _make_bookmaker("Bet365", _home_away_outcomes(1.89, 3.38, 3.93)),
        ]
        lags = detect_soft_book_lag(bms, "inter", "juventus")
        assert len(lags) == 0

    def test_nessun_lag_senza_pinnacle(self):
        """Senza Pinnacle non possiamo rilevare lag."""
        bms = [_make_bookmaker("Bet365", _home_away_outcomes(2.10, 3.50, 4.00))]
        lags = detect_soft_book_lag(bms, "inter", "juventus")
        assert len(lags) == 0


# --- find_best_prices_with_advantage ---

class TestFindBestPrices:
    def test_miglior_prezzo_selezionato(self):
        bms = [
            _make_bookmaker("Pinnacle", _home_away_outcomes(1.90, 3.40, 3.95)),
            _make_bookmaker("Bet365", [
                {"name": "Inter", "price": 2.15},
                {"name": "Draw", "price": 3.45},
                {"name": "Juventus", "price": 3.80},
            ]),
            _make_bookmaker("William Hill", [
                {"name": "Inter", "price": 2.00},
                {"name": "Draw", "price": 3.50},
                {"name": "Juventus", "price": 4.10},
            ]),
        ]
        result = find_best_prices_with_advantage(bms, "inter", "juventus")
        # Miglior prezzo per Inter: 2.15 (Bet365)
        assert result["1"]["best_price"] == 2.15
        assert result["1"]["best_book"] == "Bet365"
        assert result["1"]["advantage_pct"] > 0


# --- analyze_match_bookmakers ---

class TestAnalyzeMatchBookmakers:
    def test_analisi_completa(self):
        match = {
            "id": "m1",
            "home_team": "Inter", "away_team": "Juventus",
            "bookmakers": [
                _make_bookmaker("Pinnacle",
                               _home_away_outcomes(1.90, 3.40, 3.95)),
                _make_bookmaker("Bet365", [
                    {"name": "Inter", "price": 2.10},
                    {"name": "Draw", "price": 3.40},
                    {"name": "Juventus", "price": 3.95},
                ]),
            ],
        }
        result = analyze_match_bookmakers(match)
        assert result["n_bookmakers"] == 2
        assert result["has_sharp"]
        assert "best_prices" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
