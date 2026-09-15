"""decision/feedback.py — Il lato PERSISTENZA della catena (feedback engine).

La catena produce un `DecisionRecord` per ogni segnale (input, verdetto, stake);
questo modulo lo SCRIVE nel ledger `decisions` di `tracker.py` e ne legge la
telemetria. E' il passo che rende la decisione misurabile a posteriori: senza
le righe perse non c'e' modo di sapere se i gate avevano ragione.

Perche' sta QUI e non in `pipeline.py`: `pipeline` resta orchestratore puro
(niente DB), come dichiara la sua docstring. La persistenza e' un effetto
collaterale e vive fuori dal calcolo — stesso schema di `research_graph`, dove
il `TraceStore` e' esterno al grafo.

Regole:

- **Le scritture sono FAIL-SAFE**: un ledger non scrivibile non deve mai
  fermare una puntata. `persist()` non solleva mai: ritorna
  `{"saved": bool, "error": str}` e logga.
- **Le letture sono read-only**: `stats()`/`snapshot()` non scrivono nulla.
- **Nessun import di `tracker` a livello di modulo** (import pigro dentro le
  funzioni): il tripwire in `test_decision_pipeline.py` pretende che
  `import decision` non carichi la produzione.
- **Il `store` e' iniettabile**: i test passano un finto ledger per verificare
  il comportamento fail-safe senza toccare il DB reale.

Uso tipico (in un job):

    from decision import decide, KillSwitchStatus, RiskLimits, persist

    record = decide(signal, kills=KillSwitchStatus(mode="live", provider_ready=True),
                    limits=RiskLimits.from_env(), bankroll=38.0)
    persist(record)                       # fail-safe: mai un'eccezione

    # dopo l'esecuzione dell'ordine
    from decision.feedback import attach_order
    attach_order(record.record_id, {"bet_id": bet_id, "status": "FULLY_FILLED"})

CLI: `venv/bin/python -m decision feedback [--json] [--settle]`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("decision.feedback")

#: Nome della funzione che scrive una decisione sul ledger (contratto minimo
#: di uno `store`: `save_decision(record) -> record_id`).
PERSIST_FN = "save_decision"


def _ledger(store: Optional[Any] = None) -> Any:
    """Il ledger (import PIGRO di tracker, o lo `store` iniettato)."""
    if store is not None:
        return store
    import tracker                                  # pigro: vedi docstring
    return tracker


def persist(record, store: Optional[Any] = None) -> dict:
    """Scrive la decisione sul ledger. Non solleva MAI (fail-safe).

    Ritorna `{"saved": bool, "record_id": str, "error": str}`: il chiamante
    puo' loggare l'errore ma la puntata va avanti — il ledger e' telemetria,
    non un gate.
    """
    record_id = str(getattr(record, "record_id", "") or "")
    try:
        module = _ledger(store)
        record_id = module.save_decision(record) or record_id
        return {"saved": True, "record_id": record_id, "error": ""}
    except Exception as exc:                        # qualunque cosa
        logger.warning("decision.feedback: decisione %s NON salvata (%s)",
                       record_id or "?", exc)
        return {"saved": False, "record_id": record_id, "error": str(exc)}


def persist_many(records: Iterable, store: Optional[Any] = None) -> dict:
    """Persiste piu' record. Ritorna il conteggio di salvati/falliti."""
    saved = failed = 0
    ids: list[str] = []
    for record in records or []:
        out = persist(record, store=store)
        if out["saved"]:
            saved += 1
            ids.append(out["record_id"])
        else:
            failed += 1
    return {"saved": saved, "failed": failed, "record_ids": ids}


def attach_order(record_id: str, order: dict, store: Optional[Any] = None) -> dict:
    """Aggancia l'ordine eseguito (bet_id/status) alla decisione. Fail-safe."""
    try:
        module = _ledger(store)
        changed = module.update_decision_order(record_id, order or {})
        return {"updated": bool(changed), "record_id": record_id, "error": ""}
    except Exception as exc:
        logger.warning("decision.feedback: ordine non agganciato a %s (%s)",
                       record_id, exc)
        return {"updated": False, "record_id": record_id, "error": str(exc)}


