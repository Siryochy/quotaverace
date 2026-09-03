"""Test del motore Poisson/Dixon-Coles (poisson_engine.py).

Verifica che la matrice punteggi includa la correzione Dixon-Coles (rho),
che le probabilita' 1X2/OU/BTTS/AH siano coerenti (somma 1, in [0,1]) e
che il motore sia quello usato dal flusso di analisi (fixture_engine).
"""

import math

import pytest

import poisson_engine
from poisson_engine import (
    ah_outcome_probs,
    expected_goals,
    prob_1x2,
    prob_btts,
    prob_over_under,
    prob_score,
)


class TestProbabilitaCoerenti:
    def test_1x2_somma_a_uno(self):
        p1, px, p2 = prob_1x2(1.5, 1.2)
        assert abs(p1 + px + p2 - 1.0) < 1e-9
        assert all(0.0 <= p <= 1.0 for p in (p1, px, p2))

    def test_over_under_somma_a_uno(self):
        p_over, p_under = prob_over_under(1.5, 1.2)
        assert abs(p_over + p_under - 1.0) < 1e-9

    def test_btts_e_ah_in_range(self):
        p_btts = prob_btts(1.5, 1.2)
        assert 0.0 <= p_btts <= 1.0
        for line in (-0.75, 0.25, 0.0, 2.5):
            pw, pp, pl = ah_outcome_probs(1.5, 1.2, line)
            assert abs(pw + pp + pl - 1.0) < 1e-9
            assert all(0.0 <= p <= 1.0 for p in (pw, pp, pl))

    def test_prob_score_piu_forte_con_lam_alto(self):
        # Con lam piu' alti la probabilita' di tanti gol cresce
        p_low = prob_score(2, 1, 1.0, 1.0)
        p_high = prob_score(2, 1, 2.0, 2.0)
        assert p_high > p_low


class TestDixonColes:
    def test_correzione_rho_aumenta_il_draw(self):
        """Con rho < 0 la probabilita' del pareggio cresce rispetto al
        Poisson puro: e' la correzione Dixon-Coles sulla correlazione dei
        punteggi bassi (0-0, 1-0, 0-1, 1-1)."""
        lam_h, lam_a = 1.5, 1.2
        p1_dc, px_dc, p2_dc = prob_1x2(lam_h, lam_a)
        original_rho = poisson_engine.RHO
        try:
            poisson_engine.RHO = 0.0  # Poisson puro
            p1_p, px_p, p2_p = prob_1x2(lam_h, lam_a)
        finally:
            poisson_engine.RHO = original_rho
        assert px_dc > px_p
        # Il draw aumenta a scapito dei due esiti (soprattutto del 2, che
        # nei punteggi 0-1/1-1 viene corretto verso il basso)
        assert p2_dc < p2_p

    def test_rho_non_rompe_la_somma(self):
        p1, px, p2 = prob_1x2(2.2, 0.9)
        assert abs(p1 + px + p2 - 1.0) < 1e-9


class TestIntegrazione:
    def test_expected_goals_sane(self):
        """expected_goals alimenta prob_1x2: rating reali -> lam plausibili
        e probabilita' coerenti (integrazione col flusso di analisi)."""
        lam_h, lam_a = expected_goals("Inter", "Juventus")
        assert lam_h > 0 and lam_a > 0
        p1, px, p2 = prob_1x2(lam_h, lam_a)
        assert abs(p1 + px + p2 - 1.0) < 1e-9


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))