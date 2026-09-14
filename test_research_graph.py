"""Test del workflow agentico di ricerca (research_graph/) — 14/09/2026.

Tutto gira sui MOCK: nessuna rete, nessun provider, nessuna credenziale.
Copre i tre scenari della specifica e i tripwire dell'architettura:

  1. STOP IMMEDIATO   — il validator da' `pass` al 1o attempt e il flusso chiude;
  2. RETRY CON FEEDBACK — `fail` al 1o giro: il feedback strutturato guida il
     Research Agent in query mirate e il 2o attempt passa;
  3. HARD STOP        — 3 attempt sempre falliti: chiusura di sicurezza, zero
     loop infiniti (il validator viene chiamato esattamente 3 volte).

Piu': controlli deterministici Pydantic (schema, campi obbligatori, dedup),
coerenza del verdetto, agnosticismo dei provider (un adapter custom entra nel
grafo senza toccare nodi/edge) e guardie dell'engine (wiring rotto, ciclo).
"""

import pytest

from research_graph import (
    END, CompiledGraph, GraphError, MockLLMValidator, MockSearchTool, StateGraph,
    AttemptRecord, Finding, ResearchState, ValidationFeedback, ValidationRequest,
    ValidationVerdict, build_research_graph, run_research,
    verdict_fail, verdict_pass,
)

QUERY = "strategie di value betting 2026"


# ---------------------------------------------------------------------------
# Helper: payload di evidenze
# ---------------------------------------------------------------------------

def finding(claim: str, source: str = "https://example.org/a",
            confidence: float = 0.8, evidence: str = None, query: str = "") -> dict:
    return {
        "claim": claim,
        "evidence": evidence or f"evidenza concreta e verificabile per: {claim}",
        "source": source,
        "confidence": confidence,
        "query": query,
    }


def raw_schema_break() -> dict:
    """Payload malformato: `evidence` assente e confidence fuori range."""
    return {"claim": "claim senza evidenza", "source": "https://example.org/x",
            "confidence": 1.7}


# ---------------------------------------------------------------------------
# 0. Schema deterministico (Pydantic)
# ---------------------------------------------------------------------------

class TestSchemaPydantic:
    def test_finding_valido(self):
        f = Finding(**finding("il CLV batte il ROI come indicatore di edge"))
        assert f.confidence == 0.8
        assert f.key[0].startswith("il clv")

    @pytest.mark.parametrize("broken", [
        {"evidence": "evidenza lunga abbastanza per passare il minimo", "source": "x", "confidence": 0.5},
        {"claim": "claim ok"},                                   # mancano evidence/source/confidence
        {"claim": "c", "evidence": "corta", "source": "x", "confidence": 0.5},
        {"claim": "claim ok", "evidence": "evidenza lunga abbastanza", "source": "x", "confidence": 1.5},
    ])
    def test_campi_obbligatori_e_range(self, broken):
        with pytest.raises(Exception):
            Finding(**broken)

    def test_key_ignora_schema_e_protocollo(self):
        a = Finding(**finding("claim identico", source="https://www.example.org/a"))
        b = Finding(**finding("Claim Identico", source="http://example.org/a"))
        assert a.key == b.key

    def test_verdetto_fail_senza_feedback_rifiutato(self):
        with pytest.raises(Exception):
            ValidationVerdict(outcome="fail")
        with pytest.raises(Exception):
            ValidationVerdict(outcome="pass",
                              feedback=ValidationFeedback(reason="contraddittorio"))

    def test_factory_verdict(self):
        assert verdict_pass().passed and verdict_pass().feedback is None
        negative = verdict_fail("manca la copertura del costo", missing_claims=["costo"])
        assert negative.blocked and negative.feedback.kind == "semantic"


# ---------------------------------------------------------------------------
# 1. Stop immediato
# ---------------------------------------------------------------------------

