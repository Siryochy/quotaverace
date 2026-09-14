"""research_graph/graph.py — Mini StateGraph + wiring del workflow di ricerca.

Motore a grafo minimale (nodi + edge, anche condizionali) coerente con la
filosofia del progetto: nessuna dipendenza pesante, comportamento esplicito
e ispezionabile. Bastano queste regole, verificate in `compile()`:

  * ogni nodo ha UNA sola uscita (edge semplice o condizionale);
  * i target di ogni edge/route devono esistere;
  * il runner ha un tetto di nodi eseguiti (`max_steps`): se il wiring
    contenesse un ciclo non protetto, il grafo SOLLEVA invece di girare
    all'infinito (e' il tripwire contro il loop infinito a livello di engine,
    oltre al cap degli attempt a livello di dominio).

`build_research_graph(search=..., validator=...)` e' l'unico punto in cui
entrano le dipendenze: sostituire i Mock con gli adapter reali NON altera il grafo.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional, Sequence

from .models import (
    MAX_ATTEMPTS, MAX_RETRY_QUERIES, MIN_FINDINGS, ResearchState,
)
from .nodes import (
    finalize_node, make_decision_node, make_decision_route,
    make_deterministic_check_node, make_hard_stop_node, make_research_node,
    make_semantic_validation_node, route_after_deterministic,
)
from .providers import SearchTool, Validator

logger = logging.getLogger("research_graph")

# Nodo terminale (niente funzione: il runner si ferma).
END = "__end__"

Node = Callable[[ResearchState], Any]
Router = Callable[[ResearchState], str]


class GraphError(RuntimeError):
    """Errore di wiring o di esecuzione del grafo."""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class StateGraph:
    """Costruttore del grafo: nodi, edge semplici, edge condizionali."""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, str] = {}
        self._conditional: dict[str, tuple[Router, dict[str, str]]] = {}
        self._entry: Optional[str] = None

    # -- costruzione -------------------------------------------------------
    def add_node(self, name: str, fn: Node) -> "StateGraph":
        if name in self._nodes:
            raise GraphError(f"nodo duplicato: {name}")
        if not callable(fn):
            raise GraphError(f"nodo non chiamabile: {name}")
        self._nodes[name] = fn
        return self

    def add_edge(self, source: str, target: str) -> "StateGraph":
        if source in self._edges or source in self._conditional:
            raise GraphError(f"il nodo '{source}' ha gia' un'uscita")
        self._edges[source] = target
        return self

    def add_conditional_edges(self, source: str, router: Router,
                              mapping: Mapping[str, str]) -> "StateGraph":
        if source in self._edges or source in self._conditional:
            raise GraphError(f"il nodo '{source}' ha gia' un'uscita")
        self._conditional[source] = (router, dict(mapping))
        return self

    def set_entry_point(self, name: str) -> "StateGraph":
        self._entry = name
        return self

    # -- compilazione ------------------------------------------------------
    def compile(self, *, max_steps: Optional[int] = None) -> "CompiledGraph":
        if self._entry is None:
            raise GraphError("entry point non impostato")
        if self._entry not in self._nodes:
            raise GraphError(f"entry point sconosciuto: {self._entry}")

        for name in self._nodes:
            if name not in self._edges and name not in self._conditional:
                raise GraphError(f"il nodo '{name}' non ha uscite")

        for source, target in self._edges.items():
            self._check_node(source)
            if target != END:
                self._check_node(target)

        for source, (router, mapping) in self._conditional.items():
            self._check_node(source)
            if not callable(router):
                raise GraphError(f"router non chiamabile sul nodo '{source}'")
            if not mapping:
                raise GraphError(f"mapping vuoto sul nodo '{source}'")
            for route, target in mapping.items():
                if target != END:
                    self._check_node(target)

        return CompiledGraph(self._nodes, dict(self._edges),
                            dict(self._conditional), self._entry, max_steps=max_steps)

    def _check_node(self, name: str) -> None:
        if name not in self._nodes:
            raise GraphError(f"nodo non registrato: {name}")


class CompiledGraph:
    """Grafo eseguibile: applica i nodi in sequenza finche' non raggiunge END."""

    def __init__(self, nodes: Mapping[str, Node], edges: Mapping[str, str],
                 conditional: Mapping[str, tuple[Router, Mapping[str, str]]],
                 entry: str, *, max_steps: Optional[int] = None) -> None:
        self._nodes = dict(nodes)
        self._edges = dict(edges)
        self._conditional = {k: (r, dict(m)) for k, (r, m) in conditional.items()}
        self._entry = entry
        self._max_steps = max_steps

    @property
    def node_names(self) -> list[str]:
        return list(self._nodes)

    def default_max_steps(self) -> int:
        """Tetto di nodi eseguiti: 4 nodi per attempt + chiusura (mai un loop)."""
        if self._max_steps is not None:
            return int(self._max_steps)
        return 4 * MAX_ATTEMPTS + 4

    def run(self, state: ResearchState, *, max_steps: Optional[int] = None) -> ResearchState:
        limit = int(max_steps or self.default_max_steps())
        current = self._entry
        steps = 0
        while current != END:
            steps += 1
            if steps > limit:
                raise GraphError(
                    f"limite di {limit} nodi eseguiti: probabile ciclo non protetto nel grafo")
            fn = self._nodes.get(current)
            if fn is None:
                raise GraphError(f"nodo non registrato durante l'esecuzione: {current}")
            result = fn(state)
            if result is not None:
                state = result
            current = self._next(current, state)
        return state

    def _next(self, node: str, state: ResearchState) -> str:
        if node in self._conditional:
            router, mapping = self._conditional[node]
            route = router(state)
            target = mapping.get(route)
            if target is None:
                raise GraphError(f"route '{route}' non mappata sul nodo '{node}'")
            return target
        target = self._edges.get(node)
        if target is None:
            raise GraphError(f"il nodo '{node}' non ha uscite")
        return target


