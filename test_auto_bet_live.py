"""Test del wiring auto_bet → execution_engine (ordini REALI, dal 08/09).

La modalità live si attiva SOLO con AUTO_BET_MODE=live|real E provider reale
configurato (EXECUTION_PROVIDER + credenziali, niente DryRun). Con un ordine
riempito la riga in `bets` è mode='live' con market_id/selection_id/bet_id;
gli ordini rifiutati o i salti (mercato assente, prezzo sotto il floor EV)
NON lasciano righe sul ledger (un FAILED verrebbe saldato come perdita).
Senza provider reale configurato si ripiega sulla SIM (o fail-closed se il
chiamante passa allow_sim=False).
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tracker
import auto_bet


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture(autouse=True)
def _env_clean(monkeypatch):
    """I test partono sempre da AUTO_BET_MODE non impostato."""
    monkeypatch.delenv("AUTO_BET_MODE", raising=False)
    yield


def _seed_value_match(mid="m1", home="Osasuna", away="Getafe", esito="1",
                      quota=2.20, status="value", commence=None):
    start = commence or (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    tracker.save_match(mid, "Serie A", home, away, start)
    best_esito = home if esito == "1" else (away if esito == "2" else "Draw")
    tracker.save_analysis(mid, 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                          best_esito, quota, "Pinnacle", status,
                          market_prob=0.45, market_edge=0.07)


def _fixed_stake(monkeypatch):
    """Stake fisso (adaptive assente) per avere stake deterministici."""
    monkeypatch.setitem(sys.modules, "adaptive_staking", None)


def _filled():
    """Esito positivo simulato di _live_fill: ordine SX riempito."""
    return {"ok": True, "market_id": "0xbb4826699a0c7d80", "selection_id": 1,
            "bet_id": "0xabc123", "status": "FULLY_FILLED",
            "price": 2.20, "stake": 5.0}


def _sx_catalogue(home="Osasuna", away="Getafe", ts=None):
    """Tre mercati binari 'X vs Not X' di un evento SX Bet (1X2)."""
    if ts is None:
        ts = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")

    def row(mid, o1):
        return {"market_id": mid, "event_name": f"{home} vs {away}",
                "open_date": ts, "team_one_name": home, "team_two_name": away,
                "outcome_one_name": o1, "outcome_two_name": f"Not {o1}",
                "runners": [{"selection_id": 1, "name": o1},
                            {"selection_id": 2, "name": f"Not {o1}"}]}

    return [row("m-home", home), row("m-away", away), row("m-tie", "Tie")]


class TestLiveMode:
    def test_ordine_reale_riempito_salva_mode_live(self, monkeypatch, temp_db):
        """AUTO_BET_MODE=live + ordine riempito: riga `bets` con mode='live',
        market_id/selection_id/bet_id reali e stake/prezzo matched."""
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=2.20)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: _filled())

        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1
        p = placed[0]
        assert p["mode"] == "live" and p["status"] == "FULLY_FILLED"
        assert p["market_id"] == "0xbb4826699a0c7d80"
        assert p["selection_id"] == 1 and p["bet_id"] == "0xabc123"
        assert p["price"] == 2.20 and p["stake"] == 5.0

        bets = tracker.get_bets()
        assert len(bets) == 1
        b = bets[0]
        assert b["mode"] == "live"
        assert b["market_id"] == "0xbb4826699a0c7d80"
        assert b["selection_id"] == 1 and b["bet_id"] == "0xabc123"
        assert b["price"] == 2.20 and b["stake"] == 5.0
        assert tracker.bet_exists_open("m1", "1") is True

    def test_ordine_non_riempito_non_lascia_righe(self, monkeypatch, temp_db):
        """Ordine rifiutato/non riempito dall'exchange: nessuna riga sul
        ledger (un FAILED verrebbe saldato come perdita reale)."""
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=2.20)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        monkeypatch.setattr(
            auto_bet, "_live_fill",
            lambda pick, stake, floor: {"ok": False, "market_id": "0xm",
                                        "selection_id": 1,
                                        "status": "FAILURE",
                                        "error": "INSUFFICIENT_LIQUIDITY"})
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []
        assert tracker.get_bets() == []
        assert tracker.bet_exists_open("m1", "1") is False

    def test_salto_mercato_non_trovato_non_lascia_righe(self, monkeypatch, temp_db):
        """Mercato SX non trovato/ambiguo (o prezzo sotto il floor EV):
        _live_fill ritorna None -> nessun ordine, nessuna riga."""
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=2.20)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: None)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []
        assert tracker.get_bets() == []

    def test_stake_matched_usato_se_diverso(self, monkeypatch, temp_db):
        """Rippegno parziale: si registra lo stake/prezzo EFFETTIVAMENTE
        riempiti (non il richiesto)."""
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=2.20)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        filled = _filled()
        filled["stake"] = 3.0          # fill parziale di 5 richiesti
        filled["price"] = 2.30         # matched meglio del floor 2.20
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: filled)

        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed[0]["stake"] == 3.0 and placed[0]["price"] == 2.30
        b = tracker.get_bets()[0]
        assert b["stake"] == 3.0 and b["price"] == 2.30


class TestModeSelection:
    def test_default_senza_env_resta_sim(self, monkeypatch, temp_db):
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=2.20)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1 and placed[0]["mode"] == "sim"

    def test_live_richiesto_senza_provider_ripiega_sim(self, monkeypatch, temp_db):
        """AUTO_BET_MODE=live ma provider non configurato in questa env:
        nessun ordine reale — fallback SIM (allow_sim default True)."""
        monkeypatch.setenv("AUTO_BET_MODE", "live")
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=2.20)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1 and placed[0]["mode"] == "sim"

    def test_live_richiesto_senza_provider_fail_closed(self, monkeypatch, temp_db):
        """allow_sim=False senza provider configurato: nessuna puntata."""
        monkeypatch.setenv("AUTO_BET_MODE", "live")
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=2.20)
        placed = auto_bet.run_today_bets(stake_eur=5.0, allow_sim=False)
        assert placed == []
        assert tracker.get_bets() == []

    def test_provider_ready_riconosce_sxbet_configurato(self, monkeypatch):
        """_provider_ready deve leggere i flag del modulo execution_engine
        (monkeypatchati): con sxbet + credenziali -> True."""
        import execution_engine as ee
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "sxbet")
        monkeypatch.setattr(ee, "_creds_configured", lambda: True)
        assert auto_bet._provider_ready() is True

    def test_provider_ready_dry_run_falso(self, monkeypatch):
        import execution_engine as ee
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", True)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "sxbet")
        monkeypatch.setattr(ee, "_creds_configured", lambda: True)
        assert auto_bet._provider_ready() is False


class TestLiveFill:
    """La VERA _live_fill con ExecutionEngine finto (nessuna rete): floor EV,
    risoluzione mercato e riporto dell'esito dell'ordine."""

    def _setup(self, monkeypatch, provider):
        import execution_engine as ee
        _fixed_stake(monkeypatch)
        engine = type("_Eng", (), {"provider": provider})()
        monkeypatch.setattr(ee, "ExecutionEngine", lambda *a, **k: engine)
        return ee

    def _pick(self, **kw):
        base = {"match_id": "m1", "home": "Osasuna", "away": "Getafe",
                "esito_key": "1", "mercato": "1X2",
                "commence": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")}
        base.update(kw)
        return base

    class _Prov:
        name = "sxbet"

        def __init__(self, best=None, order=None, catalogue=None):
            self.best = best
            self.order = order
            self.catalogue = catalogue or _sx_catalogue()
            self.place_calls = []

        def best_back_price(self, market_id, selection_id):
            return self.best

        def list_market_catalogue(self, event_type_ids=("5",),
                                  market_type="1X2", max_results=400):
            return self.catalogue

        def place_limit_order(self, market_id, selection_id, side, price,
                              size, persistence="LAPSE"):
            self.place_calls.append((market_id, selection_id, side, price, size))
            return self.order

    def test_floor_ev_best_sotto_quota_salta(self, monkeypatch):
        """Best SX 2.10 < floor segnale 2.20: niente ordine (EV perso)."""
        import execution_engine as ee
        prov = self._Prov(best=2.10)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(self._pick(), stake=5.0, floor=2.20)
        assert res is None
        assert prov.place_calls == []

    def test_ordine_riempito_al_floor_o_meglio(self, monkeypatch):
        import execution_engine as ee
        order = ee.OrderResult(True, "0x9", "FULLY_FILLED", 2.20, 2.30,
                               5.0, 12.0)
        prov = self._Prov(best=None, order=order)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(self._pick(), stake=5.0, floor=2.20)
        assert res and res["ok"] is True
        assert res["market_id"] == "m-home" and res["selection_id"] == 1
        assert res["bet_id"] == "0x9" and res["status"] == "FULLY_FILLED"
        assert res["price"] == 2.30 and res["stake"] == 5.0
        # ordine richiesto al floor EV (bound), non sotto
        assert prov.place_calls[0][2] == "BACK"
        assert prov.place_calls[0][3] == 2.20

    def test_ordine_rifiutato_riporta_ok_false(self, monkeypatch):
        import execution_engine as ee
        order = ee.OrderResult(False, None, "FAILURE", 2.20, None, 0.0, 5.0,
                               error="INSUFFICIENT_FUNDS")
        prov = self._Prov(best=None, order=order)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(self._pick(), stake=5.0, floor=2.20)
        assert res is not None and res["ok"] is False
        assert res["status"] == "FAILURE"

    def test_mercato_ambiguo_salta(self, monkeypatch):
        # Due eventi candidati (stessi nomi, kickoff diversi): ambiguo -> skip
        c1 = _sx_catalogue()
        c2 = _sx_catalogue()
        for m in c2:
            m["market_id"] = "x-" + m["market_id"]
            m["open_date"] = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat().replace("+00:00", "Z")
        prov = self._Prov(best=None, order=None, catalogue=c1 + c2)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(self._pick(), stake=5.0, floor=2.20)
        assert res is None
