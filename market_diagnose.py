"""market_diagnose.py — Diagnosi calibrazione per mercato (ledger previsioni).

Quando il ledger raggiunge ~100 previsioni chiuse (STRATEGY.md: da quel
campione il primo segnale e' affidabile), questo script confronta per ogni
mercato:

  - ROI realizzato  vs EV atteso  (gap): il modello sta perdendo dove
    prometteva di vincere?
  - hit rate vs probabilita' media del modello (prob_gap): overconfidence?

I mercati con ROI < 0 e gap sistematicamente negativo (ROI < EV di almeno
`gap_pp` punti percentuali) su un campione sufficiente sono i candidati alla
messa a punto: peso blend, soglia EV, metodo di devig.

CLI:
  venv/bin/python market_diagnose.py                 # analisi sul DB locale
  venv/bin/python market_diagnose.py --json          # output JSON (report)
  venv/bin/python market_diagnose.py --min-total 50  # soglia campione totale

Exit code: 0 = nessuna azione consigliata (anche campione insufficiente),
           1 = trovati mercati critici da mettere a punto.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, List, Optional

# Soglie di default, coerenti con STRATEGY.md:
# - MIN_TOTAL: primo segnale affidabile da ~100 previsioni chiuse;
# - MIN_PER_MARKET: sotto 10 chiusi per mercato e' solo rumore;
# - GAP_PP: soglia coerente con MARKET_EDGE_MIN (+3pp) usata dal filtro value;
# - PROB_GAP_PP: overconfidence se hit rate < prob media di almeno 5pp.
MIN_TOTAL = 100
MIN_PER_MARKET = 10
GAP_PP = 3.0
PROB_GAP_PP = 5.0

LABELS = {
    "1X2": "1X2",
    "OU": "Over/Under",
    "AH": "Asian Handicap",
    "BTTS": "BTTS (Gol/Niente Gol)",
}


def _label(key: str) -> str:
    return LABELS.get(key, key)


def diagnose(by_mkt: Dict[str, Dict], min_total: int = MIN_TOTAL,
             min_per_market: int = MIN_PER_MARKET, gap_pp: float = GAP_PP,
             prob_gap_pp: float = PROB_GAP_PP) -> Dict:
    """Analizza predictions_summary() per mercato e ritorna la diagnosi.

    `by_mkt`: {mercato: {n, won, lost, push, hit_rate, roi, avg_ev, gap,
    avg_prob, avg_market_edge}} — esattamente l'output di tracker.
    predictions_summary().

    Ritorna un dict con: totals (intero ledger), markets (per mercato,
    ordinati per volume), sufficiente (campione totale >= min_total),
    critici (mercati da mettere a punto, ordinati per ROI crescente),
    azioni (idem ma SOLO se il campione e' sufficiente) e note.
    """
    totals = {"n": 0, "won": 0, "lost": 0, "push": 0, "pnl": 0.0,
              "ev_sum": 0.0, "roi": 0.0, "avg_ev": 0.0, "gap": 0.0}
    markets: List[Dict] = []

    for key, b in by_mkt.items():
        n = int(b.get("n", 0) or 0)
        if n <= 0:
            continue
        won = int(b.get("won", 0) or 0)
        lost = int(b.get("lost", 0) or 0)
        push = int(b.get("push", 0) or 0)
        pnl = float(n * (b.get("roi", 0) or 0) / 100.0)
        roi = float(b.get("roi", 0) or 0)
        avg_ev = float(b.get("avg_ev", 0) or 0)
        gap = float(b.get("gap", 0) or 0)
        hit_rate = float(b.get("hit_rate", 0) or 0)
        avg_prob = float(b.get("avg_prob", 0) or 0)
        prob_gap = hit_rate - avg_prob * 100.0
        edge = b.get("avg_market_edge")
        edge_v = float(edge) if edge is not None else None

        segnali: List[str] = []
        if roi < 0 and gap <= -gap_pp:
            segnali.append(f"ROI {roi:+.1f}% < EV {avg_ev:+.1f}% di "
                           f"{abs(gap):.1f}pp (soglia {gap_pp:g}pp)")
        if prob_gap <= -prob_gap_pp:
            segnali.append(f"hit rate {hit_rate:.1f}% molto sotto la prob."
                           f" media del modello {avg_prob*100:.1f}% "
                           f"(overconfidence, -{abs(prob_gap):.1f}pp)")

        critico = (
            n >= min_per_market
            and roi < 0
            and gap <= -gap_pp
        )

        markets.append({
            "mercato": str(key), "label": _label(str(key)),
            "n": n, "won": won, "lost": lost, "push": push,
            "hit_rate": round(hit_rate, 2), "roi": round(roi, 2),
            "avg_ev": round(avg_ev, 2), "gap": round(gap, 2),
            "prob_gap": round(prob_gap, 2),
            "avg_market_edge": round(edge_v, 2) if edge_v is not None else None,
            "critico": critico, "segnali": segnali,
        })

        totals["n"] += n
        totals["won"] += won
        totals["lost"] += lost
        totals["push"] += push
        totals["pnl"] += pnl
        totals["ev_sum"] += avg_ev * n / 100.0

    if totals["n"]:
        totals["roi"] = round(totals["pnl"] / totals["n"] * 100.0, 2)
        totals["avg_ev"] = round(totals["ev_sum"] / totals["n"] * 100.0, 2)
        totals["gap"] = round(totals["roi"] - totals["avg_ev"], 2)

    markets.sort(key=lambda m: (-m["n"], m["mercato"]))
    critici = [m for m in markets if m["critico"]]
    critici.sort(key=lambda m: (m["roi"], m["mercato"]))

    sufficiente = totals["n"] >= min_total
    azioni = [_azioni_market(m) for m in critici] if sufficiente else []

    note = []
    if not sufficiente:
        note = (f"campione totale {totals['n']} < {min_total}: per STRATEGY.md il "
                f"primo segnale affidabile arriva a ~100 previsioni chiuse "
                f"(ROI vs EV pienamente leggibile da 500-1000). Nessuna azione "
                f"consigliata per ora.")
    elif not critici:
        note = "nessun mercato sotto la soglia di intervento."

    return {
        "totals": totals,
        "markets": markets,
        "sufficiente": sufficiente,
        "critici": critici,
        "azioni": azioni,
        "note": note,
        "parametri": {"min_total": min_total, "min_per_market": min_per_market,
                      "gap_pp": gap_pp, "prob_gap_pp": prob_gap_pp},
    }


def _azioni_market(m: Dict) -> Dict:
    """Suggerimenti di messa a punto per un mercato critico."""
    azioni = [
        f"ridurre il peso del modello nel blend (market_calib.blend_"
        f"probability) per {m['label']} — EB realizzato sotto l'EV atteso",
        "alzare la soglia EV minima per questo mercato nel value_filter",
    ]
    if m.get("avg_market_edge") is not None and m["avg_market_edge"] < 3.0:
        azioni.append("provare un metodo di devig piu' aggressivo "
                      "(power/shin) o un line shopping piu' restrittivo: "
                      f"edge medio sul mercato +{m['avg_market_edge']:.1f}pp")
    return {
        "mercato": m["mercato"], "label": m["label"], "n": m["n"],
        "roi": m["roi"], "avg_ev": m["avg_ev"], "gap": m["gap"],
        "segnali": m["segnali"], "azioni_da_fare": azioni,
    }


def analyze_db(**kwargs) -> Dict:
    """Diagnosi sul DB reale: legge predictions_summary() da tracker."""
    from tracker import predictions_summary
    return diagnose(predictions_summary(), **kwargs)


def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "n.d."
    return f"{v:+.{digits}f}"


def _table(markets: List[Dict]) -> str:
    head = (f"{'Mercato':<14} {'n':>5} {'V/P':>7} {'hit%':>6} {'ROI%':>7} "
            f"{'EV%':>7} {'gap%':>7} {'probGap':>8} {'edge%':>7}")
    lines = [head, "-" * len(head)]
    for m in markets:
        vp = f"{m['won']}/{m['lost']}"
        edge = _fmt_pct(m["avg_market_edge"])
        mark = " ⚠️" if m["critico"] else ""
        lines.append(
            f"{m['label']:<14} {m['n']:>5} {vp:>7} "
            f"{m['hit_rate']:>6.1f} {_fmt_pct(m['roi'], 2):>7} "
            f"{_fmt_pct(m['avg_ev'], 2):>7} {_fmt_pct(m['gap'], 2):>7} "
            f"{_fmt_pct(m['prob_gap']):>8} {edge:>7}{mark}")
    return "\n".join(lines)


def _report(res: Dict) -> str:
    t = res["totals"]
    out = ["🔬 Diagnosi calibrazione per mercato (ledger previsioni)"]
    out.append(
        f"Totale chiusi: {t['n']} (V {t['won']} / P {t['lost']} / Push "
        f"{t['push']}) | ROI {_fmt_pct(t['roi'], 2)} | EV atteso "
        f"{_fmt_pct(t['avg_ev'], 2)} | gap {_fmt_pct(t['gap'], 2)}")
    if not res["markets"]:
        out.append("ℹ️  Nessuna previsione chiusa nel ledger: niente da analizzare.")
        return "\n".join(out)
    out.append("")
    out.append(_table(res["markets"]))
    out.append("")
    if not res["sufficiente"]:
        out.append(f"ℹ️  {res['note']}")
    elif not res["azioni"]:
        out.append("✅ Nessun mercato sotto la soglia di intervento: "
                   "il ROI e' coerente (o migliore) dell'EV atteso.")
    else:
        out.append(f"❌ {len(res['azioni'])} mercato/i da mettere a punto:")
        for a in res["azioni"]:
            out.append(f"   • {a['label']} (n={a['n']}, ROI {a['roi']:+.2f}% "
                       f"vs EV {a['avg_ev']:+.2f}%, gap {a['gap']:+.2f}pp)")
            for s in a["segnali"]:
                out.append(f"     - {s}")
            for x in a["azioni_da_fare"]:
                out.append(f"     → {x}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Diagnosi calibrazione per mercato (ledger previsioni)")
    ap.add_argument("--json", action="store_true",
                    help="output JSON (per report automatici)")
    ap.add_argument("--min-total", type=int, default=MIN_TOTAL,
                    help=f"soglia campione totale (default {MIN_TOTAL})")
    ap.add_argument("--min-per-market", type=int, default=MIN_PER_MARKET,
                    help=f"soglia chiusi per mercato (default {MIN_PER_MARKET})")
    ap.add_argument("--gap", type=float, default=GAP_PP,
                    help=f"soglia gap ROI-EV in pp (default {GAP_PP:g})")
    ap.add_argument("--prob-gap", type=float, default=PROB_GAP_PP,
                    help=f"soglia overconfidence in pp (default {PROB_GAP_PP:g})")
    args = ap.parse_args(argv)

    res = analyze_db(min_total=args.min_total, min_per_market=args.min_per_market,
                     gap_pp=args.gap, prob_gap_pp=args.prob_gap)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(_report(res))
    return 1 if (res["sufficiente"] and res["azioni"]) else 0


if __name__ == "__main__":
    sys.exit(main())