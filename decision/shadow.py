"""decision/shadow.py — Shadow mode: la catena nuova accanto a quella che gira.

Scelta del proprietario (15/09/2026): la catena Command si collega ad `auto_bet`
in **shadow mode**. Significa che, a ogni giro, `auto_bet` valuta i segnali
aperti anche con la catena nuova, ne emette i comandi e li registra — ma
**l'esecuzione reale resta quella attuale**. Cosi' il confronto fra i due
percorsi e' misurato sui dati veri prima di sostituire qualcosa.

Tre garanzie, tutte verificate dai test:

1. **Nessun effetto reale**: i gateway sono lo `ShadowGateway` (registra) e
   basta — niente ordini, niente Telegram, niente righe sul ledger `decisions`.
   Perche' non si scrive sul ledger: il job gira ogni 60s e lo stesso segnale
   verrebbe registrato mille volte al giorno. Il registro della shadow mode e'
   il suo JSONL, deduplicato per `dedup_key`.
2. **Zero crediti** the-odds-api: del mercato si legge SOLO il feed primario
   (`decision/feeds.py`, SX pubblica: nessuna credenziale, nessun ordine). Con
   `DECISION_FEED_ENABLED=0` la chain valuta senza il gate di mercato (nessun
   accesso di rete: e' la modalita' dei test e delle diagnosi offline). Il gate
   di liquidita' dello Stake Engine continua a non scattare (`depth_usdc`
   arriva dal feed solo se interrogato). Tripwire: con il feed spento la rete
   NON viene toccata.
3. **Fail-safe totale**: qualunque errore torna come `{"error": ...}`, mai
   un'eccezione verso `auto_bet` (il giro puntate non si rompe per la
   telemetria).

Fail fast anche qui, e prima di tutto: se il kill switch o lo stop-loss sono
attivi, `run_shadow` esce **senza nemmeno interrogare il ledger** — non c'e'
nulla da confrontare quando le puntate sono ferme.

CLI: `venv/bin/python -m decision shadow [--json] [--days N]`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from . import engine, guards, kill_switch as kill_switch_mod
from .dispatcher import Dispatcher
from .feeds import feed_enabled as feeds_enabled, feed_from_env
from .gateways import ShadowGateway, shadow_log_path
from .limits import RiskLimits
from .middleware import Observability, TraceContext
from .models import Signal
from .review_queue import ReviewQueue

logger = logging.getLogger("decision.shadow")

SHADOW_ENABLED_ENV = "DECISION_SHADOW"
REVIEWS_ENABLED_ENV = "DECISION_REVIEWS"


def shadow_enabled(value: Optional[str] = None) -> bool:
    """Shadow mode attiva di default (non esegue nulla: rischio nullo)."""
    raw = os.getenv(SHADOW_ENABLED_ENV) if value is None else value
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def reviews_enabled(value: Optional[str] = None) -> bool:
    """La coda delle revisioni umane si riempie? (default si).

    Senza, un verdetto `review` verrebbe registrato solo nel log: l'operatore
    non vedrebbe mai il bottone e la revisione resterebbe una nota a verbale.
    """
    raw = os.getenv(REVIEWS_ENABLED_ENV) if value is None else value
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def run_shadow(*, signals: Optional[Sequence[Signal]] = None, bankroll: float = 0.0,
               mode: str = "sim", hours: float = 24.0, limit: Optional[int] = None,
               observability: Optional[Observability] = None,
               request_id: str = "", shadow_path: Optional[str | Path] = None,
               kills: Optional[Any] = None, limits: Optional[RiskLimits] = None,
               feed: Optional[Any] = None, feed_required: Optional[bool] = None,
               review_queue: Optional[Any] = None,
               reviews: Optional[bool] = None) -> dict:
    """Valuta i segnali aperti in shadow mode. NON esegue nulla, non solleva.

    Il **feed di mercato** (`decision/feeds.py`) e' la sorgente primaria dei
    dati di quotazione: se non viene iniettato e `DECISION_FEED_ENABLED` non e'
    a zero, la catena ne forza il refresh PRIMA di ogni valutazione di rischio
    e blocca il giro se il feed non e' fresco, conforme e validato.
    """
    obs = observability or Observability()
    ctx = obs.new_trace(request_id=request_id)
    out: dict[str, Any] = {"evaluated": 0, "plans": [], "by_verdict": {},
                           "by_command": {}, "shadow": True, "blocked": None,
                           "market": None, "market_blocked": None, "errors": [],
                           "reviews_queued": 0}
    try:
        status = kills or kill_switch_mod.status()

        # FAIL FAST: nessun lavoro se le puntate sono ferme.
        block = guards.first(status, stage=guards.STAGE_BETTING)
        if block is not None:
            obs.event("shadow.blocked", ctx=ctx, outcome="blocked",
                      reason=block.reason.value, block=block.name,
                      detail=block.detail)
            out["blocked"] = block.as_json()
            return out
        advisories = [b.name for b in guards.advisories(status)]

        if signals is None:
            from .adapters import iter_signals
            signals = iter_signals(hours=hours, limits=limits)
        if limit is not None:
            signals = list(signals)[:limit]

        if not signals:
            # Nessun segnale aperto: non c'e' nulla da confrontare. Si esce
            # SENZA eventi — il job gira ogni 60s e 1440 giri/giorno di
            # "nessun segnale" sarebbero solo rumore sul volume.
            out["no_signals"] = True
            return out

        # FEED di mercato: refresh forzato prima del Risk Engine (un giro, non
        # uno per segnale). Se il feed e' disattivato si valuta senza il gate di
        # mercato — scelta esplicita e tracciata, mai silenziosa.
        market_feed = feed
        required = (market_feed is not None or feeds_enabled()) if feed_required is None \
            else bool(feed_required)
        if market_feed is None and required:
            market_feed = feed_from_env(observability=obs)
        if market_feed is None:
            obs.event("feed.disabled", ctx=ctx, stage="market",
                      detail="gate di mercato non attivo (DECISION_FEED_ENABLED=0): "
                             "la catena valuta senza quota verificata")
        elif getattr(market_feed, "has_sources", True) is False:
            obs.event("feed.nosources", ctx=ctx, stage="market", outcome="error",
                      detail="nessuna sorgente di mercato configurata (fail-closed)")

        # Coda delle revisioni umane: un verdetto `review` entra qui e diventa
        # un prompt Telegram con bottoni (`decision/review_telegram.py`). La
        # coda e' idempotente per segnale, quindi il giro ogni 60s non la
        # riempie di copie dello stesso segnale.
        if review_queue is None and (reviews if reviews is not None else reviews_enabled()):
            review_queue = ReviewQueue()

        dispatcher = Dispatcher([ShadowGateway(shadow_path)], observability=obs)
        obs.event("shadow.start", ctx=ctx, signals=len(signals), mode=mode,
                  bankroll=bankroll, path=str(shadow_path or shadow_log_path()),
                  advisories=advisories)

        for signal in signals:
            # Una trace per decisione (stesso request_id): guardie, rischio e
            # comandi di QUEL segnale restano leggibili insieme anche quando
            # il giro ne valuta molti.
            plan_trace = obs.new_trace(request_id=ctx.request_id)
            plan = engine.build_plan(signal, kills=status, limits=limits,
                                     bankroll=bankroll, mode=mode,   # type: ignore[arg-type]
                                     observability=obs, ctx=plan_trace,
                                     review_queue=review_queue,
                                     feed=market_feed, feed_required=required)
            report = dispatcher.dispatch(plan, ctx=plan_trace)
            verdict = plan.record.risk.verdict
            if verdict == "review" and review_queue is not None:
                out["reviews_queued"] += 1
            if plan.market:
                out["market"] = plan.market
            if plan.blocked and plan.blocked.get("stage") == "market":
                out["market_blocked"] = plan.blocked.get("reason")
            out["by_verdict"][verdict] = out["by_verdict"].get(verdict, 0) + 1
            for command in plan.commands:
                key = command.kind.value
                out["by_command"][key] = out["by_command"].get(key, 0) + 1
            out["plans"].append({
                "record_id": plan.record.record_id,
                "blocked": (plan.blocked or {}).get("reason"),
                "match_id": signal.match_id,
                "outcome": signal.outcome,
                "verdict": verdict,
                "reason": plan.record.risk.reason.value,
                "commands": plan.kinds(),
                "would_order": plan.places_order,
                "stake": (plan.record.stake.stake if plan.record.stake else 0.0),
                "executed": report.executed,
                "duplicated": report.duplicated,
            })
            out["errors"].extend(report.errors)
            out["evaluated"] += 1

        obs.event("shadow.end", ctx=ctx, outcome="ok" if not out["errors"] else "error",
                  evaluated=out["evaluated"], verdicts=out["by_verdict"],
                  commands=out["by_command"], errors=len(out["errors"]),
                  reviews_queued=out["reviews_queued"],
                  market_blocked=out["market_blocked"], **(out["market"] or {}))
        return out
    except Exception as exc:                       # la shadow non rompe mai il job
        logger.warning("shadow: valutazione fallita (%s)", exc)
        out["errors"].append(str(exc))
        obs.event("shadow.error", ctx=ctx, outcome="error",
                  error=f"{type(exc).__name__}: {exc}")
        return out


# ---------------------------------------------------------------------------
# Lettura del registro (per il confronto misurato)
# ---------------------------------------------------------------------------

def iter_shadow_commands(path: Optional[str | Path] = None, *,
                         limit: int = 500) -> list[dict]:
    """Comandi registrati, piu' recenti prima (righe corrotte ignorate)."""
    target = Path(path) if path else shadow_log_path()
    if not target.exists():
        return []
    entries: list[dict] = []
    try:
        with open(target, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except Exception as exc:
        logger.warning("shadow: registro non leggibile (%s)", exc)
        return []
    entries.reverse()
    return entries[:limit]


def shadow_summary(path: Optional[str | Path] = None, *, limit: int = 500) -> dict:
    """Riepilogo del registro: comandi per tipo, esiti, segnali distinti."""
    entries = iter_shadow_commands(path, limit=limit)
    by_kind: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    signals: set[str] = set()
    would_order = 0
    for entry in entries:
        command = entry.get("command") or {}
        kind = str(command.get("kind") or "?")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        payload = command.get("payload") or {}
        if kind == "place_order":
            would_order += 1
        for key in ("verdict", "reason"):
            value = payload.get(key)
            if value:
                by_verdict[value] = by_verdict.get(value, 0) + 1
        if command.get("signal_id"):
            signals.add(str(command["signal_id"]))
    return {
        "entries": len(entries),
        "by_kind": by_kind,
        "by_verdict": by_verdict,
        "distinct_signals": len(signals),
        "would_order": would_order,
        "first_ts": (entries[-1].get("ts") if entries else None),
        "last_ts": (entries[0].get("ts") if entries else None),
    }


def format_report(summary_or_path: Any = None) -> str:
    """Report Telegram-friendly del registro shadow."""
    data = summary_or_path if isinstance(summary_or_path, dict) else shadow_summary(summary_or_path)
    lines = ["👻 Shadow mode (nessuna esecuzione reale)",
             f"  comandi registrati: {data.get('entries', 0)} "
             f"(segnali distinti {data.get('distinct_signals', 0)})"]
    by_kind = data.get("by_kind") or {}
    if by_kind:
        lines.append("  per tipo: " + " | ".join(f"{k} {v}" for k, v in sorted(by_kind.items())))
    if data.get("would_order"):
        lines.append(f"  ordini che SAREBBERO partiti: {data['would_order']}")
    verdicts = data.get("by_verdict") or {}
    if verdicts:
        lines.append("  esiti: " + " | ".join(f"{k} {v}" for k, v in sorted(verdicts.items())))
    if data.get("last_ts"):
        lines.append(f"  ultimo comando: {data['last_ts']}")
    return "\n".join(lines)


__all__ = [
    "REVIEWS_ENABLED_ENV", "SHADOW_ENABLED_ENV", "format_report",
    "iter_shadow_commands", "reviews_enabled", "run_shadow", "shadow_enabled",
    "shadow_summary",
]

