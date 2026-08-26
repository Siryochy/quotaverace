"""
tracker.py – Database SQLite per il tracking dei segnali inviati.

Ogni segnale generato dal bot viene loggato con:
- timestamp di invio
- evento, esito, quota al momento dell'invio (opening line)
- probabilità Poisson e EV calcolato
- esito finale (won/lost/pending) — aggiornabile a posteriori
- profitto/loss in unità

Schema tabella `signals`:
    id INTEGER PRIMARY KEY
    chat_id INTEGER
    timestamp TEXT (ISO 8601)
    sport TEXT
    evento TEXT
    esito TEXT
    quota REAL
    probabilita REAL
    ev REAL
    esito_finale TEXT (won/lost/pending)
    profit REAL
    stake REAL DEFAULT 1.0
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

DB_PATH = Path(__file__).parent / "quotaverace.db"


@dataclass
class Signal:
    id: int
    chat_id: int
    timestamp: str
    sport: str
    evento: str
    esito: str
    quota: float
    probabilita: float
    ev: float
    esito_finale: str
    profit: float
    stake: float


# ---------------------------------------------------------------------------
# Inizializzazione
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Crea le tabelle signals e bankroll se non esistono."""
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                sport TEXT NOT NULL DEFAULT 'calcio',
                evento TEXT NOT NULL,
                esito TEXT NOT NULL,
                quota REAL NOT NULL,
                probabilita REAL NOT NULL,
                ev REAL NOT NULL,
                esito_finale TEXT NOT NULL DEFAULT 'pending',
                profit REAL,
                stake REAL NOT NULL DEFAULT 1.0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signals_evento ON signals(evento)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bankroll (
                chat_id INTEGER PRIMARY KEY,
                amount REAL NOT NULL DEFAULT 1000.0,
                fraction REAL NOT NULL DEFAULT 0.25,
                updated_at TEXT
            )
            """
        )
        conn.commit()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def log_signal(
    chat_id: int,
    evento: str,
    esito: str,
    quota: float,
    probabilita: float,
    ev: float,
    sport: str = "calcio",
    stake: float = 1.0,
) -> int:
    """
    Registra un nuovo segnale inviato.
    Ritorna l'id del record inserito.
    """
    ts = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO signals
            (chat_id, timestamp, sport, evento, esito, quota, probabilita, ev, esito_finale, profit, stake)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?)
            """,
            (chat_id, ts, sport, evento, esito, quota, probabilita, ev, stake),
        )
        conn.commit()
        return cursor.lastrowid


def update_result(signal_id: int, esito_finale: str) -> None:
    """
    Aggiorna l'esito finale di un segnale e calcola il profitto.
    esito_finale: 'won' | 'lost' | 'pending'
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT quota, stake FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Signal {signal_id} non trovato")

        quota, stake = row["quota"], row["stake"]
        if esito_finale == "won":
            profit = (quota - 1) * stake
        elif esito_finale == "lost":
            profit = -stake
        else:
            profit = None

        conn.execute(
            "UPDATE signals SET esito_finale = ?, profit = ? WHERE id = ?",
            (esito_finale, profit, signal_id),
        )
        conn.commit()


def get_signals(
    chat_id: Optional[int] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    esito_finale: Optional[str] = None,
    limit: int = 1000,
) -> List[Signal]:
    """
    Recupera segnali con filtri opzionali.
    Date in formato ISO (YYYY-MM-DD).
    """
    query = "SELECT * FROM signals WHERE 1=1"
    params: List = []

    if chat_id is not None:
        query += " AND chat_id = ?"
        params.append(chat_id)
    if from_date:
        query += " AND DATE(timestamp) >= ?"
        params.append(from_date)
    if to_date:
        query += " AND DATE(timestamp) <= ?"
        params.append(to_date)
    if esito_finale:
        query += " AND esito_finale = ?"
        params.append(esito_finale)

    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_signal(r) for r in rows]


def get_performance_summary(days: int = 30) -> dict:
    """
    Ritorna riepilogo performance per una finestra temporale.
    """
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN esito_finale = 'won' THEN 1 ELSE 0 END) AS won,
                SUM(CASE WHEN esito_finale = 'lost' THEN 1 ELSE 0 END) AS lost,
                SUM(CASE WHEN esito_finale = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(profit) AS net_profit,
                AVG(profit) AS avg_profit
            FROM signals
            WHERE DATE(timestamp) >= DATE('now', '-{} days')
            AND esito_finale != 'pending'
            """.format(days)
        ).fetchone()

        closed = row["won"] + row["lost"]
        roi = (row["net_profit"] / closed * 100) if closed else None

        return {
            "days": days,
            "total": row["total"],
            "closed": closed,
            "won": row["won"],
            "lost": row["lost"],
            "pending": row["pending"],
            "net_profit": round(row["net_profit"], 2) if row["net_profit"] else 0,
            "roi": round(roi, 2) if roi is not None else None,
        }


# ---------------------------------------------------------------------------
# Bankroll
# ---------------------------------------------------------------------------

def get_bankroll(chat_id: int) -> tuple[float, float]:
    """Ritorna (amount, fraction) per l'utente. Default: 1000€, 0.25 (quarter-kelly)."""
    with _get_conn() as conn:
        # Migrazione: crea tabella se il DB esiste senza di essa
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bankroll (
                chat_id INTEGER PRIMARY KEY,
                amount REAL NOT NULL DEFAULT 1000.0,
                fraction REAL NOT NULL DEFAULT 0.25,
                updated_at TEXT
            )
            """
        )
        conn.commit()
        row = conn.execute(
            "SELECT amount, fraction FROM bankroll WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row:
            return row["amount"], row["fraction"]
        return 1000.0, 0.25


def set_bankroll(chat_id: int, amount: float, fraction: float = 0.25) -> None:
    """Imposta bankroll e frazione Kelly per l'utente."""
    ts = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO bankroll (chat_id, amount, fraction, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                amount=excluded.amount,
                fraction=excluded.fraction,
                updated_at=excluded.updated_at
            """,
            (chat_id, amount, fraction, ts),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------

@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _row_to_signal(row: sqlite3.Row) -> Signal:
    return Signal(
        id=row["id"],
        chat_id=row["chat_id"],
        timestamp=row["timestamp"],
        sport=row["sport"],
        evento=row["evento"],
        esito=row["esito"],
        quota=row["quota"],
        probabilita=row["probabilita"],
        ev=row["ev"],
        esito_finale=row["esito_finale"],
        profit=row["profit"] if row["profit"] is not None else 0.0,
        stake=row["stake"],
    )


if __name__ == "__main__":
    init_db()
    print(f"Database inizializzato: {DB_PATH}")
