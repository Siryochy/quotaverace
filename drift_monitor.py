"""drift_monitor.py — Rilevamento Concept Drift del modello.

Controllo periodico della calibrazione del modello: confronta il Brier
score MOBILE sulle ultime previsioni chiuse con la baseline storica.
Se le probabilità predette stanno diventando meno affidabili (drift),
segnala il retraining dell'ensemble ML.

Metriche:
  - Brier score rolling: media di (prob - esito)^2 sulle ultime N
    previsioni chiuse (esito = 1 se vinta, 0 se persa; push escluse).
  - LogLoss rolling: -log(prob assegnata all'esito vero).
  - Baseline: stesse metriche su TUTTO lo storico precedente la finestra.

Soglie (DRIFT_RATIO / DRIFT_ABS): l'alert scatta se il Brier rolling
degrada oltre la baseline del RATIO (es. +30%) o di ABS in valore assoluto.
Serve un minimo di previsioni chiuse (MIN_ROLLING) per dare un segnale
statisticamente utile: sotto quella soglia lo stato e' "insufficient".

CLI:
  venv/bin/python drift_monitor.py            # report testuale
  venv/bin/python drift_monitor.py --json     # output JSON
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# --- Config ---
MIN_ROLLING = 15        # minimo previsioni chiuse per valutare il drift
ROLLING_WINDOW = 30     # finestra mobile (ultime N previsioni chiuse)
DRIFT_RATIO = 1.30      # alert se Brier rolling > 1.30x baseline
DRIFT_ABS = 0.03        # ... oppure +0.03 in valore assoluto


def _esito_bin(esito_finale: Optional[str]):
    """1 se vinta, 0 se persa, None se push/ignota."""
    e = str(esito_finale or "").lower().strip()
    if e in ("won", "vinta", "win", "1"):
        return 1.0
    if e in ("lost", "persa", "loss", "lose", "0"):
        return 0.0
    return None


def load_settled_predictions(limit: int = 500) -> List[Dict]:
    """Previsioni chiuse con prob e esito finale (dalla piu' recente)."""
    from tracker import _get_conn
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT prob, esito_finale, created_at, mercato, esito "
            "FROM predictions "
            "WHERE esito_finale IS NOT NULL AND prob IS NOT NULL "
            "ORDER BY COALESCE(settled_at, created_at) DESC LIMIT ?",
            (limit,)).fetchall()
    finally:
        conn.close()
    out = []
    for prob, ef, created, mercato, esito in rows:
        y = _esito_bin(ef)
        if y is None or prob is None or float(prob) <= 0.0:
            continue
        p = min(max(float(prob), 1e-6), 1.0 - 1e-6)
        out.append({"prob": p, "y": y, "created_at": created,
                    "mercato": mercato, "esito": esito})
    return out


def _brier(preds: List[Dict]) -> Optional[float]:
    if not preds:
        return None
    return sum((p["prob"] - p["y"]) ** 2 for p in preds) / len(preds)


def _logloss(preds: List[Dict]) -> Optional[float]:
    if not preds:
        return None
    # Probabilita' assegnata all'esito vero: p se vinta, 1-p se persa.
    return -sum(math.log(p["prob"] if p["y"] == 1 else 1.0 - p["prob"])
                for p in preds) / len(preds)


def check_drift(window: int = ROLLING_WINDOW,
                min_rolling: int = MIN_ROLLING) -> Dict:
    """Confronta il Brier/LogLoss rolling con la baseline storica.

    Returns:
        Dict: status ('ok' | 'drift' | 'insufficient'), metriche rolling
        e baseline, soglie, e raccomandazione retraining.
    """
    preds = load_settled_predictions()
    if len(preds) < min_rolling:
        return {
            "status": "insufficient",
            "n": len(preds),
            "min_required": min_rolling,
            "message": (f"Servono almeno {min_rolling} previsioni chiuse "
                        f"per il monitoraggio drift (ora {len(preds)})."),
        }

    rolling = preds[:window]
    baseline = preds[window:]
    rb = _brier(rolling)
    bb = _brier(baseline) if baseline else None
    rl = _logloss(rolling)
    bl = _logloss(baseline) if baseline else None

    degraded = False
    reasons = []
    if bb is not None and rb is not None:
        if rb > bb * DRIFT_RATIO:
            degraded = True
            reasons.append(f"Brier rolling {rb:.4f} > "
                           f"{DRIFT_RATIO:.2f}x baseline {bb:.4f}")
        elif rb - (bb or 0.0) >= DRIFT_ABS:
            degraded = True
            reasons.append(f"Brier rolling {rb:.4f} oltre baseline "
                           f"{bb:.4f} di >= {DRIFT_ABS:.2f}")

    status = "drift" if degraded else "ok"
    return {
        "status": status,
        "n": len(preds),
        "window": len(rolling),
        "baseline_n": len(baseline),
        "brier_rolling": round(rb, 4) if rb is not None else None,
        "brier_baseline": round(bb, 4) if bb is not None else None,
        "logloss_rolling": round(rl, 4) if rl is not None else None,
        "logloss_baseline": round(bl, 4) if bl is not None else None,
        "drift_ratio": round(DRIFT_RATIO, 2),
        "drift_abs": DRIFT_ABS,
        "reasons": reasons,
        "recommendation": ("🔄 RETRAINING consigliato: il modello sta "
                           "perdendo calibrazione sulle ultime previsioni."
                           if degraded else
                           "Modello calibrato: nessun drift rilevato."),
    }


def format_drift_report(d: Dict) -> List[str]:
    """Righe Telegram per il report giornaliero (senza titolo di sezione)."""
    if d["status"] == "insufficient":
        return [f"🧠 *Drift modello:* {d['message']}"]
    lines = [f"🧠 *Drift modello:* {d['status'].upper()} "
             f"({d['n']} previsioni chiuse)"]
    if d["brier_rolling"] is not None:
        lines.append(
            f"   Brier rolling: **{d['brier_rolling']:.4f}** "
            f"(baseline {d['brier_baseline'] or '—'})")
    if d["logloss_rolling"] is not None:
        lines.append(
            f"   LogLoss rolling: {d['logloss_rolling']:.4f} "
            f"(baseline {d['logloss_baseline'] or '—'})")
    lines.append(f"   {d['recommendation']}")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Monitor concept drift del modello")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--window", type=int, default=ROLLING_WINDOW)
    args = ap.parse_args(argv)

    d = check_drift(window=args.window)
    if args.json:
        print(json.dumps(d, indent=2))
    else:
        print("\n".join(format_drift_report(d)))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())