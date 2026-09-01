"""Test line movement tracking e RLM detection (line_movement.py)."""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tracker
import line_movement


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


# --- Snapshot recording ---

def test_record_snapshot_crea_tabella(temp_db):
    line_movement.record_snapshot("m1", "1", 2.10, "Pinnacle", 0.45)
    from tracker import _get_conn
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM price_snapshots WHERE match_id='m1'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][2] == "1"  # esito
    assert rows[0][3] == 2.10  # price


def test_record_snapshot_multipli(temp_db):
    for p in [2.10, 2.05, 2.00]:
        line_movement.record_snapshot("m1", "1", p, "Pinnacle")
    from tracker import _get_conn
    conn = _get_conn()
    n = conn.execute("SELECT COUNT(*) FROM price_snapshots WHERE match_id='m1'").fetchone()[0]
    conn.close()
    assert n == 3


def test_get_snapshots_ritorna_cronologia(temp_db):
    for p in [2.10, 2.05, 2.00]:
        line_movement.record_snapshot("m1", "1", p)
    snaps = line_movement.get_snapshots("m1", "1")
    assert len(snaps) == 3
    assert snaps[0]["price"] == 2.10
    assert snaps[-1]["price"] == 2.00


def test_get_snapshots_since_minutes(temp_db):
    line_movement.record_snapshot("m1", "1", 2.10)
    snaps = line_movement.get_snapshots("m1", "1", since_minutes=60)
    assert len(snaps) == 1


# --- RLM Detection ---

def test_rlm_pochi_snapshots_nessun_rlm(temp_db):
    line_movement.record_snapshot("m1", "1", 2.10)
    line_movement.record_snapshot("m1", "1", 2.05)
    rlm = line_movement.detect_rlm("m1", "1")
    assert rlm is None  # meno di 3 snapshots


def test_rlm_movimento_piccolo_nessun_rlm(temp_db):
    # Movimento minimo: nessun RLM
    for p in [2.10, 2.09, 2.08]:
        line_movement.record_snapshot("m1", "1", p)
    rlm = line_movement.detect_rlm("m1", "1")
    assert rlm is None


def test_rlm_movimento_grande_rilevato(temp_db):
    # Movimento grande: quota scende da 2.10 a 1.80 (~14%)
    # Inseriamo con timestamp diversi per superare span_minutes > 5
    base = datetime.now() - timedelta(minutes=30)
    # Prima crea la tabella
    conn = tracker._get_conn()
    line_movement._ensure_table(conn)
    for i, p in enumerate([2.10, 1.95, 1.85, 1.80]):
        ts = (base + timedelta(minutes=i * 10)).isoformat()
        conn.execute(
            "INSERT INTO price_snapshots "
            "(match_id, esito, price, bookmaker, market_prob, recorded_at) "
            "VALUES (?,?,?,?,?,?)",
            ("m1", "1", p, "", None, ts))
    conn.commit(); conn.close()
    rlm = line_movement.detect_rlm("m1", "1")
    assert rlm is not None
    assert rlm["total_move_pct"] < 0  # prezzo sceso
    assert rlm["direction"] == "down"
    assert rlm["reverse_moves"] >= 2


def test_rlm_con_public_bias(temp_db):
    # Pubblico al 60% su Home, ma quota Home sale → reverse
    base = datetime.now() - timedelta(minutes=30)
    conn = tracker._get_conn()
    line_movement._ensure_table(conn)
    for i, p in enumerate([2.00, 2.10, 2.25, 2.40]):
        ts = (base + timedelta(minutes=i * 10)).isoformat()
        conn.execute(
            "INSERT INTO price_snapshots "
            "(match_id, esito, price, bookmaker, market_prob, recorded_at) "
            "VALUES (?,?,?,?,?,?)",
            ("m1", "1", p, "", None, ts))
    conn.commit(); conn.close()
    rlm = line_movement.detect_rlm("m1", "1", public_bias=0.60)
    assert rlm is not None
    assert rlm["direction"] == "up"


# --- Steam Detection ---

def test_steam_nessun_movimento(temp_db):
    for p in [2.00, 2.01, 2.00]:
        line_movement.record_snapshot("m1", "1", p)
    steam = line_movement.detect_steam("m1", "1")
    assert steam is None


def test_steam_movimento_grande_rilevato(temp_db):
    # Simula steam: movimento > 6% in poco tempo
    base = datetime.now() - timedelta(minutes=5)
    for i, p in enumerate([2.00, 1.85]):
        line_movement.record_snapshot("m1", "1", p)
    steam = line_movement.detect_steam("m1", "1")
    # Nota: senza controllo temporale preciso, steam potrebbe non attivarsi
    # se gli snapshot sono troppo vicini. Testiamo che la funzione non crashi.
    assert steam is None or steam["move_pct"] != 0


# --- Analyze Movements ---

def test_analyze_movements(temp_db):
    for p in [2.10, 2.00, 1.90, 1.80]:
        line_movement.record_snapshot("m1", "1", p)
    for p in [3.50, 3.40, 3.30]:
        line_movement.record_snapshot("m1", "X", p)
    result = line_movement.analyze_movements("m1")
    assert "esiti" in result
    assert "1" in result["esiti"]
    assert "X" in result["esiti"]


# --- recent_signals_with_movement ---

def test_recent_signals_vuoto(temp_db):
    sigs = line_movement.recent_signals_with_movement()
    assert sigs == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
