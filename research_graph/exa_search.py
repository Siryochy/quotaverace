"""research_graph/exa_search.py — Adapter REALE del `SearchTool` (Exa).

Implementa il contratto `providers.SearchTool` con una ricerca web vera, senza
aggiungere dipendenze: `requests` e' gia' nel progetto.

    POST https://api.exa.ai/search
    Authorization: Bearer $EXA_API_KEY
    {"query": ..., "type": "neural", "numResults": 5,
     "contents": {"highlights": true, "text": true}}

Perche' Exa: e' un motore pensato per agenti (risultati citabili, `highlights`
= estratti dei soli token rilevanti, ~10x piu' densi del testo intero) — proprio
il materiale che serve a `Finding.evidence`.

Mapping risultato Exa -> payload grezzo del `Finding`:
  claim      <- titolo della pagina (l'affermazione che la fonte sostiene)
  evidence   <- highlights (fallback: testo troncato)
  source     <- url
  confidence <- `score` di rilevanza se numerico in [0, 1], altrimenti
                DEFAULT_CONFIDENCE (0.5)

⚠️ Exa restituisce `score` SOLO per la ricerca `type: "neural"`: col tipo
`auto` (o `fast`/`keyword`) il payload non contiene il campo e la confidence
resta il default 0.5 (verificato sul campo il 14/09/2026). Il default e' quindi
`neural` (`DEFAULT_SEARCH_TYPE`, override con l'env `EXA_SEARCH_TYPE`), cosi'
la confidenza dei finding e' una misura reale di rilevanza; passando
`search_type="auto"` si torna al comportamento senza score.
Il gate deterministico (Pydantic) valida la struttura; e' il validator
semantico a giudicare se l'evidenza sostiene davvero il claim. Nessuna
interpretazione in questo modulo: qui si MAPPA, non si giudica (una estrazione
del claim via LLM sarebbe un'estensione futura).

Regole operative:
  - chiave SOLO da env (`EXA_API_KEY`) o iniettata dal chiamante: mai nel codice;
  - senza chiave la ricerca e' fail-closed (`ExaSearchError`) e NON inventa nulla;
  - errori HTTP/timeout/JSON -> `ExaSearchError` (il nodo li registra e il ciclo
    di retry prosegue, con il cap degli attempt come rete di sicurezza);
  - risposta senza `results` -> lista vuota (nessuna evidenza: il gate
    deterministico produce il feedback per il retry).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable, Iterable, Optional, Sequence

from ._env import ensure_env

logger = logging.getLogger("research_graph.exa")

EXA_SEARCH_URL = os.getenv("EXA_SEARCH_URL", "https://api.exa.ai/search")
EXA_TIMEOUT_SECONDS = float(os.getenv("EXA_TIMEOUT_SECONDS", "25"))
EXA_MAX_RESULTS = int(os.getenv("EXA_MAX_RESULTS", "10"))
MAX_EVIDENCE_CHARS = int(os.getenv("EXA_MAX_EVIDENCE_CHARS", "1200"))
DEFAULT_CONFIDENCE = 0.5
MAX_HIGHLIGHTS = 3
# Tipo di ricerca usato quando il chiamante non lo specifica: `neural` perche'
# e' l'unico tipo che restituisce `score` (=> `Finding.confidence` reale).
DEFAULT_SEARCH_TYPE = "neural"


class ExaSearchError(RuntimeError):
    """Errore di configurazione o di trasporto dell'adapter Exa."""


def exa_configured() -> bool:
    """True se `EXA_API_KEY` e' presente (ambiente, `.env` o vault)."""
    ensure_env()
    return bool(os.getenv("EXA_API_KEY"))


def default_search_type() -> str:
    """Tipo di ricerca di default (env `EXA_SEARCH_TYPE`, altrimenti neural)."""
    return (os.getenv("EXA_SEARCH_TYPE") or DEFAULT_SEARCH_TYPE).strip()


def _clean(text: Any, limit: Optional[int] = None) -> str:
    """Compatta gli spazi (le highlight arrivano con a capo e spazi doppi)."""
    out = re.sub(r"\s+", " ", str(text or "")).strip()
    if limit is not None and len(out) > limit:
        out = out[:limit].rstrip()
    return out


def result_to_finding(result: Any, *, query: str = "",
                      max_evidence_chars: int = MAX_EVIDENCE_CHARS) -> dict:
    """Converte un risultato Exa nel payload grezzo di un `Finding`."""
    item: dict = result if isinstance(result, dict) else {}

    highlights = item.get("highlights")
    if isinstance(highlights, str):
        highlights = [highlights]
    evidence = ""
    if isinstance(highlights, (list, tuple)):
        parts = [_clean(h) for h in highlights[:MAX_HIGHLIGHTS]]
        evidence = _clean(" | ".join(p for p in parts if p))
    if not evidence:
        evidence = _clean(item.get("text"), limit=max_evidence_chars)
    if len(evidence) > max_evidence_chars:
        evidence = evidence[:max_evidence_chars].rstrip()

    score = item.get("score")
    try:
        confidence = float(score)
    except (TypeError, ValueError):
        confidence = DEFAULT_CONFIDENCE
    if not 0.0 <= confidence <= 1.0:
        confidence = DEFAULT_CONFIDENCE

    return {
        "claim": _clean(item.get("title"), limit=500),
        "evidence": evidence,
        "source": _clean(item.get("url") or item.get("id"), limit=500),
        "confidence": confidence,
        "query": query,
    }


