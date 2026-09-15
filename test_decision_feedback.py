"""Test del feedback engine della catena di decisione (decision/feedback.py).

OFFLINE: ledger SQLite TEMPORANEO (monkeypatch di `tracker.DB_PATH`), nessuna
rete, nessun provider, nessun ordine. Copre tre cose:

1. il ledger `decisions` di `tracker.py` (schema + migrazione idempotente);
2. `decision.feedback` (persist fail-safe, aggancio ordine, settlement,
   telemetria + misure "shadow" sui segnali scartati);
3. i tripwire di progetto: `import decision` non carica la produzione e la
   persistenza non solleva mai (la telemetria non ferma una puntata).
"""

import sqlite3
import subprocess
import sys
from datetime import timedelta

import pytest

import tracker
from decision import KillSwitchStatus, RiskLimits, decide, persist
from decision import feedback
from decision.adapters import FULL_SAMPLE
from decision.adapters import model_coverage
from decision.models import DataQuality, ReasonCode, Signal, utcnow

ALLOWED_LEAGUE = "Premier League"


# ---------------------------------------------------------------------------
# Ledger temporaneo
# ---------------------------------------------------------------------------

@pytest.fixture
def db(monkeypatch, tmp_path):
    """DB temporaneo con lo schema di produzione (creato da tracker)."""
    path = tmp_path / "decisioni.db"
    monkeypatch.setattr(tracker, "DB_PATH", path)
    conn = tracker._get_conn()
    conn.close()
    return path


@pytest.fixture
def limits():
    return RiskLimits.from_env()


def live_kills(**kwargs):
    base = {"mode": "live", "env_mode": "live", "provider_ready": True}
    base.update(kwargs)
    return KillSwitchStatus(**base)


def make_signal(*, match_id="sx-1", outcome="1", price=1.60, market_prob=0.58,
                blended_prob=0.65, league=ALLOWED_LEAGUE, tier="strong_value",
                confidence=0.90, coverage=1.0, depth=900.0):
    return Signal(
        match_id=match_id,
        league=league,
        outcome=outcome,
        selection_label=f"{match_id} ({outcome})",
        kickoff=utcnow() + timedelta(hours=6),
        price=price,
        price_source="test",
        market_prob=market_prob,
        model_prob=blended_prob + 0.01,
        blended_prob=blended_prob,
        tier=tier,
        confidence=confidence,
        data_quality=DataQuality(model_coverage=coverage, calibrated=True,
                                 depth_usdc=depth),
    )


def approve_record(limits, **kwargs):
    return decide(make_signal(**kwargs), kills=live_kills(), limits=limits,
                  bankroll=1000.0, provider="sxbet")


def reject_record(limits, **kwargs):
    kwargs.setdefault("price", 2.35)
    kwargs.setdefault("market_prob", 0.42)
    kwargs.setdefault("blended_prob", 0.50)
    kwargs.setdefault("tier", "value")
    return decide(make_signal(**kwargs), kills=live_kills(), limits=limits,
                  bankroll=1000.0)


def add_result(match_id="sx-1", home="Inter", away="Cagliari", sh=2, sa=1,
               league=ALLOWED_LEAGUE):
    tracker.save_match(match_id, league, home, away, "2026-09-14T18:45:00Z")
    tracker.save_result(match_id, league, home, away, sh, sa,
                        "2026-09-14T20:45:00Z")


def row_for(record_id):
    rows = [r for r in tracker.get_decisions(limit=1000) if r["record_id"] == record_id]
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# 1. Schema e migrazione del ledger
# ---------------------------------------------------------------------------

class TestLedgerDecisions:
    def test_tabella_creata_con_tutte_le_colonne(self, db):
        conn = tracker._get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
        conn.close()
        for name in tracker.DECISION_FIELDS + tracker.DECISION_LATE_FIELDS:
            assert name in cols, name

    def test_migrazione_idempotente_su_tabella_vecchia(self, db):
        """Un `decisions` creato da un deploy precedente viene completato."""
        conn = sqlite3.connect(str(db))
        conn.execute("DROP TABLE decisions")
        conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "record_id TEXT UNIQUE, verdict TEXT)")
        conn.commit(); conn.close()

        conn = tracker._get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
        conn.close()
        assert "stake" in cols and "esito_finale" in cols and "profit" in cols
        # idempotente: un secondo giro non cambia nulla
        conn = tracker._get_conn()
        cols2 = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
        conn.close()
        assert cols2 == cols


# ---------------------------------------------------------------------------
# 2. Persistenza
# ---------------------------------------------------------------------------

