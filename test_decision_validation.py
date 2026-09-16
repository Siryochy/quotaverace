"""Test della Shadow Validation (`decision/validation.py` + gateway di storage) — OFFLINE.

Cosa si verifica, in ordine di importanza:

1. **"Persistito" non e' "approvato"**: la riga nasce `pending`, il motore di
   convalida la legge DAL LEDGER e solo `validated` autorizza l'ordine reale.
2. **Ordine dei passi**: salva -> rileggi -> convalida -> scrivi lo stato. Il
   convalida non vede mai l'oggetto in memoria, solo cio' che e' stato scritto.
3. **Fail-closed**: riga non rileggibile, stato non scritto o persist fallito ->
   ordine bloccato (`require_persist`), mentre audit e notifiche proseguono.
4. **Comportamento di default INVARIATO**: senza `require_persist` il dispatcher
   resta fail-soft, e senza `DECISION_SHADOW_PERSIST` la shadow mode non scrive
   nulla sul ledger (il progetto del 15/09 non cambia da solo).
5. **Tracciabilita'**: `record_id` e `trace_id` presenti negli eventi del
   middleware.

Nessun test tocca rete, provider o DB di produzione: gateway finti in memoria e
ledger SQLite temporaneo.
"""

import json
import sqlite3

import pytest

import tracker
from decision.commands import (
    CommandKind, notify_command, persist_decision_command, place_order_command,
    plan_for_record,
)
from decision.dispatcher import Dispatcher
from decision.feedback import read_row, row_exists_for_signal, set_status
from decision.gateways import (
    BaseGateway, CommandResult, LedgerGateway, PlaceOrderGateway,
    ValidatingLedgerGateway,
)
from decision.limits import RiskLimits
from decision.middleware import ListSink, Observability
from decision.models import (
    DECISION_STATUS_PENDING, DECISION_STATUS_REJECTED, DECISION_STATUS_VALIDATED,
    DECISION_STATUSES, DecisionRecord, KillSwitchStatus, ReasonCode,
    risk_approve, risk_reject, risk_review,
)
from decision.shadow import (
    SHADOW_PERSIST_ENV, run_shadow, shadow_persist_enabled,
)
from decision.validation import ValidationOutcome, validate_row
from test_decision_commands import make_signal, plan_for
from test_decision_shadow import add_open_signal, live


@pytest.fixture
def limits():
    return RiskLimits.from_env()


@pytest.fixture
def db(monkeypatch, tmp_path):
    """Ledger temporaneo con lo schema di produzione.

    Il nome del file e' quello di `test_decision_shadow.add_open_signal` (che
    scrive le righe di ledger a mano): cosi' gli helper di quella suite si
    possono riusare senza duplicare lo schema.
    """
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "shadow.db")
    conn = tracker._get_conn()
    conn.close()
    return tmp_path


class FakeGateway(BaseGateway):
    """Gateway di test: registra i comandi ricevuti e ritorna l'esito voluto."""

    def __init__(self, name, kinds, *, ok=True, status="executed", data=None, calls=None):
        self.name = name
        self.kinds = tuple(kinds)
        self._result = (ok, status, dict(data or {}))
        self.calls = calls if calls is not None else []

    def _run(self, command, *, ctx, obs):
        self.calls.append(command.kind.value)
        ok, status, data = self._result
        return CommandResult(kind=command.kind, ok=ok, status=status, data=dict(data))


def row_for(*, verdict="approve", reason="ok", stake_executable=1,
            record_id="rec-1", status=None):
    """Riga piatta del ledger (stessa forma di `tracker.get_decision`)."""
    row = {"record_id": record_id, "signal_id": "sig-1", "verdict": verdict,
           "reason": reason, "stake_executable": stake_executable, "stake": 1.0,
           "price": 1.6}
    if status is not None:
        row["status"] = status
    return row


# ---------------------------------------------------------------------------
# 1. Il motore di convalida (puro)
# ---------------------------------------------------------------------------

