"""Test dell'adapter REALE del validator semantico (Gemini, google-genai).

La suite gira OFFLINE: il client `google-genai` e' iniettato come finto, quindi
il prompt, il parsing del JSON, il fail-closed e l'integrazione col grafo sono
verificati senza rete, senza chiave e senza costi. La chiamata vera resta nel
test marcato `integration` (saltato senza `GOOGLE_API_KEY`).
"""

import json

import pytest

from research_graph import (
    GeminiValidator, GeminiValidatorError, MockLLMValidator, MockSearchTool,
    ValidationRequest, extract_json, gemini_configured, run_research,
    verdict_from_data,
)
from research_graph.gemini_validator import JSON_CONTRACT

QUERY = "strategie di value betting 2026"

# Credenziale FINTA per i test, marcata `fake/` (convenzione del progetto):
FAKE_KEY = "fake/gemini-not-a-credential"


def finding(claim, evidence=None, source="https://example.org/a", confidence=0.8):
    return {"claim": claim,
            "evidence": evidence or f"evidenza concreta e verificabile per {claim}",
            "source": source, "confidence": confidence}


def request(query=QUERY, claims=(), findings=(), attempt=1):
    return ValidationRequest(
        query=query, required_claims=list(claims),
        findings=[__import__("research_graph").Finding(**f) for f in findings],
        attempt=attempt)


# ---------------------------------------------------------------------------
# Client finto (zero rete)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, text=None, candidates=None):
        self.text = text
        self.candidates = candidates or []


class FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate_content(self, model=None, contents=None, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if not self.responses:
            raise RuntimeError("nessuna risposta scriptata")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeResponse(text=nxt)


class FakeGenaiClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)


def config_value(config, key):
    """Il config e' un `GenerateContentConfig` (SDK presente) o un dict."""
    if hasattr(config, key):
        return getattr(config, key)
    return config.get(key)


# ---------------------------------------------------------------------------
# 1. Prompt deterministico
# ---------------------------------------------------------------------------

class TestPrompt:
    def test_contiene_query_claim_ed_evidenze(self):
        validator = GeminiValidator(client=FakeGenaiClient([]))
        prompt = validator.build_prompt(request(
            claims=["copertura del costo operativo"],
            findings=[finding("il CLV batte il ROI", evidence="estratto concreto sul CLV")]))

        assert QUERY in prompt
        assert "copertura del costo operativo" in prompt
        assert "estratto concreto sul CLV" in prompt
        assert "https://example.org/a" in prompt
        assert "0.80" in prompt                      # confidenza
        assert JSON_CONTRACT in prompt
        assert "TENTATIVO: 1" in prompt

    def test_senza_claim_espliciti_lo_dice(self):
        prompt = GeminiValidator(client=FakeGenaiClient([])).build_prompt(request())
        assert "nessuno esplicito" in prompt

    def test_limite_al_numero_di_finding_nel_prompt(self):
        validator = GeminiValidator(client=FakeGenaiClient([]), max_findings=2)
        prompt = validator.build_prompt(request(
            findings=[finding(f"claim {i}") for i in range(1, 6)]))

        assert "claim 1" in prompt and "claim 2" in prompt
        assert "claim 3" not in prompt
        assert "(5)" in prompt                        # il totale resta visibile

    def test_la_chiave_api_non_finisce_nel_prompt(self):
        validator = GeminiValidator(FAKE_KEY,
                                   client=FakeGenaiClient(['{"outcome": "pass"}']))
        validator.validate(request(findings=[finding("claim A")]))

        assert FAKE_KEY not in validator.build_prompt(request())


# ---------------------------------------------------------------------------
# 2. Parsing del verdetto (difensivo)
# ---------------------------------------------------------------------------