class TestStopImmediato:
    def test_pass_al_primo_attempt_chiude_il_flusso(self):
        search = MockSearchTool({QUERY: [finding("claim A"), finding("claim B")]})
        validator = MockLLMValidator(["pass"])

        state = run_research(QUERY, search=search, validator=validator)

        assert state.status == "passed"
        assert state.attempt == 1
        assert state.validation_feedback is None      # nessun feedback su un pass
        assert len(state.findings) == 2
        assert search.calls == [QUERY]                # una sola ricerca generale
        assert validator.calls == 1
        assert state.node_trace == [
            "research", "deterministic_check", "semantic_validation",
            "decision:pass", "finalize",
        ]

    def test_il_validator_riceve_i_finding_validati(self):
        search = MockSearchTool({QUERY: [finding("claim A"), finding("claim B")]})
        validator = MockLLMValidator(["pass"])
        run_research(QUERY, search=search, validator=validator,
                     required_claims=["costo operativo"])

        request = validator.requests[0]
        assert isinstance(request, ValidationRequest)
        assert request.attempt == 1
        assert request.required_claims == ["costo operativo"]
        assert all(isinstance(f, Finding) for f in request.findings)
        assert len(request.findings) == 2

    def test_registro_attempt_coerente(self):
        search = MockSearchTool({QUERY: [finding("claim A"), finding("claim B")]})
        state = run_research(QUERY, search=search, validator=MockLLMValidator(["pass"]))

        assert len(state.attempts_log) == 1
        record = state.attempts_log[0]
        assert isinstance(record, AttemptRecord)
        assert record.attempt == 1 and record.verdict == "pass"
        assert record.queries == [QUERY] and record.findings_added == 2


# ---------------------------------------------------------------------------
# 2. Retry con feedback
# ---------------------------------------------------------------------------

class TestRetryConFeedback:
    def _run(self, **kwargs):
        search = MockSearchTool({
            QUERY: [finding("claim A"), finding("claim B")],
            f"{QUERY} costo operativo": [finding("claim C")],
        }, default=[])
        validator = MockLLMValidator([
            verdict_fail("le evidenze non coprono il costo operativo del sistema",
                         missing_claims=["costo operativo"],
                         suggested_queries=[f"{QUERY} costo operativo"]),
            "pass",
        ])
        state = run_research(QUERY, search=search, validator=validator,
                             required_claims=["costo operativo"], **kwargs)
        return state, search, validator

    def test_fail_genera_un_nuovo_attempt_che_passa(self):
        state, search, validator = self._run()

        assert state.status == "passed"
        assert state.attempt == 2                      # il retry e' avvenuto
        assert validator.calls == 2
        assert state.node_trace[0] == "research"
        assert "decision:retry" in state.node_trace
        assert state.node_trace.count("research") == 2
        assert state.node_trace[-2:] == ["decision:pass", "finalize"]

    def test_il_feedback_arriva_al_research_agent_come_query_mirate(self):
        state, search, _ = self._run()

        assert f"{QUERY} costo operativo" in search.calls     # query mirata eseguita
        # nessun duplicato della ricerca generale nel retry
        assert search.calls.count(QUERY) == 1
        assert len(state.queries_used) == len(set(q.lower() for q in state.queries_used))

    def test_findings_cumulativi_e_feedback_registrato(self):
        state, _, _ = self._run()

        assert [f.claim for f in state.findings] == ["claim A", "claim B", "claim C"]
        first = state.attempts_log[0]
        assert first.verdict == "fail" and first.feedback_kind == "semantic"
        assert "costo operativo" in (first.reason or "")
        assert state.attempts_log[1].verdict == "pass"
        assert state.validation_feedback is None       # ripulito dal pass

    def test_feedback_costruisce_query_deduplicate_e_limitate(self):
        feedback = ValidationFeedback(
            reason="lacune", missing_claims=["costo", "varianza"],
            suggested_queries=[f"{QUERY} costo", f"{QUERY} costo"],
            issues=["campo mancante", "range errato", "terzo problema"],
        )
        queries = feedback.queries_for_retry(QUERY, limit=3)
        assert len(queries) == 3
        assert queries[0] == f"{QUERY} costo"
        assert len(set(queries)) == len(queries)
        # il fallback garantisce che il retry abbia sempre qualcosa da chiedere
        assert ValidationFeedback(reason="x").queries_for_retry(QUERY) == [
            f"{QUERY} approfondimento"]


# ---------------------------------------------------------------------------
# 3. Hard stop (limite di sicurezza)
# ---------------------------------------------------------------------------

