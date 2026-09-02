"""Test del flusso di puntate automatiche (auto_bet.run_today_bets).

Usa un client finto e un catalogo di scansione sintetico: nessuna chiamata
di rete, nessun ordine reale.
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


class FakeClient:
    dry_run = True

    def __init__(self):
        self.calls = []

    def place_orders(self, market_id, instructions, customer_ref=None):
        self.calls.append((market_id, instructions, customer_ref))
        return {"status": "SUCCESS", "betId": f"DRY-{len(self.calls)}"}


def _seed_value_match(mid="m1", home="Osasuna", away="Getafe", esito="1",
                      quota=2.10, status="value", commence=None):
    start = commence or (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    tracker.save_match(mid, "Serie A", home, away, start)
    # esito "1" -> best_esito = nome squadra di casa (come da API bookmaker)
    best_esito = home if esito == "1" else (away if esito == "2" else "Draw")
    tracker.save_analysis(mid, 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                          best_esito, quota, "Pinnacle", status,
                          market_prob=0.45, market_edge=0.07)


def _scan(day=None, price=2.10, start_offset_h=3):
    start = (datetime.now(timezone.utc) + timedelta(hours=start_offset_h)) \
        .isoformat().replace("+00:00", "Z")
    return {
        "day": day or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "opportunities": [
            {"event_id": "e1", "event_name": "Osasuna v Getafe",
             "market_id": "1.100", "market_type": "MATCH_ODDS",
             "selection_id": 101, "selection_name": "Osasuna", "side": "BACK",
             "price": price, "price_size": 200.0, "start_time": start},
        ],
    }


def test_places_dry_run_bet(monkeypatch, temp_db):
    # Stake fisso: forza il fallback disabilitando adaptive_staking (import
    # fallito -> run_today_bets usa stake_eur). Lo stake ADATTIVO e' testato
    # in test_auto_bet_usa_stake_adaptivo.
    monkeypatch.setitem(sys.modules, "adaptive_staking", None)
    _seed_value_match()
    scan = _scan(price=2.20)
    monkeypatch.setattr(auto_bet, "load_latest_scan", lambda: scan)
    fc = FakeClient()
    placed = auto_bet.run_today_bets(client=fc, stake_eur=5.0)
    assert len(placed) == 1
    p = placed[0]
    assert p["esito_key"] == "1" and p["price"] == 2.20 and p["stake"] == 5.0
    assert p["mode"] == "dry-run" and p["status"] == "SUCCESS"
    # ordine inviato al client finto con la selezione giusta
    assert fc.calls[0][0] == "1.100"
    ins = fc.calls[0][1][0]
    assert ins["selectionId"] == 101 and ins["side"] == "BACK"
    assert ins["limitOrder"]["size"] == 5.0 and ins["limitOrder"]["price"] == 2.20
    # registrata nel DB
    bets = tracker.get_bets()
    assert len(bets) == 1
    assert tracker.bet_exists_open("m1", "1") is True


def test_no_duplicate_bet_on_rerun(monkeypatch, temp_db):
    _seed_value_match()
    monkeypatch.setattr(auto_bet, "load_latest_scan", lambda: _scan(price=2.20))
    fc = FakeClient()
    auto_bet.run_today_bets(client=fc, stake_eur=5.0)
    placed2 = auto_bet.run_today_bets(client=fc, stake_eur=5.0)
    assert placed2 == []           # gia' aperta
    assert len(tracker.get_bets()) == 1


def test_skips_low_price(monkeypatch, temp_db):
    _seed_value_match(quota=2.10)
    # prezzo Exchange 1.95 < 2.10*0.95 -> salto
    monkeypatch.setattr(auto_bet, "load_latest_scan", lambda: _scan(price=1.95))
    fc = FakeClient()
    placed = auto_bet.run_today_bets(client=fc, stake_eur=5.0)
    assert placed == [] and fc.calls == []


def test_skips_near_start(monkeypatch, temp_db):
    _seed_value_match()
    monkeypatch.setattr(auto_bet, "load_latest_scan",
                        lambda: _scan(price=2.20, start_offset_h=0))
    fc = FakeClient()
    placed = auto_bet.run_today_bets(client=fc, stake_eur=5.0)
    assert placed == [] and fc.calls == []


def test_skips_stale_catalogue(monkeypatch, temp_db):
    _seed_value_match()
    monkeypatch.setattr(auto_bet, "load_latest_scan",
                        lambda: _scan(price=2.20, day="2020-01-01"))
    fc = FakeClient()
    placed = auto_bet.run_today_bets(client=fc, stake_eur=5.0)
    assert placed == [] and fc.calls == []


def test_normalizes_stake_below_minimum(monkeypatch, temp_db):
    # Guardia minimo Exchange sul percorso a stake fisso (adaptive disattivo:
    # con Kelly attivo lo stake non dipende da stake_eur).
    monkeypatch.setitem(sys.modules, "adaptive_staking", None)
    _seed_value_match()
    monkeypatch.setattr(auto_bet, "load_latest_scan", lambda: _scan(price=2.20))
    fc = FakeClient()
    # stake 1.00 -> sotto il minimo Exchange Italia: nessun ordine
    placed = auto_bet.run_today_bets(client=fc, stake_eur=1.0)
    assert placed == [] and fc.calls == []


def test_sim_senza_betfair(monkeypatch, temp_db):
    """allow_sim=True senza Betfair: puntate SIM con la quota del segnale
    (paper test per il ML anche senza credenziali Exchange)."""
    # Come sopra: fallback a stake fisso per l'assert deterministico.
    monkeypatch.setitem(sys.modules, "adaptive_staking", None)
    _seed_value_match(quota=2.20)
    monkeypatch.setattr(auto_bet, "get_client", lambda: None)
    placed = auto_bet.run_today_bets(allow_sim=True, stake_eur=5.0)
    assert len(placed) == 1
    p = placed[0]
    assert p["mode"] == "sim"
    assert p["price"] == 2.20  # quota del segnale
    assert p["stake"] == 5.0
    bets = tracker.get_bets()
    assert len(bets) == 1 and bets[0]["mode"] == "sim"


def test_auto_bet_usa_stake_adaptivo(monkeypatch, temp_db):
    """Con adaptive_staking disponibile il stake della puntata arriva dal
    modulo adattivo (Kelly dinamico), non da stake_eur (solo fallback)."""
    import adaptive_staking
    calls = {}

    def _fake_adaptive(**kw):
        calls.update(kw)
        return {"stake": 7.5, "kelly_fraction": 0.2, "confidence_score": 0.6,
                "drawdown_factor": 1.0, "raw_stake": 7.5, "capped": False,
                "reason": "test"}

    monkeypatch.setattr(adaptive_staking, "adaptive_stake", _fake_adaptive)
    monkeypatch.setattr(adaptive_staking, "bankroll_stats",
                        lambda: {"current": 100.0, "peak": 100.0})
    _seed_value_match()
    monkeypatch.setattr(auto_bet, "load_latest_scan", lambda: _scan(price=2.20))
    fc = FakeClient()
    placed = auto_bet.run_today_bets(client=fc, stake_eur=5.0)
    assert len(placed) == 1
    assert placed[0]["stake"] == 7.5            # stake adattivo, non 5.0
    assert calls["odds"] == 2.20                # usa il prezzo Exchange
    ins = fc.calls[0][1][0]
    assert ins["limitOrder"]["size"] == 7.5


def test_senza_betfair_senza_sim_resta_fail_closed(monkeypatch, temp_db):
    """Senza allow_sim il comportamento resta invariato: nessuna puntata."""
    _seed_value_match()
    monkeypatch.setattr(auto_bet, "get_client", lambda: None)
    placed = auto_bet.run_today_bets(allow_sim=False, stake_eur=5.0)
    assert placed == []
    assert tracker.get_bets() == []


def test_sim_salta_vicino_all_inizio(monkeypatch, temp_db):
    """La guardia dei 15 min vale anche in modalita' SIM."""
    iniziata = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _seed_value_match(quota=2.20, commence=iniziata)
    monkeypatch.setattr(auto_bet, "get_client", lambda: None)
    placed = auto_bet.run_today_bets(allow_sim=True, stake_eur=5.0)
    assert placed == []


