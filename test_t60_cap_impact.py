"""Tripwire di `t60_cap_impact.py` (misura del cap CB1 sulla corsia Kelly).

Il modulo e' una DIAGNOSTICA: deve restare di sola lettura (il ledger reale non
si tocca), offline (nessuna quota, nessun ordine) e deve misurare la cosa vera
— cioe' l'`adaptive_stake` di produzione, non una formula ricopiata.

Tutti i test sono OFFLINE: DB SQLite temporaneo costruito a mano, zero rete,
zero ordini.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import t60_cap_impact as tci


@pytest.fixture()
def ledger(tmp_path):
    """Ledger minimo con lo schema di produzione (matches + predictions)."""
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE matches (id TEXT PRIMARY KEY, league TEXT, "
                 "home_team TEXT, away_team TEXT, commence_time TEXT)")
    conn.execute("CREATE TABLE predictions (match_id TEXT, mercato TEXT, "
                 "esito TEXT, quota REAL, market_edge REAL, ev REAL, "
                 "status TEXT, esito_finale TEXT)")
    conn.execute("INSERT INTO matches VALUES "
                 "('m1','Premier League','Arsenal','Chelsea','2026-09-17T20:00:00Z')")
    conn.execute("INSERT INTO predictions VALUES "
                 "('m1','1X2','Arsenal',1.65,0.04,0.06,'value',NULL)")
    conn.execute("INSERT INTO predictions VALUES "
                 "('m1','1X2','Chelsea',2.10,0.01,0.01,'rejected',NULL)")
    conn.execute("INSERT INTO predictions VALUES "
                 "('m1','OU','over',1.90,0.02,0.03,'value',NULL)")
    conn.commit()
    conn.close()
    return db


class TestSolaLettura:
    def test_nessuna_scrittura_nel_sorgente(self):
        """Il sorgente non deve contenere istruzioni di scrittura SQL."""
        src = Path(tci.__file__).read_text(encoding="utf-8").upper()
        for kw in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE"):
            assert kw not in src, f"il misuratore non deve scrivere: {kw}"

    def test_nessuna_rete_nel_sorgente(self):
        src = Path(tci.__file__).read_text(encoding="utf-8")
        for mod in ("import requests", "odds_api", "urllib", "httpx"):
            assert mod not in src, f"la misura e' offline: {mod}"

    def test_connessione_di_sola_lettura(self, ledger):
        conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE predictions SET quota = 99")
        conn.close()
        # e il modulo legge davvero in mode=ro (nessun side effect)
        assert len(tci.ledger_signals(ledger)) == 1


class TestStakeCorsia:
    def test_floor_prevale_col_cap_severo_off(self):
        """Produzione (STAKE_CAP_HARD=0): sotto il floor lo stake e' 1.00."""
        res = tci.lane_stake(36.0, price=1.70, status="value", cap_hard=False)
        assert res["skipped"] is False
        assert res["stake"] == 1.00
        assert res["floor_applied"] is True

    def test_cap_severo_on_salta_sotto_il_floor(self):
        res = tci.lane_stake(36.0, price=1.70, status="value", cap_hard=True)
        assert res["skipped"] is True
        assert res["stake"] == 0.0

    def test_usa_adaptive_staking_reale(self):
        """La misura passa dallo staking di produzione (non da una copia)."""
        src = Path(tci.__file__).read_text(encoding="utf-8")
        assert "from adaptive_staking import adaptive_stake" in src

    def test_bankroll_grande_supera_il_cap(self):
        res = tci.lane_stake(500.0, price=1.70, status="strong_value",
                             cap_hard=False)
        assert res["stake"] > tci.cb1_cap()

    def test_configurazione_letture_dall_env(self, monkeypatch):
        """`cap_hard_active` segue l'env di produzione, non un default fisso."""
        import auto_bet
        monkeypatch.setattr(auto_bet, "STAKE_CAP_HARD", False)
        assert tci.cap_hard_active() is False
        monkeypatch.setattr(auto_bet, "STAKE_CAP_HARD", True)
        assert tci.cap_hard_active() is True


