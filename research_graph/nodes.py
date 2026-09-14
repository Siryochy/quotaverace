"""research_graph/nodes.py — I nodi del grafo di ricerca.

Flusso:

    research --> deterministic_check --(ok)--> semantic_validation --+
                       |                                             |
                       +--(ko: schema/completezza)--------------------> decision
                                                                     |
                 +-------------- retry ------------------------------+
                 |
                 +-- pass --> finalize
                 +-- cap raggiunto --> hard_stop

Ogni nodo e' una factory (`make_*_node`) che riceve la dipendenza astratta
(search tool / validator) e ritorna una funzione `(state) -> state`. Le
dipendenze entrano SOLO da qui: sostituire i mock con gli adapter reali non
tocca ne' i nodi ne' gli edge.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from pydantic import ValidationError

from .models import (
    MAX_ATTEMPTS, MAX_RETRY_QUERIES, MIN_FINDINGS,
    Finding, ResearchState, ValidationRequest, ValidationVerdict,
    verdict_fail, verdict_pass,
)
from .providers import SearchTool, Validator

logger = logging.getLogger("research_graph")

Node = Callable[[ResearchState], ResearchState]


# ---------------------------------------------------------------------------
# Normalizzazione del verdetto (adapter-agnostica)
# ---------------------------------------------------------------------------

def coerce_verdict(raw: Any) -> ValidationVerdict:
    """Normalizza l'uscita del validator con Pydantic.

    Accetta `ValidationVerdict`, un dict JSON o una stringa secca
    ("pass"/"fail"): cosi' un adapter reale puo' restituire quello che gli
    viene comodo senza importare questo modulo.
    """
    if isinstance(raw, ValidationVerdict):
        return raw
    if raw is None:
        raise ValueError("validator ha restituito None (nessun verdetto)")
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("pass", "passed", "ok", "true"):
            return verdict_pass()
        if text in ("fail", "failed", "false", "ko"):
            return verdict_fail("validator semantico: esito 'fail' senza motivazione")
        raise ValueError(f"verdetto non riconosciuto: {raw!r}")
    if isinstance(raw, Mapping):
        return ValidationVerdict.model_validate(dict(raw))
    raise TypeError(f"verdetto di tipo non supportato: {type(raw).__name__}")


# ---------------------------------------------------------------------------
# Nodo 1 — Research Agent
# ---------------------------------------------------------------------------

def make_research_node(search: SearchTool, *, max_retry_queries: int = MAX_RETRY_QUERIES) -> Node:
    """Research Agent: ricerca generale al primo giro, MIRATA sui retry.

    - attempt 1: una sola query, quella dell'obiettivo.
    - attempt > 1: legge `validation_feedback` e ne deriva query mirate per
      colmare le lacune, usando solo il search tool astratto.
    - le query gia' eseguite non vengono ripetute (`state.add_query`), e gli
      errori del tool NON fanno crashare il grafo: il controllo
      deterministico giudichera' il materiale raccolto (fail-safe).
    """

    def research_node(state: ResearchState) -> ResearchState:
        state.attempt += 1
        state.reset_raw()   # il materiale grezzo e' quello di QUESTO attempt
        record = state.current_record()

        feedback = state.validation_feedback
        if state.attempt <= 1 or feedback is None:
            queries = [state.query]
        else:
            queries = feedback.queries_for_retry(state.query, limit=max_retry_queries)

        added = 0
        for query in queries:
            if not state.add_query(query):       # gia' usata: niente duplicati
                logger.debug("query gia' usata, saltata: %s", query)
                continue
            try:
                batch = search.search(query)
            except Exception as exc:             # fail-safe: decide la validazione
                logger.warning("search tool in errore su %r: %s", query, exc)
                record.errors.append(f"{type(exc).__name__}: {exc}")
                continue
            added += state.add_raw(batch)
            record.queries.append(query)

        record.findings_added = added                # raw raccolti in questo attempt
        record.findings_total = len(state.findings)  # evidenze VALID e cumulative
        state.node_trace.append("research")
        return state

    return research_node


# ---------------------------------------------------------------------------
# Nodo 2 — Controlli deterministici (Pydantic)
# ---------------------------------------------------------------------------

def make_deterministic_check_node(*, min_findings: int = MIN_FINDINGS) -> Node:
    """Validazione deterministica: struttura, campi obbligatori, completezza.

    Verifica con Pydantic il materiale grezzo dell'ATTEMPT CORRENTE, deduplica i
    validi e li accorpa a `state.findings` (cumulativo). Produce un
    `ValidationVerdict` di fail *strutturato* (indici respinti + campi mancanti)
    che il Research Agent usara' per il retry. I payload respinti restano in
    `state.rejected` e NON vengono ri-validati: un finding malformato non puo'
    avvelenare tutti i giri successivi.

    Il gate giudica il MATERIALE DISPONIBILE (buffer dell'attempt + evidenze
    gia' validate): se un retry esaurisce le query nuove, il lavoro dei giri
    precedenti non viene azzerato e la decisione torna al validator semantico.
    """

    def deterministic_check_node(state: ResearchState) -> ResearchState:
        record = state.current_record()
        issues: list[str] = []
        rejected: list[dict[str, Any]] = []
        valid: list[Finding] = []

        for index, raw in enumerate(state.raw_findings):
            if isinstance(raw, Finding):
                valid.append(raw)
                continue
            if not isinstance(raw, Mapping):
                rejected.append({"index": index, "payload": type(raw).__name__,
                                 "expected": "oggetto con claim/evidence/source/confidence"})
                issues.append(f"finding #{index}: payload non e' un oggetto ({type(raw).__name__})")
                continue
            try:
                valid.append(Finding.model_validate(dict(raw)))
            except ValidationError as err:
                fields = sorted({".".join(str(p) for p in e.get("loc", ())) or "<root>"
                                 for e in err.errors()})
                rejected.append({"index": index, "fields": fields,
                                 "errors": [e.get("msg", "") for e in err.errors()]})
                issues.append(f"finding #{index}: campi non validi ({', '.join(fields) or 'schema'})")

        # dedup: la stessa evidenza da due query diverse conta una volta sola.
        # I finding gia' validati negli attempt precedenti restano (cumulativi).
        seen: set[tuple[str, str]] = {f.key for f in state.findings}
        findings: list[Finding] = list(state.findings)
        duplicates = 0
        for item in valid:
            if item.key in seen:
                duplicates += 1
                continue
            seen.add(item.key)
            findings.append(item)

        state.findings = findings
        state.rejected = list(state.rejected) + [
            {**item, "attempt": state.attempt} for item in rejected
        ]

        if not state.raw_findings and not state.findings:
            verdict: ValidationVerdict = verdict_fail(
                "nessuna evidenza raccolta in questo attempt",
                kind="deterministic",
                issues=["il search tool non ha restituito alcun finding"],
            )
        elif rejected:
            verdict = verdict_fail(
                f"{len(rejected)}/{len(state.raw_findings)} finding non conformi allo schema",
                kind="deterministic",
                issues=issues,
                offending=[int(r["index"]) for r in rejected],
                suggested_queries=[f"{state.query} {r.get('fields', ['evidenza'])[0]}".strip()
                                   for r in rejected[:2]],
            )
        elif len(findings) < min_findings:
            verdict = verdict_fail(
                f"evidenze insufficienti: {len(findings)} validi, minimo {min_findings}",
                kind="deterministic",
                issues=[f"servono almeno {min_findings} evidenze strutturalmente valide"],
            )
        else:
            verdict = verdict_pass()

        if duplicates:
            logger.debug("deterministic_check: %d duplicati rimossi", duplicates)

        # il verdetto e' SEMPRE esplicito: `None` non e' un pass (il router
        # legge `last_verdict`, quindi "nessun verdetto" non deve esistere)
        state.last_verdict = verdict
        state.validation_feedback = verdict.feedback
        record.rejected = len(rejected)
        state.node_trace.append("deterministic_check")
        return state

    return deterministic_check_node


def route_after_deterministic(state: ResearchState) -> str:
    """Edge condizionale: i dati malformati non arrivano al validator semantico."""
    verdict = state.last_verdict
    return "valid" if (verdict is not None and verdict.passed) else "invalid"


# ---------------------------------------------------------------------------
# Nodo 3 — LLM Validator (validazione semantica)
# ---------------------------------------------------------------------------

def make_semantic_validation_node(validator: Validator) -> Node:
    """Validazione semantica: le evidenze sostengono DAVVERO i claim?

    Il validator riceve un `ValidationRequest` (query, claim richiesti,
    findings validati, attempt) e risponde pass/fail + feedback strutturato.
    Qualunque errore dell'adapter diventa un fail con feedback (fail-closed):
    il retry e' possibile e il cap degli attempt impedisce il loop infinito.
    """

    def semantic_validation_node(state: ResearchState) -> ResearchState:
        record = state.current_record()
        request = ValidationRequest(
            query=state.query,
            required_claims=list(state.required_claims),
            findings=list(state.findings),
            attempt=state.attempt,
        )
        try:
            verdict = coerce_verdict(validator.validate(request))
        except Exception as exc:
            logger.warning("validator semantico in errore: %s", exc)
            record.errors.append(f"validator: {type(exc).__name__}: {exc}")
            verdict = verdict_fail(
                f"validator semantico non disponibile: {type(exc).__name__}: {exc}",
                issues=["il controllo semantico non ha potuto esprimersi"],
            )
        state.last_verdict = verdict
        state.validation_feedback = verdict.feedback
        state.node_trace.append("semantic_validation")
        return state

    return semantic_validation_node


# ---------------------------------------------------------------------------
# Nodo 4 — Decision (loop)
# ---------------------------------------------------------------------------

def make_decision_route(max_attempts: int = MAX_ATTEMPTS) -> Callable[[ResearchState], str]:
    """Router del decision node: pass | retry | hard_stop.

    Il LIMITE DI SICUREZZA sta qui: al raggiungimento di `max_attempts`
    (default 3) un fail diventa hard stop, mai un nuovo giro.
    """
    def decide(state: ResearchState) -> str:
        verdict = state.last_verdict
        if verdict is None:
            raise RuntimeError("decision node raggiunto senza verdetto")
        if verdict.passed:
            return "pass"
        if state.attempt >= max_attempts:
            return "hard_stop"
        return "retry"
    return decide


def make_decision_node(route: Callable[[ResearchState], str]) -> Node:
    """Nodo di decisione: rende esplicita la scelta nel trace e nello stato."""

    def decision_node(state: ResearchState) -> ResearchState:
        verdict = state.last_verdict
        chosen = route(state)
        record = state.current_record()
        record.verdict = "pass" if (verdict is not None and verdict.passed) else "fail"
        if verdict is not None and verdict.feedback is not None:
            record.feedback_kind = verdict.feedback.kind
            record.reason = verdict.feedback.reason
        elif verdict is not None:
            record.reason = "evidenze validate"
        state.node_trace.append(f"decision:{chosen}")
        return state

    return decision_node


# ---------------------------------------------------------------------------
# Nodi terminali
# ---------------------------------------------------------------------------

def finalize_node(state: ResearchState) -> ResearchState:
    """Chiusura positiva: il flusso si ferma col materiale validato."""
    state.status = "passed"
    state.validation_feedback = None
    state.node_trace.append("finalize")
    return state


def make_hard_stop_node(max_attempts: int = MAX_ATTEMPTS) -> Node:
    """Chiusura di sicurezza: limite di attempt raggiunto, nessun loop infinito."""

    def hard_stop_node(state: ResearchState) -> ResearchState:
        state.status = "hard_stop"
        state.error = f"limite di {max_attempts} attempt raggiunto senza validazione"
        state.node_trace.append("hard_stop")
        return state

    return hard_stop_node


__all__ = [
    "coerce_verdict", "make_research_node", "make_deterministic_check_node",
    "route_after_deterministic", "make_semantic_validation_node",
    "make_decision_node", "make_decision_route", "finalize_node",
    "make_hard_stop_node",
]
