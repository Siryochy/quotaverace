"""Test del kill-switch Telegram su auto_bet (dal 08/09).

Il comando /autobet scrive un override persistente in
data/execution/auto_bet_mode.json (volume condiviso, sopravvive ai
redeploy): "off" -> stop totale (nessuna puntata, ne' reale ne' simulata),
"sim" -> pausa ordini reali (resta paper trading), "live" -> ripristina
AUTO_BET_MODE env. In assenza del file vale AUTO_BET_MODE env (default
"sim"). Il kill-switch ha precedenza su tutto: anche con AUTO_BET_MODE=live
e provider configurato, "off"/"sim" bloccano gli ordini reali.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import auto_bet
import tracker


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture()
def ks_file(tmp_path, monkeypatch):
    """Il file del kill-switch punta su tmp: i test non toccano mai il
    volume reale (data/execution/auto_bet_mode.json)."""
    f = tmp_path / "auto_bet_mode.json"
    monkeypatch.setattr(auto_bet, "KILL_SWITCH_FILE", f)
    return f


@pytest.fixture(autouse=True)
def _isolate_daily_stop(tmp_path, monkeypatch):
    """Lo stop-loss giornaliero usa un file temporaneo (mai il volume reale)."""
    monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE", tmp_path / "daily_stop.json")


def _seed_value_match(mid="m1", home="Osasuna", away="Getafe", esito="1",
                      quota=1.65, status="value"):
    start = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    tracker.save_match(mid, "Serie A", home, away, start)
    best_esito = home if esito == "1" else (away if esito == "2" else "Draw")
    tracker.save_analysis(mid, 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                          best_esito, quota, "Pinnacle", status,
                          market_prob=0.60, market_edge=0.07)
    # Ledger previsioni: _today_value_picks legge da QUI dal 09/09.
    # quota 1.65 / prob. 0.60 = favorito netto (strategia 11/09).
    tracker.save_prediction(mid, "1X2", best_esito, quota, 0.52, 0.08,
                            market_prob=0.60, market_edge=0.07, status=status)


class TestOverride:
    def test_nessun_override_usa_env(self, monkeypatch, ks_file):
        monkeypatch.delenv("AUTO_BET_MODE", raising=False)
        assert auto_bet._requested_mode() == "sim"
        monkeypatch.setenv("AUTO_BET_MODE", "live")
        assert auto_bet._requested_mode() == "live"

    def test_set_off_blocca_tutto(self, monkeypatch, ks_file):
        monkeypatch.delenv("AUTO_BET_MODE", raising=False)
        auto_bet.set_kill_switch("off")
        assert auto_bet._requested_mode() == "off"
        # anche allow_sim=True non deve mai piazzare nulla
        assert auto_bet._execution_mode(allow_sim=True) == "off"
        assert auto_bet._execution_mode(allow_sim=False) == "off"
        data = json.loads(ks_file.read_text())
        assert data["mode"] == "off"
        assert "updated_at" in data

    def test_set_sim_blocca_i_reali(self, monkeypatch, ks_file):
        # env live + provider pronto non basta: l'override sim vince.
        monkeypatch.setenv("AUTO_BET_MODE", "live")
        monkeypatch.setattr(auto_bet, "_provider_ready", lambda: True)
        auto_bet.set_kill_switch("sim")
        assert auto_bet._requested_mode() == "sim"
        assert auto_bet._execution_mode(allow_sim=True) == "sim"

    def test_clear_ritorna_all_env(self, monkeypatch, ks_file):
        auto_bet.set_kill_switch("off")
        auto_bet.clear_kill_switch()
        assert not ks_file.exists()
        monkeypatch.delenv("AUTO_BET_MODE", raising=False)
        assert auto_bet._requested_mode() == "sim"
        assert auto_bet._execution_mode(allow_sim=True) == "sim"

    def test_valore_non_valido_raise(self, ks_file):
        with pytest.raises(ValueError):
            auto_bet.set_kill_switch("ciao")

    def test_real_normalizzato_a_live(self, ks_file):
        auto_bet.set_kill_switch("real")
        assert json.loads(ks_file.read_text())["mode"] == "live"

    def test_file_corrotto_ignorato(self, monkeypatch, ks_file):
        ks_file.write_text("{non-json", encoding="utf-8")
        monkeypatch.delenv("AUTO_BET_MODE", raising=False)
        assert auto_bet._requested_mode() == "sim"

    def test_status(self, monkeypatch, ks_file):
        monkeypatch.delenv("AUTO_BET_MODE", raising=False)
        st = auto_bet.kill_switch_status()
        assert st["override"] is None
        assert st["effective"] == "sim"
        auto_bet.set_kill_switch("off")
        st = auto_bet.kill_switch_status()
        assert st["override"] == "off"
        assert st["effective"] == "off"


class TestRunTodayBets:
    def test_off_non_piazza_neanche_sim(self, monkeypatch, temp_db, ks_file):
        """Kill-switch off: nessuna riga sul ledger anche con segnali."""
        monkeypatch.setitem(sys.modules, "adaptive_staking", None)
        monkeypatch.delenv("AUTO_BET_MODE", raising=False)
        _seed_value_match(quota=1.65)
        auto_bet.set_kill_switch("off")
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []
        assert tracker.get_bets() == []

    def test_sim_con_override_sim_resta_paper(self, monkeypatch, temp_db,
                                              ks_file):
        """Override 'sim': piazzata la puntata simulata, mai live."""
        monkeypatch.setitem(sys.modules, "adaptive_staking", None)
        monkeypatch.setenv("AUTO_BET_MODE", "live")
        # provider "pronto" ma l'override sim impedisce gli ordini reali
        monkeypatch.setattr(auto_bet, "_provider_ready", lambda: True)
        _seed_value_match(quota=1.65)
        auto_bet.set_kill_switch("sim")
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1
        assert placed[0]["mode"] == "sim"
        bets = tracker.get_bets()
        assert len(bets) == 1 and bets[0]["mode"] == "sim"