class TestCB1:
    def test_taglia_ma_non_aumenta(self):
        assert tci.with_cb1(5.0) == tci.cb1_cap()
        assert tci.with_cb1(0.4) == 0.4

    def test_a_bankroll_reale_non_cambia_nulli(self):
        """36 USDC (equity 17/09): ogni ordine e' gia' 1.00 = cap CB1."""
        for tier in tci.TIERS:
            res = tci.lane_stake(tci.WALLET_EQUITY_17_09, price=1.70,
                                 status=tier, cap_hard=False)
            assert tci.with_cb1(res["stake"]) == res["stake"]

    def test_soglie_di_rottura(self):
        """Cap 1% (value/moderate) -> ~100 USDC; cap 2% (strong) -> ~50."""
        assert tci.break_even_bankroll("value", cap_hard=False) == \
            pytest.approx(100.5, abs=0.5)
        assert tci.break_even_bankroll("moderate", cap_hard=False) == \
            pytest.approx(100.5, abs=0.5)
        assert tci.break_even_bankroll("strong_value", cap_hard=False) == \
            pytest.approx(50.25, abs=0.5)

    def test_sotto_e_sopra_la_soglia(self):
        soglia = tci.break_even_bankroll("strong_value", cap_hard=False)
        under = tci.lane_stake(soglia - 5, price=1.70, status="strong_value",
                               cap_hard=False)
        over = tci.lane_stake(soglia + 5, price=1.70, status="strong_value",
                              cap_hard=False)
        assert under["stake"] <= tci.cb1_cap()
        assert over["stake"] > tci.cb1_cap()

    def test_griglia_copre_il_wallet_reale(self):
        grid = tci.analytical(bankrolls=(36.0, 250.0), cap_hard=False)
        reale, grande = grid[0], grid[1]
        assert reale["tiers"]["value"]["delta"] == 0.0
        assert grande["tiers"]["value"]["delta"] < 0.0


class TestLedger:
    def test_legge_solo_i_segnali_qualificati_1x2_aperchi(self, ledger):
        rows = tci.ledger_signals(ledger)
        assert len(rows) == 1              # la rejected e l'OU sono escluse
        assert rows[0]["match_id"] == "m1"
        assert rows[0]["status"] == "value"

    def test_db_assente_non_solleva(self, tmp_path):
        assert tci.ledger_signals(tmp_path / "non_esiste.db") == []

    def test_tabella_mancante_non_solleva(self, tmp_path):
        db = tmp_path / "vuoto.db"
        sqlite3.connect(db).close()
        assert tci.ledger_signals(db) == []

    def test_nessun_taglio_col_wallet_reale(self, ledger):
        m = tci.measure(tci.WALLET_EQUITY_17_09, db_path=ledger, cap_hard=False)
        assert m["ledger_signals"] == 1
        assert m["ledger_capped"] == []

    def test_taglio_visibile_con_bankroll_grande(self, ledger):
        m = tci.measure(500.0, db_path=ledger, cap_hard=False)
        assert len(m["ledger_capped"]) == 1
        assert "impatto REALE" in m["verdict"]


class TestReport:
    def test_report_contiene_cap_soglie_e_verdetto(self, ledger):
        m = tci.measure(tci.WALLET_EQUITY_17_09, db_path=ledger, cap_hard=False)
        text = tci.format_report(m)
        assert "CB1" in text
        assert "1.00 USDC/ordine" in text
        assert "50.25" in text and "100.50" in text
        assert "non cambia NESSUNA puntata" in text

    def test_scenario_clv_non_cambia_le_soglie(self):
        """Il cap PERCENTUALE domina: la soglia non dipende dalla frazione Kelly."""
        base = tci.break_even_bankroll("value", clv_positive=False)
        clv = tci.break_even_bankroll("value", clv_positive=True)
        assert clv == pytest.approx(base, abs=0.5)
