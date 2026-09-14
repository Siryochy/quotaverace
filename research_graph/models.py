"""research_graph/models.py — Schema Pydantic e stato del grafo (validazione deterministica).

Qui vive la parte DETERMINISTICA del sistema di validazione ibrida:

  - `Finding`               struttura rigida di una singola evidenza
                            (claim + evidence + source + confidence):
                            la presenza dei campi obbligatori e i range
                            sono garantiti da Pydantic, non dal prompt.
  - `ValidationFeedback`    feedback STRUTTURATO di un fallimento (lacune,
                            query suggerite, problemi di schema): e' il
                            contratto con cui il ciclo di retry alimenta il
                            Research Agent.
  - `ValidationVerdict`     esito `pass`/`fail`. Un `fail` SENZA feedback e'
                            un errore di programmazione (il retry non saprebbe
                            cosa colmare) e viene rifiutato alla costruzione.
  - `AttemptRecord`         traccia di un singolo attempt (query usate,
                            findings aggiunti, esito, motivo).
  - `ResearchState`         stato centrale tracciato dal grafo, con
                            `findings`, `validation_feedback` e `attempt`
                            gestiti come campi SEPARATI.

Il modulo non conosce ne' provider ne' rete: e' agnostico per costruzione.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

# --- Config -----------------------------------------------------------------
MIN_FINDINGS = 2         # evidenze minime (strutturalmente valide) per attempt
MIN_EVIDENCE_CHARS = 20  # lunghezza minima dell'evidenza (testo concreto, non un titolo)
MAX_ATTEMPTS = 3         # limite di sicurezza: massimo 3 attempt totali
MAX_RETRY_QUERIES = 3    # query mirate massime generate da un feedback

Outcome = Literal["pass", "fail"]
Status = Literal["pending", "passed", "hard_stop", "error"]
FeedbackKind = Literal["deterministic", "semantic"]


def _norm_text(text: str) -> str:
    """Normalizza un testo per confronti/dedup (minuscole, spazi compattati)."""
    return re.sub(r"\s+", " ", str(text).strip().lower())


def _dedup(seq: list[str]) -> list[str]:
    """Rimuove i duplicati preservando l'ordine di inserimento."""
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        skip = _norm_text(item)
        if not skip or skip in seen:
            continue
        seen.add(skip)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Finding — la struttura che i controlli deterministici pretendono
# ---------------------------------------------------------------------------

class Finding(BaseModel):
    """Evidenza raccolta dal Research Agent.

    Tutti i campi sono obbligatori: un finding senza fonte o con confidenza
    fuori da [0, 1] viene RESPINTO dal controllo deterministico (Pydantic).
    """

    claim: str = Field(..., min_length=3, max_length=500,
                       description="affermazione che l'evidenza deve sostenere")
    evidence: str = Field(..., min_length=MIN_EVIDENCE_CHARS, max_length=4000,
                          description="estratto concreto che sostiene il claim")
    source: str = Field(..., min_length=3, max_length=500,
                        description="fonte verificabile (URL o riferimento)")
    confidence: float = Field(..., ge=0.0, le=1.0,
                              description="confidenza della fonte, in [0, 1]")
    query: str = Field(default="", max_length=300,
                       description="query che ha prodotto il finding")

    @property
    def key(self) -> tuple[str, str]:
        """Chiave di dedup: claim normalizzato + fonte normalizzata.

        Lo schema dell'URL (http/https/www) non conta: la stessa evidenza
        raccolta da due query diverse deve contare una volta sola.
        """
        source = _norm_text(self.source)
        source = re.sub(r"^https?://", "", source)
        source = re.sub(r"^www\.", "", source)
        return _norm_text(self.claim), source


# ---------------------------------------------------------------------------
# Feedback e verdetto
# ---------------------------------------------------------------------------

class ValidationFeedback(BaseModel):
    """Feedback strutturato di un attempt fallito (deterministico o semantico)."""

    kind: FeedbackKind = "semantic"
    reason: str = Field(default="", max_length=1000)
    issues: list[str] = Field(default_factory=list)          # problemi puntuali
    missing_claims: list[str] = Field(default_factory=list)  # lacune da colmare
    suggested_queries: list[str] = Field(default_factory=list)
    offending: list[int] = Field(default_factory=list)       # indici dei finding respinti

    def queries_for_retry(self, base_query: str, limit: int = MAX_RETRY_QUERIES) -> list[str]:
        """Query MIRATE per il prossimo attempt, deduplicate e limitate.

        Ordine di priorita': query suggerite dal validator, poi una query per
        ogni lacuna (claim mancante). Se non c'e' nulla di meglio, i problemi
        puntuali (ripuliti dal prefisso diagnostico "finding #N:") e infine un
        fallback deterministico: il retry ha SEMPRE almeno una query da
        eseguire, anche con un feedback scarno.
        """
        candidates = list(self.suggested_queries)
        for claim in self.missing_claims:
            candidates.append(f"{base_query} {claim}")
        if not candidates:
            for issue in self.issues[:2]:
                brief = str(issue).split(":")[-1].strip()
                if brief:
                    candidates.append(f"{base_query} {brief}")
        candidates.append(f"{base_query} approfondimento")
        clean = [c for c in (str(item).strip() for item in candidates) if c]
        return _dedup(clean)[:max(1, limit)]


