"""
market_calib.py — Calibrazione del modello contro il mercato.

Implementa i due pilastri della strategia value betting confermati dalla
ricerca (2025-2026):

1. DEVIGGING
   La probabilita' "vera" di un esito e' quella implicita nelle quote di
   mercato PRIVATE del margine del bookmaker. La closing line e' la stima
   piu' accurata della probabilita' reale: se il modello non batte il
   mercato, l'edge e' un'illusione del modello.

2. BEATING THE MARKET
   Un segnale e' valore SOLO se il modello stima una probabilita'
   SUPERIORE alla probabilita' implicita del mercato (devig), non solo se
   l'EV contro un singolo bookmaker e' positivo. I bookmaker caricano piu'
   margine sui longshot (favourite-longshot bias): il metodo power (e Shin
   per mercati a piu' esiti) corregge questo bias dando piu' credito ai
   favoriti.

Metodi di devig supportati:
- "multiplicative": p_i / somma(p_i) — default semplice, buono su mercati
  bilanciati;
- "power": trova k tale che somma(p_i^k) = 1 — corregge il
  favourite-longshot bias, default consigliato per 1X2 e totals;
- "shin": risolve il parametro z del modello di insider trading di
  Shin (1993) — piu' aggressivo sul bias, consigliato su mercati lopsided
  a molti esiti.
"""

from __future__ import annotations

from typing import Dict, List, Optional

# --- Soglie di mercato (ricerca: servono almeno 3-5 punti percentuali
# --- di edge vs la closing line perche' il valore non sia rumore)
MARKET_EDGE_MIN = 0.03          # +3pp vs mercato per "value"
MARKET_EDGE_STRONG = 0.05       # +5pp vs mercato per "strong_value"

# Peso del modello nel blending modello+mercato (0.5 = pari peso).
# Il mercato e' quasi sempre piu' calibrato del modello: non superare 0.6.
BLEND_WEIGHT = 0.5

# Sopra questa quota il modello tende a sovrastimare (longshot bias):
# la probabilita' del modello viene compressa verso il mercato.
LONG_SHOT_ODDS = 3.5

_MIN_P = 1e-9


# ---------------------------------------------------------------------------
# Devigging
# ---------------------------------------------------------------------------

def devig_multiplicative(probs: List[float]) -> List[float]:
    """Devig proporzionale: p_i / somma(p_i)."""
    total = sum(max(p, _MIN_P) for p in probs)
    if total <= 0:
        return [1.0 / len(probs)] * len(probs)
    return [max(p, _MIN_P) / total for p in probs]


def devig_power(probs: List[float], max_iter: int = 100) -> List[float]:
    """Devig con metodo power: trova k tale che somma(p_i^k) = 1.

    Correzione del favourite-longshot bias: i longshot assorbono piu'
    margine dei favoriti, quindi la probabilita' fair del favorito sale.
    Richiede ricerca iterativa (nessuna soluzione in forma chiusa).
    """
    n = len(probs)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    ps = [max(p, _MIN_P) for p in probs]
    lo, hi = 0.01, 20.0
    for _ in range(max_iter):
        k = (lo + hi) / 2.0
        s = sum(p ** k for p in ps)
        if s > 1.0:
            lo = k          # serve k piu' grande per ridurre la somma
        else:
            hi = k
    k = (lo + hi) / 2.0
    fair = [p ** k for p in ps]
    total = sum(fair)
    return [f / total for f in fair]


def _shin_fair(ps: List[float], max_iter: int = 200) -> List[float]:
    """Risolve il modello di Shin (1993) per la distribuzione fair.

    z rappresenta la proporzione di insider nel mercato; la formula e':
        p_fair = (sqrt(z^2 + 4*(1-z)*p^2) - z) / (2*(1-z))
    con z tale che somma(p_fair) = 1. Aggressivo sul longshot bias.
    """
    n = len(ps)
    if n == 0:
        return []
    if n == 1:
        return [1.0]

    def _fair_for_z(z: float) -> List[float]:
        denom = 2.0 * (1.0 - z) if z < 1.0 else 1e-9
        out = []
        for p in ps:
            disc = z * z + 4.0 * (1.0 - z) * p * p
            out.append((max(disc, 0.0) ** 0.5 - z) / denom)
        return out

    lo, hi = 0.0, 1.0
    for _ in range(max_iter):
        z = (lo + hi) / 2.0
        s = sum(_fair_for_z(z))
        if s > 1.0:
            lo = z
        else:
            hi = z
    fair = _fair_for_z((lo + hi) / 2.0)
    total = sum(fair)
    if total <= 0:
        return devig_multiplicative(ps)
    return [f / total for f in fair]


