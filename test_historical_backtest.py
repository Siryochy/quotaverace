"""Test del backtest storico walk-forward (historical_backtest.py).

Copre gli STRUMENTI DIAGNOSTICI aggiunti il 05/09 (che hanno smascherato
il falso edge 3.5-4.0 e il leak del mercato Under):

  1. --flat-stake-pct: stake fisso per bet, il run NON va in bancarotta
     e copre tutte le stagioni, MA la selezione delle pick resta identica
     a quella di Kelly (invariante fondamentale per la diagnostica).
  2. --no-ou / --no-under: esclusione dei mercati OU (misura l'impatto
     del leak degli Under senza ricampionare a mano).
  3. shrink asimmetrico: rampa lineare tra SHRINK_ODDS_MIN e
     SHRINK_ODDS_MAX, fattore 1.0 sotto soglia (favoriti pieni).

Usa un dataset sintetico (2 leghe × 60 partite, squadre dominanti in
casa) generato a runtime: niente rete, niente file, riproducibile.
"""

from datetime import datetime, timedelta

import pytest

import historical_backtest as hb
from historical_backtest import (
    FTR_MAP,
    SHRINK_LONG_SHOT,
    SHRINK_ODDS_MAX,
    SHRINK_ODDS_MIN,
    build_candidates,
    run_backtest,
    shrink_factor,
    shrink_prob,
)


# ---------------------------------------------------------------------------
# Dati sintetici: 2 leghe × 60 partite, squadre di casa dominanti.
# Produce ~43-50 bet (verificato): il 90% sul mercato OU, il resto 1X2.
# ---------------------------------------------------------------------------

def _synthetic_matches(n_league: int = 60) -> list:
    teams = [f"Team{i}" for i in range(12)]
    start = datetime(2022, 8, 1)
    matches = []
    mid = 0
    for league, code in (("Premier League", "E0"), ("Serie A", "I1")):
        for k in range(n_league):
            home = teams[(mid + k) % 12]
            away = teams[(mid + k + 3) % 12]
            if home == away:
                continue
            d = start + timedelta(days=k)
            sh = 3 if k % 3 == 0 else (2 if k % 3 == 1 else 1)
            sa = 1 if k % 2 == 0 else 0
            ftr = "H" if sh > sa else ("A" if sa > sh else "D")
            matches.append(dict(
                date=d, season="2223", bet=True, code=code, league=league,
                home=home, away=away, sh=sh, sa=sa, ftr=FTR_MAP[ftr],
                op=[2.2, 3.4, 3.2], cl=[2.1, 3.5, 3.3],
                ou_entry=[1.9, 1.9], ou_closing=[1.85, 1.95],
            ))
    return matches


# ---------------------------------------------------------------------------
# Shrink asimmetrico
# ---------------------------------------------------------------------------

class TestShrinkAsimmetrico:
    def test_fattore_1_sotto_soglia(self):
        """Sotto SHRINK_ODDS_MIN nessuno shrink: fiducia piena al blend."""
        assert shrink_factor(1.30) == 1.0
        assert shrink_factor(2.49) == 1.0

    def test_fattore_massimo_sopra_la_rampa(self):
        """Sopra SHRINK_ODDS_MAX lo shrink raggiunge il massimo."""
        assert shrink_factor(SHRINK_ODDS_MAX) == pytest.approx(SHRINK_LONG_SHOT)
        assert shrink_factor(8.0) == pytest.approx(SHRINK_LONG_SHOT)

    def test_rampa_lineare_in_mezzo(self):
        """Tra le due soglie il fattore scende linearmente da 1.0 a max."""
        span = SHRINK_ODDS_MAX - SHRINK_ODDS_MIN
        for t in (0.25, 0.5, 0.75):
            odds = SHRINK_ODDS_MIN + t * span
            expected = 1.0 - (1.0 - SHRINK_LONG_SHOT) * t
            assert shrink_factor(odds) == pytest.approx(expected)

    def test_monotona_non_crescente(self):
        """Il fattore non aumenta mai con la quota (mai piu' fiducia ai longshot)."""
        vals = [shrink_factor(1.2 + 0.1 * i) for i in range(50)]
        assert all(vals[i] >= vals[i + 1] - 1e-9 for i in range(len(vals) - 1))

    def test_shrink_prob_cap_edge_e_clip(self):
        """shrink_prob: verso il mercato + cap MAX_EDGE + clip in [0.01, 0.99]."""
        p = shrink_prob(0.90, market_prob=0.50, odds=3.0)
        # cap: mai oltre market + MAX_EDGE
        assert p <= 0.50 + hb.MAX_EDGE + 1e-9
        assert p >= 0.50
        # clip
        assert shrink_prob(0.9999, market_prob=None, odds=2.0) <= 0.99
        assert shrink_prob(0.0001, market_prob=None, odds=2.0) >= 0.01


