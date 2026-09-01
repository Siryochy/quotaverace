"""ml_dataset.py — Dataset di addestramento per la machine learning.

Parte dal LEDGER (predictions + bets) che il bot registra per OGNI segnale
proposto e per ogni puntata automatica, e lo arricchisce con le feature del
modello (gol attesi, probabilità Poisson, probabilità di mercato, edge) e il
RISULTATO REALE (label win/loss). E' la materia prima per addestrare
l'orchestra: il mini-batch di ogni giornata completa il training set.

CLI:
  venv/bin/python ml_dataset.py                 # scrive data/training_dataset.csv
  venv/bin/python ml_dataset.py /path/out.csv   # output custom
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import List, Dict

from tracker import _get_conn


def build_training_rows(limit: int | None = None, source: str = "all") -> List[Dict]:
    """Righe di addestramento chiuse (esito_finale noto), piu' recenti prima.

    Feature per riga:
      - contesto: match_id, league, mercato, esito
      - modello: lam_h, lam_a, prob_1, prob_X, prob_2, prob_over (Poisson)
      - mercato: quota, prob (blend), ev, market_prob, market_edge, status
      - outcome (label): esito_finale (won/lost/push), profit, label_ml (1/0)

    source: "predictions" (ledger), "bets" (puntate auto) o "all" (default).
    """
    conn = _get_conn()
    c = conn.cursor()
    try:
        rows = []
        if source in ("predictions", "all"):
            rows += c.execute(
                """SELECT p.match_id, m.league, p.mercato, p.esito,
                          a.lam_h, a.lam_a, a.prob_1, a.prob_X, a.prob_2, a.prob_over,
                          p.quota, p.prob, p.ev, p.market_prob, p.market_edge, p.status,
                          p.esito_finale, p.profit, p.settled_at
                   FROM predictions p
                   LEFT JOIN matches m ON p.match_id = m.id
                   LEFT JOIN match_analysis a ON p.match_id = a.match_id
                   WHERE p.esito_finale IS NOT NULL
                   ORDER BY p.settled_at DESC
                   LIMIT ?""", (limit or 100000,)).fetchall()
        if source in ("bets", "all"):
            rows += c.execute(
                """SELECT b.match_id, m.league, b.mercato, b.esito,
                          a.lam_h, a.lam_a, a.prob_1, a.prob_X, a.prob_2, a.prob_over,
                          b.price, NULL, NULL, NULL, NULL, b.status,
                          b.esito_finale, b.profit, b.settled_at
                   FROM bets b
                   LEFT JOIN matches m ON b.match_id = m.id
                   LEFT JOIN match_analysis a ON b.match_id = a.match_id
                   WHERE b.esito_finale IS NOT NULL
                   ORDER BY b.settled_at DESC
                   LIMIT ?""", (limit or 100000,)).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        (match_id, league, mercato, esito, lam_h, lam_a, p1, px, p2, p_over,
         quota, prob, ev, market_prob, market_edge, status,
         esito_finale, profit, settled_at) = r
        out.append({
            "match_id": match_id or "",
            "league": league or "",
            "mercato": mercato or "",
            "esito": esito or "",
            "lam_h": lam_h, "lam_a": lam_a,
            "prob_1": p1, "prob_X": px, "prob_2": p2, "prob_over": p_over,
            "quota": quota, "prob": prob, "ev": ev,
            "market_prob": market_prob, "market_edge": market_edge,
            "status": status or "",
            "esito_finale": esito_finale or "",
            "profit": profit,
            "label_ml": 1 if esito_finale == "won" else 0,
            "settled_at": settled_at or "",
        })
    return out


def export_csv(path) -> int:
    """Scrive il CSV di addestramento completo. Ritorna il numero di righe."""
    rows = build_training_rows()
    cols = ["match_id", "league", "mercato", "esito", "lam_h", "lam_a",
            "prob_1", "prob_X", "prob_2", "prob_over", "quota", "prob", "ev",
            "market_prob", "market_edge", "status", "esito_finale", "profit",
            "label_ml", "settled_at"]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k) for k in cols})
    return len(rows)


if __name__ == "__main__":
    from config import DATA_DIR
    out = sys.argv[1] if len(sys.argv) > 1 else str(DATA_DIR / "training_dataset.csv")
    n = export_csv(out)
    print(f"✅ Dataset di addestramento: {n} righe chiuse → {out}")
    if n:
        won = sum(1 for r in build_training_rows() if r["label_ml"] == 1)
        print(f"   label: {won} vinte / {n - won} perse-push")