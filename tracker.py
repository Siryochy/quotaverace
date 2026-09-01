"""Tracker SQLite per segnali, calendario e analisi"""
import math
import sqlite3
import os
from datetime import datetime
from pathlib import Path

from config import DATA_DIR

DB_PATH = DATA_DIR / "quotaverace.db"

class Signal:
    def __init__(self, id, chat_id, evento, esito, quota, probabilita, ev, timestamp, esito_finale, profit):
        self.id = id; self.chat_id = chat_id; self.evento = evento; self.esito = esito
        self.quota = quota; self.probabilita = probabilita; self.ev = ev
        self.timestamp = timestamp; self.esito_finale = esito_finale; self.profit = profit

def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER, evento TEXT, esito TEXT, quota REAL,
        probabilita REAL, ev REAL, timestamp TEXT, esito_finale TEXT, profit REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY)''')
    # Migrazione free/premium: colonne tier e scadenza abbonamento.
    sub_cols = [r[1] for r in c.execute("PRAGMA table_info(subscribers)")]
    if "tier" not in sub_cols:
        c.execute("ALTER TABLE subscribers ADD COLUMN tier TEXT DEFAULT 'free'")
    if "premium_until" not in sub_cols:
        c.execute("ALTER TABLE subscribers ADD COLUMN premium_until TEXT")
    c.execute('''CREATE TABLE IF NOT EXISTS matches (
        id TEXT PRIMARY KEY, league TEXT, home_team TEXT, away_team TEXT,
        commence_time TEXT, status TEXT, last_updated TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS match_analysis (
        id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT,
        lam_h REAL, lam_a REAL, prob_1 REAL, prob_X REAL, prob_2 REAL,
        prob_over REAL, best_ev REAL, best_esito TEXT, best_quota REAL,
        best_bookmaker TEXT, status TEXT, timestamp TEXT,
        market_prob REAL, market_edge REAL,
        FOREIGN KEY (match_id) REFERENCES matches(id))''')
    # Migrazione per DB esistenti: aggiunge le colonne mercato se mancano.
    cols = [r[1] for r in c.execute("PRAGMA table_info(match_analysis)")]
    if "market_prob" not in cols:
        c.execute("ALTER TABLE match_analysis ADD COLUMN market_prob REAL")
    if "market_edge" not in cols:
        c.execute("ALTER TABLE match_analysis ADD COLUMN market_edge REAL")
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        match_id TEXT, date TEXT, PRIMARY KEY (match_id, date))''')
    c.execute('''CREATE TABLE IF NOT EXISTS clv_history (
        match_id TEXT, esito TEXT,
        signal_quota REAL, closing_quota REAL, updated_at TEXT,
        PRIMARY KEY (match_id, esito))''')
    c.execute('''CREATE TABLE IF NOT EXISTS cassa (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data TEXT, partita TEXT, esito TEXT, quota REAL,
        importo REAL, ev REAL, timestamp TEXT)''')
    # Migrazione: colonne di saldo (risultato reale + profitto realizzato).
    cassa_cols = [r[1] for r in c.execute("PRAGMA table_info(cassa)")]
    if "esito_finale" not in cassa_cols:
        c.execute("ALTER TABLE cassa ADD COLUMN esito_finale TEXT")
    if "profit" not in cassa_cols:
        c.execute("ALTER TABLE cassa ADD COLUMN profit REAL")
    if "settled_at" not in cassa_cols:
        c.execute("ALTER TABLE cassa ADD COLUMN settled_at TEXT")
    # Ledger previsioni: TUTTI i segnali proposti dal motore, con mercato,
    # saldati a fine partita (esito_finale) per calibrare il modello.
    c.execute('''CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT, mercato TEXT, esito TEXT,
        quota REAL, prob REAL, ev REAL,
        market_prob REAL, market_edge REAL,
        status TEXT, esito_finale TEXT, profit REAL,
        created_at TEXT, settled_at TEXT,
        UNIQUE(match_id, mercato, esito))''')
    # Puntate automatiche su Betfair (auto_bet.py): ordini reali o dry-run,
    # saldati a fine partita come le previsioni.
    c.execute('''CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT, mercato TEXT, esito TEXT,
        market_id TEXT, selection_id INTEGER,
        price REAL, stake REAL, mode TEXT, status TEXT, bet_id TEXT,
        esito_finale TEXT, profit REAL,
        created_at TEXT, settled_at TEXT,
        UNIQUE(match_id, esito))''')
    conn.commit()
    return conn


