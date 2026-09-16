"""Test del confronto shadow catena ↔ corsia (`decision/compare.py`) — OFFLINE.

Cosa si verifica, in ordine di importanza:

1. **I cinque casi sono esaustivi**: ogni riga dei due ledger finisce in una
   cella (entrambe giocano / la catena blocca ma la corsia gioca / la catena
   gioca ma la corsia salta / entrambe fuori / non confrontabili) e nessuna
   riga sparisce in silenzio;
2. **le puntate REALI che la catena avrebbe rifiutato** sono contate col motivo
   del rifiuto e col P/L che hanno realizzato (e' il caso che decide il passo 3);
3. **le puntate senza riga nella catena NON sono divergenze**: sono osservazioni
   mancanti (finestra di persistenza shadow) e finiscono in `unobserved`;
4. **sola lettura**: connessione `mode=ro`, tentativo di scrittura rifiutato dal
   DB, `import decision.compare` che non carica `tracker`/`auto_bet`/`bot`/
   `odds_api`/`sx_signals`;
5. **fail-safe**: DB assente, tabelle mancanti (volume di un deploy precedente)
   o file corrotto ritornano `error`, mai un'eccezione.

Ledger SQLite temporaneo (schema di produzione via `tracker`) e nessuna rete.
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tracker
from decision import compare
from decision.compare import (
    AGREE_SKIP,
    BLOCKED_PLAYED,
    BOTH_PLAY,
    UNOBSERVED,
    WOULD_PLAY_SKIPPED,
    chain_would_play,
    compare_enabled,
    format_report,
    key_of,
    lane_skip_hints,
    measure,
    window_days,
)


# ---------------------------------------------------------------------------
# Fixture e helper
# ---------------------------------------------------------------------------

@pytest.fixture
def db(monkeypatch, tmp_path):
    """Ledger temporaneo con lo schema di produzione (via `tracker`)."""
    path = tmp_path / "compare.db"
    monkeypatch.setattr(tracker, "DB_PATH", path)
    conn = tracker._get_conn()          # crea lo schema completo
    conn.close()
    return path


def kickoff(hours: float = 3.0) -> str:
    """Kickoff RELATIVO a now: una data fissa scadrebbe col calendario."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)) \
        .isoformat().replace("+00:00", "Z")


def add_match(match_id: str, *, home="Inter", away="Roma", league="Serie A",
              hours: float = 3.0):
    tracker.save_match(match_id, league, home, away, kickoff(hours))


def add_decision(match_id: str, outcome: str, *, record_id="", verdict="approve",
                 reason="ok", stake=1.0, stake_executable=1, status="validated",
                 price=1.60, ev=0.05, mode="live", league="Serie A",
                 esito_finale=None, profit=None):
    """Riga del ledger `decisions` (verdetto + stato di convalida)."""
    tracker.save_decision({
        "record_id": record_id or f"rec-{match_id}-{outcome}",
        "signal_id": f"sig-{match_id}-{outcome}",
        "match_id": match_id, "league": league, "market": "1X2", "outcome": outcome,
        "selection_label": f"{outcome}", "kickoff": kickoff(),
        "price": price, "ev": ev, "stake": stake,
        "stake_executable": stake_executable, "verdict": verdict,
        "reason": reason, "status": status, "mode": mode,
        "esito_finale": esito_finale, "profit": profit,
    })


def add_bet(match_id: str, outcome: str, *, price=1.60, stake=1.0, mode="live",
            status="FULLY_FILLED", esito=None):
    """Puntata della corsia (esito canonico se non diversamente indicato)."""
    tracker.save_bet(match_id=match_id, mercato="1X2", esito=esito or outcome,
                     price=price, stake=stake, mode=mode, status=status)


