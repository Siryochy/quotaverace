"""research_graph/gemini_validator.py — Adapter REALE del validator (Gemini).

Implementa il contratto `providers.Validator` con un LLM vero, usando
`google-genai` (gia' nel progetto: vedi `ai_commander.py` per il pattern).

Responsabilita' del validator semantico: dire se le EVIDENZE raccolte
sostengono DAVVERO i claim richiesti, e — se no — restituire un feedback
STRUTTURATO (lacune + query mirate) con cui il Research Agent fara' il retry.
Il modello risponde JSON:

    {"outcome": "pass"|"fail", "reason": "...",
     "missing_claims": ["..."], "suggested_queries": ["..."], "issues": ["..."]}

Regole operative:
  - chiave SOLO da env (`GOOGLE_API_KEY`), mai nel codice ne' nei log;
  - il prompt e' costruito in modo deterministico (`build_prompt`): query,
    claim richiesti e OGNI finding con la sua evidenza — cosi' e' ispezionabile
    e testabile senza rete;
  - FAIL-CLOSED: un file JSON illeggibile NON diventa un `pass`, ma un `fail`
    con feedback (il retry ha comunque istruzioni); un errore di trasporto
    solleva e il nodo lo registra come fail (poi decide il cap degli attempt);
  - la normalizzazione del verdetto resta a Pydantic (`coerce_verdict` nel
    nodo): qui si produce un `ValidationVerdict` valido per costruzione.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, List, Mapping, Optional

from ._env import ensure_env
from .models import (
    ValidationRequest, ValidationVerdict, verdict_fail, verdict_pass,
)

logger = logging.getLogger("research_graph.gemini")

MODEL_NAME = os.getenv("RESEARCH_LLM_MODEL", "gemini-3.6-flash")
TEMPERATURE = float(os.getenv("RESEARCH_LLM_TEMPERATURE", "0.0"))
MAX_EVIDENCE_CHARS = int(os.getenv("RESEARCH_LLM_MAX_EVIDENCE_CHARS", "1200"))
MAX_FINDINGS_IN_PROMPT = int(os.getenv("RESEARCH_LLM_MAX_FINDINGS", "12"))

SYSTEM_INSTRUCTION = (
    "Sei il validator di un workflow di ricerca. Il tuo unico compito e' "
    "stabilire se le EVIDENZE raccolte sostengono CONCRETAMENTE i claim "
    "richiesti. Regole:\n"
    "1. Rispondi SOLO con un oggetto JSON, senza testo attorno.\n"
    "2. `pass` solo se OGNI claim richiesto e' sostenuto da almeno una "
    "evidenza concreta (numeri, fatti o citazioni), non da affermazioni "
    "generiche.\n"
    "3. In caso di `fail` compila `missing_claims` con i claim non coperti e "
    "`suggested_queries` con 2-3 query di ricerca MIRATE che colmerebbero le "
    "lacune (diverse da quelle gia' usate).\n"
    "4. Non inventare fatti e non riscrivere le evidenze: giudichi cio' che "
    "ricevi."
)

JSON_CONTRACT = (
    '{"outcome": "pass"|"fail", "reason": "motivazione breve", '
    '"missing_claims": ["..."], "suggested_queries": ["..."], "issues": ["..."]}'
)


class GeminiValidatorError(RuntimeError):
    """Errore di configurazione o di trasporto dell'adapter Gemini."""


def gemini_configured() -> bool:
    """True se `GOOGLE_API_KEY` e' presente (ambiente, `.env` o vault)."""
    ensure_env()
    return bool(os.getenv("GOOGLE_API_KEY"))


def _clean(text: Any, limit: Optional[int] = None) -> str:
    out = re.sub(r"\s+", " ", str(text or "")).strip()
    if limit is not None and len(out) > limit:
        out = out[:limit].rstrip()
    return out


