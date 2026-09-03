"""Test del flusso di puntate automatiche (auto_bet.run_today_bets).

Dal 04/09 auto_bet è SIM-only permanente: nessun client Exchange, nessun
catalogo di scansione — le puntate sono simulate con la quota del segnale
(mode='sim') e registrate in `bets` per ledger/ML.
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


def _seed_value_match(mid="m1", home="Osasuna", away="Getafe", esito="1",
                      quota=2.10, status="value", commence=None):
    start = commence or (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    tracker.save_match(mid, "Serie A", home, away, start)
    # esito "1" -> best_esito = nome squadra di casa (come da API bookmaker)
    best_esito = home if esito == "1" else (away if esito == "2" else "Draw")
    tracker.save_analysis(mid, 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                          best_esito, quota, "Pinnacle", status,
                          market_prob=0.45, market_edge=0.07)


def test_sim_piazzata_con_quota_segnale(monkeypatch, temp_db):
    """SIM-only: puntata simulata con la quota del segnale, mode='sim'."""
    monkeypatch.setitem(sys.modules, "adaptive_staking", None)
    _seed_value_match(quota=2.20)
    placed = auto_bet.run_today_bets(stake_eur=5.0)
    assert len(placed) == 1
    p = placed[0]
    assert p["esito_key"] == "1" and p["price"] == 2.20 and p["stake"] == 5.0
    assert p["mode"] == "sim" and p["status"] == "SUCCESS"
    # registrata nel DB
    bets = tracker.get_bets()
    assert len(bets) == 1
    assert bets[0]["mode"] == "sim"
    assert tracker.bet_exists_open("m1", "1") is True


def test_no_duplicate_bet_on_rerun(monkeypatch, temp_db):
    _seed_value_match()
    auto_bet.run_today_bets(stake_eur=5.0)
    placed2 = auto_bet.run_today_bets(stake_eur=5.0)
    assert placed2 == []           # gia' aperta
    assert len(tracker.get_bets()) == 1


def test_skips_near_start(monkeypatch, temp_db):
    iniziata = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _seed_value_match(quota=2.20, commence=iniziata)
    placed = auto_bet.run_today_bets(stake_eur=5.0)
    assert placed == []


def test_skips_quota_non_valida(monkeypatch, temp_db):
    _seed_value_match(quota=1.0)
    placed = auto_bet.run_today_bets(stake_eur=5.0)
    assert placed == []


def test_senza_segnali_nessuna_puntata(monkeypatch, temp_db):
    placed = auto_bet.run_today_bets(stake_eur=5.0)
    assert placed == []
    assert tracker.get_bets() == []


def test_normalizes_stake_below_minimum(monkeypatch, temp_db):
    # Guardia minimo Exchange sul percorso a stake fisso (adaptive disattivo)
    monkeypatch.setitem(sys.modules, "adaptive_staking", None)
    _seed_value_match()
    # stake 1.00 -> sotto il minimo: nessuna puntata
    placed = auto_bet.run_today_bets(stake_eur=1.0)
    assert placed == []


def test_auto_bet_usa_stake_adaptivo(monkeypatch, temp_db):
    """Con adaptive_staking disponibile lo stake arriva dal modulo adattivo
    (Kelly dinamico), non da stake_eur (solo fallback)."""
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
    _seed_value_match(quota=2.20)
    placed = auto_bet.run_today_bets(stake_eur=5.0)
    assert len(placed) == 1
    assert placed[0]["stake"] == 7.5            # stake adattivo, non 5.0
    assert calls["odds"] == 2.20                # quota del segnale


class TestCorrelationCap:
    """Correlation risk cap: gli stake di esiti correlati (stessa partita o
    stessa lega+finestra) vengono ridotti se l'esposizione del blocco supera
    il cap, per proteggere il bankroll dalla varianza condivisa."""

    def _cand(self, mid, stake, commence=None, league="Serie A", **kw):
        c = {"match_id": mid, "league": league,
             "commence": commence, "stake": stake, "esito_key": "1"}
        c.update(kw)
        return c

    def test_senza_correlazione_nessun_taglio(self):
        # Leghe diverse e kickoff lontani: nessun cap
        cands = [
            self._cand("a", 5.0, "2026-09-03T14:00:00Z", league="Serie A"),
            self._cand("b", 5.0, "2026-09-03T14:05:00Z", league="Premier League"),
            self._cand("c", 5.0, "2026-09-03T20:00:00Z", league="Serie A"),
        ]
        out = auto_bet.apply_correlation_cap(cands, bankroll=100.0)
        assert all(not c.get("corr_cap") for c in out)
        assert [c["stake"] for c in out] == [5.0, 5.0, 5.0]

    def test_stessa_lega_finestra_scala_gli_stake(self):
        # 4 esiti Serie A con kickoff ravvicinati: 4*5=20 > cap 30% di 50=15
        start = "2026-09-03T14:00:00Z"
        cands = [self._cand(f"m{i}", 5.0, start, league="Serie A")
                 for i in range(4)]
        out = auto_bet.apply_correlation_cap(cands, bankroll=50.0)
        tot = sum(c["stake"] for c in out)
        assert tot <= 15.0 + 0.01
        assert all(c.get("corr_cap") for c in out)
        # Scaling proporzionale: tutti ridotti dello stesso fattore
        assert out[0]["stake"] == out[1]["stake"]

    def test_stessa_partita_sempre_correlata(self):
        # 1X2 + Over sulla stessa partita (match_id uguale): correlati
        cands = [
            self._cand("x", 5.0, "2026-09-03T14:00:00Z", league="Serie A",
                       esito_key="1"),
            self._cand("x", 5.0, "2026-09-03T14:00:00Z", league="Serie A",
                       esito_key="Over 2.5"),
        ]
        out = auto_bet.apply_correlation_cap(cands, bankroll=20.0)
        assert sum(c["stake"] for c in out) <= 6.0 + 0.01  # 30% di 20

    def test_blocchi_temporali_disgiunti_niente_cap(self):
        # Stessa lega ma kickoff a 3h di distanza: indipendenti
        cands = [
            self._cand("a", 5.0, "2026-09-03T14:00:00Z", league="Serie A"),
            self._cand("b", 5.0, "2026-09-03T17:00:00Z", league="Serie A"),
        ]
        out = auto_bet.apply_correlation_cap(cands, bankroll=20.0)
        assert not any(c.get("corr_cap") for c in out)

    def test_cap_rispetta_il_ranking_ev(self):
        # Stake diversi: scaling proporzionale (l'esito a stake piu' alto
        # resta quello piu' alto)
        cands = [
            self._cand("a", 8.0, "2026-09-03T14:00:00Z", league="Serie A"),
            self._cand("b", 4.0, "2026-09-03T14:05:00Z", league="Serie A"),
            self._cand("c", 4.0, "2026-09-03T14:10:00Z", league="Serie A"),
        ]
        out = auto_bet.apply_correlation_cap(cands, bankroll=40.0)
        assert out[0]["stake"] > out[1]["stake"]  # ranking preservato
        assert sum(c["stake"] for c in out) <= 12.0 + 0.01

    def test_run_today_bets_sim_usa_il_cap(self, monkeypatch, temp_db):
        """Flusso completo SIM: 4 value della stessa lega ravvicinate -> gli
        stake passano dal correlation cap prima di essere salvati."""
        monkeypatch.setitem(sys.modules, "adaptive_staking", None)
        start = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()\
            .replace("+00:00", "Z")
        for i in range(4):
            _seed_value_match(mid=f"sim{i}", home=f"Home{i}", away=f"Away{i}",
                              esito="1", quota=2.10, status="value",
                              commence=start)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 4
        assert all(p["mode"] == "sim" for p in placed)
        assert all(p["stake"] > 0 for p in placed)


class TestTotalExposureCap:
    """Cap di portafoglio: l'esposizione TOTALE del giorno non deve superare
    il 40% del bankroll (varianza additiva tra pick indipendenti)."""

    def _cand(self, mid, stake, league="Serie A"):
        return {"match_id": mid, "league": league, "stake": stake,
                "commence": "2026-09-03T14:00:00Z", "esito_key": "1"}

    def test_sotto_il_cap_nessun_taglio(self):
        cands = [self._cand("a", 3.0), self._cand("b", 3.0)]  # 6 <= 40% di 100
        out = auto_bet.apply_total_exposure_cap(cands, bankroll=100.0)
        assert all(not c.get("total_cap") for c in out)
        assert [c["stake"] for c in out] == [3.0, 3.0]

    def test_sopra_il_cap_scala_proporzionalmente(self):
        # 5*5=25 > 40% di 50=20 -> factor 0.8, ranking preservato
        cands = [self._cand(f"m{i}", 5.0, league=f"Lega{i}") for i in range(5)]
        out = auto_bet.apply_total_exposure_cap(cands, bankroll=50.0)
        tot = sum(c["stake"] for c in out)
        assert tot <= 20.0 + 0.01
        assert all(c.get("total_cap") for c in out)
        assert out[0]["stake"] == out[1]["stake"]  # proporzionale

    def test_no_op_con_pochi_candidati(self):
        cands = [self._cand("a", 5.0)]
        out = auto_bet.apply_total_exposure_cap(cands, bankroll=100.0)
        assert out == cands and not cands[0].get("total_cap")

    def test_flusso_sim_applica_il_cap_totale(self, monkeypatch, temp_db):
        """9 value in 9 leghe diverse (nessuna correlazione): stake fisso 5
        -> totale 45 > 40% di 100 -> gli stake vengono ridotti dal cap
        esposizione totale prima del salvataggio."""
        monkeypatch.setitem(sys.modules, "adaptive_staking", None)
        start = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()\
            .replace("+00:00", "Z")
        for i in range(9):
            _seed_value_match(mid=f"tc{i}", home=f"Home{i}", away=f"Away{i}",
                              esito="1", quota=2.10, status="value",
                              commence=start)
        # Leghe diverse per match: evita il correlation cap (che agirebbe
        # prima) e isola il cap TOTALE. Il seed usa sempre "Serie A": lo
        # forzo via DB dopo il seed.
        conn = tracker._get_conn()
        conn.execute("UPDATE matches SET league = 'Lega' || rowid WHERE id LIKE 'tc%'")
        conn.commit()
        conn.close()
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 9
        tot = sum(p["stake"] for p in placed)
        assert tot <= 40.0 + 0.01  # 40% di 100
        assert all(p["stake"] > 0 for p in placed)


class TestClvWiring:
    """Il CLV storico (battere la closing line) deve arrivare ad
    adaptive_stake come has_clv_positive: edge confermato = stake piu' alto."""

    def _run_con_stub(self, monkeypatch, temp_db, avg_clv):
        import adaptive_staking
        calls = {}

        def _fake_adaptive(**kw):
            calls.update(kw)
            return {"stake": 4.0, "kelly_fraction": 0.2,
                    "confidence_score": 0.5, "drawdown_factor": 1.0,
                    "raw_stake": 4.0, "capped": False, "reason": "test"}

        monkeypatch.setattr(adaptive_staking, "adaptive_stake", _fake_adaptive)
        monkeypatch.setattr(adaptive_staking, "bankroll_stats",
                            lambda: {"current": 100.0, "peak": 100.0})
        _seed_value_match()
        auto_bet.run_today_bets(stake_eur=5.0)
        return calls

    def test_passa_has_clv_positive(self, monkeypatch, temp_db):
        """has_clv_positive arriva ad adaptive_stake (bool) anche senza
        storico CLV (None -> False, mai assente)."""
        calls = self._run_con_stub(monkeypatch, temp_db, avg_clv=0.0)
        assert "has_clv_positive" in calls
        assert calls["has_clv_positive"] is False

    def test_clv_positivo_vero(self, monkeypatch, temp_db):
        """Con una riga clv_history a CLV positivo (signal_quota >
        closing_quota) il flag passa True."""
        conn = tracker._get_conn()
        conn.execute(
            "INSERT INTO clv_history (match_id, esito, signal_quota, "
            "closing_quota, updated_at) VALUES ('m1','1', 2.20, 2.00, ?)",
            (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
        conn.close()
        calls = self._run_con_stub(monkeypatch, temp_db, avg_clv=0.1)
        assert calls["has_clv_positive"] is True