class TestHardStop:
    def test_tre_attempt_falliti_chiudono_il_flusso(self):
        # Il search tool restituisce SEMPRE le stesse due evidenze valide: il
        # gate deterministico passa a ogni giro, quindi e' il validator
        # semantico a bocciare — ed e' lui a dover essere chiamato 3 volte.
        valid = [finding("claim A"), finding("claim B")]
        search = MockSearchTool({QUERY: valid}, default=valid)
        validator = MockLLMValidator(default=verdict_fail(
            "evidenze non sufficienti sul costo operativo",
            missing_claims=["costo operativo"]))

        state = run_research(QUERY, search=search, validator=validator)

        assert state.status == "hard_stop"
        assert state.attempt == 3                      # esattamente il cap
        assert validator.calls == 3                    # nessun quarto giro
        assert state.node_trace.count("research") == 3
        assert state.node_trace[-1] == "hard_stop"
        assert "3 attempt" in state.error
        assert [r.verdict for r in state.attempts_log] == ["fail", "fail", "fail"]

    def test_il_retry_non_ripete_mai_la_stessa_query(self):
        search = MockSearchTool({QUERY: [finding("claim A"), finding("claim B")]}, default=[])
        validator = MockLLMValidator(default=verdict_fail("manca la copertura del costo"))
        state = run_research(QUERY, search=search, validator=validator)

        assert state.status == "hard_stop"
        assert len(search.calls) == len({q.lower() for q in search.calls})
        assert search.calls.count(QUERY) == 1

    def test_cap_configurabile(self):
        search = MockSearchTool({QUERY: [finding("claim A"), finding("claim B")]})
        validator = MockLLMValidator(default=verdict_fail("mai soddisfatto"))
        state = run_research(QUERY, search=search, validator=validator, max_attempts=1)

        assert state.status == "hard_stop" and state.attempt == 1
        assert validator.calls == 1

    def test_il_loop_non_esiste_a_livello_di_engine(self):
        """Un ciclo non protetto SOLLEVA invece di girare all'infinito."""
        graph = (StateGraph()
                 .add_node("a", lambda s: s)
                 .add_node("b", lambda s: s)
                 .set_entry_point("a")
                 .add_edge("a", "b")
                 .add_edge("b", "a")
                 .compile(max_steps=5))
        with pytest.raises(GraphError, match="ciclo"):
            graph.run(ResearchState(query=QUERY))


# ---------------------------------------------------------------------------
# 4. Controlli deterministici (gate prima dell'LLM)
# ---------------------------------------------------------------------------

class TestControlliDeterministici:
    def test_payload_malformato_blocca_prima_del_validator(self):
        search = MockSearchTool(
            {QUERY: [finding("claim A"), raw_schema_break()]},
            default=[finding("claim B"), finding("claim C")])
        validator = MockLLMValidator(["pass"])

        state = run_research(QUERY, search=search, validator=validator)

        assert state.status == "passed"
        assert state.attempt == 2
        # 1o giro: il gate deterministico boccia, l'LLM non viene interpellato
        assert validator.calls == 1
        assert state.attempts_log[0].feedback_kind == "deterministic"
        assert state.attempts_log[0].rejected == 1
        assert "schema" in (state.attempts_log[0].reason or "")

    def test_il_malformato_non_avvelena_i_retry(self):
        """Il payload respinto NON viene ri-validato: si registra e si prosegue."""
        search = MockSearchTool(
            {QUERY: [finding("claim A"), raw_schema_break()]},
            default=[finding("claim B"), finding("claim C")])
        state = run_research(QUERY, search=search, validator=MockLLMValidator(["pass"]))

        assert len(state.rejected) == 1                # una sola riga respinta
        assert state.rejected[0]["attempt"] == 1
        assert state.rejected[0]["index"] == 1
        assert state.rejected[0]["fields"] == ["confidence", "evidence"]
        # e tutti i finding validati arrivano al validator deduplicati
        assert len(state.findings) == 3

    def test_nessuna_evidenza_e_fail_deterministico(self):
        search = MockSearchTool({}, default=[])
        validator = MockLLMValidator(default="pass")
        state = run_research(QUERY, search=search, validator=validator)

        assert state.status == "hard_stop"
        assert state.attempts_log[0].feedback_kind == "deterministic"
        assert "nessuna evidenza" in (state.attempts_log[0].reason or "")

    def test_retry_senza_query_nuove_non_azzera_il_lavoro(self):
        """Query esaurite: il gate giudica il materiale gia' raccolto e passa
        la palla al validator semantico (che resta l'arbitro finale)."""
        valid = [finding("claim A"), finding("claim B")]
        search = MockSearchTool({QUERY: valid}, default=valid)
        validator = MockLLMValidator(default=verdict_fail(
            "manca il costo operativo", missing_claims=["costo operativo"]))

        state = run_research(QUERY, search=search, validator=validator)

        assert state.status == "hard_stop" and state.attempt == 3
        assert validator.calls == 3                 # consultato a ogni attempt
        assert state.findings                     # il materiale non si azzera
        assert state.attempts_log[2].feedback_kind == "semantic"
        assert state.attempts_log[2].queries == []  # nessuna query nuova: niente duplicati

    def test_minimo_findings_configurabile(self):
        search = MockSearchTool({QUERY: [finding("claim A")]})
        validator = MockLLMValidator(default="pass")

        ok = run_research(QUERY, search=search, validator=validator, min_findings=1)
        ko = run_research(QUERY, search=MockSearchTool({QUERY: [finding("claim A")]}),
                          validator=MockLLMValidator(default="pass"), min_findings=3)

        assert ok.status == "passed"
        assert ko.status == "hard_stop"
        assert "evidenze insufficienti" in (ko.attempts_log[0].reason or "")

    def test_dedup_dello_stesso_finding_da_query_diverse(self):
        same = finding("claim ripetuto", source="https://example.org/dup")
        search = MockSearchTool({QUERY: [same, dict(same), finding("claim B")]})
        state = run_research(QUERY, search=search, validator=MockLLMValidator(["pass"]))

        assert [f.claim for f in state.findings] == ["claim ripetuto", "claim B"]

    def test_esito_di_fail_scarico_resta_ciclabile(self):
        """Un feedback scarno produce comunque query (fail-safe del retry)."""
        valid = [finding("claim A"), finding("claim B")]
        search = MockSearchTool({QUERY: valid}, default=valid)
        validator = MockLLMValidator([verdict_fail(""), "pass"])
        state = run_research(QUERY, search=search, validator=validator)

        assert state.status == "passed" and state.attempt == 2
        assert f"{QUERY} approfondimento" in search.calls


