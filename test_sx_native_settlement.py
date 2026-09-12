"""Test del settlement NATIVO SX (12/09/2026).

Perche': le leghe che SX scansiona ma the-odds-api NON copre (Primera A
Colombia, Primera Nacional Argentina, K2-League) lasciavano bet e
previsioni aperte per sempre — nessuna fonte esterna puo' refertarle.
Ma l'esito lo conosce SX stesso: `markets/find` (market_hash salvato sulla
bet) ritorna outcome + punteggi del mercato saldato, e `/markets/active`
esponde i punteggi live degli eventi in corso. Entrambi GRATUITI e senza
matching per nome (la causa principale delle bet rimaste aperte il 12/09).

Semantica SX: `outcome` e' relativo alla GAMBA del mercato binario
(1 = vince outcomeOne, 2 = vince outcomeTwo, 0 = void), mentre i punteggi
teamOneScore/teamTwoScore sono SEMPRE quelli dell'evento (casa vs trasferta):
il verdetto 1X2 lo emette comunque settle_bets/settle_predictions dai
punteggi (fail-closed), non dall'outcome della gamba.
"""
import time
from datetime import datetime, timezone

import pytest

import sx_signals
import tracker
from execution_engine import SX_PROB_SCALE


def _pct(price: float) -> int:
    return int(SX_PROB_SCALE / price)


NOW_S = int(time.time())


def _find_market(market_hash, outcome=1, sh=2, sa=0, home="Alpha",
                 away="Beta", league="Primera A"):
    """Mercato saldato come lo ritorna markets/find (camposet reale)."""
    return {"marketHash": market_hash, "type": 1, "status": "ACTIVE",
            "outcome": outcome, "teamOneScore": sh, "teamTwoScore": sa,
            "teamOneName": home, "teamTwoName": away,
            "outcomeOneName": home, "outcomeTwoName": f"Not {home}",
            "leagueLabel": league, "sportXeventId": "LEV1",
            "gameTime": NOW_S - 7200}


def _active_market(ev_id="LEV2", ko_s=None, sh=1, sa=1, home="Gamma",
                   away="Delta", league="K2-League"):
    return {"marketHash": "0xact" + ev_id, "type": 1,
            "sportXeventId": ev_id, "gameTime": ko_s or (NOW_S - 3600),
            "teamOneName": home, "teamTwoName": away,
            "outcomeOneName": home, "outcomeTwoName": f"Not {home}",
            "leagueLabel": league, "teamOneScore": sh, "teamTwoScore": sa}


class FakeSxSettle:
    """Provider SX canned per il settlement (nessuna rete)."""

    def __init__(self, find_data=None, active_data=None):
        self.find_data = find_data or []
        self.active_data = active_data or []
        self.find_calls = []
        self.active_calls = 0

    def _get(self, path, params=None):
        if path == "markets/find":
            self.find_calls.append(params)
            return {"status": "success", "data": list(self.find_data)}
        if path == "markets/active":
            self.active_calls += 1
            return {"status": "success",
                    "data": {"markets": list(self.active_data),
                             "nextKey": None}}
        raise AssertionError(f"endpoint inatteso: {path}")


@pytest.fixture()
def temp_db(monkeypatch):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture(autouse=True)
def sources_on(monkeypatch):
    """Fonti punteggi configurate: il percorso SX nativo e' attivo."""
    monkeypatch.setenv("ODDS_API_KEY", "test")
    monkeypatch.setenv("API_FOOTBALL_KEY", "")


def _bet_outcome(mid):
    conn = tracker._get_conn()
    row = conn.execute("SELECT esito_finale, profit FROM bets WHERE match_id=?",
                       (mid,)).fetchone()
    conn.close()
    return row


def _pred_outcome(mid):
    conn = tracker._get_conn()
    row = conn.execute(
        "SELECT esito_finale, profit FROM predictions WHERE match_id=?",
        (mid,)).fetchone()
    conn.close()
    return row


class TestFindPath:
    """Percorso (1): market_hash delle bet -> markets/find."""

    def test_bet_sx_saldata_dal_mercato_sx(self, temp_db, monkeypatch):
        tracker.save_match("sx-LEV1", "Primera A", "Alpha", "Beta",
                           "2026-09-11T23:15:00Z")
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.3324, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xh1", outcome=1,
                                                    sh=2, sa=2)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["source"] == "sx" and res["results"] == 1
        assert res["settled"] == 1
        # score 2-2: esito 1 -> lost (i punteggi dell'evento decidono,
        # NON l'outcome della gamba)
        assert _bet_outcome("sx-LEV1") == ("lost", -1.0)

    def test_bet_orfana_senza_riga_matches(self, temp_db, monkeypatch):
        """Bet #21 (senza riga in matches): si salda lo stesso, nomi dalla
        risposta SX."""
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 3.252, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xh1", outcome=1,
                                                    sh=2, sa=1)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["settled"] == 1
        assert _bet_outcome("sx-LEV1")[0] == "won"
        assert _bet_outcome("sx-LEV1")[1] == pytest.approx(2.25)  # round(,2)

    def test_hash_batchati_in_una_sola_chiamata(self, temp_db):
        for i in (1, 2):
            tracker.save_bet(f"sx-LEV{i}", "1X2", "1", f"0xh{i}", 1, 2.0,
                             1.0, mode="live")
        find = [_find_market(f"0xh{i}", sh=1, sa=0) for i in (1, 2)]
        prov = FakeSxSettle(find_data=find)
        sx_signals.settle_sx_bets(provider=prov)
        assert len(prov.find_calls) == 1
        sent = prov.find_calls[0]["marketHashes"]
        assert sent == "0xh1,0xh2"

    def test_punteggi_mancanti_fail_closed(self, temp_db):
        """Nomi o punteggi assenti dalla find: NESSUN risultato salvato."""
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[
            {"marketHash": "0xh1", "type": 1, "outcome": 1,
             "teamOneName": "Alpha", "teamTwoName": "Beta"}])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 0 and res["settled"] == 0
        assert _bet_outcome("sx-LEV1") == (None, None)

    def test_hash_sconosciuti_ignorati(self, temp_db):
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xALTRO", sh=9, sa=9)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 0 and res["settled"] == 0

    def test_senza_fonti_nessuna_rete(self, temp_db, monkeypatch):
        """Chiavi assenti (fail-closed offline): il percorso SX non viene
        nemmeno interrogato (stessa logica delle fonti esterne)."""
        monkeypatch.setenv("ODDS_API_KEY", "")
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xh1")])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert prov.find_calls == [] and prov.active_calls == 0
        assert res["settled"] == 0 and res["source"] is None

    def test_gate_env_sx_native_settlement(self, temp_db, monkeypatch):
        """SX_NATIVE_SETTLEMENT=0: percorso SX disattivabile da env."""
        monkeypatch.setenv("SX_NATIVE_SETTLEMENT", "0")
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xh1")])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert prov.find_calls == []
        assert res["settled"] == 0 and res["source"] is None