class TestMotoreDiConvalida:
    def test_approvata_con_stake_eseguibile_e_validata(self):
        out = validate_row(row_for(verdict="approve", stake_executable=1))
        assert out.status == DECISION_STATUS_VALIDATED
        assert out.reason is ReasonCode.OK
        assert out.validated is True and out.order_allowed is True and out.decided is True

    @pytest.mark.parametrize("stake_executable", [0, None])
    def test_approvata_senza_stake_eseguibile_e_rifiutata(self, stake_executable):
        """Cap severo / stake sotto il floor: l'ordine non puo' partire."""
        out = validate_row(row_for(verdict="approve", stake_executable=stake_executable))
        assert out.status == DECISION_STATUS_REJECTED
        assert out.reason is ReasonCode.STAKE_NOT_EXECUTABLE
        assert out.order_allowed is False and out.decided is True

    @pytest.mark.parametrize("reason", ["kill_switch_off", "daily_stop_loss",
                                        "feed_stale", "odds_too_high", "league_not_allowed"])
    def test_rifiuto_porta_il_suo_motivo(self, reason):
        out = validate_row(row_for(verdict="reject", reason=reason))
        assert out.status == DECISION_STATUS_REJECTED
        assert out.reason.value == reason
        assert out.decided is True

    def test_rifiuto_con_motivo_illeggibile_non_ne_inventa_uno(self):
        out = validate_row(row_for(verdict="reject", reason="motivo-inesistente"))
        assert out.status == DECISION_STATUS_REJECTED
        assert out.reason is ReasonCode.VALIDATION_INCOMPLETE

    def test_revisione_umana_resta_pending(self):
        out = validate_row(row_for(verdict="review", reason="review_pending"))
        assert out.status == DECISION_STATUS_PENDING
        assert out.reason is ReasonCode.REVIEW_PENDING
        assert out.validated is False and out.decided is False

    @pytest.mark.parametrize("verdict", ["", None, "boh", "APPROVED"])
    def test_verdetto_assente_o_ignoto_resta_pending(self, verdict):
        out = validate_row(row_for(verdict=verdict))
        assert out.status == DECISION_STATUS_PENDING
        assert out.reason is ReasonCode.VALIDATION_INCOMPLETE
        assert out.order_allowed is False

    def test_verdetto_in_maiuscolo_e_riconosciuto(self):
        out = validate_row(row_for(verdict="APPROVE", stake_executable=1))
        assert out.status == DECISION_STATUS_VALIDATED

    @pytest.mark.parametrize("row", [None, {}, {"signal_id": "s"}, {"verdict": "approve"}])
    def test_riga_assente_o_senza_record_id_non_convalida(self, row):
        out = validate_row(row)
        assert out.status == DECISION_STATUS_PENDING
        assert out.reason is ReasonCode.VALIDATION_INCOMPLETE

    def test_esito_e_serializzabile(self):
        data = validate_row(row_for()).as_json()
        assert data["validated"] is True and data["status"] == "validated"
        assert data["reason"] == "ok"

    def test_gli_stati_del_ledger_e_del_pacchetto_coincidono(self):
        """Tripwire: le due tabelle di stringhe non possono divergere.

        Il ledger (`tracker`) non importa il pacchetto `decision` e viceversa,
        quindi gli stati sono duplicati di proposito: qui si verifica che le due
        copie dicano la stessa cosa, cosi' un rinominamento a meta' fallisce il
        test invece di rompere la produzione in silenzio.
        """
        import decision.models as models

        assert set(tracker.DECISION_STATUSES) == set(models.DECISION_STATUSES)
        assert set(tracker.DECISION_STATUSES) == set(DECISION_STATUSES)
        assert tracker.DECISION_STATUS_PENDING == DECISION_STATUS_PENDING
        assert tracker.DECISION_STATUS_VALIDATED == DECISION_STATUS_VALIDATED
        assert tracker.DECISION_STATUS_REJECTED == DECISION_STATUS_REJECTED
        assert "status" in tracker.DECISION_FIELDS


# ---------------------------------------------------------------------------
# 2. Il gateway di storage con la convalida dentro
# ---------------------------------------------------------------------------

