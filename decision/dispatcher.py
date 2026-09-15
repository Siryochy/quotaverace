"""decision/dispatcher.py — Instrada i comandi ai gateway (unico punto di esecuzione).

Il `Dispatcher` e' l'UNICO posto in cui i comandi diventano effetti. Prende un
`CommandPlan` dal motore e, per ogni comando:

1. sceglie il primo gateway che lo sa gestire (l'ordine di registrazione e'
   la priorita': in shadow mode si registra SOLO lo `ShadowGateway`, quindi il
   gateway d'esecuzione non viene nemmeno consultato);
2. apre uno **span** nel middleware (nome `command.<kind>`) con durata, esito e
   `dedup_key`;
3. raccoglie il `CommandResult` senza mai lasciar passare un'eccezione: i
   gateway sono fail-safe e il dispatch e' *fail-soft* — un comando fallito non
   impedisce l'esecuzione degli altri (l'audit sul ledger viene prima
   dell'ordine proprio per questo).

`raise_on_error=True` rende il dispatch *fail-fast* (solleva `GatewayError`)
per chi vuole la semantica opposta: la scelta e' esplicita, non implicita.

Nessun import di produzione a livello di modulo: il dispatcher orchestra e
basta, quindi si testa con gateway finti in memoria (zero rete, zero crediti).
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from pydantic import BaseModel, Field

from .commands import Command, CommandKind, CommandPlan
from .gateways import CommandResult, Gateway
from .middleware import Observability, TraceContext

logger = logging.getLogger("decision.dispatcher")


class GatewayError(Exception):
    """Sollevata con `raise_on_error=True` se un comando e' fallito.

    Porta i MOTIVI (anche quelli senza `CommandResult`, come un comando senza
    gateway: senza i motivi l'errore direbbe solo "qualcosa e' andato storto").
    """

    def __init__(self, errors: Sequence[str],
                 results: Optional[Sequence[CommandResult]] = None) -> None:
        self.errors = [str(e) for e in errors]
        self.results = [r for r in (results or []) if not r.ok]
        super().__init__(f"{len(self.errors)} comandi falliti: "
                         + "; ".join(self.errors))


class DispatchReport(BaseModel):
    """Cosa e' successo eseguendo un piano (dato serializzabile)."""

    plan_id: str
    record_id: str = ""
    results: list[CommandResult] = Field(default_factory=list)
    ok: bool = True
    executed: int = 0
    skipped: int = 0
    duplicated: int = 0
    errors: list[str] = Field(default_factory=list)
    #: True se i gateway usati NON eseguono (shadow/dry-run): nessun effetto reale.
    shadow: bool = False

    def of_kind(self, kind: CommandKind) -> list[CommandResult]:
        return [r for r in self.results if r.kind == kind]

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Dispatcher:
    """Esegue piani su un insieme ordinato di gateway."""

    def __init__(self, gateways: Sequence[Gateway], *,
                 observability: Optional[Observability] = None,
                 raise_on_error: bool = False) -> None:
        self.gateways: list[Gateway] = list(gateways)
        self.observability = observability or Observability()
        self.raise_on_error = raise_on_error

    # -- helper ----------------------------------------------------------
    def gateway_for(self, command: Command) -> Optional[Gateway]:
        for gateway in self.gateways:
            try:
                if gateway.handles(command.kind):
                    return gateway
            except Exception:                     # gateway malformato: lo salto
                continue
        return None

    @property
    def shadow(self) -> bool:
        """True se nessun gateway registrato esegue effetti reali."""
        return bool(self.gateways) and all(
            bool(getattr(gateway, "dry_run", False)) for gateway in self.gateways)

    # -- esecuzione ------------------------------------------------------
    def dispatch(self, plan: CommandPlan, *,
                 ctx: Optional[TraceContext] = None) -> DispatchReport:
        """Esegue i comandi del piano nell'ordine dichiarato (`Command.order`)."""
        scope = ctx or self.observability.new_trace()
        report = DispatchReport(plan_id=plan.plan_id, record_id=plan.record.record_id,
                                shadow=self.shadow)
        self.observability.event(
            "plan.dispatch", ctx=scope, stage="dispatch",
            verdict=plan.record.risk.verdict, reason=plan.record.risk.reason.value,
            mode=plan.record.mode, commands=plan.kinds(), shadow=self.shadow)

        for command in sorted(plan.commands, key=lambda c: c.order):
            gateway = self.gateway_for(command)
            if gateway is None:
                report.errors.append(f"{command.kind.value}: nessun gateway")
                report.skipped += 1
                self.observability.event("command.unhandled", ctx=scope, stage="dispatch",
                                         command=command.kind.value,
                                         command_id=command.command_id)
                continue
            with self.observability.span(f"command.{command.kind.value}", ctx=scope,
                                         stage="dispatch", gateway=gateway.name,
                                         dedup_key=command.dedup_key,
                                         mode=command.mode) as span:
                result = gateway.execute(command, ctx=span, obs=self.observability)
            report.results.append(result)
            if result.status == "duplicate":
                report.duplicated += 1
            elif result.executed:
                report.executed += 1
            elif result.ok:
                report.skipped += 1
            else:
                report.errors.append(f"{command.kind.value}: {result.detail}")
            self.observability.event(
                "command.result", ctx=span, stage="dispatch",
                command=command.kind.value, command_id=command.command_id,
                gateway=result.gateway, ok=result.ok, status=result.status,
                duration_ms=result.duration_ms, detail=result.detail,
                dedup_key=command.dedup_key)

        report.ok = not report.errors
        self.observability.event(
            "plan.dispatched", ctx=scope, stage="dispatch", outcome="ok" if report.ok else "error",
            executed=report.executed, skipped=report.skipped,
            duplicated=report.duplicated, errors=len(report.errors))
        if report.errors and self.raise_on_error:
            raise GatewayError(report.errors, report.results)
        return report


def dispatch(plan: CommandPlan, gateways: Sequence[Gateway], *,
             observability: Optional[Observability] = None,
             ctx: Optional[TraceContext] = None,
             raise_on_error: bool = False) -> DispatchReport:
    """Scorciatoia: dispatcher usa-e-getta su un piano."""
    return Dispatcher(gateways, observability=observability,
                      raise_on_error=raise_on_error).dispatch(plan, ctx=ctx)


__all__ = ["DispatchReport", "Dispatcher", "GatewayError", "dispatch"]
