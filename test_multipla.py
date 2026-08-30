"""
Test unitari per la Multipla Prolungata (schedina multi-esito).
"""

import pytest

from value_filter import combined_quota, combined_probability, multipla_stake
from fixture_engine import build_multipla, build_multipla_block


def _pick(esito, quota, ev):
    return {"evento": "Lega – X vs Y", "esito": esito, "quota": quota,
            "bookmaker": "Book", "prob": 0.5, "ev": ev}


class TestCombinazione:
    def test_quota_combinata_prodotto(self):
        assert combined_quota([2.0, 3.0]) == pytest.approx(6.0)
        assert combined_quota([1.5, 2.0, 4.0]) == pytest.approx(12.0)

    def test_prob_combinata_prodotto(self):
        assert combined_probability([0.5, 0.5]) == pytest.approx(0.25)
        assert combined_probability([0.5, 0.5, 0.5]) == pytest.approx(0.125)

    def test_ev_combinata(self):
        from value_filter import compute_ev
        # 2 eventi: prob 0.5 quota 2.1
        quota = combined_quota([2.1, 2.1])          # 4.41
        prob = combined_probability([0.5, 0.5])     # 0.25
        ev = compute_ev(prob, quota)
        assert ev == pytest.approx(0.25 * 4.41 - 1.0)


class TestMultiplaStake:
    def test_cap_1_percento(self):
        # prob con EV > soglia -> stake limitato dal cap 1%
        stake = multipla_stake(100.0, 0.5, 2.1)
        assert stake <= 1.0
        assert stake > 0

    def test_stake_zero_no_value(self):
        # multipla fortemente negativa -> Kelly 0
        stake = multipla_stake(100.0, 0.1, 2.0)  # EV = -0.8
        assert stake == pytest.approx(0.0)


class TestBuildMultipla:
    def test_serve_almeno_due_esiti(self):
        assert build_multipla([_pick("1", 2.0, 0.05)]) is None
        assert build_multipla([]) is None

    def test_limite_massimo_sette_esiti(self):
        picks = [_pick(f"esito{i}", 2.0, 0.05) for i in range(10)]
        mp = build_multipla(picks)
        assert mp is not None
        assert len(mp["legs"]) == 7
        assert mp["quota"] == pytest.approx(2.0 ** 7)

    def test_prob_ed_ev_combinati(self):
        p1, p2 = _pick("1", 2.1, 0.05), _pick("X", 3.0, 0.04)
        mp = build_multipla([p1, p2])
        exp_prob = (p1["ev"] + 1 / p1["quota"]) * (p2["ev"] + 1 / p2["quota"])
        assert mp["prob"] == pytest.approx(exp_prob)

    def test_esiti_uniti_con_plus(self):
        mp = build_multipla([_pick("1", 2.0, 0.05), _pick("Over 2.5", 2.0, 0.05)])
        assert mp["esiti"] == "1 + Over 2.5"


class TestMultiplaBlock:
    def test_vuoto_con_meno_di_due_picks(self):
        assert build_multipla_block([]) == ""
        assert build_multipla_block([_pick("1", 2.0, 0.05)]) == ""

    def test_contiene_quota_e_stake(self):
        block = build_multipla_block(
            [_pick("1", 2.1, 0.05), _pick("X", 3.0, 0.04)], bankroll=100.0)
        assert "Quota combinata" in block
        assert "Probabilità congiunta" in block
        assert "Stake suggerito" in block

    def test_verdetto_negativo_per_ev_negativo(self):
        # esiti con EV negativo -> anche la multipla è negativa
        block = build_multipla_block([_pick("1", 2.0, -0.02), _pick("2", 2.0, -0.02)],
                                     bankroll=100.0)
        assert "NEGATIVA" in block

    def test_verdetto_marginale_per_ev_basso(self):
        # EV positivo ma sotto soglia 5% -> marginale
        block = build_multipla_block([_pick("1", 1.9, 0.001), _pick("2", 1.9, 0.001)],
                                     bankroll=100.0)
        assert "MARGINALE" in block