class TestValidatingLedgerGateway:
    def _gateway(self, *, steps=None, persist_ok=True, row=None, write_ok=True,
                 validator=None):
        """Gateway con persist/lettura/scrittura iniettati (nessun DB)."""
        store = {}
        steps = steps if steps is not None else []

        def persist(body):
            steps.append("persist")
            store[body.get("record_id")] = dict(body)
            if not persist_ok:
                return {"saved": False, "record_id": body.get("record_id"),
                        "error": "database is locked"}
            return {"saved": True, "record_id": body.get("record_id"), "error": ""}

        def reader(record_id):
            steps.append("read")
            if row is not None:
                return row
            return store.get(record_id)

        def writer(record_id, status):
            steps.append("write")
            if not write_ok:
                return {"updated": False, "status": status, "error": "database is locked"}
            store[record_id]["status"] = status
            return {"updated": True, "status": status, "error": ""}

        gateway = ValidatingLedgerGateway(persist=persist, reader=reader, writer=writer,
                                          validator=validator)
        return gateway, store, steps

    def _command(self, limits):
        plan = plan_for(limits=limits)
        return plan.record, persist_decision_command(plan.record)

    def test_salva_rilegge_convalida_e_scrive_lo_stato(self, limits):
        gateway, store, steps = self._gateway()
        record, command = self._command(limits)
        result = gateway.execute(command)

        assert result.ok is True and result.status == "executed"
        assert result.data["validated"] is True
        assert store[record.record_id]["status"] == "validated"
        # L'ORDINE dei passi e' il contratto: prima si scrive, poi si convalida.
        assert steps == ["persist", "read", "write"]

    def test_la_convalida_legge_la_riga_persistita_non_l_oggetto(self, limits):
        """Il validatore riceve cio' che e' sul ledger, non il record in memoria."""
        visto = {}

        def validator(row):
            visto.update(row)
            return validate_row(row)

        gateway, _store, _steps = self._gateway(validator=validator)
        record, command = self._command(limits)
        gateway.execute(command)

        assert visto["record_id"] == record.record_id
        assert visto["verdict"] == record.risk.verdict
        # Nessun oggetto pydantic: solo la riga piatta (dizionario).
        assert isinstance(visto, dict) and "signal" not in visto

    def test_convalida_negativa_scrive_rejected_e_non_autorizza(self, limits):
        """Approvata ma senza stake eseguibile: riga `rejected`, ordine bloccato."""
        gateway, store, _steps = self._gateway(row=row_for(stake_executable=0))
        record, command = self._command(limits)
        result = gateway.execute(command)

        assert result.ok is True                     # il gateway ha fatto il suo lavoro
        assert result.data["validated"] is False
        assert result.data["decision_status"] == "rejected"
        assert result.data["validation_reason"] == "stake_not_executable"
        assert store[record.record_id]["status"] == "rejected"

    def test_pending_non_autorizza_l_ordine(self, limits):
        gateway, _store, _steps = self._gateway(row=row_for(verdict="review"))
        _record, command = self._command(limits)
        result = gateway.execute(command)
        assert result.data["validated"] is False
        assert result.data["decision_status"] == "pending"
        assert result.ok is True

    def test_riga_non_rileggibile_e_fail_closed(self, limits):
        gateway, store, steps = self._gateway()
        gateway._reader = lambda record_id: None      # riga sparita/inaccessibile
        _record, command = self._command(limits)
        result = gateway.execute(command)

        assert result.ok is False and result.status == "error"
        assert "non rileggibile" in result.detail
        assert result.data["validated"] is False
        assert "write" not in steps                   # non si scrive uno stato senza riga
        # La riga resta `pending` (lo stato del salvataggio): non e' stata ne'
        # convalidata ne' promossa, quindi l'ordine resta bloccato.
        assert all(body.get("status") == DECISION_STATUS_PENDING
                   for body in store.values())

    def test_stato_non_scritto_e_fail_closed(self, limits):
        gateway, _store, _steps = self._gateway(write_ok=False)
        _record, command = self._command(limits)
        result = gateway.execute(command)

        assert result.ok is False and result.status == "error"
        assert "non scritto" in result.detail and "database is locked" in result.detail
        assert result.data["validated"] is False

    def test_salvataggio_fallito_non_attiva_la_convalida(self, limits):
        gateway, _store, steps = self._gateway(persist_ok=False)
        _record, command = self._command(limits)
        result = gateway.execute(command)

        assert result.ok is False and "locked" in result.detail
        assert steps == ["persist"]                   # nessuna lettura, nessuna scrittura

    def test_validatore_che_esplode_non_solleva(self, limits):
        def boom(_row):
            raise RuntimeError("convalida rotta")

        gateway, _store, _steps = self._gateway(validator=boom)
        _record, command = self._command(limits)
        result = gateway.execute(command)             # mai un'eccezione oltre il gateway

        assert result.ok is False and result.status == "error"
        assert "RuntimeError: convalida rotta" in result.detail

    def test_il_gateway_di_storage_semplice_non_convalida(self, limits):
        """Regressione: il gateway storico resta com'e' (nessuno stato scritto)."""
        gateway = LedgerGateway(persist=lambda row: {"saved": True,
                                                     "record_id": row["record_id"],
                                                     "error": ""})
        _record, command = self._command(limits)
        result = gateway.execute(command)
        assert result.ok is True
        assert "validated" not in result.data


