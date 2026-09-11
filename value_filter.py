"""Value bet filter e Kelly Criterion Pro"""

from typing import List, Dict, Any

from market_calib import (
    blend_probability,
    favourite_longshot_adjust,
    market_edge as _market_edge,
    MARKET_EDGE_MIN,  # ri-esportata: soglia +2pp sul mercato (09/09)
    MARKET_EDGE_MODERATE,
    MARKET_EDGE_STRONG,
)


# === FILTRI DI SANITÀ ===
EV_MIN = 0.02            # +2% minimo (09/09: abbassato per più segnali)
EV_MAX = 0.15            # +15% massimo (oltre = anomalia)
ODDS_MIN = 1.50          # quota minima

# === STRATEGIA SOLO FAVORITI (11/09/2026) ===
# Direttiva del proprietario dopo il passaggio a live: vietato tassativamente
# puntare su squadre sfavorite o quote alte (motivo: rischio bancarotta).
# Il gate ha DUE vincoli, entrambi obbligatori:
#   1. quota <= ODDS_MAX (1.80 = favorito forte, prob. implicita ~55%);
#   2. l'esito deve essere il FAVORITO NETTO del mercato: prob. devigata
#      >= MIN_FAVOURITE_MARKET_PROB e la PIU' ALTA del mercato 1X2.
# La sola quota non basta: con un overround alto un 1.80 puo' non essere il
# favorito. Nessun escape hatch silenzioso: gli esiti fuori gate restano
# "rejected" e non finiscono ne' in schedina ne' nel ledger.
ODDS_MAX = 1.80          # quota massima (era 3.00)
FAVOURITES_ONLY = True   # solo pronostici sui favoriti netti
MIN_FAVOURITE_MARKET_PROB = 0.50   # prob. di mercato minima del favorito
KELLY_FRACTION = 0.25    # 1/4 Kelly
MAX_STAKE_PCT = 0.01     # cap 1% del bankroll (11/09: era 3%, staking prudente)

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
            market_edge_min: float = MARKET_EDGE_MIN,
            odds_max: float = ODDS_MAX,
            favourites_only: bool = FAVOURITES_ONLY) -> tuple[bool, str]:
    """Verifica se il segnale supera i filtri di sanità.

    Con market_prob disponibile, aggiunge il vincolo "beating the market":
    il segnale e' valore solo se il modello stima una probabilita' SUPERIORE
    a quella implicita nel mercato (devig). Questo e' il test decisivo
    della strategia value betting: EV positivo contro un bookmaker non basta,
    bisogna battere la closing line.

    In piu' (11/09) applica la STRATEGIA SOLO FAVORITI: quota entro ODDS_MAX
    e, se la prob. di mercato e' nota, esito che il mercato considera
    favorito (prob. >= MIN_FAVOURITE_MARKET_PROB).

    `odds_max`/`favourites_only` permettono all'harness di ricerca
    (`historical_backtest.py`, che ha gia' i suoi flag --max-odds e
    --high-prob-shrink) di non applicare DUE volte il gate di produzione:
    la produzione usa sempre i default (= costanti sopra).
    """
    if odds < ODDS_MIN:
        return False, f"quota troppo bassa ({odds:.2f} < {ODDS_MIN})"
    if odds > odds_max:
        return False, (f"quota troppo alta ({odds:.2f} > {odds_max}): "
                       "strategia solo favoriti netti")
    if favourites_only and market_prob is not None \
            and market_prob < MIN_FAVOURITE_MARKET_PROB:
        return False, (f"non è il favorito di mercato (prob. "
                       f"{market_prob*100:.1f}% < "
                       f"{MIN_FAVOURITE_MARKET_PROB*100:.0f}%)")
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


def get_signal_tier(ev: float, market_edge: float | None = None) -> str:
    """Classifica un segnale in tier basato su EV e edge vs mercato.

    Tier: strong_value (>= +5pp), value (>= +2pp), moderate (>= 0pp).
    """
    if market_edge is not None:
        if market_edge >= MARKET_EDGE_STRONG:
            return "strong_value"
        elif market_edge >= MARKET_EDGE_MODERATE:
            return "value"
    if ev >= 0.05:
        return "strong_value"
    elif ev >= EV_MIN:
        return "value"
    return "moderate"


def filter_value_bets(odds_data: List[Dict[str, Any]], ev_threshold: float = EV_MIN) -> List[Dict[str, Any]]:
    """Filtra le quote con EV positivo, applicando filtri di sanità Pro.

    Classifica ogni segnale in tier (strong_value/value/moderate).
    Backward-compatible: se la riga non ha "market_prob" mantiene il
    comportamento storico.
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
            odd["tier"] = get_signal_tier(ev, odd.get("market_edge"))
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