# --- Cassa (registro scommesse inserite dal sito) ---
def save_cassa_entry(partita, esito, quota, importo, ev=0.0, data=None,
                     esito_finale=None, profit=None, settled_at=None):
    """Registra una scommessa nella cassa."""
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO cassa (data, partita, esito, quota, importo, ev, timestamp,
                                    esito_finale, profit, settled_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?)''',
              (data or datetime.now().strftime("%Y-%m-%d"), partita, esito,
               float(quota), float(importo), float(ev), datetime.now().isoformat(),
               esito_finale, profit, settled_at))
    conn.commit(); conn.close()


def get_cassa():
    """Tutte le scommesse della cassa, dalla piu' recente (compreso il saldo)."""
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT id, data, partita, esito, quota, importo, ev, timestamp,"
              " esito_finale, profit, settled_at FROM cassa ORDER BY id DESC")
    rows = c.fetchall(); conn.close()
    return [
        {"id": r[0], "data": r[1], "partita": r[2], "esito": r[3],
         "quota": r[4], "importo": r[5], "ev": r[6], "timestamp": r[7],
         "esito_finale": r[8], "profit": r[9], "settled_at": r[10]}
        for r in rows
    ]


def _norm_team(name):
    """Normalizza un nome squadra per il match: minuscole, no accenti, no 'fc/cf'."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    return " ".join(w for w in s.split() if w not in ("fc", "cf"))


def _esito_won(esito, sh, sa):
    """True/False se l'esito e' vincente col risultato (sh, sa); None se non riconosciuto."""
    el = str(esito or "").lower().strip()
    if "over" in el:
        return (sh + sa) >= 3
    if "under" in el:
        return (sh + sa) <= 2
    if "btts" in el or "gol gol" in el:
        return sh > 0 and sa > 0
    if el == "1" or "casa" in el:
        return sh > sa
    if el == "2" or "trasferta" in el:
        return sa > sh
    if el == "x" or "pareggio" in el:
        return sh == sa
    return None


def settle_cassa():
    """Salda le scommesse aperte della cassa con i risultati reali (match_results).

    Idempotente: tocca solo le righe con esito_finale NULL. Il match avviene
    per coppia di squadre normalizzate; le scommesse senza risultato ancora
    disponibile restano in 'in gioco'. Ritorna il numero saldate in questa
    chiamata.
    """
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        rows = c.execute("SELECT id, partita, esito, quota, importo FROM cassa "
                         "WHERE esito_finale IS NULL").fetchall()
        results = c.execute("SELECT home_team, away_team, score_home, score_away "
                            "FROM match_results").fetchall()
    finally:
        conn.close()
    norm_map = {}
    for home, away, sh, sa in results:
        norm_map.setdefault((_norm_team(home), _norm_team(away)), (sh, sa))

    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    settled = 0
    for cid, partita, esito, quota, importo in rows:
        # "Serie A – Roma vs Empoli" -> ultimo " vs " separa casa/trasferta
        clean = partita.split(" – ")[-1].strip() if " – " in partita else partita
        if " vs " not in clean:
            continue
        parts = clean.split(" vs ")
        home, away = _norm_team(parts[0].strip()), _norm_team(parts[1].strip())
        match = norm_map.get((home, away))
        if match is None:
            continue
        sh, sa = match
        won = _esito_won(esito, sh, sa)
        if won is None:
            continue
        profit = round((quota - 1) * importo, 2) if won else round(-importo, 2)
        c.execute("UPDATE cassa SET esito_finale=?, profit=?, settled_at=? WHERE id=?",
                  ("won" if won else "lost", profit, now, cid))
        settled += 1
    conn.commit(); conn.close()
    return settled


def cassa_totals(entries=None):
    """Totali cassa: speso, potenziale delle in gioco, P/L realizzato."""
    entries = entries if entries is not None else get_cassa()
    speso = sum(e["importo"] or 0 for e in entries)
    vincita = sum((e["importo"] or 0) * (e["quota"] or 0) for e in entries)
    closed = [e for e in entries if e.get("esito_finale")]
    in_gioco = [e for e in entries if not e.get("esito_finale")]
    vinti = sum(1 for e in closed if e["esito_finale"] == "won")
    persi = sum(1 for e in closed if e["esito_finale"] == "lost")
    profit_real = sum(e.get("profit") or 0 for e in closed)
    speso_chiuso = sum(e["importo"] or 0 for e in closed)
    roi = (profit_real / speso_chiuso * 100) if speso_chiuso else 0.0
    return {"n": len(entries), "totale_speso": round(speso, 2),
            "vincita_potenziale": round(vincita, 2),
            "profit_potenziale": round(vincita - speso, 2),
            "chiusi": len(closed), "in_gioco": len(in_gioco),
            "vinti": vinti, "persi": persi,
            "speso_chiuso": round(speso_chiuso, 2),
            "profit_realizzato": round(profit_real, 2),
            "roi": round(roi, 2)}


# --- Ledger previsioni (segnale proposto -> esito reale -> calibrazione) ---
# Ogni segnale che il motore propone (qualunque mercato: 1X2, Over/Under,
# BTTS, Asian Handicap) viene registrato qui con il suo mercato, cosi' a
# fine partita si puo' verificare torto/ragione per mercato e correggere
# il modello dove sbaglia.

def save_prediction(match_id, mercato, esito, quota, prob, ev,
                    market_prob=None, market_edge=None, status="value"):
    """Registra (o aggiorna, se ancora non chiusa) una previsione del motore.

    Idempotente per (match_id, mercato, esito): a ogni nuova analisi la
    previsione non ancora saldata viene aggiornata con i prezzi correnti;
    quella gia' saldata non viene toccata (per non falsare il record).
    """
    now = datetime.now().isoformat()
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO predictions (match_id, mercato, esito, quota, prob, ev,
                                          market_prob, market_edge, status, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(match_id, mercato, esito) DO UPDATE SET
                   quota=excluded.quota, prob=excluded.prob, ev=excluded.ev,
                   market_prob=excluded.market_prob, market_edge=excluded.market_edge,
                   status=excluded.status, created_at=excluded.created_at
                 WHERE esito_finale IS NULL''',
              (match_id, mercato, esito, float(quota), float(prob), float(ev),
               market_prob, market_edge, status, now))
    conn.commit(); conn.close()