# ---------------------------------------------------------------------------
# 3. Blocco dell'ordine (require_persist: opt-in)
# ---------------------------------------------------------------------------

class TestBloccoDellOrdine:
    def _plan(self, limits):
        return plan_for(limits=limits)

    def test_default_invariato_fail_soft(self, limits):
        """Senza il flag un persist fallito NON blocca l'ordine (comportamento storico)."""
        ledger = FakeGateway("ledger", (CommandKind.PERSIST_DECISION,),
                             ok=False, status="error")
        orders = FakeGateway("orders", (CommandKind.PLACE_ORDER,), ok=True)
        report = Dispatcher([ledger, orders], observability=Observability(sink=ListSink())
                            ).dispatch(self._plan(limits))

        assert orders.calls == ["place_order"]
        assert report.aborted is False and report.blocked_reason == ""

    def test_flag_attivo_blocca_l_ordine_su_persist_fallito(self, limits):
        ledger = FakeGateway("ledger", (CommandKind.PERSIST_DECISION,),
                             ok=False, status="error")
        orders = FakeGateway("orders", (CommandKind.PLACE_ORDER,), ok=True)
        report = Dispatcher([ledger, orders], observability=Observability(sink=ListSink()),
                            require_persist=True).dispatch(self._plan(limits))

        assert orders.calls == []                      # l'ordine NON e' mai stato eseguito
        assert report.aborted is True
        assert "persistenza fallita" in report.blocked_reason
        assert report.skipped >= 1

    def test_flag_attivo_blocca_su_convalida_non_positiva(self, limits):
        for state, reason in (("pending", "review_pending"),
                              ("rejected", "stake_not_executable")):
            ledger = FakeGateway("ledger_validating", (CommandKind.PERSIST_DECISION,),
                                 ok=True, status="executed",
                                 data={"validated": False, "decision_status": state,
                                       "validation_reason": reason})
            orders = FakeGateway("orders", (CommandKind.PLACE_ORDER,), ok=True)
            report = Dispatcher([ledger, orders],
                                observability=Observability(sink=ListSink()),
                                require_persist=True).dispatch(self._plan(limits))

            assert orders.calls == [], state
            assert report.aborted is True
            assert f"convalida {state}" in report.blocked_reason

    def test_flag_attivo_blocca_un_piano_senza_persist(self, limits):
        record = self._plan(limits).record
        plan = plan_for_record(record, [place_order_command(record)])
        orders = FakeGateway("orders", (CommandKind.PLACE_ORDER,), ok=True)
        report = Dispatcher([orders], observability=Observability(sink=ListSink()),
                            require_persist=True).dispatch(plan)

        assert orders.calls == []
        assert report.blocked_reason == "nessun persist_decision nel piano"

    def test_flag_attivo_lascia_passare_un_persist_convalidato(self, limits):
        ledger = FakeGateway("ledger_validating", (CommandKind.PERSIST_DECISION,),
                             ok=True, data={"validated": True,
                                            "decision_status": "validated"})
        orders = FakeGateway("orders", (CommandKind.PLACE_ORDER,), ok=True)
        report = Dispatcher([ledger, orders], observability=Observability(sink=ListSink()),
                            require_persist=True).dispatch(self._plan(limits))

        assert orders.calls == ["place_order"]
        assert report.aborted is False

    def test_flag_attivo_basta_il_salvataggio_se_il_gateway_non_convalida(self, limits):
        """Un ledger senza Shadow Validation non ha `validated`: il salvataggio basta."""
        ledger = FakeGateway("ledger", (CommandKind.PERSIST_DECISION,), ok=True)
        orders = FakeGateway("orders", (CommandKind.PLACE_ORDER,), ok=True)
        report = Dispatcher([ledger, orders], observability=Observability(sink=ListSink()),
                            require_persist=True).dispatch(self._plan(limits))

        assert orders.calls == ["place_order"] and report.aborted is False

    def test_le_notifiche_passano_anche_con_l_ordine_bloccato(self, limits):
        """Bloccare l'ordine non deve diventare un silenzio operativo."""
        record = self._plan(limits).record
        plan = plan_for_record(record, [
            persist_decision_command(record),
            place_order_command(record),
            notify_command(record, kind="info", text="segnala"),
        ])
        ledger = FakeGateway("ledger", (CommandKind.PERSIST_DECISION,),
                             ok=False, status="error")
        orders = FakeGateway("orders", (CommandKind.PLACE_ORDER,), ok=True)
        notify = FakeGateway("notify", (CommandKind.NOTIFY_OPERATORS,), ok=True)
        report = Dispatcher([ledger, orders, notify],
                            observability=Observability(sink=ListSink()),
                            require_persist=True).dispatch(plan)

        assert orders.calls == []
        assert notify.calls == ["notify_operators"]
        assert report.aborted is True

    def test_il_gateway_iniettabile_blocca_end_to_end(self, limits):
        """Catena vera: persist -> (riga non rileggibile) -> ordine bloccato."""
        plan = plan_for(limits=limits)
        ledger = ValidatingLedgerGateway(
            persist=lambda row: {"saved": True, "record_id": row.get("record_id"),
                                 "error": ""},
            reader=lambda _rid: None,
            writer=lambda _rid, status: {"updated": True, "status": status, "error": ""})
        orders = FakeGateway("orders", (CommandKind.PLACE_ORDER,), ok=True)
        report = Dispatcher([ledger, orders], observability=Observability(sink=ListSink()),
                            require_persist=True).dispatch(plan)

        assert orders.calls == [] and report.aborted is True
        assert "non rileggibile" in report.blocked_reason

    def test_la_catena_reale_esegue_l_ordine_solo_a_convalida_positiva(self, limits):
        """persist -> read -> validate -> write -> ordine, con la riga nel gateway."""
        steps = []
        store = {}

        def persist(row):
            steps.append("persist")
            store[row["record_id"]] = dict(row)
            return {"saved": True, "record_id": row["record_id"], "error": ""}

        def reader(record_id):
            steps.append("read")
            return store.get(record_id)

        def writer(record_id, status):
            steps.append("write")
            store[record_id]["status"] = status
            return {"updated": True, "status": status, "error": ""}

        ledger = ValidatingLedgerGateway(persist=persist, reader=reader, writer=writer)
        orders = FakeGateway("orders", (CommandKind.PLACE_ORDER,), ok=True)
        plan = plan_for(limits=limits)
        report = Dispatcher([ledger, orders], observability=Observability(sink=ListSink()),
                            require_persist=True).dispatch(plan)

        assert steps == ["persist", "read", "write"]
        assert orders.calls == ["place_order"]
        assert store[plan.record.record_id]["status"] == "validated"
        assert report.aborted is False


