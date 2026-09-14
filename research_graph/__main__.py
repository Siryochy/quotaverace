"""Demo CLI del workflow (`venv/bin/python -m research_graph [scenario] [opzioni]`).

Scenari (tutti OFFLINE, sui Mock: nessuna rete, nessun credito):
  retry      fail semantico al 1o attempt -> il feedback guida query mirate
             -> pass al 2o attempt;
  hard-stop  il validator fallisce sempre -> 3 attempt e chiusura di sicurezza;
  schema     il search tool restituisce un finding malformato -> il controllo
             deterministico (Pydantic) blocca il giro prima dell'LLM;
  live       adapter REALI: ricerca Exa (richiede `EXA_API_KEY`) + validator
             Gemini (se c'e' `GOOGLE_API_KEY`, altrimenti mock);
  all        i tre scenari offline, in sequenza.

Opzioni:
  --trace            persiste ogni run e mostra il riepilogo dei trace;
  --trace-path PATH  path del log dei trace (default: env RESEARCH_TRACE_DIR).
"""

from __future__ import annotations

import sys
from pathlib import Path

from .exa_search import ExaSearchTool, exa_configured
from .gemini_validator import GeminiValidator, gemini_configured
from .graph import run_research
from .models import verdict_fail
from .providers import MockLLMValidator, MockSearchTool

QUERY = "strategie di value betting 2026 basate sul CLV"

FINDING_CLV = {
    "claim": "il CLV e' il miglior indicatore di edge nel lungo periodo",
    "evidence": "il CLV vig-free calcolato sulla closing line devigata batte il ROI come predittore di edge",
    "source": "https://example.org/clv-vig-free-2026",
    "confidence": 0.72,
}
FINDING_SAMPLE = {
    "claim": "campioni piccoli rendono inaffidabile il ROI",
    "evidence": "con meno di 100 scommesse chiuse la varianza del ROI domina il segnale",
    "source": "https://example.org/variance-small-samples",
    "confidence": 0.65,
}
FINDING_COST = {
    "claim": "il costo operativo delle quote va contato nel rendimento",
    "evidence": "the-odds-api addebita un credito per mercato e regione a ogni chiamata",
    "source": "https://example.org/odds-api-pricing",
    "confidence": 0.8,
}
MALFORMED = {  # manca 'evidence' e la confidence e' fuori range
    "claim": "claim senza evidenza",
    "source": "https://example.org/broken",
    "confidence": 1.7,
}

SETUP_EXA = "https://dashboard.exa.ai  (EXA_API_KEY)"


def _print_state(state) -> None:
    print(f"  esito      : {state.status} (attempt={state.attempt})")
    print(f"  query      : {state.queries_used}")
    print(f"  findings   : {len(state.findings)} validati, {len(state.rejected)} respinti")
    print(f"  trace      : {' -> '.join(state.node_trace)}")
    if state.validation_feedback is not None:
        fb = state.validation_feedback
        print(f"  feedback   : [{fb.kind}] {fb.reason}")
        if fb.issues:
            print(f"               issues: {fb.issues}")
    if state.error:
        print(f"  errore     : {state.error}")
    print("  per-finding:")
    for finding in state.findings:
        print(f"    - [{finding.confidence:.2f}] {finding.claim}")


def scenario_retry(trace_store=None) -> None:
    print("\n=== SCENARIO 1: retry con feedback (fail semantico -> pass) ===")
    search = MockSearchTool({
        QUERY: [FINDING_CLV, FINDING_SAMPLE],
        f"{QUERY} costo operativo": [FINDING_COST],
    })
    validator = MockLLMValidator([
        verdict_fail("le evidenze non coprono il costo operativo del sistema",
                     missing_claims=["costo operativo"],
                     suggested_queries=[f"{QUERY} costo operativo"]),
        "pass",
    ])
    state = run_research(QUERY, search=search, validator=validator,
                         required_claims=["costo operativo"], trace_store=trace_store)
    _print_state(state)
    print(f"  chiamate search tool: {len(search.calls)} -> {search.calls}")


def scenario_hard_stop(trace_store=None) -> None:
    print("\n=== SCENARIO 2: hard stop (3 attempt, mai un loop infinito) ===")
    valid = [FINDING_CLV, FINDING_SAMPLE]
    search = MockSearchTool({QUERY: valid}, default=valid)
    validator = MockLLMValidator(default=verdict_fail(
        "evidenze non sufficienti sul costo operativo",
        missing_claims=["costo operativo"]))
    state = run_research(QUERY, search=search, validator=validator, trace_store=trace_store)
    _print_state(state)
    print(f"  chiamate validator  : {validator.calls} (cap a 3 attempt)")


def scenario_schema(trace_store=None) -> None:
    print("\n=== SCENARIO 3: controllo deterministico (Pydantic) ===")
    search = MockSearchTool({QUERY: [FINDING_CLV, MALFORMED]}, default=[FINDING_SAMPLE])
    validator = MockLLMValidator(["pass"])
    state = run_research(QUERY, search=search, validator=validator, trace_store=trace_store)
    _print_state(state)
    print(f"  respinti   : {state.rejected}")
    print(f"  validator chiamato: {validator.calls} volta/e (1o giro bloccato dal controllo Pydantic)")


def scenario_live(query: str = QUERY, trace_store=None) -> int:
    """Adapter REALI: Exa per la ricerca, Gemini per la validazione semantica."""
    print("\n=== SCENARIO LIVE: adapter reali (Exa + Gemini) ===")
    if not exa_configured():
        print(f"  EXA_API_KEY non configurata: impostala nell'ambiente, mai nel codice.")
        print(f"  Chiave: {SETUP_EXA}")
        return 2

    search = ExaSearchTool(num_results=5)
    if gemini_configured():
        validator = GeminiValidator()
        print("  validator: Gemini reale")
    else:
        validator = MockLLMValidator(default="pass")
        print("  validator: MOCK (GOOGLE_API_KEY assente) — solo la ricerca e' reale")

    state = run_research(query, search=search, validator=validator,
                         min_findings=1, trace_store=trace_store)
    _print_state(state)
    print(f"  pagine raccolte da Exa: {len(state.raw_findings)}")
    return 0 if state.status == "passed" else 1


SCENARIOS = {
    "retry": scenario_retry,
    "hard-stop": scenario_hard_stop,
    "schema": scenario_schema,
}


def main(argv: list[str]) -> int:
    args = list(argv)
    trace_enabled = "--trace" in args
    trace_path = None
    if "--trace-path" in args:
        idx = args.index("--trace-path")
        try:
            trace_path = Path(args[idx + 1])
        except IndexError:
            print("--trace-path richiede un percorso")
            return 2
        del args[idx:idx + 2]
    positional = [a for a in args if not a.startswith("--")]

    store = None
    if trace_enabled:
        from .trace_store import TRACE_LOG, TraceStore
        target = trace_path or TRACE_LOG
        store = TraceStore(target)
        print(f"trace: ogni run viene salvato in {target}")

    name = positional[0] if positional else "retry"
    if name == "live":
        query = " ".join(positional[1:]) or QUERY
        code = scenario_live(query, store)
    elif name in ("all", "--all"):
        for fn in SCENARIOS.values():
            fn(store)
        code = 0
    elif name in SCENARIOS:
        SCENARIOS[name](store)
        code = 0
    else:
        print(f"scenario sconosciuto: {name} (disponibili: {', '.join(SCENARIOS)}, live, all)")
        return 2

    if store is not None:
        from .trace_store import format_report
        print("\n" + (format_report(store.path) or "nessun trace registrato"))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