def get_predictions(mercato=None, status=None, closed=None, limit=500):
    conn = _get_conn(); c = conn.cursor()
    q = "SELECT match_id, mercato, esito, quota, prob, ev, market_prob, market_edge, " \
        "status, esito_finale, profit, created_at, settled_at FROM predictions"
    conds, args = [], []
    if mercato:
        conds.append("mercato=?"); args.append(mercato)
    if status:
        conds.append("status=?"); args.append(status)
    if closed is True:
        conds.append("esito_finale IS NOT NULL")
    elif closed is False:
        conds.append("esito_finale IS NULL")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall()
    conn.close()
    return [
        {"match_id": r[0], "mercato": r[1], "esito": r[2], "quota": r[3],
         "prob": r[4], "ev": r[5], "market_prob": r[6], "market_edge": r[7],
         "status": r[8], "esito_finale": r[9], "profit": r[10],
         "created_at": r[11], "settled_at": r[12]}
        for r in rows
    ]


def _ah_halves(line: float):
    """Linea AH quarter -> (due mezze linee, share 0.5); altrimenti (linea, 1.0)."""
    if abs(line * 2) % 1 != 0:  # .25 / .75 -> due mezze puntate
        low = math.floor(line * 2) / 2
        return (low, low + 0.5), 0.5
    return (line,), 1.0


def ah_pnl_units(esito: str, quota: float, adv: int) -> float:
    """P/L (in unita' da 1) di una scommessa AH flat, con split-bet per le quarter.

    esito nel formato "Home -0.75" / "Away +0.25": side + linea del lato.
    Linee intere/mezze = puntata piena; quarter (.25/.75) = due mezze.
    """
    parts = str(esito).split()
    if len(parts) < 2:
        return None
    side_raw = parts[0].lower()
    if side_raw.startswith("home"):
        side = "home"
    elif side_raw.startswith("away") or side_raw.startswith("guest"):
        side = "away"
    else:
        return None
    try:
        line = float(parts[1].replace("+", ""))
    except ValueError:
        return None
    halves, share = _ah_halves(line)
    total = 0.0
    for hline in halves:
        net = (adv + hline) if side == "home" else (-adv + hline)
        if net > 0:
            total += share * (quota - 1)
        elif net < 0:
            total -= share
    return total