def settle(store: Optional[Any] = None) -> dict:
    """Chiude le decisioni coi risultati reali. Fail-safe.

    Ritorna `{"settled": n, "pushes": m, "error": str}`. La pausa settlement
    (e la scadenza) restano di competenza di `tracker`: qui non si decide
    nulla, si riporta.
    """
    try:
        module = _ledger(store)
        settled, pushes = module.settle_decisions()
        return {"settled": settled, "pushes": pushes, "error": ""}
    except Exception as exc:
        logger.warning("decision.feedback: settlement decisioni fallito (%s)", exc)
        return {"settled": 0, "pushes": 0, "error": str(exc)}


def stats(store: Optional[Any] = None) -> dict:
    """Telemetria del ledger decisioni (read-only). Vuota se illeggibile."""
    try:
        module = _ledger(store)
        return module.decision_stats()
    except Exception as exc:
        logger.warning("decision.feedback: statistiche non leggibili (%s)", exc)
        return {"n": 0, "error": str(exc), "by_verdict": {}, "by_reason": {},
                "settled": {}, "shadow": {}}


def snapshot(store: Optional[Any] = None, queue: Optional[Any] = None) -> dict:
    """Istantanea per report/pagina: ledger decisioni + coda revisioni."""
    out: dict = {"decisions": stats(store=store)}
    try:
        if queue is None:
            from .review_queue import ReviewQueue
            queue = ReviewQueue()
        out["reviews"] = queue.summary()
    except Exception as exc:
        logger.warning("decision.feedback: coda revisioni non leggibile (%s)", exc)
        out["reviews"] = {"error": str(exc)}
    return out


def _bucket_line(label: str, bucket: dict) -> str:
    return (f"  {label}: n={bucket.get('n', 0)} hit {bucket.get('hit_rate', 0.0):.1f}% "
            f"| ROI flat {bucket.get('roi_flat', 0.0):+.2f}% "
            f"| su stake {bucket.get('roi_staked', 0.0):+.2f}% "
            f"| EV atteso {bucket.get('avg_ev', 0.0):+.2f}% "
            f"| gap {bucket.get('gap_pp', 0.0):+.2f}pp")


def format_report(data: dict) -> str:
    """Report Telegram-friendly della telemetria (nessuna tabella, testo)."""
    decisions = (data or {}).get("decisions") or data or {}
    n = decisions.get("n", 0)
    lines = [f"🧭 Decisioni registrate: {n}"]
    if not n:
        lines.append("  (nessuna decisione nel ledger: la catena non e' ancora "
                     "collegata alla produzione)")
        return "\n".join(lines)

    verdicts = decisions.get("by_verdict") or {}
    if verdicts:
        order = ("approve", "review", "reject")
        parts = [f"{v} {verdicts[v]}" for v in order if v in verdicts]
        parts += [f"{v} {c}" for v, c in verdicts.items() if v not in order]
        lines.append("  verdetti: " + " | ".join(parts))
    reasons = decisions.get("by_reason") or {}
    if reasons:
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:5]
        lines.append("  motivi: " + " | ".join(f"{k} {v}" for k, v in top))
    lines.append(f"  stake totale: {decisions.get('stake_total', 0.0):.2f} "
                 f"| ordini agganciati: {decisions.get('with_order', 0)} "
                 f"| aperte: {decisions.get('open', 0)}")

    settled = decisions.get("settled") or {}
    if settled.get("n"):
        lines.append("  giocate chiuse:")
        lines.append(_bucket_line("tutte", settled))
    shadow = decisions.get("shadow") or {}
    for verdict in sorted(shadow):
        bucket = shadow[verdict] or {}
        if bucket.get("n"):
            if not settled.get("n"):
                lines.append("  shadow (non giocate):")
            lines.append(_bucket_line(verdict, bucket))
    if not settled.get("n") and not shadow:
        lines.append("  nessuna decisione chiusa col risultato")

    reviews = (data or {}).get("reviews") or {}
    if isinstance(reviews, dict) and reviews.get("pending") is not None:
        lines.append(f"  revisioni in coda: {reviews.get('pending')}")
    return "\n".join(lines)


__all__ = ["attach_order", "format_report", "persist", "persist_many", "settle",
           "snapshot", "stats"]