class TestShrinkEstesoFavoriti:
    """Rampa lato favoriti (05/09): chiude il leak degli Under."""

    def test_retrocompatibile_default_1_0(self):
        """Con SHRINK_FAVORITE=1.0 (default) il comportamento e' invariato:
        sotto SHRINK_ODDS_MIN il fattore resta 1.0."""
        old_fav = hb.SHRINK_FAVORITE
        hb.SHRINK_FAVORITE = 1.0
        try:
            assert hb.shrink_factor(1.50) == 1.0
            assert hb.shrink_factor(2.40) == 1.0
        finally:
            hb.SHRINK_FAVORITE = old_fav

    def test_rampa_favoriti_scende_sotto_2_5(self):
        """Con SHRINK_FAVORITE=0.2 la rampa copre anche i favoriti."""
        old_fav, old_low = hb.SHRINK_FAVORITE, hb.SHRINK_FAV_ODDS_LOW
        hb.SHRINK_FAVORITE = 0.2
        hb.SHRINK_FAV_ODDS_LOW = 1.30
        try:
            # favoritissimo: piena fiducia
            assert hb.shrink_factor(1.20) == 1.0
            # a quota 1.9 (zona Under) shrink parziale, NON 1.0
            f_19 = hb.shrink_factor(1.90)
            assert 0.2 < f_19 < 1.0
            # a quota 2.5 (fine rampa favoriti) = SHRINK_FAVORITE
            assert hb.shrink_factor(2.50) == pytest.approx(0.2)
            # monotona anche lato favoriti
            vals = [hb.shrink_factor(1.2 + 0.05 * i) for i in range(30)]
            assert all(vals[i] >= vals[i + 1] - 1e-9
                       for i in range(len(vals) - 1))
        finally:
            hb.SHRINK_FAVORITE = old_fav
            hb.SHRINK_FAV_ODDS_LOW = old_low

    def test_ou_only_lascia_intatti_i_favoriti_1x2(self):
        """Con SHRINK_OU_ONLY lo shrink favoriti scatta SOLO sull'OU: un
        favorito 1X2 a quota 1.9 resta pieno (fattore legacy = 1.0)."""
        old_fav, old_only = hb.SHRINK_FAVORITE, hb.SHRINK_OU_ONLY
        hb.SHRINK_FAVORITE = 0.2
        hb.SHRINK_OU_ONLY = True
        try:
            p_ou = shrink_prob(0.60, market_prob=0.50, odds=1.90,
                               mercato="OVER_UNDER_25")
            p_1x2 = shrink_prob(0.60, market_prob=0.50, odds=1.90,
                                mercato="MATCH_ODDS")
            # l'OU viene shrinkato verso il mercato, il 1X2 no
            assert p_ou < p_1x2 - 1e-6
            assert p_1x2 == pytest.approx(0.60)  # nessuno shrink (f=1.0)
        finally:
            hb.SHRINK_FAVORITE = old_fav
            hb.SHRINK_OU_ONLY = old_only


# ---------------------------------------------------------------------------
# build_candidates: flag no_ou / no_under
# ---------------------------------------------------------------------------