def _prediction_outcome(mercato, esito, quota, sh, sa, home, away):
    """Risolve l'esito di una previsione. Ritorna (outcome, profit) o (None, None).

    outcome: 'won' | 'lost' | 'push'. Per l'Asian Handicap il profitto usa
    la logica split-bet (quarter lines = due mezze puntate).
    """
    m = str(mercato or "").upper()
    el = str(esito or "").lower().strip()
    if m == "AH" or any(t in el.split()[:1] for t in ("home", "away")):
        pnl = ah_pnl_units(esito, quota, sh - sa)
        if pnl is None:
            return None, None
        if pnl > 0:
            return "won", round(pnl, 4)
        if pnl == 0:
            return "push", 0.0
        return "lost", round(pnl, 4)
    if "over" in el:
        won = (sh + sa) >= 3
    elif "under" in el:
        won = (sh + sa) <= 2
    elif "btts" in el or "gol gol" in el:
        won = (sh > 0 and sa > 0)
    elif el in ("draw", "pareggio", "x"):
        won = sh == sa
    elif el == "1":
        won = sh > sa
    elif el == "2":
        won = sa > sh
    else:
        # esito = nome squadra (es. "Osasuna")
        hn = _norm_team(home); an = _norm_team(away); en = _norm_team(el)
        if en == hn:
            won = sh > sa
        elif en == an:
            won = sa > sh
        else:
            return None, None
    return ("won" if won else "lost"), round((quota - 1) if won else -1.0, 4)


def settle_predictions():
    """Salda le previsioni aperte coi risultati reali (idempotente).

    Ritorna (saldate, push): numero di previsioni chiuse in questa chiamata
    e quante di queste si sono concluse in push (es. handicap pari).
    """
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        open_rows = c.execute("SELECT id, match_id, mercato, esito, quota FROM predictions "
                              "WHERE esito_finale IS NULL").fetchall()
        results = c.execute("SELECT match_id, home_team, away_team, score_home, score_away "
                            "FROM match_results").fetchall()
    finally:
        conn.close()
    res_map = {r[0]: r[1:] for r in results}

    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    settled = pushes = 0
    for pid, match_id, mercato, esito, quota in open_rows:
        r = res_map.get(match_id)
        if not r:
            continue
        home, away, sh, sa = r
        outcome, profit = _prediction_outcome(mercato, esito, quota, sh, sa, home, away)
        if outcome is None:
            continue
        if outcome == "push":
            pushes += 1
        c.execute("UPDATE predictions SET esito_finale=?, profit=?, settled_at=? WHERE id=?",
                  (outcome, profit, now, pid))
        settled += 1
    conn.commit(); conn.close()
    return settled, pushes


def predictions_summary(mercato=None, settled_since=None):
    """Riepilogo previsioni CHIUSE per mercato: hit, ROI, gap EV, edge mercato.

    E' la telemetria di calibrazione: mostra per ogni mercato se il modello
    batte davvero la closing line (ROI realizzato vs EV atteso).
    Con `settled_since` (ISO, es. "2026-09-01") filtra solo le previsioni
    saldate a partire da quella data (report giornaliero).
    """
    rows = get_predictions(mercato=mercato, closed=True, limit=100000)
    if settled_since:
        rows = [r for r in rows if (r.get("settled_at") or "") >= settled_since]
    by_mkt: dict = {}
    for r in rows:
        key = r["mercato"] or "?"
        b = by_mkt.setdefault(key, {"n": 0, "won": 0, "lost": 0, "push": 0,
                                    "pnl": 0.0, "ev_sum": 0.0, "prob_sum": 0.0,
                                    "edge_sum": 0.0, "edge_n": 0})
        b["n"] += 1
        out = r["esito_finale"]
        if out == "won":
            b["won"] += 1
        elif out == "lost":
            b["lost"] += 1
        else:
            b["push"] += 1
        b["pnl"] += r["profit"] or 0.0
        b["ev_sum"] += (r["ev"] or 0.0)
        b["prob_sum"] += (r["prob"] or 0.0)
        if r.get("market_edge") is not None:
            b["edge_sum"] += r["market_edge"]
            b["edge_n"] += 1
    out = {}
    for key, b in by_mkt.items():
        closed = b["n"] - b["push"]
        roi = (b["pnl"] / b["n"] * 100) if b["n"] else 0.0
        out[key] = {
            "n": b["n"], "won": b["won"], "lost": b["lost"], "push": b["push"],
            "hit_rate": ((b["won"] / closed * 100) if closed else 0.0),
            "roi": round(roi, 2),
            "avg_ev": round(((b["ev_sum"] / b["n"]) * 100), 2) if b["n"] else 0.0,
            "gap": round(((b["pnl"] - b["ev_sum"]) / b["n"] * 100), 2) if b["n"] else 0.0,
            "avg_prob": round((b["prob_sum"] / b["n"]), 4) if b["n"] else 0.0,
            "avg_market_edge": round((b["edge_sum"] / b["edge_n"] * 100), 2) if b["edge_n"] else None,
        }
    return out