# ---------------------------------------------------------------------------
# 4. Tracciabilita' nel middleware
# ---------------------------------------------------------------------------

class TestTracciabilita:
    def _dispatch(self, limits):
        sink = ListSink()
        obs = Observability(sink=sink, component="test")
        plan = plan_for(limits=limits, sink=sink)
        ctx = obs.new_trace(request_id="giro-1", trace_id="trace-fisso")
        ledger = FakeGateway("ledger", (CommandKind.PERSIST_DECISION,), ok=True,
                             data={"validated": True, "decision_status": "validated"})
        orders = FakeGateway("orders", (CommandKind.PLACE_ORDER,), ok=True)
        Dispatcher([ledger, orders], observability=obs,
                   require_persist=True).dispatch(plan, ctx=ctx)
        return sink, plan

    def test_plan_dispatch_porta_record_id_e_trace_id(self, limits):
        sink, plan = self._dispatch(limits)
        event = sink.of("plan.dispatch")[0]
        assert event["record_id"] == plan.record.record_id
        assert event["trace_id"] == "trace-fisso"
        assert event["request_id"] == "giro-1"
        assert event["require_persist"] is True
        assert event["shadow"] is False

    def test_command_result_porta_record_id_e_trace_id(self, limits):
        sink, plan = self._dispatch(limits)
        results = sink.of("command.result")
        assert len(results) == 2
        for event in results:
            assert event["record_id"] == plan.record.record_id
            assert event["trace_id"] == "trace-fisso"
            assert event["span_id"]
        by_command = {e["command"]: e for e in results}
        assert by_command["persist_decision"]["validated"] is True

    def test_span_dei_comandi_porta_record_id(self, limits):
        sink, plan = self._dispatch(limits)
        starts = sink.of("span.start")
        commands = [e for e in starts if e["span_name"].startswith("command.")]
        assert len(commands) == 2
        assert all(e["record_id"] == plan.record.record_id for e in commands)
        assert all(e["trace_id"] == "trace-fisso" for e in commands)

    def test_ordine_bloccato_tracciato(self, limits):
        sink = ListSink()
        obs = Observability(sink=sink, component="test")
        plan = plan_for(limits=limits, sink=sink)
        ledger = FakeGateway("ledger", (CommandKind.PERSIST_DECISION,),
                             ok=False, status="error")
        orders = FakeGateway("orders", (CommandKind.PLACE_ORDER,), ok=True)
        Dispatcher([ledger, orders], observability=obs,
                   require_persist=True).dispatch(plan, ctx=obs.new_trace(trace_id="t-2"))

        blocked = sink.of("order.blocked")[0]
        assert blocked["record_id"] == plan.record.record_id
        assert blocked["trace_id"] == "t-2"
        assert "persistenza fallita" in blocked["reason"]
        assert sink.of("plan.dispatched")[0]["outcome"] == "blocked"