def extract_json(text: Any) -> Any:
    """Estrae il JSON da una risposta LLM (gestisce code fence e testo attorno).

    Solleva `ValueError` se non c'e' nessun oggetto JSON interpretabile.
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("risposta vuota")
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.IGNORECASE | re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except Exception as exc:
            raise ValueError(f"JSON non valido: {exc}") from exc
    raise ValueError("nessun oggetto JSON nella risposta")


def verdict_from_data(data: Any) -> ValidationVerdict:
    """Costruisce un verdetto COERENTE dai dati JSON del modello.

    Accetta lo schema piatto e quello annidato (`{"feedback": {...}}`), ignora
    i campi sconosciuti e trasforma un `fail` senza motivazione in un fail con
    motivazione di default (un verdetto resta sempre utile al retry).
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception as exc:
            raise ValueError(f"JSON non valido: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ValueError(f"verdetto atteso come oggetto JSON, ricevuto {type(data).__name__}")

    outcome = _clean(data.get("outcome")).lower()
    nested = data.get("feedback")
    feedback = dict(nested) if isinstance(nested, Mapping) else {}

    def pick(key: str) -> Any:
        return data.get(key, feedback.get(key))

    if outcome in ("pass", "passed", "ok", "true"):
        return verdict_pass()
    if outcome not in ("fail", "failed", "ko", "false"):
        raise ValueError(f"outcome non riconosciuto: {outcome!r}")

    def as_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [v.strip() for v in [value] if v.strip()]
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        return [str(value)]

    reason = _clean(pick("reason") or pick("motivo"))
    return verdict_fail(
        reason or "il validator semantico ha bocciato le evidenze senza motivare",
        issues=as_list(pick("issues")),
        missing_claims=as_list(pick("missing_claims")),
        suggested_queries=as_list(pick("suggested_queries")),
    )


class GeminiValidator:
    """`Validator` reale su Gemini (JSON mode), con client iniettabile.

    `client` serve ai test (client finto, zero rete). In produzione si costruisce
    da solo il client `google-genai` leggendo `GOOGLE_API_KEY` dall'ambiente.
    """

    def __init__(self, api_key: Optional[str] = None, *,
                 model_name: Optional[str] = None,
                 client: Any = None,
                 temperature: Optional[float] = None,
                 max_findings: int = MAX_FINDINGS_IN_PROMPT,
                 max_evidence_chars: int = MAX_EVIDENCE_CHARS) -> None:
        self._api_key = api_key
        self._model_name = model_name or MODEL_NAME
        self._client = client
        self._temperature = float(temperature if temperature is not None else TEMPERATURE)
        self._max_findings = max(1, int(max_findings))
        self._max_evidence_chars = int(max_evidence_chars)
        self._types: Any = None
        self.calls = 0
        self.last_error: Optional[str] = None

    # -- configurazione ----------------------------------------------------
    def configured(self) -> bool:
        ensure_env()
        return self._client is not None or bool(self._api_key or os.getenv("GOOGLE_API_KEY"))

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        ensure_env()
        api_key = (self._api_key or os.getenv("GOOGLE_API_KEY") or "").strip()
        if not api_key:
            raise GeminiValidatorError(
                "GOOGLE_API_KEY non configurata: impostala nell'ambiente (mai nel codice) "
                "oppure passa api_key/client al costruttore")
        try:
            from google import genai
        except Exception as exc:  # pragma: no cover - dipendenza dichiarata
            raise GeminiValidatorError(f"google-genai non disponibile: {exc}") from exc
        self._client = genai.Client(api_key=api_key)
        return self._client

    def _config(self) -> Any:
        kwargs = {
            "system_instruction": SYSTEM_INSTRUCTION,
            "temperature": self._temperature,
            "response_mime_type": "application/json",
        }
        try:
            from google.genai import types
            self._types = types
        except Exception:  # senza SDK il client iniettato riceve un dict
            return kwargs
        return types.GenerateContentConfig(**kwargs)

    # -- prompt ------------------------------------------------------------
    def build_prompt(self, request: ValidationRequest) -> str:
        """Prompt deterministico (ispezionabile e testabile senza rete)."""
        lines = [
            f"OBIETTIVO DI RICERCA: {request.query}",
            f"TENTATIVO: {request.attempt}",
        ]
        claims = list(request.required_claims or [])
        if claims:
            lines.append("CLAIM RICHIESTI (devono essere TUTTI coperti):")
            lines += [f"  {i}. {_clean(c)}" for i, c in enumerate(claims, 1)]
        else:
            lines.append("CLAIM RICHIESTI: (nessuno esplicito: giudica la pertinenza "
                         "delle evidenze all'obiettivo)")
        lines.append(f"EVIDENZE RACCOLTE ({len(request.findings)}):")
        for i, finding in enumerate(request.findings[:self._max_findings], 1):
            lines.append(
                f"  [{i}] claim: {_clean(finding.claim, 300)}\n"
                f"      fonte: {_clean(finding.source, 200)} "
                f"(confidenza {finding.confidence:.2f})\n"
                f"      evidenza: {_clean(finding.evidence, self._max_evidence_chars)}"
            )
        lines.append("Rispondi SOLO con un JSON di questa forma:")
        lines.append(f"  {JSON_CONTRACT}")
        return "\n".join(lines)

    # -- contratto Validator ----------------------------------------------
    def validate(self, request: ValidationRequest) -> ValidationVerdict:
        client = self._resolve_client()
        prompt = self.build_prompt(request)
        self.calls += 1
        try:
            response = client.models.generate_content(
                model=self._model_name, contents=prompt, config=self._config())
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise GeminiValidatorError(f"chiamata Gemini fallita: {self.last_error}") from exc

        text = self._extract_text(response)
        try:
            verdict = verdict_from_data(extract_json(text))
        except Exception as exc:
            # FAIL-CLOSED: risposta illeggibile -> fail con feedback utile al retry
            logger.warning("gemini: risposta non interpretabile (%s)", exc)
            self.last_error = str(exc)
            return verdict_fail(
                "il validator semantico ha risposto in un formato non interpretabile",
                issues=[f"{type(exc).__name__}: {exc}"],
            )
        self.last_error = None
        return verdict

    @staticmethod
    def _extract_text(response: Any) -> str:
        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            return text
        try:
            parts = response.candidates[0].content.parts
            joined = "".join(getattr(p, "text", "") or "" for p in parts)
            if joined.strip():
                return joined
        except Exception:
            pass
        return ""


__all__ = [
    "GeminiValidator", "GeminiValidatorError", "gemini_configured",
    "extract_json", "verdict_from_data", "SYSTEM_INSTRUCTION", "JSON_CONTRACT",
]
