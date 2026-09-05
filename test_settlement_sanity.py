"""Test SANITY CHECK settlement: blocco dei verdetti in contraddizione coi gol.

Tripwire del bug 02/09 (punteggi invertiti da the-odds-api): una bet
sull'esito "2" non deve MAI essere chiusa come VINTA quando i gol
registrati dicono vittoria della squadra di casa. Copre:

  1. _goals_sane: gol negativi/non numerici/result incoerente → non saldare;
  2. _esito_possible: esito '2' con vittoria casa → False (contraddizione);
  3. il BLOCCAGGIO nei tre settle (bets, predictions, cassa): la riga resta
     aperta e viene loggata, mai chiusa col verdetto sbagliato;
  4. settlement_sanity_check + heal_settled_contradictions: rileva e
     ri-salda automaticamente le righe GIÀ chiuse col verdetto specchiato
     (match_results corretto dopo la chiusura, es. dal watchdog);
  5. il flusso reale _update_results (via bot) che auto-corregge.
"""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tracker
import odds_api


def _started_iso(days_ago: int = 1, hours_ago: int = 2) -> str:
    """commence_time RECENTE: la refertazione mirata (get_leagues_with_open_rows)
    interroga solo partite già iniziate di recente — le date hardcoded
    escono dalla finestra e il flusso _update_results non parte mai."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago,
                                                   hours=hours_ago)).isoformat()
import bot


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        monkeypatch.setattr(odds_api, "CACHE_DIR", Path(td))
        tracker.init_db()
        yield db_path


# --- 1. _goals_sane ---------------------------------------------------------

class TestGoalsSane:
    def test_gol_validi(self):
        assert tracker._goals_sane(2, 1) is True
        assert tracker._goals_sane(0, 0) is True

    def test_negativi_bloccati(self):
        assert tracker._goals_sane(-1, 2) is False
        assert tracker._goals_sane(2, -1) is False

    def test_non_numerici_bloccati(self):
        assert tracker._goals_sane(None, 2) is False
        assert tracker._goals_sane("x", 2) is False

    def test_result_incoerente_bloccato(self):
        # result='1' (casa vince) ma gol 1-2: riga corrotta
        assert tracker._goals_sane(1, 2, result="1") is False
        assert tracker._goals_sane(2, 1, result="1") is True
        assert tracker._goals_sane(1, 1, result="X") is True


# --- 2. _esito_possible -----------------------------------------------------

class TestEsitoPossible:
    def test_esito_2_con_vittoria_casa_impossibile(self):
        """REGRESSION: esito '2' con vittoria casa (2-1) → False."""
        assert tracker._esito_possible("1X2", "2", 2, 1,
                                       "Machida", "Kawasaki") is False

    def test_esito_2_con_vittoria_trasferta_possibile(self):
        assert tracker._esito_possible("1X2", "2", 1, 2,
                                       "Machida", "Kawasaki") is True

    def test_esito_1_con_vittoria_casa_possibile(self):
        assert tracker._esito_possible("1X2", "1", 2, 1,
                                       "Machida", "Kawasaki") is True

    def test_esito_1_con_vittoria_trasferta_impossibile(self):
        assert tracker._esito_possible("1X2", "1", 1, 2,
                                       "Machida", "Kawasaki") is False

    def test_draw_richiede_pareggio(self):
        assert tracker._esito_possible("1X2", "X", 1, 1,
                                       "Machida", "Kawasaki") is True
        assert tracker._esito_possible("1X2", "X", 2, 1,
                                       "Machida", "Kawasaki") is False

    def test_nome_squadra_casa(self):
        assert tracker._esito_possible("1X2", "Machida", 2, 1,
                                       "Machida", "Kawasaki") is True
        assert tracker._esito_possible("1X2", "Machida", 1, 2,
                                       "Machida", "Kawasaki") is False

    def test_over_under(self):
        assert tracker._esito_possible(None, "Over 2.5", 2, 1) is True
        assert tracker._esito_possible(None, "Over 2.5", 1, 1) is False
        assert tracker._esito_possible(None, "Under 2.5", 1, 1) is True
        assert tracker._esito_possible(None, "Under 2.5", 2, 1) is False

    def test_ah_non_verificabile(self):
        assert tracker._esito_possible("AH", "Home -0.75", 2, 1) is None


# --- 3. Bloccaggio nei settle -----------------------------------------------

def _seed_match_scores(temp_db, mid="m1", sh=2, sa=1, result="1",
                       home="FC Machida Zelvia", away="Kawasaki Frontale"):
    tracker.save_match(mid, "J1 League", home, away, "2026-09-02T05:00:00Z")
    tracker.save_result(mid, "J1 League", home, away, sh, sa, "2026-09-02T13:53:49Z")


class TestSettleBlocked:
    def test_bet_sul_2_con_vittoria_casa_resta_aperta(self, temp_db):
        """Se i gol dicono casa e la bet è sul 2, NON chiudere: il verdetto
        'won' sarebbe specchiato. Qui l'esito calcolato è 'lost' (legittimo),
        quindi la bet VA chiusa come persa: il blocco scatta solo quando il
        verdetto CALCOLATO sarebbe 'won' coi gol contro (regressione)."""
        _seed_match_scores(temp_db)
        tracker.save_bet("m1", "1X2", "2", None, None, 4.0, 5.0)
        tracker.settle_bets()
        rows = tracker.get_bets(limit=10)
        assert rows[0]["esito_finale"] == "lost"  # vittoria casa → 2 persa
        assert rows[0]["profit"] == pytest.approx(-5.0)

    def _seed_bad_goals(self, temp_db, sh=-1, sa=2):
        """match_results con gol corrotti, inseriti via SQL diretto
        (save_result rifiuterebbe i non numerici)."""
        conn = tracker._get_conn()
        tracker._create_results_table(conn)
        conn.execute("INSERT OR REPLACE INTO match_results "
                     "(match_id, league, home_team, away_team, score_home, "
                     "score_away, result, settled_at) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     ("m1", "J1 League", "FC Machida Zelvia",
                      "Kawasaki Frontale", sh, sa, "2",
                      "2026-09-02T13:53:49Z"))
        conn.commit(); conn.close()

    def test_gol_non_validi_bloccano_la_chiusura(self, temp_db):
        """Punteggio negativo: nessuna chiusura, la bet resta APERTA."""
        tracker.save_match("m1", "J1 League", "FC Machida Zelvia",
                           "Kawasaki Frontale", "2026-09-02T05:00:00Z")
        self._seed_bad_goals(temp_db, sh=-1, sa=2)
        tracker.save_bet("m1", "1X2", "2", None, None, 4.0, 5.0)
        tracker.settle_bets()
        rows = tracker.get_bets(limit=10)
        assert rows[0]["esito_finale"] is None  # bloccata dal sanity check

    def test_gol_non_numerici_bloccano_prediction(self, temp_db):
        tracker.save_match("m1", "J1 League", "FC Machida Zelvia",
                           "Kawasaki Frontale", "2026-09-02T05:00:00Z")
        self._seed_bad_goals(temp_db, sh=None, sa=2)
        tracker.save_prediction("m1", "1X2", "2", 4.0, 0.25, 0.14,
                                status="value")
        tracker.settle_predictions()
        conn = tracker._get_conn()
        row = conn.execute("SELECT esito_finale FROM predictions "
                           "WHERE match_id='m1'").fetchone()
        conn.close()
        assert row[0] is None

    def test_cassa_gol_non_validi_resta_in_gioco(self, temp_db):
        tracker.save_match("m1", "J1 League", "FC Machida Zelvia",
                           "Kawasaki Frontale", "2026-09-02T05:00:00Z")
        self._seed_bad_goals(temp_db, sh=None, sa=2)
        tracker.save_cassa_entry("FC Machida Zelvia vs Kawasaki Frontale",
                                 "2", 4.0, 5.0)
        tracker.settle_cassa()
        conn = tracker._get_conn()
        row = conn.execute("SELECT esito_finale FROM cassa WHERE id=1").fetchone()
        conn.close()
        assert row[0] is None

    def test_cassa_preferisce_partita_recente_su_coppia_duplicata(self, temp_db):
        """Cassa su coppia con DUE partite (2025 + 2026): si salda sulla
        PIÙ RECENTE, non sulla riga storica (bug Osasuna: Over 2.5 pagato
        vinto su un 1-2 del 2025 mentre il match corrente finiva 1-0)."""
        # Partita VECCHIA: Osasuna 1-2 Getafe (3 gol → Over vinto)
        tracker.save_match("old", "La Liga", "Osasuna", "Getafe",
                           "2025-03-16T17:30:00+00:00")
        tracker.save_result("old", "La Liga", "Osasuna", "Getafe",
                            1, 2, "2025-03-16T17:30:00+00:00")
        # Partita CORRENTE: CA Osasuna 1-0 Getafe (1 gol → Over perso)
        tracker.save_match("new", "La Liga", "CA Osasuna", "Getafe",
                           "2026-08-31T19:00:00Z")
        tracker.save_result("new", "La Liga", "CA Osasuna", "Getafe",
                            1, 0, "2026-08-31T19:59:59Z")
        tracker.save_cassa_entry("Osasuna vs Getafe", "Over 2.5", 3.05, 20.0)
        tracker.settle_cassa()
        conn = tracker._get_conn()
        row = conn.execute("SELECT esito_finale, profit FROM cassa WHERE id=1").fetchone()
        conn.close()
        assert row[0] == "lost"      # 1 gol → Under 2.5 → persa
        assert row[1] == pytest.approx(-20.0)


# --- 4. sanity_check + heal -------------------------------------------------

def _seed_mirrored_settlement(temp_db):
    """Stato produzione del bug: bet sul 2 chiusa 'won' +15 su una vittoria
    casa (match_results GIÀ corretto a 2-1 dal watchdog, ledger specchiato)."""
    tracker.save_match("m1", "J1 League", "FC Machida Zelvia",
                       "Kawasaki Frontale", "2026-09-02T05:00:00Z")
    tracker.save_analysis("m1", 1.2, 1.6, 0.28, 0.26, 0.46, 0.52,
                          0.14, "2", 4.0, "Pinnacle", "value")
    tracker.save_result("m1", "J1 League", "FC Machida Zelvia",
                        "Kawasaki Frontale", 2, 1, "2026-09-02T20:10:00Z")
    tracker.save_bet("m1", "1X2", "2", None, None, 4.0, 5.0)
    tracker.save_prediction("m1", "1X2", "2", 4.0, 0.25, 0.14, status="value")
    conn = tracker._get_conn()
    conn.execute("UPDATE bets SET esito_finale='won', profit=15.0, "
                 "settled_at=? WHERE match_id='m1'",
                 ("2026-09-02T13:53:56Z",))
    conn.execute("UPDATE predictions SET esito_finale='won', profit=3.0, "
                 "settled_at=? WHERE match_id='m1'",
                 ("2026-09-02T13:53:56Z",))
    conn.commit(); conn.close()


class TestSanityCheckAndHeal:
    def test_check_rileva_verdetto_specchiato(self, temp_db):
        _seed_mirrored_settlement(temp_db)
        contrad = tracker.settlement_sanity_check()
        assert len(contrad) == 2
        kinds = {c["table"] for c in contrad}
        assert kinds == {"bets", "predictions"}
        for c in contrad:
            assert c["stored"] == "won"
            assert c["expected"] == "lost"

    def test_check_pulito_senza_contraddizioni(self, temp_db):
        _seed_match_scores(temp_db)
        tracker.save_bet("m1", "1X2", "1", None, None, 2.5, 5.0)
        tracker.settle_bets()
        assert tracker.settlement_sanity_check() == []

    def test_heal_ri_salda_bet_da_won_a_lost(self, temp_db):
        _seed_mirrored_settlement(temp_db)
        contrad = tracker.settlement_sanity_check()
        assert tracker.heal_settled_contradictions(contrad) == 2
        rows = {r["esito"]: r for r in tracker.get_bets(limit=10)}
        assert rows["2"]["esito_finale"] == "lost"
        assert rows["2"]["profit"] == pytest.approx(-5.0)
        conn = tracker._get_conn()
        row = conn.execute("SELECT esito_finale, profit FROM predictions "
                           "WHERE match_id='m1' AND esito='2'").fetchone()
        conn.close()
        assert row[0] == "lost"
        assert row[1] == pytest.approx(-1.0)
        # Dopo l'heal non ci sono più contraddizioni
        assert tracker.settlement_sanity_check() == []


# --- 5. Flusso reale _update_results ----------------------------------------

def _patch_send(monkeypatch):
    calls = []

    async def fake_send(context, text):
        calls.append(text)

    monkeypatch.setattr(bot, "_send_bet_settlements", fake_send)
    return calls


def _patch_fetch_scores(monkeypatch, payload_per_sport):
    def fake_fetch(sport=None, days_from=2):
        return payload_per_sport.get(sport, [])

    monkeypatch.setattr(odds_api, "fetch_scores", fake_fetch)
    monkeypatch.setitem(sys.modules, "odds_api", odds_api)


def _patch_ratings(monkeypatch):
    import rating_engine
    monkeypatch.setattr(rating_engine, "compute_ratings", lambda: None)
    monkeypatch.setitem(sys.modules, "rating_engine", rating_engine)


def _patch_admin(monkeypatch):
    monkeypatch.setattr(bot, "_admin_chat_ids", lambda: [])
    monkeypatch.setattr(tracker, "get_subscribers", lambda *a, **k: [])


AWAY_FIRST = {"id": "m1", "home_team": "FC Machida Zelvia",
              "away_team": "Kawasaki Frontale",
              "scores": [{"name": "Kawasaki Frontale", "score": 0},
                         {"name": "FC Machida Zelvia", "score": 1}],
              "last_update": ""}


class TestUpdateResultsSanity:
    def test_update_results_auto_corregge_verdetto_specchiato(self, temp_db,
                                                              monkeypatch):
        """Flusso REALE: match_results col punteggio INVERTITO (1-2, il bug),
        bet sul 2 chiusa won. Il job scarica i punteggi veri (away-first,
        casa 2-1) → match_results corretto → sanity check rileva la
        contraddizione e ri-salda la bet a lost."""
        tracker.save_match("m1", "J1 League", "FC Machida Zelvia",
                           "Kawasaki Frontale", _started_iso())
        tracker.save_analysis("m1", 1.2, 1.6, 0.28, 0.26, 0.46, 0.52,
                              0.14, "2", 4.0, "Pinnacle", "value")
        # Stato pre-fix: punteggio INVERTITO salvato (vittoria trasferta)
        tracker.save_result("m1", "J1 League", "FC Machida Zelvia",
                            "Kawasaki Frontale", 1, 2,
                            (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
        tracker.save_bet("m1", "1X2", "2", None, None, 4.0, 5.0)
        tracker.settle_bets()
        rows = tracker.get_bets(limit=10)
        assert rows[0]["esito_finale"] == "won"   # chiusa col punteggio sbagliato

        _patch_send(monkeypatch)
        _patch_fetch_scores(monkeypatch, {"soccer_japan_j_league": [dict(AWAY_FIRST)]})
        _patch_ratings(monkeypatch)
        _patch_admin(monkeypatch)

        updated, stats, settlements, sanity = bot._update_results()

        # Il job ha corretto il punteggio e ri-sal dato la bet
        rows = tracker.get_bets(limit=10)
        assert rows[0]["esito_finale"] == "lost"
        assert rows[0]["profit"] == pytest.approx(-5.0)
        assert updated >= 1
        # L'alert di sanity check è stato prodotto
        assert any("SANITY CHECK" in a for a in sanity)
        assert tracker.settlement_sanity_check() == []

    def test_update_results_silenzioso_senza_contraddizioni(self, temp_db,
                                                            monkeypatch):
        _patch_send(monkeypatch)
        _patch_fetch_scores(monkeypatch, {})
        _patch_ratings(monkeypatch)
        _patch_admin(monkeypatch)
        updated, stats, settlements, sanity = bot._update_results()
        assert sanity == []


if __name__ == "__main__":
    import pytest as _p
    _p.main([__file__, "-v"])