# ---------------------------------------------------------------------------
# 5. Ledger: colonna, stato, letture
# ---------------------------------------------------------------------------

class TestLedgerStato:
    def test_colonna_status_creata_e_migrata(self, db, monkeypatch, tmp_path):
        conn = tracker._get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
        conn.close()
        assert {"record_id", "status", "verdict"} <= cols

    def test_migrazione_su_tabella_vecchia(self, db):
        """Un `decisions` di un deploy precedente viene completato."""
        conn = sqlite3.connect(str(tracker.DB_PATH))
        conn.execute("DROP TABLE decisions")
        conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "record_id TEXT UNIQUE, verdict TEXT)")
        conn.commit(); conn.close()

        conn = tracker._get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(decisions)")}
        conn.close()
        assert "status" in cols and "signal_id" in cols
        assert "idx_decisions_status" in indexes

    def test_la_riga_nasce_pending(self, db):
        tracker.save_decision(row_for(status=None))
        row = tracker.get_decision("rec-1")
        assert row is not None and row["verdict"] == "approve"

    def test_save_decision_scrive_lo_stato_della_riga(self, db):
        tracker.save_decision(row_for(status=DECISION_STATUS_PENDING))
        assert tracker.get_decision("rec-1")["status"] == DECISION_STATUS_PENDING

    def test_set_status_e_filtro(self, db):
        tracker.save_decision(row_for(status=DECISION_STATUS_PENDING))
        tracker.save_decision(row_for(record_id="rec-2", status=DECISION_STATUS_PENDING))

        assert tracker.set_decision_status("rec-1", DECISION_STATUS_VALIDATED) is True
        assert tracker.set_decision_status("rec-2", DECISION_STATUS_REJECTED) is True

        assert tracker.get_decision("rec-1")["status"] == DECISION_STATUS_VALIDATED
        assert [r["record_id"] for r in tracker.get_decisions(status="validated")] == ["rec-1"]
        assert [r["record_id"] for r in tracker.get_decisions(status="rejected")] == ["rec-2"]
        assert tracker.get_decisions(status="pending") == []

    def test_set_status_su_riga_inesistente(self, db):
        assert tracker.set_decision_status("non-esiste", DECISION_STATUS_VALIDATED) is False
        assert tracker.set_decision_status("", DECISION_STATUS_VALIDATED) is False

    def test_get_decision_inesistente(self, db):
        assert tracker.get_decision("non-esiste") is None
        assert tracker.get_decision("") is None

    def test_dedup_per_segnale(self, db):
        assert tracker.decision_exists_for_signal("sig-1") is False
        assert tracker.decision_exists_for_signal("") is False
        tracker.save_decision(row_for(status=DECISION_STATUS_VALIDATED))
        assert tracker.decision_exists_for_signal("sig-1") is True

    def test_stats_contano_lo_stato(self, db):
        tracker.save_decision(row_for(status=DECISION_STATUS_PENDING))
        tracker.save_decision(row_for(record_id="rec-2", status=DECISION_STATUS_VALIDATED))
        stats = tracker.decision_stats()
        assert stats["by_status"] == {"pending": 1, "validated": 1}

    def test_uno_stato_vuoto_non_viene_contato(self, db):
        """Righe legacy (status NULL) restano fuori dal conteggio, non inventate."""
        tracker.save_decision(row_for(status=DECISION_STATUS_PENDING))
        tracker.set_decision_status("rec-1", "")
        assert tracker.decision_stats()["by_status"] == {}


