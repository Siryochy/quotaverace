"""Test sx_signals: scan SX -> ledger + visibilita' auto-bet + settlement.

Tutti i test sono OFFLINE: il provider SX viene sostituito da un fake con
risposte canned (nessuna rete, nessun ordine, nessun credito).
"""
import time
from datetime import datetime, timezone

import pytest

import sx_signals
import tracker
from execution_engine import SX_PROB_SCALE

SX = "sx-LTEST1"


def _pct(price: float) -> int:
    """Quota decimale -> percentageOdds scalato (come l'API SX)."""
    return int(SX_PROB_SCALE / price)


def _raw_markets() -> list:
    """3 mercati binari 1X2 coerenti per un evento finto."""
    now = int(time.time())
    base = {"sportId": 5, "leagueId": 999, "leagueLabel": "Italy Serie A",
            "gameTime": now + 3600, "type": 1, "status": "ACTIVE",
            "teamOneName": "Alpha", "teamTwoName": "Beta"}
    legs = [("mkt1", "Alpha"), ("mkt2", "Tie"), ("mkt3", "Beta")]
    out = []
    for h, o1 in legs:
        m = dict(base)
        m["marketHash"] = h
        m["sportXeventId"] = "LTEST1"
        m["outcomeOneName"] = o1
        m["outcomeTwoName"] = f"Not {o1}"
        out.append(m)
    return out


def _book_for(market_hash: str) -> dict:
    """Order book taker canned: quote 1X2 [1.70, 3.90, 5.80] (inv_sum ~1.02).

    Dal 11/09 la strategia ammette SOLO favoriti netti (quota <= 1.80): il
    book deve avere un favorito giocabile, altrimenti scan() giustamente
    non produce segnali.
    """
    prices = {"mkt1": 1.70, "mkt2": 3.90, "mkt3": 5.80}
    p = prices[market_hash]
    # outcomeOne = lato esito (back a quota p); outcomeTwo = complementare.
    return {"data": {
        "outcomeOne": [{"percentageOdds": _pct(p), "size": 10 * 10 ** 6}],
        "outcomeTwo": [{"percentageOdds": _pct(1.0 / (1.0 - 1.0 / p)),
                        "size": 10 * 10 ** 6}],
    }}


class FakeSxProvider:
    """Stub del provider SX: solo _get (nessuna credenziale, nessuna rete)."""

    name = "sxbet"

    def __init__(self, raw=None):
        self._raw = raw if raw is not None else _raw_markets()

    def _get(self, path, params=None):
        if path == "markets/active":
            return {"data": {"markets": self._raw, "nextKey": None}}
        if path == "orderbook-v3/snapshot":
            return _book_for((params or {}).get("marketHash"))
        raise AssertionError(f"endpoint inatteso nel test: {path}")


@pytest.fixture()
def temp_db(monkeypatch):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture()
def no_settlement_sources(monkeypatch):
    """Nessuna fonte risultati: il settlement deve restare fail-closed."""
    monkeypatch.setenv("ODDS_API_KEY", "")
    monkeypatch.setenv("API_FOOTBALL_KEY", "")


def test_kickoff_utc_ms_formats():
    # Secondi epoch (formato osservato su /markets/active)
    assert sx_signals._kickoff_utc_ms(1788980400) == 1788980400000
    # Ms epoch passa invariato
    assert sx_signals._kickoff_utc_ms(1788980400000) == 1788980400000
    # ISO (open_date del provider)
    assert sx_signals._kickoff_utc_ms("2026-09-09T19:00:00Z") == 1788980400000
    assert sx_signals._kickoff_utc_ms(None) is None
    assert sx_signals._kickoff_utc_ms(0) is None


def test_league_map_fuzzy():
    assert sx_signals._league_sx_to_sports_map("Italy Serie A") == "Serie A"
    # Etichette senza corrispondenza: None (settlement via altra fonte)
    assert sx_signals._league_sx_to_sports_map("Campionato Inventato XY") is None


