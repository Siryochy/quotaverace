"""Value bet filter e Kelly Criterion"""

from typing import List, Dict, Any


def compute_ev(prob: float, odds: float) -> float:
    """Expected Value: (prob * odds) - 1"""
    return (prob * odds) - 1.0


def kelly_fraction(prob: float, odds: float, fraction: float = 0.25) -> float:
    """Kelly Criterion: f* = (p*b - q) / b, poi moltiplicato per fraction (default 25%)"""
    if odds <= 1.0:
        return 0.0
    q = 1.0 - prob
    kelly = (prob * odds - q) / odds
    return max(0.0, kelly * fraction)


def kelly_euro(bankroll: float, prob: float, odds: float, fraction: float = 0.25) -> float:
    """Stake in euro basato su Kelly Criterion"""
    kelly = kelly_fraction(prob, odds, fraction)
    return bankroll * kelly


def filter_value_bets(odds_data: List[Dict[str, Any]], ev_threshold: float = 0.05) -> List[Dict[str, Any]]:
    """Filtra le quote con EV positivo sopra la soglia"""
    value_signals = []
    for odd in odds_data:
        prob = odd.get("probabilita", 0.0)
        quota = odd.get("quota_decimale", 1.0)
        if prob <= 0 or quota <= 1.0:
            continue
        ev = compute_ev(prob, quota)
        if ev >= ev_threshold:
            odd["ev"] = ev
            odd["kelly"] = kelly_fraction(prob, quota)
            value_signals.append(odd)
    return sorted(value_signals, key=lambda x: x["ev"], reverse=True)