class TestBuildCandidates:
    def _match(self):
        return dict(
            op=[2.0, 3.5, 3.5], ou_entry=[1.9, 1.9],
        )

    def test_default_include_ou(self):
        cands = build_candidates(self._match(), {"prob_1": 0.5, "prob_X": 0.25,
                                                 "prob_2": 0.25, "prob_over": 0.55},
                                 mkt=[0.5, 0.28, 0.28], mkt_ou=[0.55, 0.45])
        mercati = {c["mercato"] for c in cands}
        assert mercati == {"MATCH_ODDS", "OVER_UNDER_25"}
        esiti = {c["esito"] for c in cands}
        assert {"over", "under"} <= esiti

    def test_no_ou_esclude_tutto_il_mercato(self):
        cands = build_candidates(self._match(), {"prob_1": 0.5, "prob_X": 0.25,
                                                 "prob_2": 0.25, "prob_over": 0.55},
                                 mkt=[0.5, 0.28, 0.28], mkt_ou=[0.55, 0.45],
                                 no_ou=True)
        assert all(c["mercato"] == "MATCH_ODDS" for c in cands)
        assert cands  # i candidati 1X2 restano

    def test_no_under_esclude_solo_il_lato_under(self):
        cands = build_candidates(self._match(), {"prob_1": 0.5, "prob_X": 0.25,
                                                 "prob_2": 0.25, "prob_over": 0.55},
                                 mkt=[0.5, 0.28, 0.28], mkt_ou=[0.55, 0.45],
                                 no_under=True)
        esiti_ou = [c["esito"] for c in cands if c["mercato"] == "OVER_UNDER_25"]
        assert esiti_ou == ["over"]


# ---------------------------------------------------------------------------
# run_backtest: invarianti dei flag diagnostici
# ---------------------------------------------------------------------------

class TestFlatStake:
    def test_stake_fisso_uguale_per_tutte_le_bet(self):
        res = run_backtest(_synthetic_matches(), ensemble=False, flat_stake=20.0)
        assert res["status"] == "ok"
        assert res["n_bets"] > 0
        assert all(abs(b["stake"] - 20.0) < 1e-6 for b in res["_bets"])

    def test_selezione_identica_a_kelly(self):
        """Invariante chiave: lo stake NON cambia le pick (solo la dimensione).
        Su questo dataset Kelly non va in bancarotta -> stesse identiche bet."""
        m = _synthetic_matches()
        r_kelly = run_backtest(m, ensemble=False)
        r_flat = run_backtest(m, ensemble=False, flat_stake=20.0)
        assert r_kelly["status"] == "ok" and r_flat["status"] == "ok"
        picks_k = {(b["date"], b["home"], b["mercato"], b["esito"])
                   for b in r_kelly["_bets"]}
        picks_f = {(b["date"], b["home"], b["mercato"], b["esito"])
                   for b in r_flat["_bets"]}
        assert picks_k == picks_f
        assert len(picks_k) > 0

    def test_roi_flat_uguale_roi_con_stake_uniforme(self):
        """Con stake fisso, roi e roi_flat coincidono (nessuna distorsione
        dal compounding Kelly)."""
        res = run_backtest(_synthetic_matches(), ensemble=False, flat_stake=20.0)
        assert res["roi"] == pytest.approx(res["roi_flat"], abs=0.01)


class TestNoOuNoUnder:
    def test_no_ou_nessuna_bet_ou(self):
        res = run_backtest(_synthetic_matches(), ensemble=False,
                           flat_stake=20.0, no_ou=True)
        assert res["status"] == "ok"
        assert res["n_bets"] > 0
        assert all(b["mercato"] != "OVER_UNDER_25" for b in res["_bets"])
        assert all(b["mercato"] == "MATCH_ODDS" for b in res["_bets"])

    def test_no_under_nessuna_bet_under(self):
        res = run_backtest(_synthetic_matches(), ensemble=False,
                           flat_stake=20.0, no_under=True)
        assert res["status"] == "ok"
        assert res["n_bets"] > 0
        assert all(not (b["mercato"] == "OVER_UNDER_25" and b["esito"] == "under")
                   for b in res["_bets"])
        # il lato over resta disponibile (e il 1X2)
        assert any(b["mercato"] == "OVER_UNDER_25" and b["esito"] == "over"
                   for b in res["_bets"]) or \
               any(b["mercato"] == "MATCH_ODDS" for b in res["_bets"])


class TestReport:
    def test_report_completo(self):
        res = run_backtest(_synthetic_matches(), ensemble=False, flat_stake=20.0)
        for key in ("n_bets", "roi", "roi_flat", "hit_rate", "max_drawdown_pct",
                    "by_market", "by_season", "clv"):
            assert key in res

    def test_clv_campi_popolati(self):
        """Il CLV raw/vig-free viene calcolato per le bet con closing disponibile."""
        res = run_backtest(_synthetic_matches(), ensemble=False, flat_stake=20.0)
        with_raw = [b for b in res["_bets"] if b["clv_raw"] is not None]
        with_vf = [b for b in res["_bets"] if b["clv_vig_free"] is not None]
        assert len(with_raw) > 0
        assert len(with_vf) > 0