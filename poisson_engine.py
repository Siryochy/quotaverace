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

def prob_1x2(lam_h: float, lam_a: float, max_goals: int = 10) -> Tuple[float, float, float]:
    p1, px, p2 = 0.0, 0.0, 0.0
    for hg in range(max_goals + 1):
        for ag in range(max_goals + 1):
            p = prob_score(hg, ag, lam_h, lam_a)
            if hg > ag: p1 += p
            elif hg == ag: px += p
            else: p2 += p
    return p1, px, p2

def prob_over_under(lam_h: float, lam_a: float, threshold: float = 2.5, max_goals: int = 10) -> Tuple[float, float]:
    p_over = 0.0
    for hg in range(max_goals + 1):
        for ag in range(max_goals + 1):
            if hg + ag > threshold:
                p_over += prob_score(hg, ag, lam_h, lam_a)
    return p_over, 1.0 - p_over

def prob_btts(lam_h: float, lam_a: float, max_goals: int = 10) -> float:
    p = 0.0
    for hg in range(1, max_goals + 1):
        for ag in range(1, max_goals + 1):
            p += prob_score(hg, ag, lam_h, lam_a)
    return p

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
    
    home_data = ALL_LEAGUES[home_league][home_team]
    away_data = ALL_LEAGUES[away_league][away_team]
    lam_h = avg_hg * home_data["attack_home"] * away_data["defense_away"]
    lam_a = avg_ag * away_data["attack_away"] * home_data["defense_home"]
    return lam_h, lam_a