def settle_round(match_id: str, sh: int, sa: int, *, home="Inter", away="Roma",
                 league="Serie A"):
    """Referto reale: chiude puntate e decisioni coi gol veri."""
    tracker.save_result(match_id, league, home, away, sh, sa,
                        datetime.now(timezone.utc).isoformat())
    tracker.settle_bets()
    tracker.settle_decisions()


# ---------------------------------------------------------------------------
# 1. Chiave di giunzione e condizione "la catena avrebbe giocato"
# ---------------------------------------------------------------------------

class TestChiaveECondizione:
    def test_chiave_normalizza_il_maiuscolo(self):
        assert key_of("m1", "x") == key_of("m1", "X") == "m1|X"
        assert key_of("m1", "X") != key_of("m1", "2")

    def test_approvata_ed_eseguibile_avrebbe_giocato(self):
        assert chain_would_play({"verdict": "approve", "stake_executable": 1}) is True

    @pytest.mark.parametrize("stake_executable", [0, None, False])
    def test_approvata_ma_non_eseguibile_no(self, stake_executable):
        """Cap severo / floor dell'exchange: il gate lascia passare, lo stake no."""
        assert chain_would_play({"verdict": "approve",
                                 "stake_executable": stake_executable}) is False

    @pytest.mark.parametrize("verdict", ["review", "reject", "", None])
    def test_niente_verdetto_niente_giocata(self, verdict):
        assert chain_would_play({"verdict": verdict, "stake_executable": 1}) is False


# ---------------------------------------------------------------------------
# 2. I cinque casi
# ---------------------------------------------------------------------------

class TestClassificazione:
    def test_i_cinque_casi(self, db):
        """Un caso per cella: entrambe, bloccata-ma-giocata, saltata, fuori, ignota."""
        # 1. entrambe giocano
        add_match("m-both")
        add_decision("m-both", "1")
        add_bet("m-both", "1")
        # 2. la catena blocca (lega vietata) ma la corsia ha puntato
        add_match("m-blocked")
        add_decision("m-blocked", "1", verdict="reject", reason="league_not_allowed",
                     status="rejected")
        add_bet("m-blocked", "1")
        # 3. la catena avrebbe giocato, la corsia ha saltato
        add_match("m-skipped")
        add_decision("m-skipped", "1")
        # 4. entrambe fuori (rifiuto della catena, nessuna puntata)
        add_match("m-aggskip")
        add_decision("m-aggskip", "1", verdict="reject", reason="odds_too_high",
                     status="rejected")
        # 5. puntata senza riga nella catena (fuori dalla persistenza shadow)
        add_match("m-unobs")
        add_bet("m-unobs", "1")

        out = measure(days=0, path=db, with_hints=False)
        assert out.get("error") is None
        agr = out["agreement"]
        assert agr[BOTH_PLAY] == 1
        assert agr[BLOCKED_PLAYED] == 1
        assert agr[WOULD_PLAY_SKIPPED] == 1
        assert agr[AGREE_SKIP] == 1
        assert agr[UNOBSERVED] == 1
        assert agr["compared"] == 4          # le non confrontabili restano fuori
        assert agr["divergences"] == 2
        assert agr["divergence_rate"] == 0.5

    def test_non_valutate_non_sono_divergenze(self, db):
        """Una puntata che la catena non ha mai visto non e' un blocco."""
        add_match("m1")
        add_bet("m1", "1")
        out = measure(days=0, path=db, with_hints=False)
        assert out["agreement"][UNOBSERVED] == 1
        assert out["agreement"]["divergences"] == 0
        assert out["agreement"]["divergence_rate"] is None
        assert "non hanno una riga nella catena" in out["caveat"]

    def test_conteggi_e_verdetti_della_catena(self, db):
        add_match("m1")
        add_decision("m1", "1")                                   # approve
        add_decision("m1", "X", verdict="review", reason="confidence_low",
                     status="pending", stake=0.0, stake_executable=0)
        add_decision("m1", "2", verdict="reject", reason="ev_too_low",
                     status="rejected")
        out = measure(days=0, path=db, with_hints=False)
        assert out["chain"]["rows"] == 3
        assert out["chain"]["by_verdict"] == {"approve": 1, "review": 1, "reject": 1}
        assert out["chain"]["would_play"] == 1
        assert out["chain"]["by_status"]["pending"] == 1

    def test_righe_duplicate_sulla_stessa_chiave_visibili(self, db):
        """Due righe per lo stesso segnale non si fondono: dedup da verificare."""
        add_match("m1")
        for record in ("rec-a", "rec-b"):
            add_decision("m1", "1", record_id=record, verdict="reject",
                         reason="odds_too_high", status="rejected",
                         stake=0.0, stake_executable=0)
        out = measure(days=0, path=db, with_hints=False)
        assert out["chain"]["rows"] == 2
        assert out["chain"]["duplicate_keys"] == 1
        assert out["agreement"][AGREE_SKIP] == 1      # una chiave, una cella


