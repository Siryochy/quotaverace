"""Tracker SQLite per segnali, calendario e analisi"""
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
    conn.commit()
    return conn


# --- Cassa (registro scommesse inserite dal sito) ---
def save_cassa_entry(partita, esito, quota, importo, ev=0.0, data=None):
    """Registra una scommessa nella cassa."""
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO cassa (data, partita, esito, quota, importo, ev, timestamp)
                 VALUES (?,?,?,?,?,?,?)''',
              (data or datetime.now().strftime("%Y-%m-%d"), partita, esito,
               float(quota), float(importo), float(ev), datetime.now().isoformat()))
    conn.commit(); conn.close()


def get_cassa():
    """Tutte le scommesse della cassa, dalla piu' recente."""
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT id, data, partita, esito, quota, importo, ev, timestamp FROM cassa ORDER BY id DESC")
    rows = c.fetchall(); conn.close()
    return [
        {"id": r[0], "data": r[1], "partita": r[2], "esito": r[3],
         "quota": r[4], "importo": r[5], "ev": r[6], "timestamp": r[7]}
        for r in rows
    ]


def cassa_totals(entries=None):
    """Totali cassa: speso, vincita potenziale, profit potenziale."""
    entries = entries if entries is not None else get_cassa()
    speso = sum(e["importo"] or 0 for e in entries)
    vincita = sum((e["importo"] or 0) * (e["quota"] or 0) for e in entries)
    return {"n": len(entries), "totale_speso": round(speso, 2),
            "vincita_potenziale": round(vincita, 2),
            "profit_potenziale": round(vincita - speso, 2)}


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

def add_subscriber(chat_id):
    conn = _get_conn(); c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO subscribers VALUES (?)', (chat_id,))
    conn.commit(); conn.close()

def remove_subscriber(chat_id):
    conn = _get_conn(); c = conn.cursor()
    c.execute('DELETE FROM subscribers WHERE chat_id=?', (chat_id,))
    conn.commit(); conn.close()

def get_subscribers():
    conn = _get_conn(); c = conn.cursor()
    c.execute('SELECT chat_id FROM subscribers')
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
