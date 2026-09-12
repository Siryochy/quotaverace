"""Value bet filter e Kelly Criterion Pro"""

from typing import List, Dict, Any

from market_calib import (
    blend_probability,
    favourite_longshot_adjust,
    market_edge as _market_edge,
    MARKET_EDGE_MIN,  # ri-esportata: soglia +3pp sul mercato (11/09)
    MARKET_EDGE_MODERATE,
    MARKET_EDGE_STRONG,
    LEAGUE_EFFICIENCY,
)


# === STRATEGIA BASE ===
# Dati backtest storico (16.273 partite, 2022-2026, gate 1.30-1.80):
#   • Totali: 215 bet, ROI +5.7%, hit 60.5%, MaxDD 7.1%
#   • CLV vig-free: -3.3% → il modello NON batte la closing line
#   • Perde in Serie A (-5.9%), La Liga (-6.3%), Grecia (-69.4%)
#   • Vince in Bundesliga (+28.3%), PL (+16.2%), Turchia (+22.1%),
#     Ligue 1 (+9.1%)
#   • Fascia 1.60-1.80 = +21.9% ROI (n=55), 1.45-1.60 = -9.9% (n=49)
#   • Fascia 1.30-1.45 = +7.4% (n=44, marginalmente positiva)
#
# Strategia corretta (basata sui dati):
#   1) SOLO campionati con ROI positivo nel backtest
#   2) Fascia quote 1.50-2.20 (esclude i "pantano" 1.30-1.45)
#   3) Edge differenziato per efficienza lega
#   4) Kelly adattivo: +2% per PL/Bundesliga, +1.5% per altri
#   5) Esclusione automatica leghe con CLV negativo cronico

EV_MIN = 0.02            # +2% minimo (09/09: abbassato per piu' segnali)
EV_MAX = 0.15            # +15% massimo (oltre = anomalia)

# Fascia quote: esclude 1.30-1.45 (pantano -9.9% nel backtest)
# e 1.80+ (troppo lungo, alta varianza)
ODDS_MIN = 1.50          # quota minima: esclude i "pantano"
ODDS_MAX = 2.20          # quota massima: favoriti + value moderati

# === STRATEGIA PER LEGA ===
# Solo campionati con EV positivo nel backtest storico.
# Ogni lega ha: min_edge (vs mercato), kelly_mult, max_stake.
# Leghe NON elencate sono escluse automaticamente.
STRATEGY_LEAGUES = {
    # Leghe vincenti (backtest)
    "Premier League":       {"min_edge": 0.020, "kelly_mult": 1.2, "max_stake": 0.020, "efficiency": 0.85},
    "Bundesliga":           {"min_edge": 0.020, "kelly_mult": 1.3, "max_stake": 0.020, "efficiency": 0.78},
    "Turkey Super Lig":     {"min_edge": 0.025, "kelly_mult": 1.1, "max_stake": 0.018, "efficiency": 0.60},
    "Ligue 1":              {"min_edge": 0.025, "kelly_mult": 1.0, "max_stake": 0.018, "efficiency": 0.75},
    "Eredivisie":           {"min_edge": 0.025, "kelly_mult": 0.8, "max_stake": 0.015, "efficiency": 0.65},
    # Leghe perse nel backtest: generate NO segnali
    # "Serie A", "La Liga", "Belgian Pro League", "Liga Portugal",
    # "Greek Super League" — escluse per ROI negativo
}

# Fallback per leghe non in STRATEGY_LEAGUES (vietate per default)
DEFAULT_LEAGUE_STRATEGY = {"min_edge": 0.05, "kelly_mult": 0.5, "max_stake": 0.005}

FAVOURITES_ONLY = True   # mantenere: evita sfavorite ad alta quota
MIN_FAVOURITE_MARKET_PROB = 0.50   # prob. di mercato minima del favorito
KELLY_BASE = 0.015         # Kelly base frazionato (1.5% puro)
MAX_STAKE_PCT = 0.02       # cap 2% del bankroll (era 1%)

# PATCH CALIBRAZIONE bucket bassi (06/09)
LOW_PROB_THRESHOLD = 0.40
LOW_PROB_SHRINK = 0.85

# PATCH CALIBRAZIONE bucket bassi (06/09): il gap residuo della config
# 1X2-only e' sui pareggi/trasferte (bucket 0.3-0.4 = 54% del volume con
# hit 29.4% vs 35 atteso; "2" trasferta -21.9%, "1" casa -8.76%). Sotto
# LOW_PROB_THRESHOLD la deviazione dal mercato viene compressa del fattore
# LOW_PROB_SHRINK: le pick X/2 marginali escono dal filtro EV e le
# superstiti hanno edge genuino. MISURATO sul backtest storico (catena 4+1
# run flat €20): closing -6.11% -> -3.08%, strong_value -0.3% -> +6.0%.
# (Il corrispondente shrink sui bucket ALTI e' stato misurato NEGATIVO
# nella config 1X2-only e NON e' in produzione: vedi AGENTS.md.)
LOW_PROB_THRESHOLD = 0.40
LOW_PROB_SHRINK = 0.85

