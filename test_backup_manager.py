"""Test backup_manager: snapshot completo, integrity, rotazione, report."""
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tracker
import backup_manager


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    tracker.init_db()
    # config.DATA_DIR usato da backup_manager: punta su tmp
    import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(exist_ok=True)
    yield db_path


def _seed_signal(mid="m1", settle=True):
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    tracker.save_match(mid, "Serie A", "Inter", "Empoli",
                       base.isoformat().replace("+00:00", "Z"))
    tracker.save_analysis(mid, 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                          "Inter", 1.90, "Pinnacle", "value",
                          market_prob=0.45, market_edge=0.07)
    tracker.save_prediction(mid, "1X2", "Inter", 1.90, 0.55, 0.10)
    if settle:
        tracker.save_result(mid, "Serie A", "Inter", "Empoli", 2, 1,
                            datetime.now().isoformat())
        tracker.settle_predictions()


def test_run_backup_completo(temp_db, tmp_path):
    _seed_signal()
    dest = tmp_path / "backups"
    s = backup_manager.run_backup(dest_root=dest, keep=3)

    snap = Path(s["dest"])
    assert (snap / "quotaverace.db").exists()
    assert (snap / "training_dataset.csv").exists()
    assert (snap / "ml_export.json").exists()
    # integrity ok e conteggi coerenti
    assert s["db"]["integrity"] == "ok"
    assert s["db"]["tables"]["predictions"] == 1
    # dataset ML esportato: 1 riga chiusa
    assert s["ml"]["csv"] == 1
    assert s["ml"]["json_rows"] == 1
    assert s["errors"] == []


def test_backup_db_contenuto_corretto(temp_db, tmp_path):
    """Il DB copiato contiene i dati (non un file vuoto)."""
    _seed_signal("mX")
    s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
    conn = sqlite3.connect(Path(s["dest"]) / "quotaverace.db")
    n = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    conn.close()
    assert n == 1


def test_rotazione_backup(temp_db, tmp_path):
    """Oltre `keep` snapshot: i piu' vecchi vengono rimossi."""
    dest = tmp_path / "b"
    for _ in range(5):
        backup_manager.run_backup(dest_root=dest, keep=2)
    snaps = sorted(p for p in dest.iterdir() if p.is_dir())
    assert len(snaps) == 2


def test_backup_dataset_vuoto_non_esplode(temp_db, tmp_path):
    s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
    assert s["errors"] == [] or all("db" not in e for e in s["errors"])
    assert s["ml"]["csv"] == 0


def test_format_backup_report(temp_db, tmp_path):
    _seed_signal()
    s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
    text = backup_manager.format_backup_report(s)
    assert "BACKUP COMPLETATO" in text
    assert "Dataset ML" in text
    # con errori: messaggio diverso
    s2 = {**s, "errors": ["db: test error"]}
    assert "errori" in backup_manager.format_backup_report(s2)


def test_latest_backup(temp_db, tmp_path):
    assert backup_manager.latest_backup(tmp_path / "b") is None
    backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
    last = backup_manager.latest_backup(tmp_path / "b")
    assert last is not None and last.is_dir()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
