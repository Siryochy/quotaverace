#!/usr/bin/env python3
"""t60_cap_impact.py — impatto del cap CB1 (T-60) sulla corsia Kelly.

Domanda del proprietario (17/09/2026): il cap **CB1** (`T60_MAX_STAKE_USDC`,
1.00 USDC per ordine) deve valere anche sulla corsia che piazza DAVVERO —
`auto_bet.run_today_bets`, che usa il Kelly adattivo + i cap percentuali — e
non solo sul dispatch T-60? Prima di decidere: **misura**.

Perche' la risposta non e' ovvia (e non si stima a occhio): con
`STAKE_CAP_HARD=0` (scelta del proprietario del 12/09) il **floor**
dell'exchange (1.00 USDC) PREVALE sul cap percentuale — uno stake sotto il
floor viene alzato a 1.00 USDC, che e' **esattamente** il cap CB1. Quindi sotto
un certo bankroll il CB1 non cambia NULLA, e sopra taglia. La soglia dipende
dal tier (cap 1% value/moderate, 2% strong_value), dalla quota e dall'EV: qui
si calcola con l'`adaptive_stake` **reale** della produzione, non con una
copia della formula.

Il modulo NON decide e NON cambia niente: **sola LETTURA** (SQLite `mode=ro`),
nessun ordine, nessuna rete, zero crediti the-odds-api.

Uso:
    venv/bin/python t60_cap_impact.py                 # griglia + ledger locale
    venv/bin/python t60_cap_impact.py --bankroll 100
    venv/bin/python t60_cap_impact.py --clv           # scenario CLV positivo
    venv/bin/python t60_cap_impact.py --json
    venv/bin/python t60_cap_impact.py --db /percorso/quotaverace.db
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import DATA_DIR
from adaptive_staking import adaptive_stake

logger = logging.getLogger("t60_cap_impact")

# Equity reale del wallet SX al 17/09/2026 (documentata in AGENTS.md: 35.98):
# e' il bankroll con cui la corsia ragiona oggi in LIVE.
WALLET_EQUITY_17_09 = 36.0
# Scala della misura: dal wallet reale ai bankroll in cui il cap percentuale
# comincia a superare il cap CB1.
BANKROLL_GRID = (25.0, 36.0, 50.0, 75.0, 100.0, 150.0, 250.0, 500.0)
TIERS = ("value", "moderate", "strong_value")
# Parametri rappresentativi dei segnali che la strategia produce (favoriti
# netti 1.30-1.80, edge >= +3pp, EV >= 2%): servono a descrivere la forma,
# non a inventare un campione.
DEFAULT_PRICE = 1.70
DEFAULT_EDGE = 0.03
DEFAULT_EV = 0.05


def _const(name: str, default: float) -> float:
    """Legge una costante da auto_bet (import pigro: nessun effetto collaterale)."""
    try:
        import auto_bet
        return float(getattr(auto_bet, name, default))
    except Exception:
        return float(default)


def cb1_cap() -> float:
    """Tetto CB1 effettivo (env `T60_MAX_STAKE_USDC`, default 1.00 USDC)."""
    return _const("T60_MAX_STAKE_USDC", 1.0)


def cap_hard_active() -> bool:
    """`STAKE_CAP_HARD` REALE (bool di auto_bet, env `STAKE_CAP_HARD`).

    Determina il senso della misura: con il cap severo ATTIVO lo stake sotto
    il floor viene SALTATO (il CB1 non ha nulla da tagliare); con il cap
    severo OFF (produzione dal 12/09) il floor alza a 1.00 USDC.
    """
    try:
        import auto_bet
        return bool(getattr(auto_bet, "STAKE_CAP_HARD", True))
    except Exception:
        return True


def lane_stake(bankroll: float, price: float = DEFAULT_PRICE,
               status: str = "value", market_edge: float = DEFAULT_EDGE,
               best_ev: float = DEFAULT_EV, *, clv_positive: bool = False,
               odds_movement: float = 0.0,
               spendable: Optional[float] = None,
               cap_hard: Optional[bool] = None) -> Dict[str, Any]:
    """Replica ESATTA dello stake della corsia `run_today_bets` (live).

    Sequenza del codice di produzione: `adaptive_stake` (Kelly frazionato +
    cap per tier) -> bonus movimento (x1.2 se <= -5%) -> limite di cassa
    (`min(disponibile)`) -> floor dell'exchange (1.00 USDC, che con
    `STAKE_CAP_HARD=1` NON alza e l'ordine sarebbe SALTATO) -> arrotondamento
    a 2 decimali.
    """
    try:
        bankroll = float(bankroll)
    except (TypeError, ValueError):
        bankroll = 0.0
    if price is None or price <= 1.0 or bankroll <= 0:
        return {"stake": 0.0, "skipped": True, "reason": "bankroll/quota non validi"}
    if cap_hard is None:
        cap_hard = cap_hard_active()
    floor = _const("MIN_STAKE_EUR", 1.0)
    as_result = adaptive_stake(
        bankroll=bankroll,
        prob=(float(best_ev) + 1.0 / float(price)),
        odds=float(price),
        market_edge=market_edge,
        status=status,
        peak_bankroll=bankroll,
        has_clv_positive=clv_positive)
    stake = float(as_result.get("stake") or 0.0)
    if stake <= 0:
        return {"stake": 0.0, "skipped": True, "reason": "stake Kelly nullo",
                "adaptive": as_result}
    if odds_movement and float(odds_movement) <= -0.05:
        stake *= 1.2
    if spendable is not None:
        stake = min(stake, float(spendable))
    if stake < floor:
        if cap_hard:
            return {"stake": 0.0, "skipped": True,
                    "reason": f"CAP SEVERO: {stake:.2f} < floor {floor:.2f}",
                    "adaptive": as_result}
        stake = floor
    return {"stake": round(stake, 2), "skipped": False,
            "floor_applied": stake == floor, "adaptive": as_result}


def with_cb1(stake: float, cap: Optional[float] = None) -> float:
    """Stake se il cap CB1 valesse anche sulla corsia (taglio, mai aumento)."""
    return round(min(float(stake), float(cb1_cap() if cap is None else cap)), 2)


def break_even_bankroll(status: str, *, price: float = DEFAULT_PRICE,
                        market_edge: float = DEFAULT_EDGE,
                        best_ev: float = DEFAULT_EV,
                        clv_positive: bool = False,
                        cap_hard: Optional[bool] = None,
                        lo: float = 1.0, hi: float = 100_000.0) -> Optional[float]:
    """Bankroll minimo oltre il quale il CB1 morde (stake corsia > cap).

    Monotonia: lo stake cresce col bankroll, quindi la bisezione e' valida.
    `None` se nemmeno a bankroll enorme il cap morde (non dovrebbe accadere:
    con cap 1-2% lo stake supera 1 USDC oltre ~50-100 USDC) — o se col CAP
    SEVERO attivo l'ordine verrebbe SALTATO sotto il floor (nessun taglio).
    """
    def morde(b: float) -> bool:
        res = lane_stake(b, price=price, status=status,
                         market_edge=market_edge, best_ev=best_ev,
                         clv_positive=clv_positive, cap_hard=cap_hard)
        return (not res["skipped"]) and float(res["stake"]) > cb1_cap() + 1e-9

    if not morde(hi):
        return None
    if morde(lo):
        return lo
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if morde(mid):
            hi = mid
        else:
            lo = mid
    return round(hi, 2)


def analytical(bankrolls=BANKROLL_GRID, *, clv_positive: bool = False,
               price: float = DEFAULT_PRICE,
               market_edge: float = DEFAULT_EDGE,
               best_ev: float = DEFAULT_EV,
               cap_hard: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Griglia bankroll x tier: stake corsia, stake con CB1, differenza."""
    rows: List[Dict[str, Any]] = []
    for bankroll in bankrolls:
        cells: Dict[str, Any] = {}
        for tier in TIERS:
            res = lane_stake(bankroll, price=price, status=tier,
                             market_edge=market_edge, best_ev=best_ev,
                             clv_positive=clv_positive, cap_hard=cap_hard)
            stake = 0.0 if res["skipped"] else float(res["stake"])
            capped = with_cb1(stake)
            cells[tier] = {
                "stake_corsia": stake,
                "stake_cb1": capped,
                "delta": round(capped - stake, 2),
                "delta_pct": (round((capped - stake) / stake * 100.0, 1)
                              if stake > 0 else 0.0),
                "skipped": bool(res["skipped"]),
            }
        rows.append({"bankroll": float(bankroll), "tiers": cells})
    return rows