# ---------------------------------------------------------------------------
# 6. Feedback engine (wrapper fail-safe)
# ---------------------------------------------------------------------------

class TestFeedback:
    def test_lettura_scrittura_e_dedup(self, db):
        record = _decision_record()
        tracker.save_decision(record)
        assert read_row(record.record_id)["record_id"] == record.record_id
        assert row_exists_for_signal(record.signal.signal_id) is True

        out = set_status(record.record_id, DECISION_STATUS_VALIDATED)
        assert out["updated"] is True and out["error"] == ""
        assert read_row(record.record_id)["status"] == DECISION_STATUS_VALIDATED

    def test_letture_fail_safe_su_ledger_rotto(self):
        class Rotto:
            def get_decision(self, _record_id):
                raise sqlite3.OperationalError("database is locked")

            def set_decision_status(self, _record_id, _status):
                raise sqlite3.OperationalError("database is locked")

            def decision_exists_for_signal(self, _signal_id):
                raise sqlite3.OperationalError("database is locked")

        assert read_row("rec-1", store=Rotto()) is None
        assert set_status("rec-1", DECISION_STATUS_VALIDATED, store=Rotto())["updated"] is False
        # In dubbio si tenta la scrittura: un doppione e' meno grave di una
        # valutazione mai registrata.
        assert row_exists_for_signal("sig-1", store=Rotto()) is False

    def test_riga_assente(self, db):
        assert read_row("non-esiste") is None
        assert set_status("non-esiste", DECISION_STATUS_VALIDATED)["updated"] is False