class ExaSearchTool:
    """`SearchTool` reale su Exa (`venv/bin/python` + `requests`).

    `http_post` e' iniettabile: i test iniettano un finto trasporto e girano
    OFFLINE (nessuna rete, nessuna chiave). In produzione resta il default
    `requests.post`.
    """

    def __init__(self, api_key: Optional[str] = None, *,
                 num_results: int = 5,
                 search_type: Optional[str] = None,
                 include_domains: Optional[Sequence[str]] = None,
                 exclude_domains: Optional[Sequence[str]] = None,
                 timeout: Optional[float] = None,
                 max_evidence_chars: int = MAX_EVIDENCE_CHARS,
                 url: Optional[str] = None,
                 http_post: Optional[Callable[..., Any]] = None) -> None:
        self._api_key = api_key
        self._num_results = max(1, min(int(num_results), EXA_MAX_RESULTS * 10))
        # None = default dinamico (`EXA_SEARCH_TYPE` o neural); "" = nessun campo
        self._search_type = search_type
        self._include = list(include_domains or [])
        self._exclude = list(exclude_domains or [])
        self._timeout = float(timeout or EXA_TIMEOUT_SECONDS)
        self._max_evidence_chars = int(max_evidence_chars)
        self._url = url or EXA_SEARCH_URL
        self._http_post = http_post
        self.calls: list[str] = []          # ossevabilita' (come i Mock)
        self.last_error: Optional[str] = None

    # -- configurazione ----------------------------------------------------
    def configured(self) -> bool:
        ensure_env()
        return bool(self._api_key or os.getenv("EXA_API_KEY"))

    def _resolve_key(self) -> str:
        ensure_env()
        key = self._api_key or os.getenv("EXA_API_KEY") or ""
        if not key.strip():
            raise ExaSearchError(
                "EXA_API_KEY non configurata: impostala nell'ambiente (mai nel codice) "
                "oppure passa api_key al costruttore")
        return key.strip()

    def _post(self, url: str, **kwargs: Any) -> Any:
        """Trasporto HTTP (iniettabile: `(url, **kwargs)` come `requests.post`)."""
        if self._http_post is not None:
            return self._http_post(url, **kwargs)
        import requests  # dipendenza gia' presente nel progetto
        return requests.post(url, **kwargs)

    def resolved_search_type(self) -> str:
        """Tipo effettivo della richiesta (esplicito > env > default neural)."""
        if self._search_type is not None:
            return self._search_type.strip()
        return default_search_type()

    def build_payload(self, query: str) -> dict:
        """Body della richiesta (esposto per i test e per l'ispezione)."""
        payload: dict[str, Any] = {
            "query": query,
            "numResults": self._num_results,
            "contents": {"highlights": True, "text": True},
        }
        search_type = self.resolved_search_type()
        if search_type:
            payload["type"] = search_type
        if self._include:
            payload["includeDomains"] = self._include
        if self._exclude:
            payload["excludeDomains"] = self._exclude
        return payload

    # -- contratto SearchTool ---------------------------------------------
    def search(self, query: str) -> list[dict]:
        """Esegue la ricerca e ritorna i payload grezzi dei finding."""
        api_key = self._resolve_key()
        self.calls.append(query)
        started = time.monotonic()
        try:
            response = self._post(
                self._url,
                json=self.build_payload(query),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}",
                         "x-api-key": api_key},
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise ExaSearchError(f"ricerca Exa fallita su {query!r}: {self.last_error}") from exc

        results: Iterable[Any] = []
        if isinstance(data, dict):
            raw = data.get("results")
            if isinstance(raw, (list, tuple)):
                results = raw
        findings = [result_to_finding(r, query=query,
                                      max_evidence_chars=self._max_evidence_chars)
                    for r in results]
        # un risultato senza titolo ne' url non e' utilizzabile: lo scarta qui
        # (il gate deterministico vedrebbe campi vuoti, ma evitiamo rumore)
        usable = [f for f in findings if f["claim"] or f["source"]]
        logger.info("exa: %d risultati per %r in %.2fs (usabili: %d)",
                    len(findings), query, time.monotonic() - started, len(usable))
        self.last_error = None
        return usable


__all__ = [
    "EXA_MAX_RESULTS", "MAX_EVIDENCE_CHARS", "DEFAULT_CONFIDENCE",
    "DEFAULT_SEARCH_TYPE", "ExaSearchError", "ExaSearchTool",
    "default_search_type", "exa_configured", "result_to_finding",
]
