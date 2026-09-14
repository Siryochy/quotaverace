"""research_graph/trace_store.py — Persistenza dei trace del workflow di ricerca.

Ogni run (anche quello fallito o andato in hard stop) lascia UNA riga JSONL sul
volume: chi ha cercato cosa, quante evidenze, per quale motivo il validator ha
bocciato, quante query sono servite. Serve a tre cose:

  1. diagnosi: perche' questo obiettivo non si valida mai (query sbagliate?
     fonti povere? gate deterministico troppo severo?);
  2. telemetria: tasso di pass, attempt medi, tipo di feedback ricorrente;
  3. continuita': il trace sopravvive al processo (append-only sul volume).

Stesse convenzioni di `liquidity_monitor`:
  - path da env (`RESEARCH_TRACE_DIR`), default `DATA_DIR/research`;
  - append JSONL (una riga per run), lettura che ignora le righe corrotte;
  - FAIL-SAFE: `save_trace` non propaga MAI eccezioni (un disco pieno non deve
    rompere la ricerca) e ritorna il record scritto, con `error` se fallito.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

from config import DATA_DIR

logger = logging.getLogger("research_graph.trace")

TRACE_DIR = Path(os.getenv("RESEARCH_TRACE_DIR", str(DATA_DIR / "research")))
TRACE_LOG = TRACE_DIR / "traces.jsonl"
MAX_TRACE_ROWS = 500      # righe lette al massimo da iter_traces (le piu' recenti)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_trace(state: Any, *, run_id: Optional[str] = None,
                include_findings: bool = False,
                extra: Optional[dict] = None) -> dict:
    """Trasforma uno `ResearchState` nel record persistibile (puro, testabile)."""
    now = _now()
    feedback = getattr(state, "validation_feedback", None)
    record: dict = {
        "run_id": run_id or uuid.uuid4().hex[:12],
        "ts": now.isoformat(),
        "ts_epoch": now.timestamp(),
        "query": getattr(state, "query", ""),
        "required_claims": list(getattr(state, "required_claims", []) or []),
        "status": getattr(state, "status", "pending"),
        "attempt": int(getattr(state, "attempt", 0) or 0),
        "findings": len(getattr(state, "findings", []) or []),
        "rejected": len(getattr(state, "rejected", []) or []),
        "queries_used": list(getattr(state, "queries_used", []) or []),
        "node_trace": list(getattr(state, "node_trace", []) or []),
        "attempts": [r.model_dump() if hasattr(r, "model_dump") else dict(r)
                     for r in (getattr(state, "attempts_log", []) or [])],
    }
    if feedback is not None:
        record["feedback"] = {
            "kind": feedback.kind, "reason": feedback.reason,
            "missing_claims": list(feedback.missing_claims),
            "issues": list(feedback.issues),
        }
    if getattr(state, "error", None):
        record["error"] = state.error
    if include_findings:
        record["validated_findings"] = [
            f.model_dump() if hasattr(f, "model_dump") else dict(f)
            for f in (getattr(state, "findings", []) or [])
        ]
    if extra:
        record["extra"] = extra
    return record


def save_trace(state: Any, *, path: Optional[Path] = None,
               include_findings: bool = False,
               run_id: Optional[str] = None,
               extra: Optional[dict] = None) -> dict:
    """Accoda il trace del run (FAIL-SAFE: mai eccezioni)."""
    record = build_trace(state, run_id=run_id, include_findings=include_findings,
                         extra=extra)
    target = Path(path or TRACE_LOG)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # pragma: no cover - difensivo
        logger.warning("trace_store: scrittura fallita (%s)", exc)
        record["error"] = f"write_failed: {exc}"
    return record


class TraceStore:
    """Sink iniettabile: `run_research(..., trace_store=TraceStore())`."""

    def __init__(self, path: Optional[Path] = None, *,
                 include_findings: bool = False) -> None:
        self.path = Path(path or TRACE_LOG)
        self.include_findings = include_findings
        self.saved: list[dict] = []

    def save(self, state: Any) -> dict:
        record = save_trace(state, path=self.path,
                            include_findings=self.include_findings)
        self.saved.append(record)
        return record


def iter_traces(path: Optional[Path] = None, *, limit: Optional[int] = None,
                days: Optional[float] = None) -> List[dict]:
    """Trace dal piu' recente al piu' vecchio (righe corrotte ignorate)."""
    target = Path(path or TRACE_LOG)
    if not target.exists():
        return []
    cutoff = None
    if days is not None:
        cutoff = (_now() - timedelta(days=float(days))).timestamp()
    out: List[dict] = []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                if cutoff is not None:
                    ts = row.get("ts_epoch")
                    if ts is None:
                        try:
                            ts = datetime.fromisoformat(
                                str(row.get("ts")).replace("Z", "+00:00")).timestamp()
                        except Exception:
                            ts = None
                    if ts is not None and ts < cutoff:
                        continue
                out.append(row)
    except Exception as exc:  # pragma: no cover - difensivo
        logger.warning("trace_store: lettura fallita (%s)", exc)
        return []
    out.reverse()
    if limit is not None:
        out = out[:max(0, int(limit))]
    return out[:MAX_TRACE_ROWS]


def summary(path: Optional[Path] = None, *, days: Optional[float] = None) -> dict:
    """Riepilogo aggregato dei run (per il report e per i controlli veloci)."""
    rows = iter_traces(path, days=days)
    by_status: dict[str, int] = {}
    by_feedback: dict[str, int] = {}
    blocker_counts: dict[str, int] = {}
    attempts_total = 0
    for row in rows:
        status = str(row.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        attempts_total += int(row.get("attempt") or 0)
        feedback = row.get("feedback") or {}
        kind = str(feedback.get("kind") or "")
        if kind:
            by_feedback[kind] = by_feedback.get(kind, 0) + 1
            reason = str(feedback.get("reason") or "").strip()
            if reason:
                blocker_counts[reason] = blocker_counts.get(reason, 0) + 1
    total = len(rows)
    passed = by_status.get("passed", 0)
    return {
        "runs": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "avg_attempts": round(attempts_total / total, 2) if total else 0.0,
        "by_status": by_status,
        "by_feedback_kind": by_feedback,
        "top_blockers": sorted(blocker_counts.items(), key=lambda kv: -kv[1])[:5],
        "last": rows[0] if rows else None,
    }


def format_report(path: Optional[Path] = None, *, days: Optional[float] = None) -> Optional[str]:
    """Report testuale (Telegram-friendly) o None se non c'e' nulla da dire."""
    data = summary(path, days=days)
    if not data["runs"]:
        return None
    window = f" (ultimi {days:g}gg)" if days else ""
    lines = [
        f"🔎 <b>Research graph</b>{window}: {data['runs']} run, "
        f"pass {data['passed']} ({data['pass_rate'] * 100:.0f}%), "
        f"attempt medi {data['avg_attempts']}"
    ]
    for status, count in sorted(data["by_status"].items(), key=lambda kv: -kv[1]):
        lines.append(f"• {status}: {count}")
    if data["by_feedback_kind"]:
        kinds = ", ".join(f"{k}={v}" for k, v in data["by_feedback_kind"].items())
        lines.append(f"• feedback: {kinds}")
    for reason, count in data["top_blockers"]:
        lines.append(f"• blocco x{count}: {reason[:120]}")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Trace del workflow di ricerca (research_graph)")
    parser.add_argument("--limit", type=int, default=10, help="righe da mostrare")
    parser.add_argument("--days", type=float, default=None, help="finestra in giorni")
    parser.add_argument("--json", action="store_true", help="output JSON")
    parser.add_argument("--path", default=None, help="path del log (default da env)")
    args = parser.parse_args(argv)

    path = Path(args.path) if args.path else None
    if args.json:
        print(json.dumps({"summary": summary(path, days=args.days),
                          "traces": iter_traces(path, limit=args.limit, days=args.days)},
                         ensure_ascii=False, indent=2))
    else:
        report = format_report(path, days=args.days)
        print(report or "nessun trace registrato")
        for row in iter_traces(path, limit=args.limit, days=args.days):
            print(f"  [{row.get('ts', '')[:19]}] {row.get('status'):<9} "
                  f"attempt={row.get('attempt')} findings={row.get('findings')} "
                  f"| {str(row.get('query'))[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