# ---------------------------------------------------------------------------
# 3. P/L delle celle (il numero che decide il passo 3)
# ---------------------------------------------------------------------------

class TestPiuMenoDelleCelle:
    def test_puntate_reali_rifiutate_col_loro_pl(self, db):
        """Il gate della catena avrebbe evitato (o lasciato) soldi veri."""
        add_match("mWin")
        add_decision("mWin", "1", verdict="reject", reason="league_not_allowed",
                     status="rejected")
        add_bet("mWin", "1", price=2.00, stake=1.0)
        add_match("mLoss")
        add_decision("mLoss", "1", reason="not_favourite", verdict="reject",
                     status="rejected")
        add_bet("mLoss", "1", price=1.50, stake=1.0)
        settle_round("mWin", 2, 1, home="Inter", away="Roma")     # vinta: +1.00
        settle_round("mLoss", 0, 1, home="Inter", away="Roma")    # persa: -1.00

        out = measure(days=0, path=db, with_hints=False)
        bucket = out["settled"][BLOCKED_PLAYED]
        assert bucket["n"] == 2 and bucket["closed"] == 2
        assert bucket["won"] == 1 and bucket["hit_rate"] == 50.0
        assert bucket["pnl"] == pytest.approx(0.0, abs=1e-6)
        assert bucket["roi"] == pytest.approx(0.0, abs=1e-6)
        assert bucket["unit"] == "valuta"
        # Il dettaglio per motivo e' la tabella con cui decidere.
        assert out["by_reason"]["league_not_allowed"]["n"] == 1
        assert out["by_reason"]["not_favourite"]["pnl"] == pytest.approx(-1.0)
        assert out["blocked_played"][0]["reason"] in ("league_not_allowed",
                                                     "not_favourite")

    def test_corsia_ha_saltato_pl_per_unita(self, db):
        """Le non giocate non muovono denaro: il P/L resta per unita' di stake."""
        add_match("m1")
        add_decision("m1", "1", price=1.50, stake=2.0, ev=0.10)
        settle_round("m1", 3, 0)                                   # vinta: +0.50
        out = measure(days=0, path=db, with_hints=False)
        bucket = out["settled"][WOULD_PLAY_SKIPPED]
        assert bucket["unit"] == "per_unità_di_stake"
        assert bucket["closed"] == 1 and bucket["won"] == 1
        assert bucket["pnl_units"] == pytest.approx(0.50)
        assert bucket["pnl_staked"] == pytest.approx(1.00)          # 0.50 x 2.0
        assert bucket["roi_staked"] == pytest.approx(50.0)
        assert bucket["avg_ev"] == pytest.approx(10.0)
        row = out["would_play_skipped"][0]
        assert row["ev"] == pytest.approx(0.10)

    def test_affidabilita_dichiarata_sotto_soglia(self, db):
        add_match("m1")
        add_decision("m1", "1")
        add_bet("m1", "1")
        settle_round("m1", 2, 1)
        out = measure(days=0, path=db, with_hints=False)
        assert out["reliable"] is False
        assert "NON e' conclusivo" in out["caveat"]


