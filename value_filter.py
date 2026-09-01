"""Value bet filter e Kelly Criterion Pro"""

from typing import List, Dict, Any

from market_calib import (
    blend_probability,
    favourite_longshot_adjust,
    market_edge as _market_edge,
    MARKET_EDGE_MIN,  # ri-esportata: soglia +3pp sul mercato (ricerca 2026)
)


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


def combined_quota(odds: List[float]) -> float:
    """Quota combinata di una multipla (prodotto delle quote)."""
    prod = 1.0
    for o in odds:
        prod *= o
    return prod


def combined_probability(probs: List[float]) -> float:
    """Probabilità congiunta di una multipla (prodotto, ipotesi indipendenza)."""
    prod = 1.0
    for p in probs:
        prod *= p
    return prod


# Frazioni e cap dedicati alle multiple: più aggressivi sul numero di esiti
# ma molto più prudenti sullo stake (varianza alta, una sola scommessa perde tutto)
MULTIPLA_KELLY_FRACTION = 0.125   # 1/8 Kelly
MULTIPLA_MAX_STAKE_PCT = 0.01     # cap 1% del bankroll
MULTIPLA_MAX_EV = 0.05            # EV soglia entro cui una multipla ha senso


def multipla_stake(bankroll: float, prob: float, odds: float) -> float:
    """Stake in euro per una multipla: 1/8 Kelly con cap 1% del bankroll.

    Piu' prudente delle singole (cap 3%) perche' una multipla concentra tutto
    il rischio in un'unica scommessa dipendente da piu' eventi.
    """
    kelly = kelly_fraction(prob, odds, MULTIPLA_KELLY_FRACTION)
    stake = bankroll * kelly
    cap = bankroll * MULTIPLA_MAX_STAKE_PCT
    return min(stake, cap)


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


def market_edge(model_prob: float, market_prob: float) -> float:
    """Edge del modello sul mercato: model_prob - market_prob."""
    return _market_edge(model_prob, market_prob) or 0.0


def is_sane(prob: float, odds: float, ev: float,
            market_prob: float | None = None,
            market_edge_min: float = MARKET_EDGE_MIN) -> tuple[bool, str]:
    """Verifica se il segnale supera i filtri di sanità.

    Con market_prob disponibile, aggiunge il vincolo "beating the market":
    il segnale e' valore solo se il modello stima una probabilita' SUPERIORE
    a quella implicita nel mercato (devig). Questo e' il test decisivo
    della strategia value betting: EV positivo contro un bookmaker non basta,
    bisogna battere la closing line.
    """
    if odds < ODDS_MIN:
        return False, f"quota troppo bassa ({odds:.2f} < {ODDS_MIN})"
    if odds > ODDS_MAX:
        return False, f"quota troppo alta ({odds:.2f} > {ODDS_MAX})"
    if ev < EV_MIN:
        return False, f"EV troppo basso ({ev*100:.1f}% < {EV_MIN*100:.0f}%)"
    if ev > EV_MAX:
        return False, f"ANOMALIA: EV troppo alto ({ev*100:.1f}% > {EV_MAX*100:.0f}%) — possibile errore dati"
    if market_prob is not None:
        edge = prob - market_prob
        if edge < market_edge_min:
            return False, (f"non batte il mercato (edge {edge*100:.1f}pp < {market_edge_min*100:.0f}pp "
                           f"vs prob. di mercato {market_prob*100:.1f}%)")
    return True, "OK"


def adjusted_probability(model_prob: float, market_prob: float | None,
                         odds: float, league: str = "",
                         model_samples: int = 0) -> float:
    """Probabilita' finale del segnale, calibrata sul mercato.

    Combina i due correttivi della ricerca:
    1. blending modello+mercato dinamico (riduce l'overconfidence del
       modello, adattandosi all'efficienza del mercato per lega);
    2. correzione favourite-longshot (sopra LONG_SHOT_ODDS la stima del
       modello viene compressa verso il mercato).
    """
    p = blend_probability(model_prob, market_prob,
                          league=league, odds=odds,
                          model_samples=model_samples)
    return favourite_longshot_adjust(p, market_prob, odds)


def filter_value_bets(odds_data: List[Dict[str, Any]], ev_threshold: float = EV_MIN) -> List[Dict[str, Any]]:
    """Filtra le quote con EV positivo, applicando filtri di sanità Pro.

    Backward-compatible: se la riga non ha "market_prob" (nessun riferimento
    di mercato) mantiene il comportamento storico; se la ha, aggiunge i campi
    market_edge / beats_market e il vincolo "beating the market" alla sanita'.
    """
    value_signals = []
    for odd in odds_data:
        prob = odd.get("probabilita", 0.0)
        quota = odd.get("quota_decimale", 1.0)
        if prob <= 0 or quota <= 1.0:
            continue
        ev = compute_ev(prob, quota)
        market_prob = odd.get("market_prob")
        if market_prob is not None:
            edge = prob - market_prob
            odd["market_edge"] = edge
            odd["beats_market"] = edge >= MARKET_EDGE_MIN
        sane, reason = is_sane(prob, quota, ev, market_prob=market_prob)
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
