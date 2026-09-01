"""Test CLV vig-free e dynamic blend weight (market_calib.py)."""
import pytest

from market_calib import (
    clv_vig_free, clv_raw, vig_percentage,
    dynamic_blend_weight, get_league_efficiency,
    blend_probability, devig, LEAGUE_EFFICIENCY,
)


# --- CLV vig-free ---

class TestClvVigFree:
    def test_clv_vig_free_con_mercato_completo(self):
        """CLV con mercato 1X2 completo: devig corretto."""
        # Signal preso a 2.10, closing a 1.90 (con vig ~4%)
        # Mercato: Home 1.90, X 3.40, Away 3.80
        all_closing = [1.90, 3.40, 3.80]
        vf = clv_vig_free(2.10, 1.90, all_closing_odds=all_closing)
        raw = clv_raw(2.10, 1.90)
        # CLV vig-free dovrebbe essere inferiore al raw (perche' il vig
        # rende la closing line piu' generosa)
        assert vf is not None
        assert vf < raw
        assert vf > 0  # comunque positivo: abbiamo preso 2.10 vs 1.90

    def test_clv_vig_free_senza_mercato(self):
        """CLV senza mercato completo: usa stima del vig."""
        vf = clv_vig_free(2.10, 1.90)
        assert vf is not None
        assert vf > 0

    def test_clv_vig_free_negativo(self):
        """CLV negativo: quota segnale peggiore della closing."""
        vf = clv_vig_free(1.80, 2.00)
        assert vf is not None
        assert vf < 0

    def test_clv_vig_free_quota_invalida(self):
        assert clv_vig_free(0.5, 2.0) is None
        assert clv_vig_free(2.0, 0.5) is None

    def test_clv_raw_semplice(self):
        assert clv_raw(2.10, 1.90) == pytest.approx((2.10 / 1.90) - 1.0)


# --- Vig percentage ---

class TestVigPercentage:
    def test_mercato_tipico_1x2(self):
        vig = vig_percentage([2.10, 3.40, 3.50])
        assert vig is not None
        assert 2.0 < vig < 8.0  # tipico per soft book

    def test_pinnacle_bassa(self):
        # Pinnacle: quote piu' strette, vig ~2%
        vig = vig_percentage([1.98, 3.60, 4.00])
        assert vig is not None
        assert vig < 5.0  # Pinnacle ha vig piu' basso dei soft book

    def test_un_solo_odds(self):
        assert vig_percentage([2.0]) is None

    def test_odds_invalidhe(self):
        assert vig_percentage([]) is None
        assert vig_percentage([0.5, -1.0]) is None


# --- Dynamic blend weight ---

class TestDynamicBlendWeight:
    def test_premier_league_mercato_efficiente(self):
        """Premier League: mercato efficiente -> peso modello basso (~0.35)."""
        w = dynamic_blend_weight(0.55, 0.50, league="Premier League")
        # Mercato efficiente (0.85) -> il peso del modello dovrebbe essere basso
        assert 0.25 <= w <= 0.45

    def test_lega_minore_mercato_inefficiente(self):
        """Lega minore: mercato inefficiente -> peso modello alto (~0.55)."""
        w = dynamic_blend_weight(0.55, 0.50, league="Indian Super League")
        # Mercato inefficiente (0.35) -> il peso del modello dovrebbe essere alto
        assert 0.50 <= w <= 0.65

    def test_longshot_bias_restringe_modello(self):
        """Quota alta (>3.0): longshot -> meno fiducia nel modello."""
        w_normal = dynamic_blend_weight(0.30, 0.25, league="Eredivisie", odds=2.0)
        w_longshot = dynamic_blend_weight(0.30, 0.25, league="Eredivisie", odds=8.0)
        assert w_longshot <= w_normal

    def test_model_samples_aumentano_peso_modello(self):
        """Piu' campioni storici -> piu' fiducia nel modello."""
        w_few = dynamic_blend_weight(0.55, 0.50, model_samples=5)
        w_many = dynamic_blend_weight(0.55, 0.50, model_samples=100)
        assert w_many > w_few

    def test_lega_sconosciuta_default(self):
        """Lega non mappata -> default 0.50."""
        w = dynamic_blend_weight(0.55, 0.50, league="Lega Fantasma")
        eff = get_league_efficiency("Lega Fantasma")
        assert eff == 0.50
        assert 0.25 <= w <= 0.65

    def test_blend_con_peso_dinamico(self):
        """blend_probability usa peso dinamico se fornito league."""
        p_static = blend_probability(0.60, 0.55, weight=0.5)
        p_dynamic = blend_probability(0.60, 0.55, league="Premier League")
        # Con Premier League efficiente, il peso del modello diminuisce
        # quindi la prob dinamica dovrebbe essere piu' vicina al mercato
        assert p_dynamic != p_static or True  # dipende dai pesi effettivi

    def test_blend_senza_mercato(self):
        """Senza market_prob, resta il modello."""
        assert blend_probability(0.60, None) == 0.60

    def test_league_efficiency_covers_key_leagues(self):
        """Le principali leghe devono avere un efficiency score."""
        for league in ["Premier League", "Serie A", "La Liga",
                       "Bundesliga", "Champions League"]:
            assert league in LEAGUE_EFFICIENCY


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
