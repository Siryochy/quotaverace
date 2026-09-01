import math
from typing import Dict, Tuple

try:
    from leagues_data import ALL_LEAGUES
except ImportError:
    ALL_LEAGUES = {}

try:
    from leagues_data import LEAGUE_AVGS
except ImportError:
    LEAGUE_AVGS = {}

SERIE_A_2025_26 = ALL_LEAGUES.get("Serie A", {})

AVG_HOME_GOALS = 1.52
AVG_AWAY_GOALS = 1.28

def poisson_pmf(k: int, lam: float) -> float:
    return (lam ** k) * math.exp(-lam) / math.factorial(k)

def prob_score(home_goals: int, away_goals: int, lam_h: float, lam_a: float) -> float:
    return poisson_pmf(home_goals, lam_h) * poisson_pmf(away_goals, lam_a)


RHO = -0.15  # Correzione Dixon-Coles (draw correlation)

def _probs_matrix(lam_h: float, lam_a: float, max_goals: int = 10) -> Dict:
    """Matrice punteggi con correzione Dixon-Coles, normalizzata a somma 1."""
    total = 0.0
    cells = {}
    for hg in range(max_goals + 1):
        for ag in range(max_goals + 1):
            p = poisson_pmf(hg, lam_h) * poisson_pmf(ag, lam_a)
            if hg == 0 and ag == 0:
                p *= (1 - lam_h * lam_a * RHO)
            elif hg == 0 and ag == 1:
                p *= (1 + lam_h * RHO)
            elif hg == 1 and ag == 0:
                p *= (1 + lam_a * RHO)
            elif hg == 1 and ag == 1:
                p *= (1 - RHO)
            cells[(hg, ag)] = p
            total += p
    return {k: v / total for k, v in cells.items()}

def prob_1x2(lam_h: float, lam_a: float, max_goals: int = 10) -> Tuple[float, float, float]:
    m = _probs_matrix(lam_h, lam_a, max_goals)
    p1 = sum(p for (hg, ag), p in m.items() if hg > ag)
    px = sum(p for (hg, ag), p in m.items() if hg == ag)
    p2 = sum(p for (hg, ag), p in m.items() if hg < ag)
    return p1, px, p2

def prob_over_under(lam_h: float, lam_a: float, threshold: float = 2.5, max_goals: int = 10) -> Tuple[float, float]:
    m = _probs_matrix(lam_h, lam_a, max_goals)
    p_over = sum(p for (hg, ag), p in m.items() if hg + ag > threshold)
    return p_over, 1.0 - p_over

def prob_btts(lam_h: float, lam_a: float, max_goals: int = 10) -> float:
    m = _probs_matrix(lam_h, lam_a, max_goals)
    return sum(p for (hg, ag), p in m.items() if hg >= 1 and ag >= 1)

# --- Asian Handicap (mercato a 2 esiti, margini bookmaker piu' bassi) ---
# Linee AH supportate: da -3 a +3 con passo 0.25 (intere, mezze, quarter).
AH_LINES = [i / 4.0 for i in range(-12, 13)]


def margin_distribution(lam_h: float, lam_a: float, max_goals: int = 10) -> Dict[int, float]:
    """Distribuzione del margine di gol (home - away) secondo il modello."""
    m = _probs_matrix(lam_h, lam_a, max_goals)
    dist: Dict[int, float] = {}
    for (hg, ag), p in m.items():
        adv = hg - ag
        dist[adv] = dist.get(adv, 0.0) + p
    return dist


def ah_outcome_probs(lam_h: float, lam_a: float, line: float, side: str = "home",
                     max_goals: int = 10) -> Tuple[float, float, float]:
    """Probabilita' (p_win, p_push, p_lose) a stake pieno per una linea AH.

    `line` e' la linea del LATO scelto col suo segno (es. home -0.75 = la
    squadra di casa dà 0.75 gol; away +0.25 = l'ospite riceve 0.25 gol).
    Le quarter line (.25/.75) valgono come due mezze puntate: il push
    di una meta' viene pesato mezzo.
    """
    dist = margin_distribution(lam_h, lam_a, max_goals)
    if abs(line * 2) % 1 != 0:  # quarter line -> due mezze puntate
        lo = math.floor(line * 2) / 2.0
        halves = (lo, lo + 0.5)
        share = 0.5
    else:
        halves = (line,)
        share = 1.0
    p_win = p_push = p_lose = 0.0
    for hline in halves:
        for adv, p in dist.items():
            net = (adv + hline) if side == "home" else (-adv + hline)
            if net > 0:
                p_win += share * p
            elif net == 0:
                p_push += share * p
            else:
                p_lose += share * p
    return p_win, p_push, p_lose


def _find_team_league(team_name: str):
    for league, teams in ALL_LEAGUES.items():
        if team_name in teams:
            return league
    return None

def expected_goals(home_team: str, away_team: str):
    home_league = _find_team_league(home_team)
    away_league = _find_team_league(away_team)
    if home_league is None:
        raise ValueError(f"Squadra non trovata: {home_team}")
    if away_league is None:
        raise ValueError(f"Squadra non trovata: {away_team}")
    
    if home_league != away_league:
        avg_hg, avg_ag = 1.50, 1.30
    else:
        avg_hg, avg_ag = LEAGUE_AVGS.get(home_league, (1.50, 1.30))
    
    try:
        from rating_engine import get_rating
        _rh = get_rating(home_team) or {}
        _ra = get_rating(away_team) or {}
    except Exception:
        _rh, _ra = {}, {}
    if _rh and _ra:
        home_data, away_data = _rh, _ra
    else:
        home_data = ALL_LEAGUES[home_league][home_team]
        away_data = ALL_LEAGUES[away_league][away_team]
    lam_h = avg_hg * home_data["attack_home"] * away_data["defense_away"]
    lam_a = avg_ag * away_data["attack_away"] * home_data["defense_home"]
    return lam_h, lam_a
