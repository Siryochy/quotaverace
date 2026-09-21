"""Test di `league_gate_impact.py` (impatto del gate leghe sulla corsia auto-bet).

Tutto OFFLINE: ledger SQLite temporaneo, nessuna rete, nessun ordine e — cosa
piu' importante per questa diagnostica — nessuna SCRITTURA sul ledger reale
(il modulo apre la connessione in `mode=ro`, e un test lo verifica provando a
scrivere).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import league_gate_impact as lgi
import tracker


def _iso(hours_ago: float = 1.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """Ledger temporaneo con lo SCHEMA di produzione (`tracker.init_db`)."""
    db = tmp_path / "quotaverace.db"
    monkeypatch.setattr(tracker, "DB_PATH", db)
    tracker.init_db()
    return db


def _close(db: Path, table: str, match_id: str, esito_finale: str,
           profit: float) -> None:
    """Chiude una riga (setup dei test: la diagnostica NON scrive mai)."""
    conn = sqlite3.connect(db)
    conn.execute(f"UPDATE {table} SET esito_finale=?, profit=?, settled_at=? "
                 "WHERE match_id=?", (esito_finale, profit, _iso(), match_id))
    conn.commit()
    conn.close()


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Classificazione
# ---------------------------------------------------------------------------

class TestClassificazione:
    def test_lega_ammessa_dalla_strategia(self):
        assert lgi._classify({"league": "Premier League"}) == lgi.ALLOWED
        assert lgi._classify({"league": "Bundesliga"}) == lgi.ALLOWED

    def test_lega_vietata(self):
        """Dal 21/09 i vietati sono SOLO le leghe misurate negative: le altre
        (es. Scottish Premiership, Liga MX) sono passate in PROBATION e il
        misuratore le classifica come ammesse — vedi il test successivo."""
        for league in ("Serie A", "La Liga", "EFL Cup", "Belgian Pro League",
                       "Liga Portugal", "Greek Super League"):
            assert lgi._classify({"league": league}) == lgi.BLOCKED

    def test_tier2_classificata_ammessa(self):
        """Il tier-2 e' giocabile (con strategia severa): il misuratore non
        deve contarlo fra i bloccati, altrimenti la misura del gate mentirebbe."""
        import value_filter
        for league in ("EFL Championship", "Serie B", "Liga MX",
                       "Scottish Premiership", "Argentina Primera"):
            assert lgi._classify({"league": league}) == lgi.ALLOWED
            assert value_filter.league_tier(league) == "probation"

    def test_lega_assente_non_e_ammessa(self):
        """Senza riga in `matches` la lega e' ignota: bucket separato, mai
        confusa con una lega ammessa (e' il caso che oggi passa il gate per un
        difetto di propagazione, non per una scelta)."""
        assert lgi._classify({"league": None}) == lgi.UNKNOWN
        assert lgi._classify({"league": "  "}) == lgi.UNKNOWN

    def test_usa_la_stessa_fonte_della_strategia(self):
        import value_filter
        assert lgi.STRATEGY_LEAGUES is value_filter.STRATEGY_LEAGUES
        assert lgi.league_allowed is value_filter.league_allowed


# ---------------------------------------------------------------------------
# Aggregati
# ---------------------------------------------------------------------------

class TestBucket:
    def test_puntate_roi_sulla_valuta(self):
        b = lgi._bucket([
            {"source": "bets", "stake": 2.0, "profit": 3.0, "closed": True,
             "verdict": "won"},
            {"source": "bets", "stake": 2.0, "profit": -2.0, "closed": True,
             "verdict": "lost"},
            {"source": "bets", "stake": 1.0, "profit": 0.0, "closed": False,
             "verdict": None},
        ])
        assert (b["n"], b["open"], b["closed"], b["won"], b["lost"]) == (3, 1, 2, 1, 1)
        assert b["staked"] == 4.0 and b["pnl"] == 1.0
        assert b["roi"] == 0.25 and b["hit_rate"] == 0.5
        assert b["unit_roi"] is False and b["open_stake"] == 1.0

    def test_previsioni_roi_per_unita_di_stake(self):
        """`predictions.profit` e' gia' per unita' di stake: sommarlo come
        valuta falserebbe il ROI (differenza documentata nel modulo)."""
        b = lgi._bucket([
            {"source": "predictions", "stake": 1.0, "profit": 0.7,
             "closed": True, "verdict": "won"},
            {"source": "predictions", "stake": 1.0, "profit": -1.0,
             "closed": True, "verdict": "lost"},
        ])
        assert b["unit_roi"] is True
        assert b["staked"] == 2.0 and b["pnl"] == -0.3
        assert b["roi"] == -0.15

    def test_gruppo_vuoto_non_solleva(self):
        b = lgi._bucket([])
        assert b["n"] == 0 and b["roi"] is None and b["hit_rate"] is None
        assert b["staked"] == 0.0 and b["sources"] == []

    def test_fonti_mescolate_non_producono_un_roi_senza_unita(self):
        """Valuta (puntate) e unita' di stake (previsioni) non si sommano:
        con entrambe le fonti il P/L aggregato si azzera, i numeri buoni
        restano per fonte. E' il difetto che la misura in produzione ha
        rivelato il 15/09 (`--source all`)."""
        b = lgi._bucket([
            {"source": "bets", "stake": 2.0, "profit": 1.4, "closed": True,
             "verdict": "won"},
            {"source": "predictions", "stake": 1.0, "profit": -1.0,
             "closed": True, "verdict": "lost"},
        ])
        assert b["mixed"] is True
        assert (b["pnl"], b["roi"], b["staked"]) == (None, None, None)
        assert b["closed"] == 2 and b["hit_rate"] == 0.5
        assert b["by_source"]["bets"]["roi"] == 0.7
        assert b["by_source"]["predictions"]["roi"] == -1.0


# ---------------------------------------------------------------------------
# Misura end-to-end
# ---------------------------------------------------------------------------

class TestMisura:
    def test_separa_ammesse_vietate_e_senza_lega(self, ledger):
        # ammessa (PL) e chiusa vinta
        tracker.save_match("m1", "Premier League", "Arsenal", "Chelsea", _iso(3))
        tracker.save_bet("m1", "1X2", "1", None, None, 1.7, 2.0)
        _close(ledger, "bets", "m1", "won", 1.4)
        # vietata (Serie A) e chiusa persa
        tracker.save_match("m2", "Serie A", "Inter", "Napoli", _iso(3))
        tracker.save_bet("m2", "1X2", "1", None, None, 1.6, 2.0)
        _close(ledger, "bets", "m2", "lost", -2.0)
        # vietata e ANCORA IN GIOCO
        tracker.save_match("m3", "EFL Cup", "Liverpool", "Tottenham", _iso(-1))
        tracker.save_bet("m3", "1X2", "1", None, None, 1.85, 1.0)
        # senza riga in `matches`: lega ignota
        tracker.save_bet("orphan-1", "1X2", "1", None, None, 1.5, 1.0)

        conn = _conn(ledger)
        try:
            data = lgi.measure(conn=conn)
        finally:
            conn.close()
        assert data["readonly"] is True
        assert data["buckets"][lgi.ALLOWED]["n"] == 1
        assert data["buckets"][lgi.BLOCKED]["n"] == 2
        assert data["buckets"][lgi.UNKNOWN]["n"] == 1
        assert data["buckets"][lgi.BLOCKED]["pnl"] == -2.0
        assert data["blocked_by_league"][0]["league"] == "EFL Cup"
        assert [r["match_id"] for r in data["blocked_open"]] == ["m3"]
        assert data["blocked_open"][0]["stake"] == 1.0

    def test_righe_in_gioco_non_entrano_nel_roi(self, ledger):
        tracker.save_match("m1", "Premier League", "Arsenal", "Chelsea", _iso(-1))
        tracker.save_bet("m1", "1X2", "1", None, None, 1.7, 2.0)
        conn = _conn(ledger)
        try:
            data = lgi.measure(conn=conn)
        finally:
            conn.close()
        assert data["buckets"][lgi.ALLOWED]["closed"] == 0
        assert data["buckets"][lgi.ALLOWED]["roi"] is None

    def test_finestra_giorni(self, ledger):
        tracker.save_match("vecchia", "Serie A", "Inter", "Napoli", _iso(20 * 24))
        tracker.save_bet("vecchia", "1X2", "1", None, None, 1.6, 1.0)
        _close(ledger, "bets", "vecchia", "lost", -1.0)
        conn = _conn(ledger)
        try:
            tutto = lgi.measure(conn=conn)
            recente = lgi.measure(days=7, conn=conn)
        finally:
            conn.close()
        assert tutto["buckets"][lgi.BLOCKED]["n"] == 1
        assert recente["buckets"][lgi.BLOCKED]["n"] == 0

    def test_le_previsioni_si_possono_includere(self, ledger):
        tracker.save_match("m1", "La Liga", "Real", "Barca", _iso(3))
        tracker.save_prediction("m1", "1X2", "1", 1.7, 0.6, 0.02)
        _close(ledger, "predictions", "m1", "won", 0.7)
        conn = _conn(ledger)
        try:
            assert lgi.measure(source="bets", conn=conn)["buckets"][lgi.BLOCKED]["n"] == 0
            tutto = lgi.measure(source="all", conn=conn)
        finally:
            conn.close()
        assert tutto["buckets"][lgi.BLOCKED]["n"] == 1
        assert tutto["buckets"][lgi.BLOCKED]["roi"] == 0.7

    def test_copertura_del_flusso(self, ledger):
        """Quante righe e quanti segnali giocabili cadono nelle leghe ammesse.

        E' la misura che si puo' fare SUBITO (non serve aspettare le chiusure):
        se il gate ammette il 13% delle leghe ma lo 0% dei segnali giocabili,
        applicarlo spegne la corsia.
        """
        tracker.save_match("a1", "Premier League", "Arsenal", "Chelsea", _iso(3))
        tracker.save_prediction("a1", "1X2", "1", 1.7, 0.6, 0.03, status="value")
        tracker.save_match("a2", "Ligue 1", "Lione", "Nizza", _iso(3))
        tracker.save_prediction("a2", "1X2", "1", 1.8, 0.55, 0.0, status="rejected")
        tracker.save_match("b1", "Serie A", "Inter", "Napoli", _iso(3))
        tracker.save_prediction("b1", "1X2", "1", 1.6, 0.62, 0.05, status="strong_value")
        tracker.save_match("b2", "EFL Cup", "Liverpool", "Tottenham", _iso(3))
        tracker.save_prediction("b2", "1X2", "1", 1.85, 0.55, 0.04, status="value")
        tracker.save_prediction("b2", "1X2", "X", 3.9, 0.24, -0.1, status="rejected")
        conn = _conn(ledger)
        try:
            cov = lgi.measure(conn=conn)["coverage"]
        finally:
            conn.close()
        assert cov["total_rows"] == 5 and cov["total_playable"] == 3
        allowed = cov["groups"][lgi.ALLOWED]
        assert (allowed["n"], allowed["playable"], allowed["leagues"]) == (2, 1, 2)
        assert cov["groups"][lgi.BLOCKED]["playable"] == 2
        assert cov["by_league"][0]["playable"] in (1, 2)   # ordinato per giocabili

    def test_copertura_zero_giocabili_ammesse_e_segnalata(self, ledger):
        tracker.save_match("a1", "Premier League", "Arsenal", "Chelsea", _iso(3))
        tracker.save_prediction("a1", "1X2", "1", 1.7, 0.6, 0.0, status="rejected")
        tracker.save_match("b1", "Serie A", "Inter", "Napoli", _iso(3))
        tracker.save_prediction("b1", "1X2", "1", 1.6, 0.62, 0.05, status="value")
        conn = _conn(ledger)
        try:
            data = lgi.measure(conn=conn)
        finally:
            conn.close()
        assert data["coverage"]["groups"][lgi.ALLOWED]["playable"] == 0
        assert "spegnerebbe la corsia" in lgi.format_report(data)

    def test_report_con_fonti_mescolate_stampa_per_fonte(self, ledger):
        tracker.save_match("m1", "Serie A", "Inter", "Napoli", _iso(3))
        tracker.save_bet("m1", "1X2", "1", None, None, 1.6, 1.0)
        _close(ledger, "bets", "m1", "lost", -1.0)
        tracker.save_prediction("m1", "1X2", "1", 1.6, 0.6, 0.05, status="value")
        _close(ledger, "predictions", "m1", "won", 0.6)
        conn = _conn(ledger)
        try:
            report = lgi.format_report(lgi.measure(source="all", conn=conn))
        finally:
            conn.close()
        assert "bets: " in report and "predictions: " in report
        assert "per unita' di stake" in report

    def test_le_righe_rejected_non_rendono_affidabile_il_campione(self, ledger):
        """Le previsioni `rejected` restano nel ledger (dicono cosa il gate
        taglierebbe) ma NON sono segnali giocabili: contarle farebbe sembrare
        solido un confronto basato su una manciata di giocate."""
        tracker.save_match("m1", "Serie A", "Inter", "Napoli", _iso(3))
        for i, esito in enumerate(("1", "X", "2")):
            tracker.save_prediction("m1", "1X2", esito, 1.6, 0.6, -0.1,
                                    status="rejected")
            _close(ledger, "predictions", "m1", "lost", -1.0)
        tracker.save_bet("m1", "1X2", "1", None, None, 1.6, 1.0)
        _close(ledger, "bets", "m1", "lost", -1.0)
        conn = _conn(ledger)
        try:
            data = lgi.measure(source="all", conn=conn)
        finally:
            conn.close()
        blocked = data["buckets"][lgi.BLOCKED]
        assert blocked["closed"] == 4          # tutte le righe
        assert blocked["playable_closed"] == 1  # solo la puntata
        assert data["reliable"] is False

    def test_campione_piccolo_e_dichiarato_non_affidabile(self, ledger):
        """Con poche chiusure il P/L delle bloccate NON e' conclusivo: il
        modulo lo dice, invece di far leggere un ROI come un risultato."""
        tracker.save_match("m1", "Serie A", "Inter", "Napoli", _iso(3))
        tracker.save_bet("m1", "1X2", "1", None, None, 1.6, 1.0)
        _close(ledger, "bets", "m1", "lost", -1.0)
        conn = _conn(ledger)
        try:
            data = lgi.measure(conn=conn)
        finally:
            conn.close()
        assert data["reliable"] is False
        assert "NON e' conclusivo" in data["caveat"]


# ---------------------------------------------------------------------------
# Robustezza e indipendenza
# ---------------------------------------------------------------------------

class TestRobustezza:
    def test_percorso_inesistente_non_solleva(self, tmp_path):
        data = lgi.measure(path=tmp_path / "non-esiste.db")
        assert "error" in data
        assert lgi.format_report(data).startswith("⚠️")

    def test_format_report_su_misura_vuota(self, ledger):
        conn = _conn(ledger)
        try:
            text = lgi.format_report(lgi.measure(conn=conn))
        finally:
            conn.close()
        assert "Gate LEGHE" in text and "senza lega" in text

    def test_connessione_di_sola_lettura(self, ledger):
        conn = lgi._connect(ledger)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE bets SET profit=99 WHERE 1=0")
        finally:
            conn.close()


class TestCopertura:
    def test_finestra_sulla_produzione_del_segnale(self, ledger):
        """La copertura misura cio' che la corsia ha PRODOTTO nella finestra
        (`predictions.created_at`), non la data del match: le previsioni
        nascono 1-3 giorni prima del kickoff."""
        tracker.save_match("nuova", "Serie A", "Inter", "Napoli", _iso(3))
        tracker.save_prediction("nuova", "1X2", "1", 1.6, 0.6, 0.05, status="value")
        tracker.save_match("vecchia", "Serie A", "Roma", "Lazio", _iso(30 * 24))
        tracker.save_prediction("vecchia", "1X2", "1", 1.6, 0.6, 0.05, status="value")
        conn = sqlite3.connect(ledger)
        conn.execute("UPDATE predictions SET created_at=? WHERE match_id='vecchia'",
                     (_iso(30 * 24),))
        conn.commit()
        conn.close()
        ro = _conn(ledger)
        try:
            assert lgi.measure(days=7, conn=ro)["coverage"]["total_rows"] == 1
            assert lgi.measure(conn=ro)["coverage"]["total_rows"] == 2
        finally:
            ro.close()

    def test_nessuna_riga_non_solleva(self, ledger):
        conn = _conn(ledger)
        try:
            cov = lgi.measure(conn=conn)["coverage"]
        finally:
            conn.close()
        assert cov["total_rows"] == 0 and cov["total_playable"] == 0


class TestIndipendenza:
    def test_nessuna_scrittura_sul_ledger(self):
        src = Path("league_gate_impact.py").read_text(encoding="utf-8")
        for forbidden in ("INSERT", "UPDATE", "DELETE", "save_match",
                          "save_prediction", "save_bet", "import bot",
                          "import auto_bet", "place_limit_order"):
            assert forbidden not in src, f"league_gate_impact non deve usare {forbidden}"
        assert "mode=ro" in src

    def test_nessuna_chiamata_di_rete(self):
        src = Path("league_gate_impact.py").read_text(encoding="utf-8")
        for forbidden in ("import requests", "import odds_api", "import sx_signals",
                          "fetch_scores(", "from odds_api", "from sx_signals"):
            assert forbidden not in src, f"league_gate_impact non deve usare {forbidden}"