# ---------------------------------------------------------------------------
# 4. Esiti misti nel ledger della corsia
# ---------------------------------------------------------------------------

class TestEsitiMisti:
    def test_puntata_col_nome_della_squadra_aggancia(self, db):
        """Ledger misto: `fixture_engine` scrive il nome, `sx_signals` '1'."""
        add_match("m1", home="Atlético Madrid", away="Osasuna")
        add_decision("m1", "1")
        add_bet("m1", "1", esito="Atlético Madrid")
        out = measure(days=0, path=db, with_hints=False)
        assert out["agreement"][BOTH_PLAY] == 1
        assert out["lane"]["undecidable"] == 0

    def test_esito_non_decidibile_contato(self, db):
        add_match("m1")
        add_bet("m1", "1", esito="Over 2.5")            # mercato legacy nel ledger
        out = measure(days=0, path=db, with_hints=False)
        assert out["lane"]["undecidable"] == 1
        assert out["agreement"][UNOBSERVED] == 0
        assert out["lane"]["undecidable"] == 1


# ---------------------------------------------------------------------------
# 5. Sola lettura e nessuna rete
# ---------------------------------------------------------------------------

class TestSolaLettura:
    def test_connessione_in_sola_lettura(self, db):
        """Un UPDATE sulla connessione della misura e' rifiutato dal DB."""
        conn = compare._connect(db)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE decisions SET verdict='x'")
        conn.close()

    def test_sorgente_senza_istruzioni_di_scrittura(self):
        src = Path(compare.__file__).read_text(encoding="utf-8")
        for statement in ("INSERT INTO", "UPDATE decisions", "UPDATE bets",
                          "DELETE FROM", "DROP TABLE"):
            assert statement not in src, statement

    def test_import_non_carica_la_produzione(self):
        code = ("import decision.compare, sys;"
                "print(any(m in sys.modules for m in "
                "('tracker', 'auto_bet', 'bot', 'odds_api', 'sx_signals')))")
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True)
        assert out.stdout.strip() == "False", out.stdout + out.stderr


# ---------------------------------------------------------------------------
# 6. Fail-safe
# ---------------------------------------------------------------------------

class TestFailSafe:
    def test_db_assente(self, tmp_path):
        out = measure(days=0, path=tmp_path / "non-esiste.db")
        assert "error" in out and out["readonly"] is True

    def test_tabelle_mancanti(self, tmp_path):
        """Volume di un deploy precedente: niente `decisions`, nessuna eccezione."""
        path = tmp_path / "vecchio.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE matches (id TEXT)")
        conn.commit()
        conn.close()
        out = measure(days=0, path=path, with_hints=False)
        assert out.get("error") is None
        assert "decisions" in out["missing_tables"] and "bets" in out["missing_tables"]
        assert out["chain"]["rows"] == 0 and out["lane"]["bets"] == 0

    def test_file_corrotto(self, tmp_path):
        path = tmp_path / "corrotto.db"
        path.write_bytes(b"non e' un database sqlite")
        out = measure(days=0, path=path, with_hints=False)
        assert "error" in out

    def test_indizi_fail_safe_senza_monitor(self, monkeypatch):
        monkeypatch.setattr(compare, "lane_skip_hints",
                            lambda *a, **k: {})
        assert lane_skip_hints({"m1|1"}, 7) == {}


# ---------------------------------------------------------------------------
# 7. Indizi dal monitor liquidita'
# ---------------------------------------------------------------------------

