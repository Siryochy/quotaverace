"""Test della GUARDIA CASSA (finestra temporale, 11/09/2026).

`settle_cassa` aggancia le bet della cassa ai risultati per NOME (non per
id): senza una finestra temporale una coppia di squadre ripetuta (stessa
partita dell'anno prima, andata/ritorno) poteva essere saldata col
risultato SBAGLIATO. La guardia sceglie il risultato piu' vicino alla data
della bet e, se e' fuori finestra, NON chiude (resta in gioco).
"""
import tempfile
from pathlib import Path

import pytest

import tracker


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _result(mid, home, away, sh, sa, ts):
    tracker.save_result(mid, "La Liga", home, away, sh, sa, ts)


class TestGuardiaCassa:
    def test_finestra_blocca_risultato_vecchio(self, temp_db):
        """Solo un risultato di mesi prima: la guardia NON chiude la bet."""
        _result("old", "Osasuna", "Getafe", 1, 2, "2025-03-16T17:30:00+00:00")
        tracker.save_cassa_entry("Osasuna vs Getafe", "Over 2.5", 3.05, 20.0,
                                 data="2026-06-10")
        assert tracker.settle_cassa() == 0
        rows = tracker.get_cassa()
        assert rows[0]["esito_finale"] is None
        assert rows[0]["profit"] is None

    def test_scegle_il_risultato_piu_vicino_alla_bet(self, temp_db):
        """Due partite della stessa coppia: si salda sulla piu' VICINA alla
        data della bet, non sulla piu' recente in assoluto."""
        _result("far", "Osasuna", "Getafe", 1, 2, "2025-03-16T17:30:00+00:00")
        _result("near", "CA Osasuna", "Getafe", 0, 0, "2026-06-11T19:59:59Z")
        tracker.save_cassa_entry("Osasuna vs Getafe", "Over 2.5", 3.05, 20.0,
                                 data="2026-06-10")
        assert tracker.settle_cassa() == 1
        row = tracker.get_cassa()[0]
        assert row["esito_finale"] == "lost"   # 0-0 → Under, Over perso
        assert row["profit"] == pytest.approx(-20.0)

    def test_anchor_da_timestamp_se_data_assente(self, temp_db):
        _result("m", "Roma", "Empoli", 2, 1, "2026-06-10T20:00:00Z")
        tracker.save_cassa_entry("Roma vs Empoli", "Over 2.5", 2.10, 10.0,
                                 data="2026-06-10")
        assert tracker.settle_cassa() == 1

    def test_fuori_finestra_per_un_giorno_in_piu(self, temp_db):
        """20 giorni > default 14: resta in gioco finche' non si allarga la
        finestra via env."""
        _result("m", "Roma", "Empoli", 2, 1, "2026-06-30T20:00:00Z")
        tracker.save_cassa_entry("Roma vs Empoli", "Over 2.5", 2.10, 10.0,
                                 data="2026-06-10")
        assert tracker.settle_cassa() == 0
        assert tracker.get_cassa()[0]["esito_finale"] is None

    def test_finestra_allargabile_via_env(self, temp_db, monkeypatch):
        monkeypatch.setenv("CASSA_MATCH_WINDOW_DAYS", "30")
        _result("m", "Roma", "Empoli", 2, 1, "2026-06-30T20:00:00Z")
        tracker.save_cassa_entry("Roma vs Empoli", "Over 2.5", 2.10, 10.0,
                                 data="2026-06-10")
        assert tracker.settle_cassa() == 1
        assert tracker.get_cassa()[0]["esito_finale"] == "won"

    def test_senza_alcuna_data_fallback_storico(self, temp_db):
        """Nessuna data utilizzabile (ne' bet ne' risultato): si torna al
        comportamento storico (il risultato disponibile)."""
        _result("m", "Roma", "Empoli", 2, 1, "")
        tracker.save_cassa_entry("Roma vs Empoli", "Over 2.5", 2.10, 10.0)
        conn = tracker._get_conn()
        conn.execute("UPDATE cassa SET data=NULL, timestamp=NULL WHERE id=1")
        conn.commit(); conn.close()
        assert tracker.settle_cassa() == 1
        assert tracker.get_cassa()[0]["esito_finale"] == "won"

    def test_idempotente_con_guardia(self, temp_db):
        _result("m", "Inter", "Napoli", 3, 1, "2026-06-10T20:00:00Z")
        tracker.save_cassa_entry("Inter vs Napoli", "Over 2.5", 2.00, 20.0,
                                 data="2026-06-10")
        assert tracker.settle_cassa() == 1
        assert tracker.settle_cassa() == 0