# ---------------------------------------------------------------------------
# Wiring del workflow di ricerca
# ---------------------------------------------------------------------------

def build_research_graph(search: SearchTool, validator: Validator, *,
                        max_attempts: int = MAX_ATTEMPTS,
                        min_findings: int = MIN_FINDINGS,
                        max_retry_queries: int = MAX_RETRY_QUERIES) -> CompiledGraph:
    """Compone il workflow: research -> controlli -> semantica -> decisione.

    Le dipendenze astratte entrano qui e solo qui: il grafo (nodi + edge) e'
    identico con mock e con adapter reali.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts deve essere >= 1")

    decide = make_decision_route(max_attempts)

    graph = (
        StateGraph()
        .add_node("research", make_research_node(search, max_retry_queries=max_retry_queries))
        .add_node("deterministic_check", make_deterministic_check_node(min_findings=min_findings))
        .add_node("semantic_validation", make_semantic_validation_node(validator))
        .add_node("decision", make_decision_node(decide))
        .add_node("finalize", finalize_node)
        .add_node("hard_stop", make_hard_stop_node(max_attempts))
        .set_entry_point("research")
        .add_edge("research", "deterministic_check")
        .add_conditional_edges("deterministic_check", route_after_deterministic, {
            "valid": "semantic_validation",
            "invalid": "decision",
        })
        .add_edge("semantic_validation", "decision")
        .add_conditional_edges("decision", decide, {
            "pass": "finalize",
            "retry": "research",
            "hard_stop": "hard_stop",
        })
        .add_edge("finalize", END)
        .add_edge("hard_stop", END)
    )
    # 4 nodi per attempt (research, deterministic, semantic, decision) + chiusura.
    return graph.compile(max_steps=4 * max_attempts + 4)


def persist_trace(state: ResearchState, trace_store: Optional[Any]) -> Optional[dict]:
    """Persiste il trace del run, se un sink e' stato fornito.

    Sta FUORI dal grafo (il workflow resta identico) ed e' fail-safe: la
    persistenza non puo' far fallire una ricerca. Il sink e' qualunque oggetto
    con `save(state)` (`research_graph.trace_store.TraceStore`).
    """
    if trace_store is None:
        return None
    save = getattr(trace_store, "save", None)
    if not callable(save):
        logger.warning("trace_store ignorato: manca il metodo save(state)")
        return None
    try:
        return save(state)
    except Exception as exc:  # pragma: no cover - difensivo
        logger.warning("persistenza del trace fallita: %s", exc)
        return None


def run_research(query: str, *, search: SearchTool, validator: Validator,
                 required_claims: Sequence[str] = (),
                 max_attempts: int = MAX_ATTEMPTS,
                 min_findings: int = MIN_FINDINGS,
                 max_retry_queries: int = MAX_RETRY_QUERIES,
                 max_steps: Optional[int] = None,
                 trace_store: Optional[Any] = None) -> ResearchState:
    """Esegue il workflow e ritorna lo stato finale.

    Fail-safe come il resto del progetto: un errore imprevisto NON propaga
    un'eccezione al chiamante, ma chiude lo stato con `status="error"` e
    `error` valorizzato (il flusso non resta mai appeso).

    `trace_store` (opzionale, es. `TraceStore`) riceve lo stato finale — anche
    in caso di hard stop o errore — senza alterare il grafo.
    """
    state = ResearchState(query=query, required_claims=list(required_claims))
    try:
        graph = build_research_graph(search, validator, max_attempts=max_attempts,
                                     min_findings=min_findings,
                                     max_retry_queries=max_retry_queries)
        state = graph.run(state, max_steps=max_steps)
    except Exception as exc:  # mai un'eccezione fuori dal workflow
        logger.exception("ricerca interrotta da un errore imprevisto")
        state.status = "error"
        state.error = f"{type(exc).__name__}: {exc}"
        state.node_trace.append("error")
    finally:
        persist_trace(state, trace_store)
    return state


__all__ = [
    "END", "GraphError", "StateGraph", "CompiledGraph",
    "build_research_graph", "run_research", "persist_trace",
]