class ValidationVerdict(BaseModel):
    """Esito di un ciclo di validazione.

    Coerenza forzata: `fail` DEVE portare un `ValidationFeedback` strutturato
    (altrimenti il loop non saprebbe cosa correggere) e `pass` non deve
    portarne (un feedback su un pass e' ambiguo: sarebbe un fail mascherato).
    """

    outcome: Outcome
    feedback: Optional[ValidationFeedback] = None

    @model_validator(mode="after")
    def _coerente(self) -> "ValidationVerdict":
        if self.outcome == "fail" and self.feedback is None:
            raise ValueError("verdetto 'fail' senza ValidationFeedback: il retry non avrebbe istruzioni")
        if self.outcome == "pass" and self.feedback is not None:
            raise ValueError("verdetto 'pass' con ValidationFeedback: esito contraddittorio")
        return self

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"

    @property
    def blocked(self) -> bool:
        return self.outcome == "fail"


class ValidationRequest(BaseModel):
    """Payload passato al validator semantico (LLM): unico contratto."""

    query: str
    required_claims: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    attempt: int = 1


def verdict_pass() -> ValidationVerdict:
    """Verdetto positivo (nessun feedback)."""
    return ValidationVerdict(outcome="pass")


def verdict_fail(reason: str, *, kind: FeedbackKind = "semantic",
                 issues: Optional[list[str]] = None,
                 missing_claims: Optional[list[str]] = None,
                 suggested_queries: Optional[list[str]] = None,
                 offending: Optional[list[int]] = None) -> ValidationVerdict:
    """Verdetto negativo con feedback strutturato (factory per mock/adapter)."""
    return ValidationVerdict(
        outcome="fail",
        feedback=ValidationFeedback(
            kind=kind, reason=reason,
            issues=list(issues or []),
            missing_claims=list(missing_claims or []),
            suggested_queries=list(suggested_queries or []),
            offending=list(offending or []),
        ),
    )


# ---------------------------------------------------------------------------
# Stato del grafo
# ---------------------------------------------------------------------------

class AttemptRecord(BaseModel):
    """Traccia di un singolo attempt (telemetria + asserzioni dei test)."""

    attempt: int
    queries: list[str] = Field(default_factory=list)
    findings_total: int = 0     # evidenze valid e cumulative
    findings_added: int = 0     # raw raccolti in questo attempt
    rejected: int = 0           # raw respinti in questo attempt (schema)
    verdict: Optional[Outcome] = None
    feedback_kind: Optional[FeedbackKind] = None
    reason: Optional[str] = None
    errors: list[str] = Field(default_factory=list)  # errori del search tool (fail-safe)


class ResearchState(BaseModel):
    """Stato centrale del grafo.

    `findings`, `validation_feedback` e `attempt` sono tracciati come campi
    distinti e indipendenti (nessuno viene dedotto dagli altri).
    """

    query: str
    required_claims: list[str] = Field(default_factory=list)

    # evidenze
    findings: list[Finding] = Field(default_factory=list)   # validate (dedup, cumulative)
    raw_findings: list[Any] = Field(default_factory=list)   # SOLO attempt corrente
    rejected: list[dict[str, Any]] = Field(default_factory=list)

    # validazione
    validation_feedback: Optional[ValidationFeedback] = None
    last_verdict: Optional[ValidationVerdict] = None

    # ciclo
    attempt: int = 0
    status: Status = "pending"
    queries_used: list[str] = Field(default_factory=list)
    attempts_log: list[AttemptRecord] = Field(default_factory=list)
    node_trace: list[str] = Field(default_factory=list)
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def stopped(self) -> bool:
        """True quando il flusso e' finito (pass o hard stop o errore)."""
        return self.status != "pending"

    def add_query(self, query: str) -> bool:
        """Registra una query; False se era gia' stata usata (evita duplicati)."""
        if _norm_text(query) in {_norm_text(q) for q in self.queries_used}:
            return False
        self.queries_used.append(query)
        return True

    def reset_raw(self) -> None:
        """Azzera il materiale grezzo dell'attempt precedente.

        I payload RESPINTI non vengono ri-validati a ogni giro: un singolo
        finding malformato non deve avvelenare i retry (restano tracciati in
        `rejected`, con la storia completa).
        """
        self.raw_findings = []

    def add_raw(self, batch: Any) -> int:
        """Accumula il payload grezzo del search tool nell'attempt corrente.

        Un payload non conforme NON viene filtrato qui: lo giudica il controllo
        deterministico (che produce il feedback per il retry).
        """
        if batch is None:
            return 0
        if isinstance(batch, (str, bytes)) or not hasattr(batch, "__iter__"):
            batch = [batch]
        added = 0
        for item in batch:
            if isinstance(item, BaseModel):
                self.raw_findings.append(item.model_dump())
            elif isinstance(item, dict):
                self.raw_findings.append(dict(item))
            else:
                self.raw_findings.append(item)
            added += 1
        return added

    def current_record(self) -> AttemptRecord:
        """Record dell'attempt corrente (creato se il nodo non l'ha ancora fatto)."""
        if not self.attempts_log or self.attempts_log[-1].attempt != self.attempt:
            self.attempts_log.append(AttemptRecord(attempt=self.attempt))
        return self.attempts_log[-1]


__all__ = [
    "MIN_FINDINGS", "MIN_EVIDENCE_CHARS", "MAX_ATTEMPTS", "MAX_RETRY_QUERIES",
    "Finding", "ValidationFeedback", "ValidationVerdict", "ValidationRequest",
    "AttemptRecord", "ResearchState", "verdict_pass", "verdict_fail",
]
