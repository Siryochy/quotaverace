"""repair_scores.py — Ripara i punteggi invertiti dal bug dell'ordine `scores`.

Storico: fino al 02/09 _update_results assumeva che `scores[0]` di
the-odds-api fosse la squadra di casa, ma l'array NON ha ordine garantito.
Le partite con l'away in prima posizione sono state salvate in
match_results con i gol invertiti, saldando bet/previsioni/cassa con
verdetti specchiati (es. FC Machida Zelvia vs Kawasaki Frontale: vittoria
casa reale salvata 1-2 → bet sul "2" pagata come VINTA +15.00).

Cosa fa (per ogni partita di match_results con risultati the-odds-api):
  1. Riscarica i punteggi veri (fetch_scores, associazione per NOME tramite
     odds_api.match_scores_by_name — la stessa logica del fix).
  2. Confronta con il punteggio salvato: se diverso, corregge la riga.
  3. Ri-salda TUTTI i ledger (bets, predictions, cassa) usando i punteggi
     corretti: reset esito_finale/profit/settled_at delle righe gia'
     saldate su quelle partite, poi settle_* con recompute del profitto.

Dry-run di default: stampa il piano senza toccare il DB. Con --apply
esegue le correzioni (e aggiorna il bankroll implicitamente: bankroll_stats
deriva da SUM(cassa.amount), quindi la rettifica dell'entry cassa con il
profit recompute' corregge anche il totale).

Uso:
  railway ssh --service api -- "python3 repair_scores.py --days-from 7"
  railway ssh --service api -- "python3 repair_scores.py --apply --days-from 7"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from odds_api import SPORTS_MAP, fetch_scores, match_scores_by_name
from tracker import _get_conn, _create_results_table, _norm_team


def _leagues_with_results(conn) -> dict:
    """Leghe presenti in match_results -> sport key (risparmio crediti API:
    si riscaricano SOLO le leghe con risultati salvati, non tutte le 66)."""
    out = {}
    for (lg,) in conn.execute("SELECT DISTINCT league FROM match_results").fetchall():
        sport = SPORTS_MAP.get(lg)
        if sport:
            out[lg] = sport
    return out


def fetch_true_scores(days_from: int, leagues: dict = None) -> dict:
    """Punteggi veri (per match_id) da the-odds-api, associazione per nome.

    `leagues` limita il fetch alle leghe indicate {lega: sport_key}:
    senza, si interroga tutto SPORTS_MAP (molto piu' costoso in crediti).
    """
    leagues = leagues or SPORTS_MAP
    true_scores = {}
    for lg, sport in leagues.items():
        try:
            for m in fetch_scores(sport, days_from=days_from):
                if not m.get("id") or not m.get("completed"):
                    continue
                parsed = match_scores_by_name(m)
                if parsed is None:
                    continue
                true_scores[m["id"]] = (lg, m.get("home_team", ""),
                                        m.get("away_team", ""),
                                        parsed[0], parsed[1])
        except Exception as e:
            print(f"  ! fetch_scores({sport}) fallito: {e}")
    return true_scores


def repair(apply: bool, days_from: int) -> int:
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()

    saved = {r[0]: r for r in c.execute(
        "SELECT match_id, league, home_team, away_team, score_home, score_away "
        "FROM match_results").fetchall()}
    leagues = _leagues_with_results(conn)
    conn.close()
    if not saved:
        print("Nessun risultato salvato: nulla da fare.")
        return 0
    print(f"Risultati salvati: {len(saved)}. Riscarico i punteggi veri "
          f"(solo leghe con risultati, days_from={days_from})…")
    true = fetch_true_scores(days_from, leagues)
    print(f"Punteggi veri recuperati: {len(true)} partite completate.\n")

    wrong = []
    for mid, (lg, home, away, sh, sa) in true.items():
        row = saved.get(mid)
        if row is None:
            continue
        _, lg_saved, home_s, away_s, sh_s, sa_s = row
        if (sh_s, sa_s) != (sh, sa):
            wrong.append((mid, lg_saved, home_s, away_s, sh_s, sa_s, sh, sa))

    if not wrong:
        print("✅ Nessun punteggio invertito: i dati sono coerenti.")
        return 0

    print(f"⚠️ {len(wrong)} partite con punteggio errato:")
    for mid, lg, home, away, sh_s, sa_s, sh, sa in wrong:
        print(f"   {lg}: {home} {sh_s}-{sa_s} → CORRETTO {sh}-{sa} ({mid})")

    if not apply:
        print("\n(DRY-RUN: nessuna modifica. Rilancia con --apply per correggere.)")
        return 1

    # --- FIX match_results ---
    conn = _get_conn()
    c = conn.cursor()
    now = datetime.now().isoformat()
    fixed_ids = []
    for mid, lg, home, away, sh_s, sa_s, sh, sa in wrong:
        res = "1" if sh > sa else ("2" if sh < sa else "X")
        c.execute("UPDATE match_results SET score_home=?, score_away=?, result=?, "
                  "settled_at=? WHERE match_id=?", (sh, sa, res, now, mid))
        fixed_ids.append(mid)
    conn.commit()
    print(f"\nmatch_results corretti: {len(fixed_ids)}")

    # --- RESET ledger saldati sulle partite corrette ---
    qmarks = ",".join("?" for _ in fixed_ids)
    for table in ("bets", "predictions"):
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        if "esito_finale" not in cols:
            continue
        cur = c.execute(f"UPDATE {table} SET esito_finale=NULL, profit=NULL, "
                        f"settled_at=NULL WHERE match_id IN ({qmarks})",
                        fixed_ids)
        print(f"{table}: reset righe saldate: {cur.rowcount}")
    conn.commit()

    # --- RESET cassa saldata sulle partite corrette (match per nomi) ---
    norm_pairs = set()
    for mid in fixed_ids:
        row = c.execute("SELECT home_team, away_team FROM match_results "
                        "WHERE match_id=?", (mid,)).fetchone()
        if row:
            norm_pairs.add((_norm_team(row[0]), _norm_team(row[1])))
    n_cassa = 0
    for cid, partita in c.execute(
            "SELECT id, partita FROM cassa WHERE esito_finale IS NOT NULL").fetchall():
        clean = partita.split(" – ")[-1].strip() if " – " in partita else partita
        if " vs " not in clean:
            continue
        parts = clean.split(" vs ")
        pair = (_norm_team(parts[0].strip()), _norm_team(parts[1].strip()))
        if pair in norm_pairs:
            c.execute("UPDATE cassa SET esito_finale=NULL, profit=NULL, "
                      "settled_at=NULL WHERE id=?", (cid,))
            n_cassa += 1
    print(f"cassa: reset righe saldate: {n_cassa}")
    conn.commit()

    # --- RE-SETTLE con i punteggi corretti (recompute profitti) ---
    from tracker import settle_bets, settle_predictions, settle_cassa
    nb, pb = settle_bets()
    npr, ppr = settle_predictions()
    nc = settle_cassa()
    print(f"\nRe-settlement: bets={nb} (push {pb}), "
          f"predictions={npr} (push {ppr}), cassa={nc}")
    print("✅ Riparazione completata: profitti ricalcolati sui punteggi veri.")
    conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Ripara punteggi invertiti (bug ordine scores)")
    ap.add_argument("--apply", action="store_true",
                    help="applica le correzioni (default: dry-run)")
    ap.add_argument("--days-from", type=int, default=7,
                    help="finestra di riscarica the-odds-api (default 7)")
    args = ap.parse_args(argv)
    return repair(args.apply, args.days_from)


if __name__ == "__main__":
    sys.exit(main())
