"""repair_scores.py — Ripara i punteggi errati in match_results.

Storico: fino al 02/09 _update_results assumeva che `scores[0]` di
the-odds-api fosse la squadra di casa, ma l'array NON ha ordine garantito.
Dal 04/09 la refertazione usa ESCLUSIVAMENTE API-Football
(settlement_apifootball): questo strumento riscarica i punteggi veri dalla
stessa fonte e corregge le righe salvate male + i verdetti dei ledger.

Cosa fa (per ogni partita di match_results):
  1. Riscarica i punteggi veri (API-Football, associazione per NOME).
  2. Confronta con il punteggio salvato: se diverso, corregge la riga.
  3. **Audit dei verdetti sui ledger**: anche se match_results e' gia'
     corretto, le bet/previsioni/cassa possono essere state saldate col
     verdetto SPECCHIATO. Il repair le individua e le ri-salda.
  4. Ri-salda TUTTI i ledger (bets, predictions, cassa) usando i punteggi
     corretti: reset esito_finale/profit/settled_at, poi settle_*.

Dry-run di default: stampa il piano senza toccare il DB. Con --apply
esegue le correzioni (aggiorna anche il bankroll implicitamente).

Uso:
  railway ssh --service api -- "python3 repair_scores.py --days-from 3"
  railway ssh --service api -- "python3 repair_scores.py --apply --days-from 3"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from tracker import _get_conn, _create_results_table, _norm_team
from settlement_apifootball import fetch_true_scores


def _leagues_with_results(conn) -> dict:
    """Leghe presenti in match_results (per il log di repair). Dal 04/09 il
    fetch dei punteggi veri passa da settlement_apifootball (API-Football),
    che scarica SOLO le leghe con risultati salvati."""
    out = {}
    for (lg,) in conn.execute("SELECT DISTINCT league FROM match_results").fetchall():
        if lg:
            out[lg] = lg
    return out


def fetch_true_scores_apifootball(days_from: int) -> dict:
    """Punteggi veri (per match_id the-odds-api) via API-Football.

    Delega a settlement_apifootball.fetch_true_scores: scarica le fixtures
    finite delle leghe presenti in match_results, le abbina per nome ai
    match_id the-odds-api e ritorna {match_id: (lg, home, away, sh, sa)}.
    Il parametro days_from viene passato come finestra di ricerca.
    """
    return fetch_true_scores(days_back=days_from)


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


# Prefissi club comuni ignorati nel match per nome (settle_cassa usa _norm_team
# che elimina solo fc/cf: 'CA Osasuna' non agganciava 'Osasuna' → la cassa
# poteva finire saldata contro una partita VECCHIA della stessa coppia).
_CLUB_PREFIXES = ("ca ", "ac ", "as ", "cd ", "fc ", "cf ", "de ",
                   "ss ", "sc ", "us ", "ud ", "sd ", "at ", "sv ")


def _loose_team(name: str) -> str:
    """Normalizzazione TOLERANTE per il match cassa: _norm_team + rimozione
    dei prefissi club comuni (es. 'CA Osasuna' → 'osasuna')."""
    t = _norm_team(name)
    for _ in range(3):
        changed = False
        for p in _CLUB_PREFIXES:
            if t.startswith(p):
                t = t[len(p):].strip()
                changed = True
        if not changed:
            break
    return t or _norm_team(name)


def _loose_pair(home: str, away: str) -> tuple:
    return (_loose_team(home), _loose_team(away))


def _partita_pair(partita: str) -> tuple:
    """Coppia loose (home, away) da una stringa partita cassa
    ('Serie A – Osasuna vs Getafe' → ('osasuna', 'getafe'))."""
    clean = str(partita).split(" – ")[-1].strip() if " – " in str(partita) else str(partita)
    if " vs " not in clean:
        return None
    parts = clean.split(" vs ")
    return _loose_pair(parts[0].strip(), parts[1].strip())


def _find_verdict_mismatches(c, results: dict) -> list:
    """Righe ledger saldate il cui verdetto NON corrisponde al punteggio vero.

    `results`: {match_id: (home, away, sh, sa)} — i punteggi VERI.
    Per la cassa l'aggancio e' per coppia di squadre normalizzate
    (la tabella non ha match_id): si usa la stessa normalizzazione di
    settle_cassa (ultimo " vs " della partita).

    Ritorna una lista di righe (tipo, id, match_id|partita, esito, atteso).
    """
    mismatches = []
    # Mappa coppia-squadre LOOSE -> (sh, sa) per la cassa (solo partite
    # CORRENTI dentro `results`, mai le vecchie della stessa coppia)
    pair_map = {}
    for mid, (lg, home, away, sh, sa) in results.items():
        pair_map[_loose_pair(home, away)] = (sh, sa)

    for row in _settled_ledger_rows(c):
        kind, rid, ref, mercato, esito, price, stored = row
        if kind == "cassa":
            pair = _partita_pair(ref)
            if pair is None:
                continue
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
    true = fetch_true_scores_apifootball(days_from)
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

    # --- Mappa coppia-squadre LOOSE → (sh, sa) dalle partite CORRENTI ---
    # (solo dentro `true`: mai le vecchie righe della stessa coppia)
    pair_map = {_loose_pair(home, away): (sh, sa)
                for mid, (lg, home, away, sh, sa) in true.items()}

    # --- RESET cassa saldata sulle partite corrette (match per nomi LOOSE) ---
    # Es. 'CA Osasuna' vs la riga 2025 'Osasuna vs Getafe': la cassa era
    # stata saldata sulla partita VECCHIA (1-2 → Over vinto) invece che su
    # quella corrente (1-0 → Under). Solo le coppie delle partite in gioco.
    current_pairs = set()
    for mid, (lg, home, away, sh, sa) in true.items():
        involved = mid in reset_ids or any(
            (m[0] != "cassa" and m[2] == mid) or
            (m[0] == "cassa" and _loose_pair(home, away) ==
             _partita_pair(m[2]))
            for m in mismatches)
        if involved:
            current_pairs.add(_loose_pair(home, away))
    n_cassa = 0
    for cid, partita in c.execute(
            "SELECT id, partita FROM cassa WHERE esito_finale IS NOT NULL").fetchall():
        pair = _partita_pair(partita)
        if pair is None:
            continue
        if pair in current_pairs:
            c.execute("UPDATE cassa SET esito_finale=NULL, profit=NULL, "
                      "settled_at=NULL WHERE id=?", (cid,))
            n_cassa += 1
    print(f"cassa: reset righe saldate: {n_cassa}")
    conn.commit()

    # --- RE-SETTLE con i punteggi corretti (recompute profitti) ---
    # Per la cassa si ri-salda DIRETTAMENTE contro i punteggi veri delle
    # partite correnti: settle_cassa() usa _norm_team stretto e riaggancerebbe
    # la riga VECCHIA della stessa coppia (es. Osasuna-Getafe 2025 1-2 → Over
    # 2.5 vinto quando il match corrente è finito 1-0 → Under).
    from tracker import settle_bets, settle_predictions, _esito_won
    nb, pb = settle_bets()
    npr, ppr = settle_predictions()
    nc = 0
    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    for cid, partita, esito, quota, importo in c.execute(
            "SELECT id, partita, esito, quota, importo FROM cassa "
            "WHERE esito_finale IS NULL").fetchall():
        pair = _partita_pair(partita)
        if pair is None:
            continue
        match = pair_map.get(pair)
        if match is None:
            continue
        sh, sa = match
        won = _esito_won(esito, sh, sa)
        if won is None:
            continue
        profit = round((quota - 1) * importo, 2) if won else round(-importo, 2)
        c.execute("UPDATE cassa SET esito_finale=?, profit=?, settled_at=? "
                  "WHERE id=?", ("won" if won else "lost", profit, now, cid))
        nc += 1
    conn.commit(); conn.close()
    print(f"\nRe-settlement: bets={nb} (push {pb}), "
          f"predictions={npr} (push {ppr}), cassa={nc}")
    print("✅ Riparazione completata: profitti ricalcolati sui punteggi veri.")
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