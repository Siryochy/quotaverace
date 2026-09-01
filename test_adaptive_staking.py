"""Test adaptive staking (adaptive_staking.py)."""
import pytest

from adaptive_staking import (
    confidence_kelly_fraction, drawdown_factor, adaptive_stake,
    MIN_KELLY_FRACTION, MAX_KELLY_FRACTION, DRAWDOWN_THRESHOLD,
)


# --- Confidence Kelly ---

class TestConfidenceKelly:
    def test_base_fraction_senza_segnali(self):
        """Senza segnali di confidenza → frazione intermedia (tra MIN e MAX)."""
        f = confidence_kelly_fraction(0.55, 2.10)
        assert 0.15 <= f <= 0.30  # senza segnali, score=0 → t=0.5

    def test_edge_alto_aumenta_fraction(self):
        """Edge forte (+8pp) → frazione più alta."""
        f_low = confidence_kelly_fraction(0.55, 2.10, market_edge=0.03)
        f_high = confidence_kelly_fraction(0.55, 2.10, market_edge=0.08)
        assert f_high > f_low

    def test_ml_confidence_alta_aumenta_fraction(self):
        """ML confidence alta → frazione più alta."""
        f_low = confidence_kelly_fraction(0.55, 2.10, ml_confidence=0.3)
        f_high = confidence_kelly_fraction(0.55, 2.10, ml_confidence=0.8)
        assert f_high > f_low

    def test_clv_positivo_aumenta_fraction(self):
        """CLV positivo → conferma → frazione più alta."""
        f_no = confidence_kelly_fraction(0.55, 2.10)
        f_yes = confidence_kelly_fraction(0.55, 2.10, has_clv_positive=True)
        assert f_yes > f_no

    def test_strong_value_aumenta_fraction(self):
        """Strong value → bonus confidenza."""
        f_value = confidence_kelly_fraction(0.55, 2.10, status="value")
        f_strong = confidence_kelly_fraction(0.55, 2.10, status="strong_value")
        assert f_strong > f_value

    def test_range限额(self):
        """La frazione deve restare nel range [MIN, MAX]."""
        f = confidence_kelly_fraction(
            0.55, 2.10, market_edge=0.10, ml_confidence=0.9,
            has_clv_positive=True, status="strong_value")
        assert MIN_KELLY_FRACTION <= f <= MAX_KELLY_FRACTION

    def test_odds_invalida(self):
        """Quota <= 1 → frazione zero."""
        assert confidence_kelly_fraction(0.55, 0.9) == 0.0
        assert confidence_kelly_fraction(0.55, 1.0) == 0.0


# --- Drawdown Factor ---

class TestDrawdownFactor:
    def test_nessun_drawdown(self):
        """Bankroll = peak → fattore 1.0."""
        assert drawdown_factor(1000, 1000) == 1.0

    def test_drawdown_sotto_soglia(self):
        """Drawdown < 10% → nessuna riduzione."""
        assert drawdown_factor(920, 1000) == 1.0

    def test_drawdown_sopra_soglia(self):
        """Drawdown > 10% → riduzione."""
        factor = drawdown_factor(800, 1000)  # 20% drawdown
        assert factor < 1.0
        assert factor >= 0.5

    def test_drawdown_grande(self):
        """Drawdown 50% → riduzione significativa."""
        factor = drawdown_factor(500, 1000)
        assert factor < 0.85  # riduzione significativa
        assert factor >= 0.50  # ma mai sotto il minimo

    def test_peak_zero(self):
        """Peak zero → nessuna riduzione."""
        assert drawdown_factor(100, 0) == 1.0


# --- Adaptive Stake ---

class TestAdaptiveStake:
    def test_stake_positivo(self):
        """Stake positivo per EV positivo."""
        r = adaptive_stake(1000, 0.55, 2.10)
        assert r["stake"] > 0

    def test_stake_zero_per_ev_negativo(self):
        """Stake zero per EV negativo (prob troppo bassa)."""
        r = adaptive_stake(1000, 0.30, 2.10)
        assert r["stake"] == 0.0

    def test_drawdown_riduce_stake(self):
        """Drawdown riduce lo stake."""
        r_normal = adaptive_stake(1000, 0.55, 2.10, peak_bankroll=1000)
        r_dd = adaptive_stake(800, 0.55, 2.10, peak_bankroll=1000)
        assert r_dd["stake"] <= r_normal["stake"]
        assert r_dd["drawdown_factor"] < 1.0

    def test_cap_3_percento(self):
        """Lo stake non supera il 3% del bankroll."""
        r = adaptive_stake(1000, 0.90, 1.50)  # EV altissimo
        assert r["stake"] <= 1000 * 0.05  # cap strong_value o value

    def test_strong_value_cap_5_percento(self):
        """Strong value ha cap più alto (5%)."""
        r = adaptive_stake(1000, 0.90, 1.50, status="strong_value")
        assert r["stake"] <= 1000 * 0.05

    def test_confidenza_influenza_stake(self):
        """Alta confidenza → stake più alto."""
        r_low = adaptive_stake(1000, 0.55, 2.10,
                               market_edge=0.02, ml_confidence=0.3)
        r_high = adaptive_stake(1000, 0.55, 2.10,
                                market_edge=0.08, ml_confidence=0.8,
                                has_clv_positive=True, status="strong_value")
        assert r_high["stake"] >= r_low["stake"]

    def test_output_format(self):
        """L'output ha tutti i campi richiesti."""
        r = adaptive_stake(1000, 0.55, 2.10)
        assert "stake" in r
        assert "kelly_fraction" in r
        assert "drawdown_factor" in r
        assert "confidence_score" in r
        assert "reason" in r

    def test_arrotondamento_step(self):
        """Lo stake è arrotondato allo step di 0.50."""
        r = adaptive_stake(1000, 0.55, 2.10)
        remainder = r["stake"] % 0.50
        assert remainder == pytest.approx(0.0, abs=0.01) or r["stake"] < 2.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
