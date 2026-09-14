"""decision/stake_engine.py — Quanto puntare. Si attiva SOLO se il rischio approva.

Regole:

1. **Kelly frazionato, scalato UNA VOLTA.** La frazione viene da
   `adaptive_staking.confidence_kelly_fraction` (edge, confidenza ML, CLV,
   tier); il Kelly puro viene calcolato con `value_filter.kelly_fraction(...,
   fraction=1.0)`. Moltiplicare la frazione anche dentro Kelly sarebbe il bug
   del 13/09 (stake incollato al floor con l'EV scalato due volte): qui la
   scalatura e' esattamente una.
2. **Tre cap, vince il piu' stretto**: tier (1% value/moderate, 2% strong),
   lega (`STRATEGY_LEAGUES[...]["max_stake"]`) e l'eventuale cap chiesto dal
   Risk Engine (che puo' solo stringere).
3. **Cap severo fail-closed**: se lo stake cappato sta sotto il floor
   dell'ordine, la puntata viene SALTATA invece di forzare il floor (che su un
   wallet piccolo diventa una percentuale di bankroll piu' alta del cap). Con
   `STAKE_CAP_HARD=0` si accetta il floor, come da configurazione di produzione.
4. **Liquidita' relativa allo stake**: il controllo di `auto_bet`
   (`max(stake x 2.0, 25 USDC)`) vive qui perche' serve il numero che solo
   questo motore conosce.
"""

from __future__ import annotations

from typing import Optional

from .limits import RiskLimits
from .models import Mode, ReasonCode, RiskDecision, Signal, StakeDecision


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return round(value, 2)
    return round(round(value / step) * step, 6)


def kelly_stake(signal: Signal, *, bankroll: float, limits: RiskLimits,
                ml_confidence: Optional[float] = None,
                has_clv_positive: Optional[bool] = None) -> tuple[float, float]:
    """(stake Kelly, frazione usata) — Kelly puro scalato una sola volta."""
    import adaptive_staking as stake_mod
    import value_filter as vf

    fraction = stake_mod.confidence_kelly_fraction(
        prob=signal.blended_prob,
        odds=signal.price,
        market_edge=signal.edge,
        ml_confidence=ml_confidence,
        has_clv_positive=has_clv_positive,
        status=signal.tier,
    )
    fraction = max(limits.kelly_min, min(limits.kelly_max, float(fraction)))
    full = vf.kelly_fraction(signal.blended_prob, signal.price, fraction=1.0)
    return (bankroll * full * fraction, fraction)


def size(signal: Signal, risk: RiskDecision, *, bankroll: float,
         limits: RiskLimits, mode: Mode = "sim",
         ml_confidence: Optional[float] = None,
         has_clv_positive: Optional[bool] = None) -> StakeDecision:
    """Stake per un segnale APPROVATO (o approvato da un umano)."""
    base = StakeDecision(bankroll=bankroll, mode=mode)
    if not risk.allows_stake:
        base.reason = risk.reason
        base.detail = f"nessuno stake: verdetto '{risk.verdict}' ({risk.reason.value})"
        base.executable = False
        return base
    if bankroll <= 0:
        base.reason = ReasonCode.STAKE_BELOW_FLOOR
        base.detail = "bankroll non disponibile (saldo insufficiente o non leggibile)"
        base.executable = False
        return base

    stake_value, fraction = kelly_stake(signal, bankroll=bankroll, limits=limits,
                                        ml_confidence=ml_confidence,
                                        has_clv_positive=has_clv_positive)
    base.kelly_fraction = fraction
    base.kelly_stake = _round_step(stake_value, limits.stake_step)

    caps: list[tuple[float, str]] = [
        (limits.cap_for(signal.tier), "tier"),
        (limits.league_max_stake_pct(signal.league), "league"),
    ]
    if "max_stake_pct" in risk.tightened:
        caps.append((float(risk.tightened["max_stake_pct"]), "risk"))
    cap_pct, cap_source = min(caps, key=lambda item: item[0])
    base.cap_pct = cap_pct
    base.cap_source = cap_source

    stake_value = min(stake_value, bankroll * cap_pct)
    stake_value = _round_step(stake_value, limits.stake_step)
    base.stake = stake_value
    base.floor = limits.floor_for(mode)

    if stake_value <= 0:
        base.reason = ReasonCode.STAKE_BELOW_FLOOR
        base.detail = f"stake calcolato {stake_value:.2f} (nessun edge residuo)"
        base.executable = False
        return base

    if stake_value < base.floor:
        if limits.stake_cap_hard:
            base.reason = ReasonCode.STAKE_BELOW_FLOOR
            base.detail = (f"CAP SEVERO: stake cappato {stake_value:.2f} < minimo ordine "
                           f"{base.floor:.2f} — ordine saltato (fail-closed). "
                           f"Cap {cap_pct*100:.2f}% su bankroll {bankroll:.2f}")
            base.executable = False
            return base
        base.stake = base.floor
        base.detail = (f"floor {base.floor:.2f} applicato (STAKE_CAP_HARD=0): stake "
                       f"{base.stake:.2f} = {base.stake/bankroll*100:.2f}% del bankroll")

    depth = signal.data_quality.depth_usdc
    if depth is not None:
        required = limits.required_depth(base.stake)
        if depth < required:
            base.reason = ReasonCode.LIQUIDITY_LOW
            base.detail = (f"liquidita' {depth:.2f} < richiesta {required:.2f} "
                           f"(stake {base.stake:.2f} x {limits.depth_multiplier}) — "
                           "salto (rischio slippage)")
            base.executable = False
            return base

    base.executable = True
    base.reason = ReasonCode.OK
    if risk.tightened:
        base.detail = (base.detail + " | cap ridotto dal Risk Engine: "
                       + ", ".join(f"{k}={v}" for k, v in risk.tightened.items())).strip(" |")
    return base


__all__ = ["kelly_stake", "size"]