class TestPersistenza:
    def test_decisione_approvata_salvata(self, db, limits):
        record = approve_record(limits)
        out = persist(record)
        assert out["saved"] and out["record_id"] == record.record_id

        row = row_for(record.record_id)
        assert row["verdict"] == "approve"
        assert row["reason"] == ReasonCode.OK.value
        assert row["match_id"] == "sx-1" and row["outcome"] == "1"
        assert row["stake"] == pytest.approx(record.stake.stake)
        assert row["stake_executable"] == 1
        assert row["mode"] == "live" and row["provider"] == "sxbet"
        assert row["selection_label"] == "sx-1 (1)"
        assert row["calibrated"] == 1
        assert row["esito_finale"] is None

    def test_decisione_rifiutata_senza_stake(self, db, limits):
        record = reject_record(limits)
        assert record.risk.verdict == "reject"
        persist(record)
        row = row_for(record.record_id)
        assert row["verdict"] == "reject"
        assert row["reason"] == ReasonCode.ODDS_TOO_HIGH.value
        assert row["stake"] is None and row["stake_executable"] in (None, 0)

    def test_idempotente_su_record_id(self, db, limits):
        record = approve_record(limits)
        persist(record)
        persist(record)
        assert len([r for r in tracker.get_decisions(limit=100)
                    if r["record_id"] == record.record_id]) == 1

    def test_ripersistere_non_cancella_ordine_ed_esito(self, db, limits):
        """Le colonne 'late' (ordine/esito) non vengono azzerate dal re-persist."""
        record = approve_record(limits)
        persist(record)
        tracker.update_decision_order(record.record_id,
                                      {"bet_id": "0xabc", "status": "FULLY_FILLED"})
        add_result()
        tracker.settle_decisions()

        persist(record)                       # secondo salvataggio
        row = row_for(record.record_id)
        assert row["order_id"] == "0xabc"
        assert row["order_status"] == "FULLY_FILLED"
        assert row["esito_finale"] == "won"

    def test_record_senza_id_solleva(self, db):
        with pytest.raises(ValueError):
            tracker.save_decision({"verdict": "approve"})

    def test_persist_fail_safe_su_ledger_rotto(self, limits):
        class Rotto:
            @staticmethod
            def save_decision(record):
                raise sqlite3.OperationalError("database is locked")

        out = persist(approve_record(limits), store=Rotto())
        assert out["saved"] is False
        assert "locked" in out["error"]

    def test_persist_many_conta_salvati_e_falliti(self, db, limits):
        class Mezzo:
            def __init__(self):
                self.calls = 0

            def save_decision(self, record):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("boom")
                return record.record_id

        records = [approve_record(limits, match_id=f"sx-{i}") for i in range(3)]
        out = feedback.persist_many(records, store=Mezzo())
        assert out["saved"] == 2 and out["failed"] == 1


# ---------------------------------------------------------------------------
# 3. Ordine e settlement
# ---------------------------------------------------------------------------

class TestOrdineESettlement:
    def test_aggancio_ordine(self, db, limits):
        record = approve_record(limits)
        persist(record)
        out = feedback.attach_order(record.record_id,
                                    {"bet_id": "tx-1", "status": "FULLY_FILLED"})
        assert out["updated"] is True
        row = row_for(record.record_id)
        assert row["order_id"] == "tx-1" and row["order_status"] == "FULLY_FILLED"

    def test_aggancio_ordine_su_record_inesistente(self, db):
        out = feedback.attach_order("non-esiste", {"bet_id": "x"})
        assert out["updated"] is False and out["error"] == ""

    def test_settlement_chiude_col_risultato(self, db, limits):
        record = approve_record(limits, price=1.60)
        persist(record)
        add_result(sh=2, sa=1)                # esito "1" -> vinto
        settled, pushes = tracker.settle_decisions()
        assert (settled, pushes) == (1, 0)
        row = row_for(record.record_id)
        assert row["esito_finale"] == "won"
        assert row["profit"] == pytest.approx(0.60)     # P/L per unita' di stake
        assert row["settled_at"]

    def test_riga_senza_risultato_resta_aperta(self, db, limits):
        record = approve_record(limits)
        persist(record)
        assert tracker.settle_decisions() == (0, 0)
        assert row_for(record.record_id)["esito_finale"] is None

    def test_pausa_settlement_blocca_la_chiusura(self, db, limits, monkeypatch):
        record = approve_record(limits)
        persist(record)
        add_result(sh=2, sa=1)
        monkeypatch.setattr(tracker, "settlement_paused", lambda: True)
        assert tracker.settle_decisions() == (0, 0)
        assert row_for(record.record_id)["esito_finale"] is None

    def test_gol_non_validi_bloccano_la_chiusura(self, db, limits):
        record = approve_record(limits)
        persist(record)
        tracker.save_match("sx-1", ALLOWED_LEAGUE, "Inter", "Cagliari",
                           "2026-09-14T18:45:00Z")
        tracker.save_result("sx-1", ALLOWED_LEAGUE, "Inter", "Cagliari",
                            -1, 2, "2026-09-14T20:45:00Z")   # punteggio corrotto
        assert tracker.settle_decisions() == (0, 0)
        assert row_for(record.record_id)["esito_finale"] is None


# ---------------------------------------------------------------------------
# 4. Telemetria (feedback engine)
# ---------------------------------------------------------------------------

