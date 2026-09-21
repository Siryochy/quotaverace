"""Tripwire di drift delle soglie (`decision/limits.py`) — OFFLINE.

`decision` non ricopia nessuna soglia: le legge dai moduli che oggi le
applicano. Questo file ROMPE se qualcuno cambia un valore da una parte sola, che
e' il modo in cui la documentazione e il codice hanno gia' iniziato a divergere
(es. il cap di stake mostrato dai tool vs quello applicato dal bot).
"""

import pytest

import adaptive_staking as stake_mod
import market_calib as calib
import value_filter as vf
from decision.limits import RiskLimits, limits_from_env


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits.from_env()


class TestAllineamentoSoglie:
    def test_fascia_quote(self, limits):
        assert limits.odds_min == vf.ODDS_MIN
        assert limits.odds_max == vf.ODDS_MAX

    def test_soglie_ev(self, limits):
        assert limits.ev_min == vf.EV_MIN
        assert limits.ev_max == vf.EV_MAX

    def test_soglie_edge(self, limits):
        assert limits.edge_min == calib.MARKET_EDGE_MIN
        assert limits.edge_strong == calib.MARKET_EDGE_STRONG

    def test_solo_favoriti(self, limits):
        assert limits.favourites_only is vf.FAVOURITES_ONLY
        assert limits.min_favourite_prob == vf.MIN_FAVOURITE_MARKET_PROB

    def test_kelly(self, limits):
        assert limits.kelly_min == stake_mod.MIN_KELLY_FRACTION
        assert limits.kelly_max == stake_mod.MAX_KELLY_FRACTION

    def test_cap_applicato_segue_adaptive_staking(self, limits):
        """Il cap che il BOT applica."""
        assert limits.cap_value == stake_mod.MAX_STAKE_PCT
        assert limits.cap_strong == stake_mod.MAX_STAKE_PCT_STRONG

    def test_cap_mostrato_segue_value_filter(self, limits):
        """Il cap che i TOOL mostrano.

        Oggi i due divergono (2% mostrato vs 1% applicato): il test non impone
        che siano diversi, impone che ciascuno segua la PROPRIA fonte — cosi' la
        differenza resta visibile invece di nascondersi dietro un numero solo.
        """
        assert limits.cap_display == vf.MAX_STAKE_PCT

    def test_floor_di_codice(self, limits):
        assert limits.order_floor == stake_mod.MIN_STAKE_EUR
        assert limits.stake_step == stake_mod.STAKE_STEP

    def test_edge_e_cap_di_lega(self, limits):
        for league in ("Premier League", "Bundesliga", "", "Lega Sconosciuta"):
            strategy = vf.get_league_strategy(league)
            assert limits.league_min_edge(league) == strategy["min_edge"]
            assert limits.league_max_stake_pct(league) == strategy["max_stake"]


class TestEnvOverride:
    def test_cap_da_env(self, monkeypatch):
        monkeypatch.setenv("STAKE_CAP_PCT", "0.004")
        monkeypatch.setenv("STAKE_CAP_PCT_STRONG", "0.008")
        limits = RiskLimits.from_env()
        assert limits.cap_value == pytest.approx(0.004)
        assert limits.cap_strong == pytest.approx(0.008)

    def test_stake_cap_hard(self, monkeypatch):
        monkeypatch.setenv("STAKE_CAP_HARD", "0")
        assert RiskLimits.from_env().stake_cap_hard is False
        monkeypatch.delenv("STAKE_CAP_HARD")
        assert RiskLimits.from_env().stake_cap_hard is True

    def test_floor_exchange(self, monkeypatch):
        monkeypatch.setenv("EXCHANGE_MIN_ORDER_USDC", "2.5")
        assert RiskLimits.from_env().exchange_floor == pytest.approx(2.5)

    def test_soglia_review(self, monkeypatch):
        monkeypatch.setenv("DECISION_REVIEW_CONFIDENCE", "0.7")
        assert RiskLimits.from_env().review_confidence_min == pytest.approx(0.7)
        monkeypatch.setenv("DECISION_REVIEW_ENABLED", "0")
        assert RiskLimits.from_env().review_enabled is False

    def test_env_rotta_torna_al_default(self, monkeypatch):
        monkeypatch.setenv("STAKE_CAP_PCT", "non-un-numero")
        assert RiskLimits.from_env().cap_value == stake_mod.MAX_STAKE_PCT


class TestDerivati:
    def test_cap_per_tier(self, limits):
        assert limits.cap_for("strong_value") == limits.cap_strong
        assert limits.cap_for("value") == limits.cap_value
        assert limits.cap_for("moderate") == limits.cap_value

    def test_floor_per_modalita(self, limits):
        assert limits.floor_for("sim") == limits.order_floor
        assert limits.floor_for("live") == max(limits.order_floor, limits.exchange_floor)

    def test_required_depth(self, limits):
        """Formula di produzione: max(stake x 1.6, 20 USDC) — taratura 21/09
        (era max(stake x 2.0, 25) dell'11/09). Il vincolo NON e' copiato a
        mano: segue la stessa fonte del percorso ordini (`auto_bet`)."""
        import auto_bet
        assert limits.min_exec_depth_usdc == auto_bet.MIN_EXEC_DEPTH_USDC
        assert limits.depth_multiplier == auto_bet.SX_DEPTH_MULTIPLIER
        assert limits.required_depth(1.0) == limits.min_exec_depth_usdc
        assert limits.required_depth(5.0) == limits.min_exec_depth_usdc
        assert limits.required_depth(20.0) == pytest.approx(
            20.0 * auto_bet.SX_DEPTH_MULTIPLIER)
        assert limits.required_depth("boh") == limits.min_exec_depth_usdc

    def test_scorciatoia(self):
        assert isinstance(limits_from_env(), RiskLimits)