def get_league_strategy(league: str = "") -> dict:
    """Ritorna la configurazione strategica per una lega.

    Le leghe con ROI positivo nel backtest hanno parametri generosi.
    Le leghe non elencate usano il fallback severo (effectivamente
    vietate). Se la lega e' vuota, usa il fallback.
    """
    if not league or league not in STRATEGY_LEAGUES:
        return DEFAULT_LEAGUE_STRATEGY
    return STRATEGY_LEAGUES[league]


def league_allowed(league: str = "") -> bool:
    """True se la lega e' nelle strategie vincenti (ROI positivo)."""
    return bool(league and league in STRATEGY_LEAGUES)


def compute_ev(prob: float, odds: float) -> float:
    """Expected Value: (prob * odds) - 1"""
    return (prob * odds) - 1.0


def combined_quote(odds: List[float]) -> float:
    """Quota combinata di una multipla (prodotto delle quote)."""
    prod = 1.0
    for o in odds:
        prod *= o
    return prod


def combined_probability(probs: List[float]) -> float:
    """Probabilita' congiunta di una multipla (prodotto, ipotesi indipendenza)."""
    prod = 1.0
    for p in probs:
        prod *= p
    return prod


# Frazioni e cap dedicati alle multiple
MULTIPLA_KELLY_FRACTION = 0.125
MULTIPLA_MAX_STAKE_PCT = 0.01
MULTIPLA_MAX_EV = 0.05


def kelly_fraction(prob: float, odds: float, fraction: float = KELLY_BASE) -> float:
    """Kelly Criterion frazionario (default: KELLY_BASE)"""
    if odds <= 1.0:
        return 0.0
    q = 1.0 - prob
    kelly_full = (prob * odds - q) / odds
    return max(0.0, kelly_full * fraction)


def kelly_euro(bankroll: float, prob: float, odds: float,
               league: str = "", fraction: float | None = None) -> float:
    """Stake in euro con Kelly adattivo per lega e cap.

    Usa la strategia specifica della lega (kelly_mult e max_stake).
    """
    strat = get_league_strategy(league)
    frac = fraction if fraction is not None else KELLY_BASE * strat["kelly_mult"]
    kelly = kelly_fraction(prob, odds, frac)
    stake = bankroll * kelly
    cap = bankroll * strat["max_stake"]
    return min(stake, cap)


def market_edge(model_prob: float, market_prob: float) -> float:
    """Edge del modello sul mercato: model_prob - market_prob."""
    return _market_edge(model_prob, market_prob) or 0.0


def is_sane(prob: float, odds: float, ev: float,
            market_prob: float | None = None,
            league: str = "",
            market_edge_min: float | None = None,
            odds_max: float = ODDS_MAX,
            favourites_only: bool = FAVOURITES_ONLY) -> tuple[bool, str]:
    """Verifica se il segnale supera i filtri di sanita' con strategia per lega.

    Con market_prob disponibile, aggiunge il vincolo "beating the market":
    il segnale e' valore solo se il modello stima una probabilita' SUPERIORE
    a quella implicita nel mercato (devig).

    In piu' applica la STRATEGIA PER LEGA:
    - leghe non in STRATEGY_LEAGUES sono vietate
    - edge minimo differenziato per lega
    - fascia quote 1.50-2.20
    """
    # Lega vietata?
    if league and not league_allowed(league):
        return False, (f"lega '{league}' esclusa per ROI negativo "
                       "(strategia solo campionati vincenti)")
    if odds < ODDS_MIN:
        return False, f"quota troppo bassa ({odds:.2f} < {ODDS_MIN})"
    if odds > odds_max:
        return False, (f"quota troppo alta ({odds:.2f} > {odds_max})")
    if favourites_only and market_prob is not None \
            and market_prob < MIN_FAVOURITE_MARKET_PROB:
        return False, (f"non e' il favorito di mercato (prob. "
                       f"{market_prob*100:.1f}% < "
                       f"{MIN_FAVOURITE_MARKET_PROB*100:.0f}%)")
    if ev < EV_MIN:
        return False, f"EV troppo basso ({ev*100:.1f}% < {EV_MIN*100:.0f}%)"
    if ev > EV_MAX:
        return False, f"ANOMALIA: EV troppo alto ({ev*100:.1f}% > {EV_MAX*100:.0f}%)"
    if market_prob is not None:
        edge = prob - market_prob
        # Edge minimo differenziato per lega
        if market_edge_min is None:
            strat = get_league_strategy(league)
            market_edge_min = strat["min_edge"]
        if edge < market_edge_min:
            return False, (f"non batte il mercato (edge {edge*100:.1f}pp < "
                           f"{market_edge_min*100:.1f}pp vs prob. "
                           f"di mercato {market_prob*100:.1f}%)")
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
    p = favourite_longshot_adjust(p, market_prob, odds)
    # PATCH CALIBRAZIONE bucket bassi: comprimi la deviazione dal mercato
    # quando la probabilità finale è bassa (pareggi/trasferte sovrastimati).
    if LOW_PROB_SHRINK < 1.0 and market_prob is not None and p < LOW_PROB_THRESHOLD:
        p = market_prob + (p - market_prob) * LOW_PROB_SHRINK
    return p


