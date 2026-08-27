"""Value bet filter e Kelly Criterion Pro"""

from typing import List, Dict, Any


# === FILTRI DI SANITÀ ===
EV_MIN = 0.03          # +3% minimo
EV_MAX = 0.15          # +15% massimo (oltre = anomalia)
ODDS_MIN = 1.50        # quota minima
ODDS_MAX = 5.00        # quota massima
KELLY_FRACTION = 0.25  # 1/4 Kelly
MAX_STAKE_PCT = 0.03   # cap 3% del bankroll


def compute_ev(prob: float, odds: float) -> float:
    """Expected Value: (prob * odds) - 1"""
    return (prob * odds) - 1.0


def kelly_fraction(prob: float, odds: float, fraction: float = KELLY_FRACTION) -> float:
    """Kelly Criterion frazionario (default 1/4 Kelly)"""
    if odds <= 1.0:
        return 0.0
    q = 1.0 - prob
    kelly_full = (prob * odds - q) / odds
    return max(0.0, kelly_full * fraction)


def kelly_euro(bankroll: float, prob: float, odds: float, fraction: float = KELLY_FRACTION) -> float:
    """Stake in euro con cap al 3% del bankroll"""
    kelly = kelly_fraction(prob, odds, fraction)
    stake = bankroll * kelly
    cap = bankroll * MAX_STAKE_PCT
    return min(stake, cap)


def is_sane(prob: float, odds: float, ev: float) -> tuple[bool, str]:
    """Verifica se il segnale supera i filtri di sanità"""
    if odds < ODDS_MIN:
        return False, f"quota troppo bassa ({odds:.2f} < {ODDS_MIN})"
    if odds > ODDS_MAX:
        return False, f"quota troppo alta ({odds:.2f} > {ODDS_MAX})"
    if ev < EV_MIN:
        return False, f"EV troppo basso ({ev*100:.1f}% < {EV_MIN*100:.0f}%)"
    if ev > EV_MAX:
        return False, f"ANOMALIA: EV troppo alto ({ev*100:.1f}% > {EV_MAX*100:.0f}%) — possibile errore dati"
    return True, "OK"


def filter_value_bets(odds_data: List[Dict[str, Any]], ev_threshold: float = EV_MIN) -> List[Dict[str, Any]]:
    """Filtra le quote con EV positivo, applicando filtri di sanità Pro"""
    value_signals = []
    for odd in odds_data:
        prob = odd.get("probabilita", 0.0)
        quota = odd.get("quota_decimale", 1.0)
        if prob <= 0 or quota <= 1.0:
            continue
        ev = compute_ev(prob, quota)
        sane, reason = is_sane(prob, quota, ev)
        odd["ev"] = ev
        odd["kelly"] = kelly_fraction(prob, quota)
        odd["sane"] = sane
        odd["sane_reason"] = reason
        if sane and ev >= ev_threshold:
            value_signals.append(odd)
    return sorted(value_signals, key=lambda x: x["ev"], reverse=True)


def get_pro_stake(bankroll: float, prob: float, odds: float) -> dict:
    """Ritorna dizionario completo con stake, cap, e info filtri"""
    ev = compute_ev(prob, odds)
    sane, reason = is_sane(prob, odds, ev)
    kelly = kelly_fraction(prob, odds)
    stake_raw = bankroll * kelly
    cap = bankroll * MAX_STAKE_PCT
    stake = min(stake_raw, cap)
    return {
        "ev": ev,
        "ev_pct": ev * 100,
        "sane": sane,
        "sane_reason": reason,
        "kelly_fraction": kelly,
        "kelly_pct": kelly * 100,
        "stake_raw": stake_raw,
        "stake_cap": cap,
        "stake": stake,
        "stake_pct_of_bankroll": (stake / bankroll * 100) if bankroll > 0 else 0,
    }