# --- Puntate automatiche (auto_bet.py) ---
def save_bet(match_id, mercato, esito, market_id=None, selection_id=None,
             price=0.0, stake=0.0, mode="dry-run", status=None, bet_id=None):
    """Registra (o aggiorna, se non chiusa) una puntata automatica.

    UNIQUE(match_id, esito): una sola puntata per esito anche se il job
    viene rilanciato; la puntata gia' saldata non viene toccata.
    """
    now = datetime.now().isoformat()
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO bets (match_id, mercato, esito, market_id, selection_id,
                                    price, stake, mode, status, bet_id, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(match_id, esito) DO UPDATE SET
                   market_id=excluded.market_id, selection_id=excluded.selection_id,
                   price=excluded.price, stake=excluded.stake, mode=excluded.mode,
                   status=excluded.status, bet_id=excluded.bet_id
                 WHERE esito_finale IS NULL''',
              (match_id, mercato, esito, market_id, selection_id,
               float(price), float(stake), mode, status, bet_id, now))
    conn.commit(); conn.close()


def bet_exists_open(match_id, esito):
    """True se esiste gia' una puntata aperta per (match_id, esito)."""
    conn = _get_conn(); c = conn.cursor()
    r = c.execute("SELECT 1 FROM bets WHERE match_id=? AND esito=? "
                  "AND esito_finale IS NULL", (match_id, esito)).fetchone()
    conn.close()
    return r is not None


def get_bets(day=None, closed=None, limit=200):
    conn = _get_conn(); c = conn.cursor()
    q = "SELECT match_id, mercato, esito, market_id, selection_id, price, stake, " \
        "mode, status, bet_id, esito_finale, profit, created_at, settled_at FROM bets"
    conds, args = [], []
    if day:
        conds.append("created_at LIKE ?"); args.append(f"{day}%")
    if closed is True:
        conds.append("esito_finale IS NOT NULL")
    elif closed is False:
        conds.append("esito_finale IS NULL")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall()
    conn.close()
    return [
        {"match_id": r[0], "mercato": r[1], "esito": r[2], "market_id": r[3],
         "selection_id": r[4], "price": r[5], "stake": r[6], "mode": r[7],
         "status": r[8], "bet_id": r[9], "esito_finale": r[10], "profit": r[11],
         "created_at": r[12], "settled_at": r[13]}
        for r in rows
    ]


def settle_bets(return_details: bool = False):
    """Salda le puntate automatiche aperte coi risultati reali (idempotente).

    Ritorna (saldate, push); con `return_details=True` anche la lista dei
    verdetti appena emessi: {match_id, league, home, away, mercato, esito,
    price, stake, mode, outcome, profit} — serve alle notifiche Telegram.
    """
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        open_rows = c.execute("SELECT id, match_id, mercato, esito, price, stake, mode "
                              "FROM bets WHERE esito_finale IS NULL").fetchall()
        results = c.execute("SELECT match_id, home_team, away_team, score_home, score_away "
                            "FROM match_results").fetchall()
        leagues = {row[0]: row[1] for row in
                   c.execute("SELECT id, league FROM matches").fetchall()}
    finally:
        conn.close()
    res_map = {r[0]: r[1:] for r in results}

    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    settled = pushes = 0
    details = []
    for bid, match_id, mercato, esito, price, stake, mode in open_rows:
        r = res_map.get(match_id)
        if not r:
            continue
        home, away, sh, sa = r
        outcome, _ = _prediction_outcome(mercato, esito, price, sh, sa, home, away)
        if outcome is None:
            continue
        if outcome == "push":
            profit = 0.0
            pushes += 1
        elif outcome == "won":
            profit = round(stake * (price - 1), 2)
        else:
            profit = round(-stake, 2)
        c.execute("UPDATE bets SET esito_finale=?, profit=?, settled_at=? WHERE id=?",
                  (outcome, profit, now, bid))
        settled += 1
        details.append({
            "match_id": match_id,
            "league": leagues.get(match_id, ""),
            "home": home, "away": away,
            "mercato": mercato, "esito": esito,
            "price": price, "stake": stake, "mode": mode,
            "outcome": outcome, "profit": profit,
        })
    conn.commit(); conn.close()

    if return_details:
        return settled, pushes, details
    return settled, pushes


def bets_period(since: str):
    """Puntate automatiche piazzate da `since` (ISO): stake, chiuse e P/L."""
    rows = get_bets(limit=100000)
    rows = [r for r in rows if (r.get("created_at") or "") >= since]
    closed = [r for r in rows if r.get("esito_finale")]
    return {
        "piazzate": len(rows),
        "stake_totale": round(sum(r["stake"] or 0 for r in rows), 2),
        "chiusi": len(closed),
        "vinti": sum(1 for r in closed if r["esito_finale"] == "won"),
        "persi": sum(1 for r in closed if r["esito_finale"] == "lost"),
        "push": sum(1 for r in closed if r["esito_finale"] == "push"),
        "profit": round(sum(r["profit"] or 0 for r in closed), 2),
    }


def day_completed(day: str | None = None) -> bool:
    """True se tutte le partite del giorno (gia' iniziate) hanno il risultato.

    Le partite non ancora iniziate NON bloccano la giornata (in tarda serata
    nessuna partita inizia piu', quindi il check converge). Serve per il
    riepilogo "a fine ultima partita".
    """
    from datetime import timezone
    day = day or datetime.now().strftime("%Y-%m-%d")
    conn = _get_conn(); c = conn.cursor()
    rows = c.execute("SELECT id, commence_time FROM matches WHERE commence_time LIKE ?",
                     (f"{day}%",)).fetchall()
    conn.close()
    if not rows:
        return False  # nessuna partita in calendario per quel giorno
    now_utc = datetime.now(timezone.utc)
    result_conn = _get_conn()
    _create_results_table(result_conn)
    result_c = result_conn.cursor()
    try:
        for mid, commence in rows:
            try:
                start = datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
            except Exception:
                continue  # data malformata: non blocca
            if start > now_utc:
                continue  # non ancora iniziata
            r = result_c.execute("SELECT 1 FROM match_results WHERE match_id=?", (mid,)).fetchone()
            if r is None:
                return False
    finally:
        result_conn.close()
    return True


def cassa_period(since: str):
    """Scommesse della cassa SALDATE a partire da `since` (ISO): P/L del periodo.

    Serve ai report giornalieri (sera = oggi, mattina = ieri): quanto ho
    davvero vinto/perso con le puntate in quel periodo.
    """
    closed = [e for e in get_cassa()
              if e.get("esito_finale") and (e.get("settled_at") or "") >= since]
    vinti = sum(1 for e in closed if e["esito_finale"] == "won")
    persi = sum(1 for e in closed if e["esito_finale"] == "lost")
    profit = sum(e.get("profit") or 0 for e in closed)
    speso = sum(e["importo"] or 0 for e in closed)
    return {"chiusi": len(closed), "vinti": vinti, "persi": persi,
            "speso": round(speso, 2), "profit": round(profit, 2),
            "roi": round((profit / speso * 100), 2) if speso else 0.0}


def clear_cassa():
    """Svuota la cassa (backup server)."""
    conn = _get_conn(); c = conn.cursor()
    c.execute("DELETE FROM cassa")
    conn.commit(); conn.close()

def init_db():
    _get_conn().close()

def log_signal(chat_id, evento, esito, quota, probabilita, ev):
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO signals VALUES (NULL,?,?,?,?,?,?,?,?,?)''',
              (chat_id, evento, esito, quota, probabilita, ev, datetime.now().isoformat(), None, 0.0))
    conn.commit(); conn.close()

