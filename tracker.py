"""Tracker SQLite per segnali, calendario e analisi"""
import sqlite3
import os
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "quotaverace.db"

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
        FOREIGN KEY (match_id) REFERENCES matches(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        match_id TEXT, date TEXT, PRIMARY KEY (match_id, date))''')
    conn.commit()
    return conn

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

def save_analysis(match_id, lam_h, lam_a, p1, px, p2, p_over, best_ev, best_esito, best_quota, best_bookmaker, status):
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO match_analysis (match_id,lam_h,lam_a,prob_1,prob_X,prob_2,prob_over,best_ev,best_esito,best_quota,best_bookmaker,status,timestamp)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (match_id, lam_h, lam_a, p1, px, p2, p_over, best_ev, best_esito, best_quota, best_bookmaker, status, datetime.now().isoformat()))
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
                        a.best_esito, a.best_quota, a.best_ev, a.status
                 FROM match_results r
                 JOIN match_analysis a ON a.match_id = r.match_id''')
    rows = c.fetchall(); conn.close()
    bets = []
    for home, away, sh, sa, esito, quota, ev, status in rows:
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
    total = len(bets)
    won_n = sum(1 for b in bets if b["won"])
    net = sum((b["quota"] - 1) if b["won"] else -1 for b in bets)
    return {
        "total": total, "won": won_n, "lost": total - won_n,
        "net": net, "roi": (net / total * 100) if total else 0.0,
        "hit_rate": (won_n / total * 100) if total else 0.0,
        "avg_ev": (sum(b["ev"] for b in bets) / total) if total else 0.0,
    }
