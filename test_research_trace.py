"""Test della persistenza dei trace del workflow di ricerca (trace_store.py).

Copre: costruzione del record, append JSONL, lettura/finestra/limite, riepilogo
e report, integrazione col runner (`trace_store=`) e — soprattutto — il
FAIL-SAFE: un disco che non collabora non deve mai rompere una ricerca.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from research_graph import MockLLMValidator, MockSearchTool, run_research, verdict_fail
from research_graph.graph import persist_trace
from research_graph.models import ResearchState, verdict_pass
from research_graph import trace_store

QUERY = "strategie di value betting 2026"


def finding(claim):
    return {"claim": claim,
            "evidence": f"evidenza concreta e verificabile per {claim}",
            "source": "https://example.org/a", "confidence": 0.8}


@pytest.fixture()
def log(tmp_path) -> Path:
    return tmp_path / "research" / "traces.jsonl"


def _passing_state():
    return run_research(QUERY,
                        search=MockSearchTool({QUERY: [finding("claim A"), finding("claim B")]}),
                        validator=MockLLMValidator(["pass"]))


def _blocked_state():
    valid = [finding("claim A"), finding("claim B")]
    return run_research(QUERY, search=MockSearchTool({QUERY: valid}, default=valid),
                        validator=MockLLMValidator(default=verdict_fail(
                            "manca la copertura del costo operativo",
                            missing_claims=["costo operativo"])))


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------

class TestBuildTrace:
    def test_record_completo_di_un_run_passato(self):
        state = _passing_state()
        record = trace_store.build_trace(state)

        assert record["query"] == QUERY
        assert record["status"] == "passed"
        assert record["attempt"] == 1
        assert record["findings"] == 2
        assert record["queries_used"] == [QUERY]
        assert record["run_id"] and record["ts_epoch"] > 0
        assert "feedback" not in record
        assert len(record["attempts"]) == 1
        assert record["node_trace"][-1] == "finalize"

    def test_record_di_un_hard_stop_porta_il_feedback(self):
        record = trace_store.build_trace(_blocked_state())

        assert record["status"] == "hard_stop"
        assert record["attempt"] == 3
        assert record["feedback"]["kind"] == "semantic"
        assert record["feedback"]["missing_claims"] == ["costo operativo"]
        assert "limite di 3 attempt" in record["error"]

    def test_findings_inclusi_solo_su_richiesta(self):
        state = _passing_state()
        assert "validated_findings" not in trace_store.build_trace(state)
        full = trace_store.build_trace(state, include_findings=True)
        assert full["validated_findings"][0]["claim"] == "claim A"

    def test_run_id_ed_extra(self):
        record = trace_store.build_trace(ResearchState(query="q"), run_id="abc123",
                                         extra={"origine": "test"})
        assert record["run_id"] == "abc123"
        assert record["extra"] == {"origine": "test"}
        assert record["status"] == "pending"


# ---------------------------------------------------------------------------
# Scrittura / lettura
# ---------------------------------------------------------------------------

class TestSaveAndLoad:
    def test_append_jsonl(self, log):
        trace_store.save_trace(_passing_state(), path=log)
        trace_store.save_trace(_blocked_state(), path=log)

        rows = [json.loads(line) for line in log.read_text().splitlines()]
        assert len(rows) == 2
        assert [r["status"] for r in rows] == ["passed", "hard_stop"]

    def test_iter_traces_dal_piu_recente_e_con_limite(self, log):
        trace_store.save_trace(_passing_state(), path=log, run_id="primo")
        trace_store.save_trace(_blocked_state(), path=log, run_id="secondo")

        rows = trace_store.iter_traces(log)
        assert [r["run_id"] for r in rows] == ["secondo", "primo"]
        assert len(trace_store.iter_traces(log, limit=1)) == 1

    def test_righe_corrotte_ignorate(self, log):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("{non json}\n\n\"stringa\"\n"
                       + json.dumps({"status": "passed", "query": "ok"}) + "\n")

        rows = trace_store.iter_traces(log)
        assert len(rows) == 1 and rows[0]["query"] == "ok"

    def test_finestra_temporale(self, log):
        now = datetime.now(timezone.utc)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join(json.dumps(r) for r in [
            {"status": "passed", "ts_epoch": now.timestamp()},
            {"status": "hard_stop", "ts_epoch": (now - timedelta(days=10)).timestamp()},
        ]) + "\n")

        recent = trace_store.iter_traces(log, days=2)
        assert [r["status"] for r in recent] == ["passed"]
        assert len(trace_store.iter_traces(log)) == 2

    def test_log_inesistente_non_esplode(self, tmp_path):
        missing = tmp_path / "niente" / "traces.jsonl"
        assert trace_store.iter_traces(missing) == []
        assert trace_store.summary(missing)["runs"] == 0
        assert trace_store.format_report(missing) is None


# ---------------------------------------------------------------------------
# Fail-safe
# ---------------------------------------------------------------------------

class TestFailSafe:
    def test_scrittura_impossibile_non_solleva(self, tmp_path):
        blocker = tmp_path / "file-non-cartella"
        blocker.write_text("occupato")

        record = trace_store.save_trace(_passing_state(), path=blocker / "traces.jsonl")

        assert record["status"] == "passed"          # il record resta valido
        assert "write_failed" in record["error"]     # e l'errore e' visibile

    def test_sink_rotto_o_senza_save_non_rompe_il_run(self):
        class SinkRotto:
            def save(self, state):
                raise OSError("disco pieno")

        class NonSink:
            pass

        for sink in (SinkRotto(), NonSink()):
            state = run_research(
                QUERY,
                search=MockSearchTool({QUERY: [finding("claim A"), finding("claim B")]}),
                validator=MockLLMValidator(["pass"]),
                trace_store=sink)
            assert state.status == "passed"

        assert persist_trace(ResearchState(query="q"), None) is None

    def test_trace_scritto_anche_sul_percorso_di_errore(self, log):
        state = run_research(QUERY, search=MockSearchTool(), validator=MockLLMValidator(),
                             max_attempts=0, trace_store=trace_store.TraceStore(log))

        assert state.status == "error"
        rows = trace_store.iter_traces(log)
        assert len(rows) == 1 and rows[0]["status"] == "error"


# ---------------------------------------------------------------------------
# Integrazione col runner + riepilogo + CLI
# ---------------------------------------------------------------------------

class TestIntegrazioneEReport:
    def test_trace_store_scrive_e_ritorna_il_record(self, log):
        store = trace_store.TraceStore(log, include_findings=True)
        state = run_research(
            QUERY,
            search=MockSearchTool({QUERY: [finding("claim A"), finding("claim B")]}),
            validator=MockLLMValidator(["pass"]),
            trace_store=store)

        assert state.status == "passed"
        assert len(store.saved) == 1
        row = json.loads(log.read_text().splitlines()[0])
        assert row["run_id"] == store.saved[0]["run_id"]     # lo stesso record
        assert row["validated_findings"][0]["claim"] == "claim A"

    def test_summary_aggrega(self, log):
        trace_store.save_trace(_passing_state(), path=log)
        trace_store.save_trace(_blocked_state(), path=log)
        trace_store.save_trace(_blocked_state(), path=log)

        data = trace_store.summary(log)

        assert data["runs"] == 3
        assert data["passed"] == 1
        assert data["pass_rate"] == pytest.approx(0.3333, abs=1e-3)
        assert data["avg_attempts"] == pytest.approx((1 + 3 + 3) / 3, abs=1e-2)
        assert data["by_status"]["hard_stop"] == 2
        assert data["by_feedback_kind"]["semantic"] == 2
        assert data["top_blockers"][0][1] == 2
        assert data["last"]["status"] == "hard_stop"

    def test_format_report(self, log):
        trace_store.save_trace(_blocked_state(), path=log)
        report = trace_store.format_report(log, days=7)

        assert "Research graph" in report and "hard_stop" in report
        assert "costo operativo" in report
        assert "ultimi 7gg" in report

    def test_cli_json_e_testuale(self, log, capsys):
        trace_store.save_trace(_passing_state(), path=log)

        assert trace_store.main(["--path", str(log), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["runs"] == 1
        assert payload["traces"][0]["status"] == "passed"

        assert trace_store.main(["--path", str(log), "--limit", "1"]) == 0
        out = capsys.readouterr().out
        assert "passed" in out and QUERY[:20] in out

        assert trace_store.main(["--path", str(log.parent / "vuoto.jsonl")]) == 0
        assert "nessun trace" in capsys.readouterr().out

    def test_persist_trace_ritorna_il_record(self, log):
        record = persist_trace(_passing_state(), trace_store.TraceStore(log))
        assert record["run_id"] and trace_store.iter_traces(log)[0]["run_id"] == record["run_id"]
