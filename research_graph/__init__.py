"""research_graph — workflow agentico di ricerca su grafo.

Validazione IBRIDA (deterministica con Pydantic + semantica con LLM) e ciclo
di retry con feedback strutturato, cap a 3 attempt. Agnostico rispetto ai
provider: i contratti sono `SearchTool` e `Validator` (`providers.py`), quindi
gli adapter reali si sostituiscono ai Mock senza toccare nodi ed edge.

Uso tipico (con i Mock, offline):

    from research_graph import MockLLMValidator, MockSearchTool, run_research

    stato = run_research("obiettivo di ricerca",
                         search=MockSearchTool(...),
                         validator=MockLLMValidator(...))
    print(stato.status, stato.attempt, stato.findings)
"""

from __future__ import annotations

from .exa_search import (
    DEFAULT_SEARCH_TYPE, ExaSearchError, ExaSearchTool, default_search_type,
    exa_configured, result_to_finding,
)
from .gemini_validator import (
    GeminiValidator, GeminiValidatorError, extract_json, gemini_configured,
    verdict_from_data,
)
from .graph import (
    END, CompiledGraph, GraphError, StateGraph, build_research_graph,
    persist_trace, run_research,
)
from .models import (
    MAX_ATTEMPTS, MAX_RETRY_QUERIES, MIN_EVIDENCE_CHARS, MIN_FINDINGS,
    AttemptRecord, Finding, ResearchState, ValidationFeedback,
    ValidationRequest, ValidationVerdict, verdict_fail, verdict_pass,
)
from .providers import MockLLMValidator, MockSearchTool, SearchTool, Validator

__all__ = [
    # engine + wiring
    "StateGraph", "CompiledGraph", "GraphError", "END",
    "build_research_graph", "run_research",
    # schema e stato
    "Finding", "ValidationFeedback", "ValidationVerdict", "ValidationRequest",
    "AttemptRecord", "ResearchState", "verdict_pass", "verdict_fail",
    "MIN_FINDINGS", "MIN_EVIDENCE_CHARS", "MAX_ATTEMPTS", "MAX_RETRY_QUERIES",
    # contratti + mock
    "SearchTool", "Validator", "MockSearchTool", "MockLLMValidator",
    # adapter reali (le credenziali vivono SOLO nell'ambiente)
    "ExaSearchTool", "ExaSearchError", "exa_configured", "result_to_finding",
    "DEFAULT_SEARCH_TYPE", "default_search_type",
    "GeminiValidator", "GeminiValidatorError", "gemini_configured",
    "extract_json", "verdict_from_data",
]

# NB: la persistenza dei trace vive in `research_graph.trace_store` (import
# esplicito: tocca il volume via `config.DATA_DIR`, quindi non e' un side
# effect che il solo `import research_graph` deve attivare).
#   from research_graph.trace_store import TraceStore, summary, format_report