# ---------------------------------------------------------------------------
# 5. Agnosticismo rispetto ai provider
# ---------------------------------------------------------------------------

class AdapterSearch:
    """Adapter "reale" finto: nessuna parentela coi Mock, solo il contratto."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    def search(self, query):
        self.calls.append(query)
        return self.batches.pop(0) if self.batches else []


class AdapterValidator:
    """Validator custom: decide in base ai finding ricevuti (dict JSON)."""

    def __init__(self, needed_claim):
        self.needed = needed_claim
        self.seen = []

    def validate(self, request):
        self.seen.append(request)
        covered = any(self.needed in f.claim for f in request.findings)
        if covered:
            return {"outcome": "pass"}
        return {"outcome": "fail",
                "feedback": {"reason": "lacuna non coperta",
                             "missing_claims": [self.needed],
                             "suggested_queries": [f"{QUERY} {self.needed}"]}}


class TestAgnosticismoProvider:
    def test_adapter_custom_entra_nel_grafo_senza_modifiche(self):
        search = AdapterSearch([[finding("claim A")],
                                [finding("claim B")],
                                [finding("copre il costo operativo")]])
        validator = AdapterValidator("costo operativo")

        state = run_research(QUERY, search=search, validator=validator, min_findings=1)

        assert state.status == "passed" and state.attempt == 2
        assert f"{QUERY} costo operativo" in search.calls
        assert all(isinstance(f, Finding) for f in validator.seen[-1].findings)

    def test_verdetto_normalizzato_da_pydantic(self):
        """Stringhe e dict sono accettati: e' il nodo a normalizzarli."""
        for raw in ["pass", "PASS", "ok", {"outcome": "pass"}]:
            state = run_research(
                QUERY, search=MockSearchTool({QUERY: [finding("claim A"), finding("claim B")]}),
                validator=MockLLMValidator([raw]))
            assert state.status == "passed", raw

    def test_search_tool_come_callable(self):
        def tool(query):
            return [finding("claim A"), finding("claim B")] if query == QUERY else []

        state = run_research(QUERY, search=MockSearchTool(tool),
                             validator=MockLLMValidator(["pass"]))
        assert state.status == "passed" and len(state.findings) == 2

    def test_wiring_esposto_e_riusabile(self):
        graph = build_research_graph(MockSearchTool({QUERY: [finding("claim A"), finding("claim B")]}),
                                     MockLLMValidator(default="pass"))
        assert isinstance(graph, CompiledGraph)
        assert graph.node_names == ["research", "deterministic_check",
                                    "semantic_validation", "decision",
                                    "finalize", "hard_stop"]
        state = graph.run(ResearchState(query=QUERY))
        assert state.status == "passed"