def get_signals(chat_id=None, limit=50):
    conn = _get_conn(); c = conn.cursor()
    if chat_id:
        c.execute("SELECT * FROM signals WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))
    else:
        c.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall(); conn.close()
    return [Signal(*r) for r in rows]

def get_performance_summary(days=30):
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*), SUM(CASE WHEN esito_finale='won' THEN 1 ELSE 0 END), SUM(CASE WHEN esito_finale='lost' THEN 1 ELSE 0 END), SUM(profit) FROM signals WHERE esito_finale IS NOT NULL")
    row = c.fetchone(); conn.close()
    total, won, lost, profit = row if row else (0,0,0,0.0)
    roi = (profit/total*100) if total>0 else 0.0
    return {"closed":total or 0, "won":won or 0, "lost":lost or 0, "net_profit":profit or 0.0, "roi":roi}

def add_subscriber(chat_id, tier="free"):
    """Iscrive una chat. tier: 'free' o 'premium'.

    Se la chat esiste gia' la riga non viene sovrascritta (INSERT OR IGNORE):
    il tier esistente resta invariato; usare set_tier() per cambiarlo.
    """
    conn = _get_conn(); c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO subscribers (chat_id, tier, premium_until) VALUES (?,?,NULL)',
              (chat_id, tier))
    conn.commit(); conn.close()