def test_skips_without_betfair(monkeypatch, temp_db):
    monkeypatch.setattr(auto_bet, "get_client", lambda: None)
    assert auto_bet.run_today_bets(stake_eur=5.0) == []


# --- Verifica incrociata runner: alias squadre + runner riconosciuto --------

def _seed_alias_match(mid="m1", home="Milan", away="Inter", esito="1",
                      quota=2.10, status="value"):
    start = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    tracker.save_match(mid, "Serie A", home, away, start)
    best_esito = home if esito == "1" else (away if esito == "2" else "Draw")
    tracker.save_analysis(mid, 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                          best_esito, quota, "Pinnacle", status,
                          market_prob=0.45, market_edge=0.07)


def _scan_alias(price=2.20, start_offset_h=3):
    """Catalogo Betfair con nomi squadra DIVERSI dal segnale (alias)."""
    start = (datetime.now(timezone.utc) + timedelta(hours=start_offset_h)) \
        .isoformat().replace("+00:00", "Z")
    return {
        "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "opportunities": [
            {"event_id": "e1", "event_name": "AC Milan v Inter",
             "market_id": "1.100", "market_type": "MATCH_ODDS",
             "selection_id": 101, "selection_name": "AC Milan", "side": "BACK",
             "price": price, "price_size": 200.0, "start_time": start},
        ],
    }