class TestTelemetria:
    def test_conteggi_per_verdetto_e_motivo(self, db, limits):
        persist(approve_record(limits, match_id="sx-1"))
        persist(reject_record(limits, match_id="sx-2"))
        stats = tracker.decision_stats()
        assert stats["n"] == 2
        assert stats["by_verdict"] == {"approve": 1, "reject": 1}
        assert stats["by_reason"][ReasonCode.ODDS_TOO_HIGH.value] == 1
        assert stats["executable"] == 1 and stats["open"] == 2

    def test_bucket_settled_con_roi_e_gap(self, db, limits):
        record = approve_record(limits, price=1.60)
        persist(record)
        add_result(sh=2, sa=1)
        tracker.settle_decisions()
        bucket = tracker.decision_stats()["settled"]
        assert bucket["n"] == 1 and bucket["won"] == 1
        assert bucket["hit_rate"] == pytest.approx(100.0)
        assert bucket["roi_flat"] == pytest.approx(60.0)
        assert bucket["roi_staked"] == pytest.approx(60.0)
        assert bucket["stake"] == pytest.approx(record.stake.stake, rel=1e-6)
        assert bucket["gap_pp"] == pytest.approx(60.0 - bucket["avg_ev"], abs=0.01)

    def test_shadow_misura_i_segnali_scartati(self, db, limits):
        """Un `reject` che avrebbe vinto e' un costo del gate: va contato."""
        persist(reject_record(limits, match_id="sx-2", outcome="1", price=2.35))
        add_result(match_id="sx-2", sh=1, sa=0)      # avrebbe vinto
        tracker.settle_decisions()
        shadow = tracker.decision_stats()["shadow"]
        assert shadow["reject"]["n"] == 1
        assert shadow["reject"]["won"] == 1
        assert shadow["reject"]["roi_flat"] == pytest.approx(135.0)
        assert shadow["reject"]["stake"] == 0.0      # non giocata: nessun peso

    def test_shadow_vuoto_senza_chiusure(self, db, limits):
        persist(reject_record(limits, match_id="sx-2"))
        stats = tracker.decision_stats()
        assert stats["shadow"] == {} and stats["settled"]["n"] == 0

    def test_letture_fail_safe(self, tmp_path, monkeypatch):
        """Ledger illeggibile -> telemetria vuota, mai un'eccezione."""
        monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "nope" / "x.db")
        stats = feedback.stats()
        assert stats["n"] == 0 and stats["error"]

    def test_snapshot_unisce_decisioni_e_coda(self, db, limits):
        persist(approve_record(limits))
        data = feedback.snapshot()
        assert data["decisions"]["n"] == 1
        assert "pending" in data["reviews"]

    def test_format_report_leggibile(self, db, limits):
        persist(approve_record(limits))
        persist(reject_record(limits, match_id="sx-2"))
        text = feedback.format_report(feedback.snapshot())
        assert "Decisioni registrate: 2" in text
        assert "approve 1" in text and "reject 1" in text
        assert "odds_too_high" in text

    def test_format_report_su_ledger_vuoto(self):
        text = feedback.format_report(feedback.stats(store=_EmptyLedger()))
        assert "nessuna decisione nel ledger" in text


class _EmptyLedger:
    @staticmethod
    def decision_stats():
        return {"n": 0, "by_verdict": {}, "by_reason": {}, "settled": {}, "shadow": {}}


# ---------------------------------------------------------------------------
# 5. Tripwire
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_import_decision_non_carica_la_produzione(self):
        code = ("import decision, sys;"
                "print(any(m in sys.modules for m in ('auto_bet', 'bot', 'tracker')))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.stdout.strip() == "False", out.stdout + out.stderr

    def test_import_feedback_non_carica_tracker(self):
        """Il ledger si importa DENTRO le funzioni, mai a livello di modulo."""
        code = ("import decision.feedback, sys;"
                "print('tracker' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.stdout.strip() == "False", out.stdout + out.stderr

    def test_solo_il_feedback_scrive_sul_ledger(self, db, limits):
        """`pipeline`/`models` restano puri: nessun accesso al DB all'import."""
        import decision.models as models
        import decision.pipeline as pipeline
        src = (models.__file__, pipeline.__file__)
        for path in src:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            assert "import tracker" not in text, path
            assert "sqlite3" not in text, path

    def test_decision_stats_coerente_con_le_righe(self, db, limits):
        """La telemetria conta ESATTAMENTE le righe del ledger."""
        persist(approve_record(limits, match_id="sx-1"))
        persist(reject_record(limits, match_id="sx-2"))
        stats = tracker.decision_stats()
        assert stats["n"] == len(tracker.get_decisions(limit=1000))
        assert stats["executable"] == len(
            [r for r in tracker.get_decisions(limit=1000) if r["stake_executable"]])

    def test_copertura_modello_non_alterata_dal_salvataggio(self, db, limits):
        """Il campo di qualita' dati arriva sul ledger senza trasformazioni."""
        coverage = model_coverage(FULL_SAMPLE, FULL_SAMPLE)
        record = approve_record(limits, coverage=coverage)
        persist(record)
        row = row_for(record.record_id)
        assert row["model_coverage"] == pytest.approx(coverage)
        assert record.signal.data_quality.model_coverage == pytest.approx(coverage)
