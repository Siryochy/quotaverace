"""research_graph/providers.py — Contratti astratti dei provider + Mock.

Il grafo dipende da DUE soli contratti (duck typing, `Protocol`):

  1. `SearchTool.search(query) -> Sequence[Mapping]`
     Il tool di ricerca astratto: il Research Agent non sa (e non deve
     sapere) se dietro c'e' un mock, una API web, un indice locale.

  2. `Validator.validate(ValidationRequest) -> ValidationVerdict | Mapping | str`
     Il validator semantico (LLM): riceve il payload validato e restituisce
     `pass`/`fail`. Le forme accettate sono normalizzate dal nodo con
     Pydantic, cosi' un adapter reale che parla JSON non deve importare
     nulla di questo modulo.

Perche' i Mock sono di prima classe: la suite di test gira interamente
offline e deterministica, e gli adapter reali si sostituiscono SENZA toccare
nodi ed edge (`build_research_graph(search=..., validator=...)`).
"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Union, runtime_checkable

from .models import Finding, ValidationRequest, ValidationVerdict

# Un verdetto puo' tornare come oggetto, come dict JSON o come stringa secca.
RawVerdict = Union[ValidationVerdict, Mapping[str, Any], str, None]
ScriptedVerdict = Union[RawVerdict, Callable[[ValidationRequest], RawVerdict]]


@runtime_checkable
class SearchTool(Protocol):
    """Tool di ricerca astratto."""

    def search(self, query: str) -> Sequence[Mapping[str, Any]]:  # pragma: no cover
        ...


@runtime_checkable
class Validator(Protocol):
    """Validator semantico astratto (LLM o qualunque cosa lo imiti)."""

    def validate(self, request: ValidationRequest) -> RawVerdict:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# Mock: Search tool a copione
# ---------------------------------------------------------------------------

class MockSearchTool:
    """Search tool deterministico e offline, guidato da un copione.

    Forme accettate per `responses`:

      * ``{"query esatta": [finding, ...], "default": [...]}``
        risposta per query (matching case-insensitive); `default` copre le
        query non previste (se assente -> lista vuota);
      * ``callable(query) -> lista di finding``
        (per scenari dinamici: es. findings solo al secondo giro);
      * ``[[finding, ...], [finding, ...]]``
        batch consumati in ordine; l'ULTIMO viene ripetuto quando il copione
        finisce (serve allo scenario "hard stop" che fallisce 3 volte).

    `calls` registra ogni query ricevuta: e' l'osservazione usata dai test
    per verificare che il retry usi query MIRATE e senza duplicati.
    """

    def __init__(self, responses: Any = (), *, default: Any = ()):
        self.calls: list[str] = []
        self._scripted: Optional[list[Any]] = None
        self._mapping: Optional[dict[str, Any]] = None
        self._fn: Optional[Callable[[str], Any]] = None

        if callable(responses):
            self._fn = responses
        elif isinstance(responses, Mapping):
            self._mapping = {str(k).strip().lower(): v for k, v in responses.items()}
        else:
            self._scripted = list(responses or [])
        self._default = default

    # -- copione -----------------------------------------------------------
    def _batch_for(self, query: str) -> Any:
        if self._fn is not None:
            return self._fn(query)
        if self._mapping is not None:
            hit = self._mapping.get(str(query).strip().lower())
            if hit is None:
                hit = self._mapping.get("default", self._default)
            return hit
        if self._scripted:
            if len(self._scripted) > 1:
                return self._scripted.pop(0)
            return self._scripted[0]  # ultimo batch: ripetuto (hard stop)
        return self._default

    def search(self, query: str) -> list[Any]:
        """Ritorna una COPIA del batch (il grafo non muta mai il copione)."""
        self.calls.append(query)
        batch = self._batch_for(query)
        if batch is None:
            return []
        if isinstance(batch, (str, bytes)) or isinstance(batch, Mapping) or isinstance(batch, Finding):
            batch = [batch]
        out = []
        for item in batch:
            out.append(item.model_dump() if isinstance(item, Finding) else item)
        return out


# ---------------------------------------------------------------------------
# Mock: LLM validator a copione
# ---------------------------------------------------------------------------

class MockLLMValidator:
    """Validator semantico simulato.

    `verdicts` e' una lista di copioni consumati in ordine; ogni elemento puo'
    essere un `ValidationVerdict`, un dict JSON, la stringa "pass"/"fail" o una
    callable(request) -> verdetto (per far dipendere l'esito dai findings).

    Quando il copione e' esaurito si usa `default`; senza default il mock
    SOLLEVA: un numero di chiamate imprevisto e' un bug nel test, e va visto
    subito invece di essere mascherato da un pass silenzioso.

    `requests` registra ogni payload ricevuto (query, required_claims,
    findings validati, attempt): i test verificano da qui che il validator
    veda i dati giusti e che il feedback NON venga perso per strada.
    """

    def __init__(self, verdicts: Sequence[ScriptedVerdict] = (), *,
                 default: ScriptedVerdict = None):
        self.requests: list[ValidationRequest] = []
        self._queue: deque[ScriptedVerdict] = deque(verdicts or ())
        self._default = default
        self.calls = 0

    def validate(self, request: ValidationRequest) -> RawVerdict:
        self.requests.append(request)
        self.calls += 1
        if self._queue:
            scripted = self._queue.popleft()
        elif self._default is not None:
            scripted = self._default
        else:
            raise RuntimeError(
                "MockLLMValidator: copione esaurito (chiamata non prevista)")
        if callable(scripted):
            scripted = scripted(request)
        return scripted


__all__ = [
    "SearchTool", "Validator", "MockSearchTool", "MockLLMValidator",
    "RawVerdict", "ScriptedVerdict",
]
