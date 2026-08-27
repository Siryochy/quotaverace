"""Tracker SQLite per segnali"""
import sqlite3
import os
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "quotaverace.db"

class Signal:
    def __init__(self, id, chat_id, evento, esito, quota, probabilita, ev, timestamp, esito_finale, profit):
        self.id = id
        self.chat_id = chat_id
        self.evento = evento
        self.esito = esito
        self.quota = quota
        self.probabilita = probabilita
        self.ev = ev
        self.timestamp = timestamp
        self.esito_finale = esito_finale
        self.profit = profit

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        evento TEXT,
        esito TEXT,
        quota REAL,
        probabilita REAL,
        ev REAL,
        timestamp TEXT,
        esito_finale TEXT,
        profit REAL
    )''')
    conn.commit()
    conn.close()

def log_signal(chat_id, evento, esito, quota, probabilita, ev):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('''INSERT INTO signals (chat_id, evento, esito, quota, probabilita, ev, timestamp, esito_finale, profit)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (chat_id, evento, esito, quota, probabilita, ev, datetime.now().isoformat(), None, 0.0))
    conn.commit()
    conn.close()

def get_signals(chat_id=None, limit=50):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    if chat_id:
        c.execute("SELECT * FROM signals WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit))
    else:
        c.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [Signal(*row) for row in rows]

def get_performance_summary(days=30):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("SELECT COUNT(*), SUM(CASE WHEN esito_finale='won' THEN 1 ELSE 0 END), SUM(CASE WHEN esito_finale='lost' THEN 1 ELSE 0 END), SUM(profit) FROM signals WHERE esito_finale IS NOT NULL")
    row = c.fetchone()
    conn.close()
    total, won, lost, profit = row if row else (0, 0, 0, 0.0)
    roi = (profit / total * 100) if total > 0 else 0.0
    return {"closed": total or 0, "won": won or 0, "lost": lost or 0, "net_profit": profit or 0.0, "roi": roi}