def ledger_signals(db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Segnali qualificati 1X2 ancora aperti dal ledger (SOLA LETTURA).

    Stessa popolazione dei candidati di `auto_bet._today_value_picks`
    (status value/strong_value/moderate, mercato 1X2, esito non chiuso).
    """
    path = Path(db_path) if db_path else Path(DATA_DIR) / "quotaverace.db"
    if not path.exists():
        return []
    # mode=ro: se il percorso fosse sbagliato o il file protetto, la lettura
    # fallisce invece di creare/modificare un database.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT m.id, m.league, p.esito, p.quota, p.market_edge, p.ev, "
            "p.status FROM matches m JOIN predictions p ON m.id = p.match_id "
            "WHERE p.status IN ('value','strong_value','moderate') "
            "AND p.mercato = '1X2' AND p.esito_finale IS NULL").fetchall()
    except sqlite3.Error as e:                       # DB vecchio/parziale
        logger.warning("ledger illeggibile (%s): misura sul solo modello", e)
        return []
    finally:
        conn.close()
    out = []
    for mid, league, esito, quota, m_edge, ev, status in rows:
        out.append({"match_id": mid, "league": league or "", "esito": esito,
                    "quota": float(quota or 0),
                    "market_edge": (float(m_edge) if m_edge is not None else None),
                    "ev": float(ev or 0.0), "status": status or "value"})
    return out


def measure(bankroll: Optional[float] = None, *, db_path: Optional[Path] = None,
            clv_positive: bool = False,
            cap_hard: Optional[bool] = None) -> Dict[str, Any]:
    """Misura completa: griglia + soglie di rottura + righe reali del ledger."""
    bankroll = float(bankroll) if bankroll is not None else WALLET_EQUITY_17_09
    if cap_hard is None:
        cap_hard = cap_hard_active()
    cap = cb1_cap()
    grid = analytical(clv_positive=clv_positive, cap_hard=cap_hard)
    thresholds = {t: break_even_bankroll(t, clv_positive=clv_positive,
                                        cap_hard=cap_hard)
                  for t in TIERS}
    signals = ledger_signals(db_path)
    capped_now: List[Dict[str, Any]] = []
    for s in signals:
        if not s["quota"]:
            continue
        res = lane_stake(bankroll, price=s["quota"], status=s["status"],
                         market_edge=(s["market_edge"] if s["market_edge"]
                                      is not None else DEFAULT_EDGE),
                         best_ev=s["ev"] or DEFAULT_EV,
                         clv_positive=clv_positive, cap_hard=cap_hard)
        if res["skipped"]:
            continue
        stake = float(res["stake"])
        capped = with_cb1(stake)
        if capped < stake - 1e-9:
            capped_now.append({**s, "stake_corsia": stake, "stake_cb1": capped,
                               "delta": round(capped - stake, 2)})
    return {
        "cb1_cap": cap,
        "bankroll": bankroll,
        "cap_hard": bool(cap_hard),
        "clv_positive": bool(clv_positive),
        "grid": grid,
        "break_even_bankroll": thresholds,
        "ledger_signals": len(signals),
        "ledger_capped": capped_now,
        "verdict": _verdict(bankroll, cap, thresholds, signals, capped_now),
    }


def _verdict(bankroll: float, cap: float, thresholds: Dict[str, Optional[float]],
             signals: List[Dict[str, Any]],
             capped: List[Dict[str, Any]]) -> str:
    primo = min((v for v in thresholds.values() if v is not None), default=None)
    if capped:
        return (f"OGGI il CB1 taglierebbe {len(capped)}/{len(signals)} segnali "
                f"vivi con bankroll {bankroll:.2f}: impatto REALE")
    if primo is None:
        return ("il CB1 non morde nemmeno a bankroll molto alti: nessun impatto "
                "sulla corsia")
    return (f"con bankroll {bankroll:.2f} il CB1 non cambia NESSUNA puntata "
            f"(floor {cap:.2f} = cap): comincia a mordere da "
            f"{primo:.2f} USDC")


def format_report(m: Dict[str, Any]) -> str:
    """Report leggibile (Telegram-friendly)."""
    cap = m["cb1_cap"]
    lines = [
        "🛑 *Cap CB1 sulla corsia Kelly — impatto misurato*",
        "",
        f"Cap CB1: *{cap:.2f} USDC/ordine* · bankroll di riferimento: "
        f"{m['bankroll']:.2f} USDC",
        f"STAKE_CAP_HARD: {'ON (floor = ordine saltato)' if m['cap_hard'] else 'OFF (floor 1.00 USDC prevale)'}"
        + (" · scenario CLV positivo" if m["clv_positive"] else ""),
        "",
        "`bankroll   value        moderate     strong`",
    ]
    for row in m["grid"]:
        cells = []
        for tier in TIERS:
            c = row["tiers"][tier]
            if c["skipped"]:
                cells.append("saltato    ")
            elif c["delta"]:
                cells.append(f"{c['stake_corsia']:.2f}→{c['stake_cb1']:.2f}"
                             f" ({c['delta_pct']:.0f}%)")
            else:
                cells.append(f"{c['stake_corsia']:.2f} =     ")
        lines.append(f"`{row['bankroll']:>7.2f}   " + "  ".join(cells) + "`")
    lines.append("")
    for tier, thr in m["break_even_bankroll"].items():
        lines.append(f"• da {thr:.2f} USDC in su il CB1 morde su *{tier}*"
                     if thr is not None else
                     f"• *{tier}*: il CB1 non morde a nessun bankroll")
    lines.append("")
    if m["ledger_capped"]:
        lines.append(f"⚠️ Ledger: {len(m['ledger_capped'])}/{m['ledger_signals']} "
                     "segnali vivi sarebbero TAGLIATI:")
        for s in m["ledger_capped"][:10]:
            lines.append(f"  · {s['match_id']} ({s['status']}) "
                         f"{s['stake_corsia']:.2f} → {s['stake_cb1']:.2f}")
    else:
        lines.append(f"Ledger: {m['ledger_signals']} segnali vivi, "
                     "0 sarebbero tagliati dal CB1.")
    lines.append("")
    lines.append(f"*Verdetto*: {m['verdict']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bankroll", type=float, default=WALLET_EQUITY_17_09,
                    help=f"bankroll di riferimento (default {WALLET_EQUITY_17_09} USDC)")
    ap.add_argument("--db", type=str, default=None,
                    help="percorso del ledger SQLite (default DATA_DIR)")
    ap.add_argument("--clv", action="store_true",
                    help="scenario con CLV storico positivo (stake piu' alti)")
    ap.add_argument("--json", action="store_true", help="output JSON")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    m = measure(args.bankroll, db_path=(Path(args.db) if args.db else None),
                clv_positive=args.clv)
    print(json.dumps(m, indent=2, default=str) if args.json
          else format_report(m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