def eligible_favourites(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filtra i candidati tenendo SOLO i favoriti netti (strategia 11/09).

    Un candidato qualifica se: ha la prob. di mercato devigata, questa e'
    >= MIN_FAVOURITE_MARKET_PROB, e' la PIU' ALTA tra i candidati dello
    stesso mercato e la sua quota non supera ODDS_MAX.

    Va chiamata sui candidati di UN SOLO mercato (i tre esiti 1X2, oppure
    le due linee di un Asian Handicap): il confronto "chi e' il favorito"
    ha senso solo tra esiti alternativi dello stesso mercato.
    Ritorna [] se nessun candidato qualifica: il match non genera segnali.
    """
    if not candidates:
        return []
    if not FAVOURITES_ONLY:
        return list(candidates)
    valid = [c for c in candidates if c.get("market_prob") is not None]
    if not valid:
        return []
    top = max(float(c["market_prob"]) for c in valid)
    out = []
    for c in valid:
        mp = float(c["market_prob"])
        if mp < MIN_FAVOURITE_MARKET_PROB:
            continue
        if mp < top - 1e-9:
            continue
        if float(c.get("quota") or 0.0) > ODDS_MAX:
            continue
        out.append(c)
    return out


def favourites_gate_reason() -> str:
    """Messaggio standard quando nessun esito e' un favorito netto."""
    return (f"nessun favorito netto (quota {ODDS_MIN:.2f}-{ODDS_MAX:.2f} e "
            f"prob. di mercato >= {MIN_FAVOURITE_MARKET_PROB*100:.0f}%)")


def get_signal_tier(ev: float, market_edge_val: float | None = None) -> str:
    """Classifica un segnale in tier basato su EV e edge vs mercato.

    Tier: strong_value (>= +5pp), value (>= +2pp), moderate (>= 0pp).
    """
    if market_edge_val is not None:
        if market_edge_val >= MARKET_EDGE_STRONG:
            return "strong_value"
        elif market_edge_val >= MARKET_EDGE_MODERATE:
            return "value"
    if ev >= 0.05:
        return "strong_value"
    elif ev >= EV_MIN:
        return "value"
    return "moderate"


def filter_value_bets(odds_data: List[Dict[str, Any]],
                       ev_threshold: float = EV_MIN) -> List[Dict[str, Any]]:
    """Filtra le quote con EV positivo, applicando filtri di sanita' Pro
    con strategia per lega.

    Classifica ogni segnale in tier (strong_value/value/moderate).
    Backward-compatible: se la riga non ha "market_prob" mantiene il
    comportamento storico.
    """
    value_signals = []
    for odd in odds_data:
        prob = odd.get("probabilita", 0.0)
        quota = odd.get("quota_decimale", 1.0)
        league = odd.get("league", "")
        if prob <= 0 or quota <= 1.0:
            continue
        ev = compute_ev(prob, quota)
        market_prob = odd.get("market_prob")
        if market_prob is not None:
            edge = prob - market_prob
            odd["market_edge"] = edge
            odd["beats_market"] = edge >= MARKET_EDGE_MIN
        sane, reason = is_sane(prob, quota, ev, market_prob=market_prob,
                                league=league)
        odd["ev"] = ev
        odd["kelly"] = kelly_fraction(prob, quota)
        odd["sane"] = sane
        odd["sane_reason"] = reason
        if sane and ev >= ev_threshold:
            odd["tier"] = get_signal_tier(ev, odd.get("market_edge"))
            value_signals.append(odd)
    return sorted(value_signals, key=lambda x: x["ev"], reverse=True)


def get_pro_stake(bankroll: float, prob: float, odds: float,
                   league: str = "") -> dict:
    """Ritorna dizionario completo con stake, cap, e info filtri
    con Kelly adattivo per lega."""
    ev = compute_ev(prob, odds)
    sane, reason = is_sane(prob, odds, ev, league=league)
    stake = kelly_euro(bankroll, prob, odds, league)
    return {
        "ev": ev,
        "ev_pct": ev * 100,
        "sane": sane,
        "sane_reason": reason,
        "kelly_fraction": kelly_fraction(prob, odds),
        "kelly_pct": kelly_fraction(prob, odds) * 100,
        "stake_raw": bankroll * kelly_fraction(prob, odds),
        "stake": stake,
        "stake_pct_of_bankroll": (stake / bankroll * 100) if bankroll > 0 else 0,
    }