def remove_subscriber(chat_id):
    conn = _get_conn(); c = conn.cursor()
    c.execute('DELETE FROM subscribers WHERE chat_id=?', (chat_id,))
    conn.commit(); conn.close()

def set_tier(chat_id, tier, premium_until=None):
    """Aggiorna tier e scadenza premium di un iscritto.

    tier 'premium' senza premium_until = abbonamento senza scadenza.
    """
    conn = _get_conn(); c = conn.cursor()
    c.execute('UPDATE subscribers SET tier=?, premium_until=? WHERE chat_id=?',
              (tier, premium_until, chat_id))
    conn.commit(); conn.close()

def get_subscription(chat_id):
    """Ritorna (tier, premium_until) di una chat, o None se non iscritta.

    Un abbonamento premium scaduto viene degradato a 'free' (senza cancellare
    la riga: e' comunque un iscritto).
    """
    conn = _get_conn(); c = conn.cursor()
    c.execute('SELECT tier, premium_until FROM subscribers WHERE chat_id=?', (chat_id,))
    row = c.fetchone(); conn.close()
    if not row:
        return None
    tier, premium_until = row
    if tier == "premium" and premium_until:
        try:
            if datetime.fromisoformat(premium_until) < datetime.now():
                tier = "free"
        except ValueError:
            tier = "free"
    return tier, premium_until

def is_premium(chat_id):
    """True se la chat e' iscritta con abbonamento premium valido."""
    sub = get_subscription(chat_id)
    return bool(sub) and sub[0] == "premium"

def get_subscribers(tier=None):
    """Chat_id degli iscritti, opzionalmente filtrati per tier.

    Con tier='premium' include solo abbonamenti ancora validi (scadenza
    futura o assente). Con tier='free' include anche i premium scaduti.
    """
    conn = _get_conn(); c = conn.cursor()
    if tier is None:
        c.execute('SELECT chat_id FROM subscribers')
    elif tier == "premium":
        c.execute("SELECT chat_id FROM subscribers WHERE tier='premium' AND "
                  "(premium_until IS NULL OR premium_until >= ?)",
                  (datetime.now().isoformat(),))
    else:
        c.execute("SELECT chat_id FROM subscribers WHERE tier=? OR tier IS NULL", (tier,))
    rows = c.fetchall(); conn.close()
    return [r[0] for r in rows]