class TestActivePath:
    """Percorso (2): match con riga nel ledger -> punteggi live su active."""

    def test_previsione_senza_bet_saldata(self, temp_db, monkeypatch):
        """Match con SOLE previsioni (niente market_id sul ledger): il
        percorso find non lo vede, lo copre active (punteggi live)."""
        monkeypatch.setattr("odds_api.fetch_scores",
                            lambda sport=None, days_from=3: (
                                (_ for _ in ()).throw(AssertionError(
                                    "no crediti")))
                            )
        tracker.save_match("sx-LEV2", "K2-League", "Gamma", "Delta",
                           "2026-09-12T15:00:00Z")
        tracker.save_prediction("sx-LEV2", "1X2", "X", 3.5, 0.30, 0.05)
        prov = FakeSxSettle(active_data=[
            _active_market(ev_id="LEV2", ko_s=NOW_S - 3 * 3600, sh=1, sa=1)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 1 and res["predictions"] == 1
        assert _pred_outcome("sx-LEV2") == ("won", 1.0 * (3.5 - 1))

    def test_match_futuro_non_saldata(self, temp_db, monkeypatch):
        """Kickoff futuro su active: nessun punteggio salvato (finestra
        live -90')."""
        tracker.save_match("sx-LEV3", "Primera A", "Alpha", "Beta",
                           "2026-09-13T15:00:00Z")
        tracker.save_prediction("sx-LEV3", "1X2", "1", 1.7, 0.60, 0.04)
        prov = FakeSxSettle(active_data=[
            _active_market(ev_id="LEV3", ko_s=NOW_S + 3600, sh=0, sa=0)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 0 and res["predictions"] == 0

    def test_esterno_solo_per_match_senza_risultato_sx(self, temp_db,
                                                       monkeypatch):
        """Con il risultato sx-* gia' salvato il match NON passa alle fonti
        esterne (zero crediti): il filtro `missing` esclude i coperti."""
        tracker.save_match("sx-LEV4", "Primera A", "Alpha", "Beta",
                           "2026-09-11T23:15:00Z")
        tracker.save_prediction("sx-LEV4", "1X2", "1", 2.0, 0.55, 0.05)

        def _boom(*a, **k):
            raise AssertionError("fetch_scores non deve essere chiamata")

        monkeypatch.setattr("odds_api.fetch_scores", _boom)
        prov = FakeSxSettle(active_data=[
            _active_market(ev_id="LEV4", ko_s=NOW_S - 3 * 3600, sh=2, sa=0)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 1 and res["predictions"] == 1
        assert res["source"] == "sx"

    def test_idempotenza_giro_doppio(self, temp_db, monkeypatch):
        """Secondo giro: i punteggi gia' salvati non vengono riscritti e le
        righe chiuse restano chiuse (INSERT OR REPLACE + esito_finale)."""
        tracker.save_match("sx-LEV5", "Primera A", "Alpha", "Beta",
                           "2026-09-11T23:15:00Z")
        tracker.save_bet("sx-LEV5", "1X2", "2", "0xh5", 1, 3.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xh5", sh=0, sa=1)])
        r1 = sx_signals.settle_sx_bets(provider=prov)
        r2 = sx_signals.settle_sx_bets(provider=prov)
        assert r1["settled"] == 1 and r2["settled"] == 0
        assert r2["results"] == 0
        assert _bet_outcome("sx-LEV5") == ("won", 1.0 * (3.0 - 1))


class TestSettlementPausa:
    def test_pausa_blocca_anche_il_percorso_sx(self, temp_db, monkeypatch):
        """Pausa settlement: nessuna lettura SX, nessuna chiusura."""
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.0, 1.0,
                         mode="live")
        tracker.set_settlement_paused(True)
        try:
            prov = FakeSxSettle(find_data=[_find_market("0xh1")])
            res = sx_signals.settle_sx_bets(provider=prov)
            assert res.get("paused") is True
            assert prov.find_calls == []
            assert _bet_outcome("sx-LEV1") == (None, None)
        finally:
            tracker.set_settlement_paused(False)
