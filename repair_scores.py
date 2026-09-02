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
  3. **Audit dei verdetti sui ledger**: anche se match_results e' gia'
     corretto (es. risalvato dal watchdog DOPO il fix, come per Machida),
     le bet/previsioni/cassa possono essere state saldate col verdetto
     SPECCHIATO (puntata sul "2" marcata won per una vittoria casa). Il
     repair le individua confrontando l'esito saldato con quello atteso
     dal punteggio vero e le ri-salda.
  4. Ri-salda TUTTI i ledger (bets, predictions, cassa) usando i punteggi
     corretti: reset esito_finale/profit/settled_at delle righe gia'
     saldate su quelle partite, poi settle_* con recompute del profitto.

Dry-run di default: stampa il piano senza toccare il DB. Con --apply
esegue le correzioni (e aggiorna il bankroll implicitamente: bankroll_stats
deriva da SUM(cassa.importo), quindi la rettifica dell'entry cassa con il
profit recompute' corregge anche il totale).

NOTA: the-odds-api /scores accetta daysFrom massimo 3 giorni (422 oltre):
il default e' gia' 3, non superarlo a mano.

Uso:
  railway ssh --service api -- "python3 repair_scores.py --days-from 3"
  railway ssh --service api -- "python3 repair_scores.py --apply --days-from 3"
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


def _settled_ledger_rows(c) -> list:
    """Righe gia' saldate di bets/predictions/cassa con i dati per l'audit."""
    rows = []
    for bid, match_id, mercato, esito, price, stake, mode, out, profit in c.execute(
            "SELECT id, match_id, mercato, esito, price, stake, mode, "
            "esito_finale, profit FROM bets WHERE esito_finale IS NOT NULL"
    ).fetchall():
        rows.append(("bet", bid, match_id, mercato, esito, price, out))
    for pid, match_id, mercato, esito, quota, out in c.execute(
            "SELECT id, match_id, mercato, esito, quota, esito_finale "
            "FROM predictions WHERE esito_finale IS NOT NULL"
    ).fetchall():
        rows.append(("pred", pid, match_id, mercato, esito, quota, out))
    # cassa: si aggancia per coppia di squadre normalizzate (non ha match_id)
    for cid, partita, esito, quota, out in c.execute(
            "SELECT id, partita, esito, quota, esito_finale "
            "FROM cassa WHERE esito_finale IS NOT NULL"
    ).fetchall():
        rows.append(("cassa", cid, partita, None, esito, quota, out))
    return rows


def _expected_outcome(mercato, esito, price, sh, sa, home, away):
    """Outcome atteso (won/lost/push) per un ledger row, o None se non risolvibile."""
    from tracker import _prediction_outcome
    outcome, _ = _prediction_outcome(mercato, esito, price, sh, sa, home, away)
    return outcome


def _find_verdict_mismatches(c, results: dict) -> list:
    """Righe ledger saldate il cui verdetto NON corrisponde al punteggio vero.

    `results`: {match_id: (home, away, sh, sa)} — i punteggi VERI.
    Per la cassa l'aggancio e' per coppia di squadre normalizzate
    (la tabella non ha match_id): si usa la stessa normalizzazione di
    settle_cassa (ultimo " vs " della partita).

    Ritorna una lista di righe (tipo, id, match_id|partita, esito, atteso).
    """
    from tracker import _norm_team as nt
    mismatches = []
    # Mappa coppia-squadre normalizzata -> (sh, sa) per la cassa
    pair_map = {}
    for mid, (lg, home, away, sh, sa) in results.items():
        pair_map.setdefault((nt(home), nt(away)), (sh, sa))

    for row in _settled_ledger_rows(c):
        kind, rid, ref, mercato, esito, price, stored = row
        if kind == "cassa":
            clean = str(ref).split(" – ")[-1].strip() if " – " in str(ref) else str(ref)
            if " vs " not in clean:
                continue
            parts = clean.split(" vs ")
            pair = (nt(parts[0].strip()), nt(parts[1].strip()))
            match = pair_map.get(pair)
            if match is None:
                continue
            sh, sa = match
            from tracker import _esito_won
            won = _esito_won(esito, sh, sa)
            expected = "won" if won else ("lost" if won is not None else None)
        else:
            match = results.get(ref)
            if match is None:
                continue
            lg, home, away, sh, sa = match
            expected = _expected_outcome(mercato, esito, price, sh, sa,
                                         home, away)
        if expected is None:
            continue
        if expected != stored:
            mismatches.append((kind, rid, ref, esito, expected, stored))
    return mismatches


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
    else:
        print(f"⚠️ {len(wrong)} partite con punteggio errato:")
        for mid, lg, home, away, sh_s, sa_s, sh, sa in wrong:
            print(f"   {lg}: {home} {sh_s}-{sa_s} → CORRETTO {sh}-{sa} ({mid})")

    # --- AUDIT verdetti sui ledger (anche con match_results gia' corretto) ---
    conn = _get_conn(); c = conn.cursor()
    mismatches = _find_verdict_mismatches(c, true)
    conn.close()
    if mismatches:
        print(f"⚠️ {len(mismatches)} righe ledger saldate col verdetto "
              f"SBAGLIATO (da ri-saldare):")
        for kind, rid, ref, esito, exp, stored in mismatches[:15]:
            short = str(ref)[:44]
            print(f"   {kind:5s} #{rid} {short}: {esito} "
                  f"(saldata '{stored}' → atteso '{exp}')")
        if len(mismatches) > 15:
            print(f"   … e altre {len(mismatches) - 15} righe.")

    if not apply:
        print("\n(DRY-RUN: nessuna modifica. Rilancia con --apply per correggere.)")
        return 1 if (wrong or mismatches) else 0

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

    # --- RESET ledger saldati sulle partite corrette + su quelli con verdetto errato ---
    # (match_results gia' corretto ma verdetto specchiato: serve il reset per
    #  far ripartire il settlement con l'esito giusto — caso Machida)
    reset_ids = set(fixed_ids)
    for kind, rid, ref, esito, exp, stored in mismatches:
        if kind == "cassa":
            continue  # la cassa si aggancia per coppia squadre sotto
        reset_ids.add(ref)
    qmarks = ",".join("?" for _ in reset_ids)
    for table in ("bets", "predictions"):
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        if "esito_finale" not in cols:
            continue
        cur = c.execute(f"UPDATE {table} SET esito_finale=NULL, profit=NULL, "
                        f"settled_at=NULL WHERE match_id IN ({qmarks})",
                        list(reset_ids))
        print(f"{table}: reset righe saldate: {cur.rowcount}")
    conn.commit()

    # --- RESET cassa saldata sulle partite corrette (match per nomi) ---
    norm_pairs = set()
    for mid in reset_ids:
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
    ap.add_argument("--days-from", type=int, default=3,
                    help="finestra di riscarica the-odds-api (max 3, default 3)")
    args = ap.parse_args(argv)
    days = max(1, min(args.days_from, 3))  # the-odds-api: 422 oltre 3
    return repair(args.apply, days)


if __name__ == "__main__":
    sys.exit(main())