# ---------------------------------------------------------------------------
# 6. Guardie dell'engine (wiring) e fail-safe
# ---------------------------------------------------------------------------

class TestEngineGrafo:
    def test_nodo_senza_uscite(self):
        graph = StateGraph().add_node("solo", lambda s: s).set_entry_point("solo")
        with pytest.raises(GraphError, match="non ha uscite"):
            graph.compile()

    def test_entry_point_mancante(self):
        with pytest.raises(GraphError, match="entry point"):
            StateGraph().add_node("a", lambda s: s).compile()

    def test_target_inesistente(self):
        graph = (StateGraph().add_node("a", lambda s: s)
                 .set_entry_point("a").add_edge("a", "fantasma"))
        with pytest.raises(GraphError, match="non registrato"):
            graph.compile()

    def test_due_uscite_dallo_stesso_nodo(self):
        graph = StateGraph().add_node("a", lambda s: s).set_entry_point("a")
        graph.add_edge("a", END)
        with pytest.raises(GraphError, match="gia' un'uscita"):
            graph.add_edge("a", END)

    def test_route_non_mappata(self):
        graph = (StateGraph().add_node("a", lambda s: s)
                 .set_entry_point("a")
                 .add_conditional_edges("a", lambda s: "boh", {"ok": END})
                 .compile())
        with pytest.raises(GraphError, match="non mappata"):
            graph.run(ResearchState(query=QUERY))

    def test_nodo_duplicato(self):
        graph = StateGraph().add_node("a", lambda s: s)
        with pytest.raises(GraphError, match="duplicato"):
            graph.add_node("a", lambda s: s)


class TestFailSafe:
    def test_errore_imprevisto_non_propaga(self):
        """Un errore di configurazione chiude lo stato, non il processo."""
        state = run_research(QUERY, search=MockSearchTool(), validator=MockLLMValidator(),
                             max_attempts=0)
        assert state.status == "error"
        assert "max_attempts" in state.error
        assert state.node_trace == ["error"]

    def test_search_tool_che_solleva_non_rompe_il_giro(self):
        class BrokenTool:
            def search(self, query):
                raise ConnectionError("rete giu'")

        state = run_research(QUERY, search=BrokenTool(), validator=MockLLMValidator(default="pass"))

        assert state.status == "hard_stop"              # fail-closed ordinato
        assert state.attempts_log[0].errors == ["ConnectionError: rete giu'"]
        assert state.attempts_log[0].feedback_kind == "deterministic"

    def test_validator_che_solleva_e_fail_closed(self):
        class BrokenValidator:
            def validate(self, request):
                raise TimeoutError("LLM non raggiungibile")

        search = MockSearchTool({QUERY: [finding("claim A"), finding("claim B")]})
        state = run_research(QUERY, search=search, validator=BrokenValidator())

        assert state.status == "hard_stop" and state.attempt == 3
        assert state.attempts_log[0].feedback_kind == "semantic"
        assert "TimeoutError" in (state.attempts_log[0].reason or "")

    def test_max_attempts_non_valido(self):
        with pytest.raises(ValueError):
            build_research_graph(MockSearchTool(), MockLLMValidator(), max_attempts=0)


# ---------------------------------------------------------------------------
# 7. Bootstrap ambiente (chiavi da .env/vault, non dal codice)
# ---------------------------------------------------------------------------

class TestBootstrapAmbiente:
    def test_ensure_env_idempotente_e_fail_safe(self):
        from research_graph import _env

        assert _env.ensure_env() is None      # mai eccezioni
        assert _env.ensure_env() is None      # idempotente
        assert _env._loaded is True

    def test_gli_adapter_real_preparano_l_ambiente(self, monkeypatch):
        """Le chiavi si leggono SOLO da env: il bootstrap deve restare agganciato."""
        from research_graph import exa_search, gemini_validator

        calls = []
        monkeypatch.setattr(exa_search, "ensure_env", lambda: calls.append("exa"))
        monkeypatch.setattr(gemini_validator, "ensure_env", lambda: calls.append("gemini"))
        monkeypatch.delenv("EXA_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        exa_search.exa_configured()
        gemini_validator.gemini_configured()

        assert calls == ["exa", "gemini"]