# --- Calendario ---
def save_match(match_id, league, home, away, commence, status="scheduled"):
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?)''',
              (match_id, league, home, away, commence, status, datetime.now().isoformat()))
    conn.commit(); conn.close()

def get_today_matches():
    conn = _get_conn(); c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("SELECT * FROM matches WHERE commence_time LIKE ? ORDER BY commence_time", (f"{today}%",))
    rows = c.fetchall(); conn.close()
    return rows

def save_analysis(match_id, lam_h, lam_a, p1, px, p2, p_over, best_ev, best_esito, best_quota, best_bookmaker, status,
                  market_prob=None, market_edge=None):
    """Salva l'analisi di un match, incluso il confronto col mercato.

    market_prob: probabilita' implicita del mercato (devig) per l'esito scelto.
    market_edge: model_prob - market_prob (quanto il modello batte il mercato).
    """
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO match_analysis (match_id,lam_h,lam_a,prob_1,prob_X,prob_2,prob_over,best_ev,best_esito,best_quota,best_bookmaker,status,timestamp,market_prob,market_edge)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (match_id, lam_h, lam_a, p1, px, p2, p_over, best_ev, best_esito, best_quota, best_bookmaker, status,
               datetime.now().isoformat(), market_prob, market_edge))
    conn.commit(); conn.close()

def get_analysis_for_match(match_id):
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM match_analysis WHERE match_id=? ORDER BY id DESC LIMIT 1", (match_id,))
    row = c.fetchone(); conn.close()
    return row

def is_notified(match_id, date_str):
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT 1 FROM notifications WHERE match_id=? AND date=?", (match_id, date_str))
    row = c.fetchone(); conn.close()
    return row is not None

def mark_notified(match_id, date_str):
    conn = _get_conn(); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO notifications VALUES (?,?)", (match_id, date_str))
    conn.commit(); conn.close()

def save_clv(match_id, esito, quota, signal_started=False):
    """Registra un campione CLV per una coppia match+esito.

    - Prima analisi del match (signal_started=True): la quota corrente diventa la
      quota del segnale, e viene usata anche come primo campione di chiusura.
    - Analisi successive (signal_started=False): aggiorna la quota di chiusura,
      che converge verso il prezzo di mercato finale (CLV).
    """
    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("SELECT signal_quota, closing_quota FROM clv_history WHERE match_id=? AND esito=?", (match_id, esito))
    row = c.fetchone()
    if row is None or signal_started:
        c.execute("INSERT OR REPLACE INTO clv_history VALUES (?,?,?,?,?)",
                  (match_id, esito, quota, quota, now))
    else:
        c.execute("UPDATE clv_history SET closing_quota=?, updated_at=? WHERE match_id=? AND esito=?",
                  (quota, now, match_id, esito))
    conn.commit(); conn.close()

def clear_old_matches():
    conn = _get_conn(); c = conn.cursor()
    c.execute("DELETE FROM matches WHERE status='finished' OR commence_time < date('now','-2 days')")
    conn.commit(); conn.close()

# --- Tracking risultati ---
def _create_results_table(conn):
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS match_results (
        match_id TEXT PRIMARY KEY, league TEXT, home_team TEXT, away_team TEXT,
        score_home INTEGER, score_away INTEGER, result TEXT, settled_at TEXT)''')

def save_result(match_id, league, home, away, sh, sa, settled_at):
    conn = _get_conn(); _create_results_table(conn); c = conn.cursor()
    res = "1" if sh > sa else ("2" if sh < sa else "X")
    c.execute('''INSERT OR REPLACE INTO match_results VALUES (?,?,?,?,?,?,?,?)''',
              (match_id, league, home, away, sh, sa, res, settled_at))
    conn.commit(); conn.close()

def get_leagues_with_signals(days=3):
    conn = _get_conn(); c = conn.cursor()
    c.execute('''SELECT DISTINCT m.league FROM matches m
                 JOIN match_analysis a ON a.match_id = m.id
                 WHERE a.status IN ('value','strong_value')
                   AND m.commence_time >= date('now', ?)''', (f"-{days} days",))
    rows = c.fetchall(); conn.close()
    return [r[0] for r in rows]

def get_results_stats():
    conn = _get_conn(); _create_results_table(conn); c = conn.cursor()
    c.execute('''SELECT r.home_team, r.away_team, r.score_home, r.score_away,
                        a.best_esito, a.best_quota, a.best_ev, a.status, a.match_id
                 FROM match_results r
                 JOIN match_analysis a ON a.match_id = r.match_id''')
    rows = c.fetchall(); conn.close()
    # Mappa CLV per (match_id, esito)
    clv_map = {}
    try:
        conn2 = _get_conn(); c2 = conn2.cursor()
        c2.execute("SELECT match_id, esito, signal_quota, closing_quota FROM clv_history")
        for mid, esito, sig, clos in c2.fetchall():
            if clos and clos > 0:
                clv_map[(mid, (esito or "").lower().strip())] = (sig / clos) - 1.0
        conn2.close()
    except Exception:
        pass
    bets = []
    clvs = []
    for home, away, sh, sa, esito, quota, ev, status, mid in rows:
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
        bets.append({"quota": quota, "won": won, "ev": ev or 0.0})
        clv = clv_map.get((mid, el))
        if clv is not None:
            clvs.append(clv)
    total = len(bets)
    won_n = sum(1 for b in bets if b["won"])
    net = sum((b["quota"] - 1) if b["won"] else -1 for b in bets)
    return {
        "total": total, "won": won_n, "lost": total - won_n,
        "net": net, "roi": (net / total * 100) if total else 0.0,
        "hit_rate": (won_n / total * 100) if total else 0.0,
        "avg_ev": (sum(b["ev"] for b in bets) / total) if total else 0.0,
        "clv_tracked": len(clvs),
        "avg_clv": (sum(clvs) / len(clvs)) if clvs else 0.0,
    }