class TestVerdictParsing:
    def test_pass(self):
        verdict = verdict_from_data({"outcome": "pass"})
        assert verdict.passed and verdict.feedback is None

    def test_fail_completo(self):
        verdict = verdict_from_data({
            "outcome": "fail", "reason": "manca il costo operativo",
            "missing_claims": ["costo operativo"],
            "suggested_queries": ["costi the-odds-api 2026"],
            "issues": ["fonte troppo generica"],
        })
        assert verdict.blocked
        assert verdict.feedback.kind == "semantic"
        assert verdict.feedback.reason == "manca il costo operativo"
        assert verdict.feedback.suggested_queries == ["costi the-odds-api 2026"]

    def test_fail_senza_motivazione_resta_utile_al_retry(self):
        verdict = verdict_from_data({"outcome": "fail"})
        assert verdict.blocked and verdict.feedback.reason
        assert verdict.feedback.queries_for_retry(QUERY)   # il retry ha istruzioni

    def test_feedback_annidato_e_stringa_singola(self):
        verdict = verdict_from_data({
            "outcome": "fail",
            "feedback": {"reason": "lacuna", "missing_claims": "costo"}})
        assert verdict.feedback.missing_claims == ["costo"]

    @pytest.mark.parametrize("payload", [
        "```json\n{\"outcome\": \"pass\"}\n```",
        "Ecco il verdetto: {\"outcome\": \"pass\"} — fine.",
        json.dumps({"outcome": "PASS"}),
    ])
    def test_json_robusto_a_fence_e_prosa(self, payload):
        assert verdict_from_data(extract_json(payload)).passed

    @pytest.mark.parametrize("bad", ["", "nessun json qui", "{rotto"])
    def test_json_illeggibile_solleva(self, bad):
        with pytest.raises(ValueError):
            extract_json(bad)

    def test_outcome_sconosciuto_rifiutato(self):
        with pytest.raises(ValueError, match="outcome"):
            verdict_from_data({"outcome": "boh"})

    def test_valori_non_lista_normalizzati(self):
        # un campo scalare al posto di una lista non deve rompere il verdetto
        verdict = verdict_from_data({"outcome": "fail", "reason": "x", "missing_claims": 42})
        assert verdict.blocked and verdict.feedback.missing_claims == ["42"]


# ---------------------------------------------------------------------------
# 3. Chiamata al modello (client finto)
# ---------------------------------------------------------------------------