def devig(odds: List[float], method: str = "power") -> List[float]:
    """Quote decimali -> probabilita' fair (somma 1.0).

    method: "multiplicative" | "power" (default) | "shin".
    """
    probs = [1.0 / o for o in odds if o and o > 1.0]
    if not probs:
        return []
    if method == "multiplicative":
        return devig_multiplicative(probs)
    if method == "shin":
        return _shin_fair(probs)
    return devig_power(probs)


def market_implied(odds_map: Dict[str, float],
                   method: str = "power") -> Optional[Dict[str, float]]:
    """Devig di un intero mercato.

    odds_map: {esito: quota_decimale} (es. {"1": 2.1, "X": 3.4, "2": 3.8}).

    Ritorna {esito: probabilita_fair, "overround": margine_brutto} oppure
    None se mancano almeno 2 esiti validi.
    """
    items = [(k, o) for k, o in odds_map.items() if o and o > 1.0]
    if len(items) < 2:
        return None
    keys = [k for k, _ in items]
    odds_list = [o for _, o in items]
    fair = devig(odds_list, method=method)
    overround = sum(1.0 / o for o in odds_list)
    result = {keys[i]: fair[i] for i in range(len(keys))}
    result["overround"] = overround
    return result


# ---------------------------------------------------------------------------
# Confronto modello vs mercato
# ---------------------------------------------------------------------------

def market_edge(model_prob: float, market_prob: Optional[float]) -> Optional[float]:
    """Edge del modello sul mercato in punti di probabilita'.

    > 0 => il modello vede piu' probabilita' di quanta il mercato ne prezza
    (candidato value). None se il mercato non e' disponibile.
    """
    if market_prob is None:
        return None
    return model_prob - market_prob


def is_beating_market(model_prob: float, market_prob: Optional[float],
                      threshold: float = MARKET_EDGE_MIN) -> bool:
    """True se il modello batte il mercato di almeno `threshold` punti."""
    if market_prob is None:
        # Senza riferimento di mercato non possiamo verificare: lasciamo
        # passare il segnale (comportamento storico) ma senza garanzia.
        return True
    return (model_prob - market_prob) >= threshold


def blend_probability(model_prob: float, market_prob: Optional[float],
                      weight: float = BLEND_WEIGHT) -> float:
    """Probabilita' finale = blend modello + mercato.

    Il mercato e' quasi sempre meglio calibrato del modello: mescolare le
    due stime riduce l'overconfidence del modello (causa n.1 dei falsi
    segnali nei backtest). Se il mercato manca, resta la stima del modello.
    """
    if market_prob is None:
        return model_prob
    return weight * model_prob + (1.0 - weight) * market_prob


def favourite_longshot_adjust(model_prob: float, market_prob: Optional[float],
                              odds: float, long_shot_odds: float = LONG_SHOT_ODDS) -> float:
    """Corregge l'overconfidence del modello sui longshot.

    Il favourite-longshot bias dice che i longshot (quote alte) sono
    sopravvalutati dal pubblico e dai modelli naive. Sopra LONG_SHOT_ODDS
    la probabilita' del modello viene compressa verso la probabilita' di
    mercato, in modo proporzionale alla quota.
    """
    if market_prob is None or odds <= long_shot_odds:
        return model_prob
    t = min(1.0, (odds - long_shot_odds) / 2.0)   # 0..1 all'aumentare della quota
    return model_prob * (1.0 - 0.5 * t) + market_prob * (0.5 * t)
