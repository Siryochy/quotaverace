"""
Backtest: misura la calibrazione dei segnali.

Confronta l'EV medio ATTESO dal modello (probabilita Poisson/Dixon-Coles contro
la quota) con il ROI REALIZZATO dagli esiti reali. La differenza tra i due e'
la metrica decisiva:

    - Se EV atteso > 0 ma ROI realizzato << EV su un campione ampio
      => il modello e' mal calibrato: le probabilita sono ottimiste e NON c'e'
      edge reale.
    - Se ROI realizzato converge verso l'EV atteso => il modello e' ben
      calibrato e l'edge previsto si sta muovendo in profitto.

Regola di buon senso: servono centinaia di scommesse per trarre conclusioni.
Un campione piccolo (n < ~50) / ROI vicino a 0 non dimostra nulla.
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Soglia minima di scommesse chiuse prima di poter affermare qualcosa
MIN_SAMPLE = 100
# Campione gia' utile per un segnale debole (da leggere con prudenza)
MIN_WARN_SAMPLE = 30

DISCLAIMER = (
    "\n\n────────────────────\n"
    "🎲 *Gioca responsabilmente*\n"
    "Le scommesse sono un gioco d'azzardo. Non puntare più di quanto puoi "
    "permetterti di perdere. Se hai bisogno di aiuto, visita il portale ADM: "
    "[www.adm.gov.it](https://www.adm.gov.it)"
)


def _load_from_db() -> List[Dict]:
    """Legge i segnali chiusi dal DB (match_results + match_analysis)."""
    from tracker import _get_conn, _create_results_table
    conn = _get_conn()
    c = conn.cursor()
    try:
        _create_results_table(conn)
        c.execute('''SELECT r.home_team, r.away_team, r.score_home, r.score_away,
                            a.best_esito, a.best_quota, a.best_ev, a.status, a.market_edge
                     FROM match_results r
                     JOIN match_analysis a ON a.match_id = r.match_id''')
        rows = c.fetchall()
    finally:
        conn.close()

    bets: List[Dict] = []
    for home, away, sh, sa, esito, quota, ev, status, market_edge in rows:
        if status not in ("value", "strong_value") or not esito or not quota or quota <= 1.0:
            continue
        el = esito.lower().strip()
        if "over" in el:
            won = (sh + sa) >= 3
        elif "under" in el:
            won = (sh + sa) <= 2
        elif el == (home or "").lower().strip():
            won = sh > sa
        elif el == (away or "").lower().strip():
            won = sa > sh
        else:
            won = sh == sa
        bets.append({
            "evento": f"{home} vs {away}",
            "esito": esito,
            "quota": quota,
            "ev": ev or 0.0,
            "won": won,
            "market_edge": market_edge,
        })
    return bets


def _load_from_json(path: str) -> List[Dict]:
    """Legge un dataset storico in formato JSON (come data/segnali.json)."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    bets: List[Dict] = []
    for item in raw:
        won_raw = item.get("esito_finale") or item.get("won")
        if won_raw in (None, "", "pending"):
            continue
        won = str(won_raw).strip().lower() == "won"
        quota = float(item.get("quota") or 1.0)
        if quota <= 1.0:
            continue
        bets.append({
            "evento": item.get("evento", ""),
            "esito": item.get("esito", ""),
            "quota": quota,
            "ev": float(item.get("ev") or 0.0),
            "won": won,
        })
    return bets