class TestGeminiValidator:
    def test_pass_end_to_end(self):
        client = FakeGenaiClient(['{"outcome": "pass", "reason": "tutto coperto"}'])
        validator = GeminiValidator(client=client)

        verdict = validator.validate(request(
            claims=["CLV"], findings=[finding("il CLV batte il ROI")]))

        assert verdict.passed
        assert client.models.calls[0]["model"] == validator._model_name
        assert config_value(client.models.calls[0]["config"], "response_mime_type") == "application/json"
        assert QUERY in client.models.calls[0]["contents"]

    def test_fail_end_to_end_con_feedback(self):
        client = FakeGenaiClient([json.dumps({
            "outcome": "fail", "reason": "manca il costo operativo",
            "missing_claims": ["costo operativo"],
            "suggested_queries": [f"{QUERY} costo operativo"]})])
        verdict = GeminiValidator(client=client).validate(request())

        assert verdict.blocked
        assert verdict.feedback.suggested_queries == [f"{QUERY} costo operativo"]

    def test_risposta_illeggibile_e_fail_closed(self):
        """Un JSON rotto NON diventa un pass: fail con feedback + errore tracciato."""
        validator = GeminiValidator(client=FakeGenaiClient(["<html>errore</html>"]))
        verdict = validator.validate(request())

        assert verdict.blocked
        assert "non interpretabile" in verdict.feedback.reason
        assert verdict.feedback.issues
        assert "nessun oggetto JSON" in validator.last_error

    def test_errore_di_trasporto_solleva(self):
        validator = GeminiValidator(client=FakeGenaiClient([TimeoutError("timeout")]))
        with pytest.raises(GeminiValidatorError, match="TimeoutError"):
            validator.validate(request())

    def test_testo_dai_candidates_se_text_manca(self):
        """Alcune versioni dell'SDK non popolano `response.text`: si legge dai parts."""
        part = type("Part", (), {"text": '{"outcome": "pass"}'})()
        content = type("Content", (), {"parts": [part]})()
        candidate = type("Candidate", (), {"content": content})()
        response = type("Response", (), {"text": None, "candidates": [candidate]})()

        client = FakeGenaiClient([])
        client.models.generate_content = lambda model=None, contents=None, config=None: response

        assert GeminiValidator(client=client).validate(request()).passed

    def test_senza_client_si_costruisce_da_env_o_e_fail_closed(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        validator = GeminiValidator()

        assert not validator.configured()
        with pytest.raises(GeminiValidatorError, match="GOOGLE_API_KEY"):
            validator.validate(request())

        monkeypatch.setenv("GOOGLE_API_KEY", FAKE_KEY)
        assert GeminiValidator().configured()

    def test_client_iniettato_e_considerato_configurato(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        assert GeminiValidator(client=FakeGenaiClient([])).configured()


# ---------------------------------------------------------------------------
# 4. Integrazione col grafo (il retry guidato dall'LLM)
# ---------------------------------------------------------------------------

class TestGeminiNelGrafo:
    def test_llm_boccia_il_retry_colma_e_passa(self):
        client = FakeGenaiClient([
            json.dumps({"outcome": "fail", "reason": "manca il costo operativo",
                        "missing_claims": ["costo operativo"],
                        "suggested_queries": [f"{QUERY} costo operativo"]}),
            '{"outcome": "pass"}',
        ])
        search = MockSearchTool({
            QUERY: [finding("claim A"), finding("claim B")],
            f"{QUERY} costo operativo": [finding("copre il costo operativo reale")],
        }, default=[])

        state = run_research(QUERY, search=search,
                             validator=GeminiValidator(client=client),
                             required_claims=["costo operativo"])

        assert state.status == "passed" and state.attempt == 2
        assert f"{QUERY} costo operativo" in search.calls
        assert [f.claim for f in state.findings][-1] == "copre il costo operativo reale"

    def test_llm_irraggiungibile_e_fail_closed_con_cap(self):
        validator = GeminiValidator(client=FakeGenaiClient([ConnectionError("giu'")]))
        state = run_research(QUERY,
                             search=MockSearchTool({QUERY: [finding("claim A"), finding("claim B")]}),
                             validator=validator)

        assert state.status == "hard_stop" and state.attempt == 3
        assert state.attempts_log[0].feedback_kind == "semantic"
        assert "ConnectionError" in (state.attempts_log[0].reason or "")

    def test_nessuna_chiamata_al_modello_se_il_gate_deterministico_boccia(self):
        client = FakeGenaiClient(['{"outcome": "pass"}'])
        search = MockSearchTool({QUERY: [{"claim": "x", "confidence": 2.0}]}, default=[])

        state = run_research(QUERY, search=search,
                             validator=GeminiValidator(client=client))

        assert state.status == "hard_stop"
        assert client.models.calls == []            # l'LLM non e' mai stato chiamato


@pytest.mark.integration
@pytest.mark.skipif(not gemini_configured(), reason="GOOGLE_API_KEY non configurata")
def test_gemini_chiamata_vera_solo_con_chiave():
    """Verifica manuale dell'integrazione reale (usa rete + quota Gemini)."""
    validator = GeminiValidator()
    verdict = validator.validate(request(
        claims=["il CLV vig-free batte il ROI come indicatore di edge"],
        findings=[finding("Intuizione: il CLV e' un buon indicatore",
                          evidence="Il CLV vig-free calcolato sulla closing line "
                                   "devigata batte il ROI come predittore di edge "
                                   "nel lungo periodo.")]))
    assert verdict.passed or verdict.blocked        # un verdetto valido in ogni caso
    if verdict.passed:
        assert verdict.feedback is None             # coerenza del contratto
    else:
        assert verdict.feedback.reason and verdict.feedback.issues