class TestIndiziLiquidita:
    def test_indizio_dallo_scarto(self, db, monkeypatch, tmp_path):
        """La corsia che salta non scrive nulla: il motivo vive nel monitor."""
        import liquidity_monitor
        monkeypatch.setattr(liquidity_monitor, "SKIP_LOG",
                            tmp_path / "skips.jsonl")
        add_match("m1")
        add_decision("m1", "1")
        liquidity_monitor.record_skip("order", "depth_vs_stake", match_id="m1",
                                      esito="1", stake=1.0, ev=0.05)
        out = measure(days=0, path=db)
        row = out["would_play_skipped"][0]
        assert row["hint"] == "depth_vs_stake"

    def test_nessuno_scarto_nessun_indizio(self, db, monkeypatch, tmp_path):
        import liquidity_monitor
        monkeypatch.setattr(liquidity_monitor, "SKIP_LOG",
                            tmp_path / "vuoto.jsonl")
        add_match("m1")
        add_decision("m1", "1")
        out = measure(days=0, path=db)
        assert out["would_play_skipped"][0]["hint"] == ""


# ---------------------------------------------------------------------------
# 8. Report e interruttore
# ---------------------------------------------------------------------------

class TestReport:
    def test_report_contiene_i_conteggi(self, db):
        add_match("m1")
        add_decision("m1", "1")
        add_bet("m1", "1")
        text = format_report(measure(days=0, path=db, with_hints=False))
        assert "Confronto shadow" in text
        assert "entrambe giocano *1*" in text
        assert "divergenza: 0/1" in text

    def test_report_su_errore(self, tmp_path):
        text = format_report(measure(days=0, path=tmp_path / "no.db"))
        assert "non disponibile" in text

    def test_interruttore_default_attivo(self):
        assert compare_enabled("") is True and compare_enabled(None) is True

    @pytest.mark.parametrize("value", ["0", "false", "off", "NO"])
    def test_interruttore_spegne(self, value):
        assert compare_enabled(value) is False

    def test_finestra_default_e_override(self, monkeypatch):
        assert window_days("") == 7.0
        assert window_days("3") == 3.0
        assert window_days("non-un-numero") == 7.0     # mai una finestra a caso
        monkeypatch.setenv(compare.COMPARE_DAYS_ENV, "2")
        assert window_days() == 2.0


# ---------------------------------------------------------------------------
# 9. CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_cli_json(self, db, monkeypatch, capsys):
        monkeypatch.setattr(compare, "_db_path", lambda: db)
        add_match("m1")
        add_decision("m1", "1")
        add_bet("m1", "1")
        assert compare.main(["--days", "0", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["agreement"][BOTH_PLAY] == 1
        assert payload["readonly"] is True

    def test_cli_all_e_errore(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(compare, "_db_path", lambda: tmp_path / "no.db")
        assert compare.main(["--all"]) == 1
        assert "non disponibile" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 10. Tripwire sul job del bot
# ---------------------------------------------------------------------------

class TestJobInBot:
    def _source(self) -> str:
        return Path("bot.py").read_text(encoding="utf-8")

    def test_job_schedulato(self):
        src = self._source()
        assert "async def decision_compare_job" in src
        assert "run_repeating(decision_compare_job" in src
        assert "from decision.compare import" in src

    def test_zero_ordini_e_alert_solo_admin(self):
        """Il job legge i ledger e scrive solo agli admin: nessun ordine."""
        body = self._source().split("async def decision_compare_job")[1] \
            .split("async def end_of_day_report_job")[0]
        assert "measure(days=days)" in body
        assert "_admin_chat_ids()" in body
        assert "_send_report_to_recipients" not in body   # non agli iscritti
        for banned in ("run_today_bets", "_live_fill", "save_bet",
                       "fetch_scores", "get_live_odds"):
            assert banned not in body, banned

    def test_anti_spam_giornaliero(self):
        body = self._source().split("async def decision_compare_job")[1] \
            .split("async def end_of_day_report_job")[0]
        assert 'is_notified("SHADOW_COMPARE"' in body
        assert 'mark_notified("SHADOW_COMPARE"' in body