def _decision_record(*, verdict="approve"):
    """`DecisionRecord` minimo per i test del ledger (nessun DB, nessuna catena)."""
    kills = KillSwitchStatus(mode="live", env_mode="live", provider_ready=True)
    risk = (risk_approve(checked=["x"]) if verdict == "approve" else
            risk_review(ReasonCode.CONFIDENCE_LOW, "bassa", checked=["x"]))
    return DecisionRecord(signal=make_signal(), kill_switch=kills, risk=risk,
                          mode="live")


# ---------------------------------------------------------------------------
# 7. Shadow mode: persistenza opt-in
# ---------------------------------------------------------------------------

class TestShadowPersist:
    def test_interruttore_spento_di_default(self, monkeypatch):
        assert shadow_persist_enabled("") is False
        assert shadow_persist_enabled(None) is False
        monkeypatch.delenv(SHADOW_PERSIST_ENV, raising=False)
        assert shadow_persist_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_interruttore_acceso(self, monkeypatch, value):
        monkeypatch.setenv(SHADOW_PERSIST_ENV, value)
        assert shadow_persist_enabled() is True

    def test_di_default_il_ledger_non_viene_toccato(self, db, tmp_path):
        """Regressione del design 15/09: la shadow non scrive sul ledger."""
        add_open_signal(tmp_path)
        out = run_shadow(kills=live(), bankroll=1000.0, mode="live",
                         shadow_path=tmp_path / "s.jsonl")

        assert out["persist_enabled"] is False
        assert tracker.get_decisions(limit=50) == []
        assert out["evaluated"] == 1

    def test_con_persistenza_la_valutazione_finisce_sul_ledger(self, db, tmp_path):
        add_open_signal(tmp_path)
        path = tmp_path / "s.jsonl"
        out = run_shadow(kills=live(), bankroll=1000.0, mode="live", persist=True,
                         shadow_path=path)

        rows = tracker.get_decisions(limit=50)
        assert len(rows) == 1
        assert rows[0]["status"] == DECISION_STATUS_VALIDATED
        assert out["persisted"] == 1 and out["order_blocked"] == 0

        # L'ordine resta REGISTRATO (shadow), mai eseguito: il registro lo prova.
        kinds = [json.loads(l)["command"]["kind"] for l in path.read_text().splitlines()]
        assert "place_order" in kinds and "persist_decision" not in kinds

    def test_un_segnale_una_riga(self, db, tmp_path):
        """Il job gira ogni 60s: senza dedup la stessa opportunita' si moltiplica."""
        add_open_signal(tmp_path)
        path = tmp_path / "s.jsonl"
        first = run_shadow(kills=live(), bankroll=1000.0, mode="live", persist=True,
                           shadow_path=path)
        second = run_shadow(kills=live(), bankroll=1000.0, mode="live", persist=True,
                            shadow_path=path)

        assert first["persisted"] == 1 and first["persisted_duplicates"] == 0
        assert second["persisted"] == 0 and second["persisted_duplicates"] == 1
        assert len(tracker.get_decisions(limit=50)) == 1

    def test_convalida_negativa_scrive_rejected(self, db, tmp_path):
        """Bankroll minuscolo -> stake sotto il floor -> riga `rejected`."""
        add_open_signal(tmp_path)
        out = run_shadow(kills=live(), bankroll=0.5, mode="live", persist=True,
                         shadow_path=tmp_path / "s.jsonl")

        row = tracker.get_decisions(limit=1)[0]
        assert row["status"] == DECISION_STATUS_REJECTED
        assert out["persisted"] == 1

    def test_la_persistenza_non_cambia_il_numero_di_valutazioni(self, db, tmp_path):
        add_open_signal(tmp_path)
        out = run_shadow(kills=live(), bankroll=1000.0, mode="live", persist=True,
                         shadow_path=tmp_path / "s.jsonl")
        assert out["shadow"] is True and out["evaluated"] == 1
        assert out["by_verdict"] == {"approve": 1}
        assert out["plans"][0]["decision_status"] == DECISION_STATUS_VALIDATED