def test_alias_team_matches_betfair_catalogue(monkeypatch, temp_db):
    """'Milan' del segnale deve matchare 'AC Milan' del catalogo Betfair
    (alias risolti via TEAM_MAP): la partita NON va persa."""
    _seed_alias_match(home="Milan", away="Inter")
    monkeypatch.setattr(auto_bet, "load_latest_scan", lambda: _scan_alias(price=2.20))
    fc = FakeClient()
    placed = auto_bet.run_today_bets(client=fc, stake_eur=5.0)
    assert len(placed) == 1
    p = placed[0]
    assert p["esito_key"] == "1" and p["price"] == 2.20
    # il runner corretto e' stato selezionato (AC Milan = esito 1)
    assert fc.calls[0][0] == "1.100"
    assert fc.calls[0][1][0]["selectionId"] == 101


def test_unrecognized_runner_never_placed(monkeypatch, temp_db):
    """Runner che NON corrisponde a nessuna squadra dell'evento: fail-closed,
    nessun ordine (mai piazzare su un runner ambiguo)."""
    _seed_alias_match(home="Milan", away="Inter")
    scan = _scan_alias(price=2.20)
    # runner "Genoa" non e' ne' AC Milan ne' Inter -> da saltare
    scan["opportunities"][0]["selection_name"] = "Genoa"
    monkeypatch.setattr(auto_bet, "load_latest_scan", lambda: scan)
    fc = FakeClient()
    placed = auto_bet.run_today_bets(client=fc, stake_eur=5.0)
    assert placed == [] and fc.calls == []


def test_runner_draw_esito_x(monkeypatch, temp_db):
    """Runner 'The Draw' deve risolversi su esito X."""
    _seed_alias_match(mid="m2", home="Milan", away="Inter", esito="X", quota=3.4)
    scan = _scan_alias(price=3.60)
    scan["opportunities"][0]["selection_name"] = "The Draw"
    scan["opportunities"][0]["selection_id"] = 102
    monkeypatch.setattr(auto_bet, "load_latest_scan", lambda: scan)
    fc = FakeClient()
    placed = auto_bet.run_today_bets(client=fc, stake_eur=5.0)
    assert len(placed) == 1 and placed[0]["esito_key"] == "X"
    assert fc.calls[0][1][0]["selectionId"] == 102


def test_opponent_team_not_confused_with_home(monkeypatch, temp_db):
    """Runner = squadra OSPITE non deve matchare un segnale sulla CASA
    (e viceversa): l'esito deve essere univoco."""
    _seed_alias_match(home="Milan", away="Inter")  # segnale: 1 (Milan)
    scan = _scan_alias(price=2.20)
    scan["opportunities"][0]["selection_name"] = "Inter"  # esito 2
    scan["opportunities"][0]["selection_id"] = 103
    monkeypatch.setattr(auto_bet, "load_latest_scan", lambda: scan)
    fc = FakeClient()
    placed = auto_bet.run_today_bets(client=fc, stake_eur=5.0)
    assert placed == [] and fc.calls == []