def backtest(bets: List[Dict]) -> Dict:
    """Valuta un elenco di segnali chiusi.

    Ritorna metriche aggregate:
      - n, won, lost, hit_rate
      - roi:          P/L medio per scommessa (flat 1 unita), in %
      - roi_edge:     EV atteso medio dal modello, in %
      - gap:          roi - roi_edge (se molto negativo => modello ottimista)
      - net_units:    P/L cumulato in unita da 1
      - sufficiente:  campione abbastanza ampio per concludere qualcosa
    """
    from value_filter import compute_ev

    records = []
    for b in bets:
        # EV atteso dal modello (se assente, da prob implicita * quota)
        ev = b.get("ev")
        if ev is None:
            ev = compute_ev(b.get("prob", 0.5), b["quota"])
        pnl = (b["quota"] - 1) if b["won"] else -1
        records.append({"quota": b["quota"], "won": b["won"], "ev": ev, "pnl": pnl})

    n = len(records)
    won = sum(1 for r in records if r["won"])
    net = sum(r["pnl"] for r in records)
    ev_total = sum(r["ev"] for r in records)

    # Split per edge sul mercato (ricerca 2026): i segnali che BATTONO la
    # closing line (market_edge >= 3pp) dovrebbero performare meglio di
    # quelli che non la battono. E' il test decisivo della calibrazione.
    beats = [r for r in records if r.get("market_edge") is not None
             and r["market_edge"] >= 0.03]
    no_beats = [r for r in records if r.get("market_edge") is not None
                and r["market_edge"] < 0.03]

    def _roi(group):
        if not group:
            return None
        g = sum(r["pnl"] for r in group)
        return g / len(group) * 100.0, len(group)

    beats_roi = _roi(beats)
    no_beats_roi = _roi(no_beats)

    return {
        "n": n,
        "won": won,
        "lost": n - won,
        "hit_rate": (won / n * 100) if n else 0.0,
        "roi": (net / n * 100) if n else 0.0,
        "roi_edge": (ev_total / n * 100) if n else 0.0,
        "gap": ((net - ev_total) / n * 100) if n else 0.0,
        "net_units": net,
        "sufficiente": n >= MIN_SAMPLE,
        "warn": n >= MIN_WARN_SAMPLE,
        "beats_market": beats_roi,
        "no_beats_market": no_beats_roi,
    }


def format_backtest(stats: Dict) -> str:
    n = stats["n"]
    if n == 0:
        return "📊 *BACKTEST*" + DISCLAIMER
    mkt_lines = ""
    if stats.get("beats_market") or stats.get("no_beats_market"):
        b = stats["beats_market"]
        nb = stats["no_beats_market"]
        b_txt = f"{b[0]:+.2f}% ({b[1]})" if b else "n.d."
        nb_txt = f"{nb[0]:+.2f}% ({nb[1]})" if nb else "n.d."
        mkt_lines = (
            f"\n🎯 *Edge vs mercato (closing line):*\n"
            f"   ✅ Batte il mercato: ROI {b_txt}\n"
            f"   ⚠️ Non batte il mercato: ROI {nb_txt}\n\n"
        )

    body = (
        "📊 *BACKTEST SEGNALI* — Verifica di calibrazione\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 Confronta l'*EV atteso* del modello con il *ROI realizzato* "
        "dagli esiti reali (flat 1 unità).\n\n"
        f"🎯 Scommesse chiuse: {n}\n"
        f"✅ Vinte: {stats['won']} | ❌ Perse: {stats['lost']}\n"
        f"📈 Hit rate: {stats['hit_rate']:.1f}%\n\n"
        f"⚖ EV atteso medio (modello): {stats['roi_edge']:+.2f}%\n"
        f"💰 ROI realizzato: {stats['roi']:+.2f}%\n"
        f"🔬 Gap calibrazione (ROI−EV): {stats['gap']:+.2f}%\n"
        f"📦 P/L cumulato: {stats['net_units']:+.2f} unità\n\n"
        + mkt_lines
    )

    if not stats["sufficiente"] and not stats["warn"]:
        verdict = (
            "⚠️ *Campione troppo piccolo.*\n"
            f"Servono ≥{MIN_SAMPLE} scommesse chiuse per trarre conclusioni; "
            "oggi hai un campione debole. Non è statisticamente significativo."
        )
    elif stats["roi"] >= 0 and stats["roi"] >= stats["roi_edge"] - 3.0:
        verdict = (
            "🟢 *MODELLO CALIBRATO.* Il ROI realizzato è coerente (o migliore) "
            "rispetto all'EV atteso: l'edge previsto si sta muovendo in profitto."
        )
    elif stats["roi"] < 0:
        verdict = (
            "🔴 *EDGE NON CONFERMATO.* ROI negativo nonostante EV atteso "
            "positivo. Possibili cause: modello ottimista, varianza, o "
            "deterioramento della strategia. Non puntare su questi segnali."
        )
    else:
        verdict = (
            "🟡 *SITUAZIONE AMBIGUA.* ROI positivo ma sotto l'EV atteso. "
            "Servono più scommesse per confermare l'edge."
        )
    return body + "━━━━━━━━━━━━━━━━━━━━━━\n" + verdict + DISCLAIMER


def _load(source: Optional[str]) -> List[Dict]:
    """Carica i segnali dalla fonte scelta (None = DB)."""
    if source:
        path = os.path.join(os.path.dirname(__file__), source)
        return _load_from_json(path)
    return _load_from_db()


def run_backtest(source: Optional[str] = None) -> str:
    """Endpoint CLI/Telegram: torna il report formattato del backtest."""
    bets = _load(source)
    return format_backtest(backtest(bets))


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else None
    print(run_backtest(src))