def test_scan_saves_value_signals(temp_db, monkeypatch):
    """scan() con provider fake: salva match/analisi/predictions con
    match_id sx-*, classifica il best esito e il candidato 1 esce value."""
    monkeypatch.setattr(sx_signals, "expected_goals",
                        lambda h, a: (1.9, 0.8))
    monkeypatch.setattr(sx_signals, "prob_1x2",
                        lambda lh, la: (0.66, 0.20, 0.14))
    # Blend deterministico: prob finale = prob modello (il devig SX resta
    # quello reale, usato per market_prob/market_edge).
    monkeypatch.setattr(sx_signals, "adjusted_probability",
                        lambda model_prob, market_prob, price, league=None:
                        model_prob)

    saved = sx_signals.scan(provider=FakeSxProvider())
    assert len(saved) == 1
    sig = saved[0]
    assert sig["match_id"] == SX
    assert sig["esito"] == "1" and sig["status"] == "strong_value"

    # Ledger: matches + match_analysis + UNICA prediction (il favorito).
    conn = tracker._get_conn()
    mrow = conn.execute("SELECT home_team, away_team, league FROM matches "
                        "WHERE id=?", (SX,)).fetchone()
    preds = conn.execute("SELECT esito, status FROM predictions "
                         "WHERE match_id=? ORDER BY esito",
                         (SX,)).fetchall()
    na = conn.execute("SELECT COUNT(*) FROM match_analysis WHERE match_id=?",
                      (SX,)).fetchone()[0]
    conn.close()
    assert mrow == ("Alpha", "Beta", "Serie A")
    # Strategia solo favoriti (11/09): X e 2 non entrano nemmeno nel ledger.
    assert [p[0] for p in preds] == ["1"]
    assert dict(preds)["1"] == "strong_value"
    assert na == 1

    # Visibilita' auto-bet: il segnale SX entra nei candidati del giorno.
    from auto_bet import _today_value_picks
    picks = [p for p in _today_value_picks() if p["match_id"] == SX]
    assert len(picks) == 1
    assert picks[0]["esito_key"] == "1"
    assert picks[0]["quota"] == pytest.approx(1.70, abs=0.01)


def test_scan_skips_incoherent_books(temp_db, monkeypatch):
    """inv_sum fuori range (mercato largo/rotto): nessun segnale salvato."""
    monkeypatch.setattr(sx_signals, "expected_goals",
                        lambda h, a: (1.6, 1.1))
    monkeypatch.setattr(sx_signals, "prob_1x2",
                        lambda lh, la: (0.45, 0.28, 0.27))
    raw = _raw_markets()
    # Rendo il book dello X larghissimo (quota 6.0 -> inv_sum ~1.2)
    prov = FakeSxProvider(raw)
    orig_book = prov._get

    def _wide(path, params=None):
        data = orig_book(path, params)
        if path == "orderbook-v3/snapshot" and \
                (params or {}).get("marketHash") == "mkt2":
            data["data"]["outcomeOne"] = [
                {"percentageOdds": _pct(6.0), "size": 10 * 10 ** 6}]
        return data

    prov._get = _wide
    saved = sx_signals.scan(provider=prov)
    assert saved == []
    conn = tracker._get_conn()
    n = conn.execute("SELECT COUNT(*) FROM predictions WHERE match_id=?",
                     (SX,)).fetchone()[0]
    conn.close()
    assert n == 0


def test_scan_requires_all_three_legs(temp_db, monkeypatch):
    """Senza il mercato X (solo 2 legs su 3) non si deviga: nessun segnale."""
    monkeypatch.setattr(sx_signals, "expected_goals",
                        lambda h, a: (1.6, 1.1))
    monkeypatch.setattr(sx_signals, "prob_1x2",
                        lambda lh, la: (0.45, 0.28, 0.27))
    raw = [m for m in _raw_markets()
           if (m.get("outcomeOneName") or "").lower() != "tie"]
    saved = sx_signals.scan(provider=FakeSxProvider(raw))
    assert saved == []


def test_settle_sx_bets_fail_closed(temp_db, no_settlement_sources):
    """Senza fonti risultati le bet SX restano aperte (nessun verdetto)."""
    tracker.save_match(SX, "Serie A", "Alpha", "Beta",
                       "2026-09-09T19:00:00Z")
    tracker.save_bet(SX, "1X2", "1", "0xabc", 1, 2.5, 1.0)
    res = sx_signals.settle_sx_bets()
    assert res["open"] == 1 and res["settled"] == 0
    assert res["source"] is None
    conn = tracker._get_conn()
    outcome = conn.execute("SELECT esito_finale FROM bets WHERE match_id=?",
                           (SX,)).fetchone()[0]
    conn.close()
    assert outcome is None


def test_settle_sx_bets_with_result(temp_db, no_settlement_sources):
    """Con il punteggio gia' in match_results la bet viene saldata."""
    tracker.save_match(SX, "Serie A", "Alpha", "Beta",
                       "2026-09-09T19:00:00Z")
    tracker.save_bet(SX, "1X2", "1", "0xabc", 1, 2.5, 1.0)
    tracker.save_result(SX, "Serie A", "Alpha", "Beta", 2, 0,
                        datetime.now(timezone.utc).isoformat())
    res = sx_signals.settle_sx_bets()
    assert res["settled"] == 1
    conn = tracker._get_conn()
    outcome, profit = conn.execute(
        "SELECT esito_finale, profit FROM bets WHERE match_id=?",
        (SX,)).fetchone()
    conn.close()
    assert outcome == "won"
    assert profit == pytest.approx(1.0 * (2.5 - 1))
