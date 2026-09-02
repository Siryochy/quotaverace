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

# Verdetto preferito nel dedup (piu' alto = meglio): il definitivo batte il
# provvisorio; le righe aperte (esito_finale "") valgono 0.
_PREFERRED_OUTCOME = {"won": 3, "push": 2, "lost": 1, "": 0}

# Chiavi normalizzate per riconoscere lo stesso segnale tra i percorsi:
# 1X2 -> "1"/"x"/"2" (i nomi squadra vengono mappati con home/away del
# match: "Inter" e "1" sono lo stesso esito); OU -> "over"/"under" (la
# LINEA e' costante per match: due quotazioni dello stesso match non sono
# righe diverse).
def esito_norm(mercato: str, esito: str, home: str = "", away: str = "") -> str:
    e = (esito or "").strip().lower()
    m = (mercato or "").strip().upper()
    if m == "1X2":
        if e in {"1", "x", "2"}:
            return e
        if e in {"pareggio", "draw"}:
            return "x"
        if home and e == (home or "").strip().lower():
            return "1"
        if away and e == (away or "").strip().lower():
            return "2"
        return e
    if m == "OU":
        if "over" in e:
            return "over"
        if "under" in e:
            return "under"
    return e


def dedupe_training_rows(rows: List[Dict]) -> List[Dict]:
    """Rimuove i duplicati del dataset mantenendo la riga migliore.

    Duplicato = stessa chiave NORMALIZZATA (match_id, mercato, esito_norm):
    "Over 2.5" e "over" sono lo stesso segnale; una previsione del ledger e
    la relativa puntata auto (stesso match, stesso esito) sono la STESSA
    scommessa e vanno contata una volta sola nell'addestramento.

    La riga conservata e' quella col verdetto definitivo (won > push >
    lost > aperta); a pari merito vince la piu' recente (settled_at).
    Stabile e idempotente: dedupe(dedupe(x)) == dedupe(x).
    """
    best: Dict[tuple, tuple] = {}
    order: Dict[tuple, int] = {}
    for i, r in enumerate(rows):
        key = (r.get("match_id") or "",
               (r.get("mercato") or "").strip().upper(),
               esito_norm(r.get("mercato"), r.get("esito"),
                          r.get("home") or "", r.get("away") or ""))
        outcome = str(r.get("esito_finale") or "").strip().lower()
        # Piu' alto = meglio: verdetto definitivo batte aperto, chiusura
        # piu' recente batte piu' vecchia; parita' totale -> prima vista.
        rank = (_PREFERRED_OUTCOME.get(outcome, 0),
                str(r.get("settled_at") or ""))
        if key not in best or rank > best[key][0]:
            best[key] = (rank, r)
            order[key] = i
    return [best[k][1] for k in sorted(order, key=lambda k: order[k])]


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
                          p.esito_finale, p.profit, p.settled_at,
                          m.home_team, m.away_team
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
                          b.esito_finale, b.profit, b.settled_at,
                          m.home_team, m.away_team
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
         esito_finale, profit, settled_at, home_team, away_team) = r
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
            # Serve al dedup normalizzato (nome squadra <-> "1"/"2"); NON
            # viene scritto nel CSV (cols invariato per backward compat).
            "home": home_team or "",
            "away": away_team or "",
        })
    # Dedup NORMALIZZATO (stessa scommessa = una sola riga): predictions e
    # bets possono descrivere lo stesso segnale, e i percorsi diversi
    # rappresentano gli esiti in modo diverso ("Over 2.5" vs "over",
    # "Inter" vs "1"). Un duplicato impara due volte lo stesso evento e
    # sposta la calibrazione (vedi audit hash 36aa024f...).
    return dedupe_training_rows(out)


def export_csv(path) -> int:
    """Scrive il CSV di addestramento completo. Ritorna il numero di righe."""
    rows = build_training_rows()
    # Cintura di sicurezza: il dedup e' gia' in build_training_rows, lo
    # riapplichiamo qui cosi' il CSV e' pulito ANCHE se rows arriva da
    # una fonte esterna (import da CSV storico, backfill, ecc.).
    rows = dedupe_training_rows(rows)
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