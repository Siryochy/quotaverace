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

`require_persist=True` (opt-in, default invariato) introduce una regola in piu',
limitata a cio' che costa denaro: **l'ordine reale parte solo se l'audit del
piano e' andato a buon fine**. Concretamente, un `place_order` viene saltato se
il `persist_decision` del piano e' fallito, se non c'e' affatto un
`persist_decision`, o se il gateway di storage ha convalidato la riga con esito
non positivo (`data["validated"] != True` — la Shadow Validation di
`decision/gateways.ValidatingLedgerGateway`).

Perche' blocca SOLO l'ordine: le notifiche e l'audit sono l'altra meta' del
lavoro. Una revisione umana deve poter arrivare anche quando il ledger ha avuto
un problema — altrimenti un guasto di telemetria diventerebbe un silenzio
operativo, che e' un guasto peggiore.

Nessun import di produzione a livello di modulo: il dispatcher orchestra e
basta, quindi si testa con gateway finti in memoria (zero rete, zero crediti).
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from pydantic import BaseModel, Field

from .commands import Command, CommandKind, CommandPlan
from .gateways import AUDIT_ONLY_ATTR, CommandResult, Gateway
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
    signal_id: str = ""
    results: list[CommandResult] = Field(default_factory=list)
    ok: bool = True
    executed: int = 0
    skipped: int = 0
    duplicated: int = 0
    errors: list[str] = Field(default_factory=list)
    #: True se i gateway usati NON eseguono (shadow/dry-run): nessun effetto reale.
    shadow: bool = False
    #: True se `require_persist` ha fermato l'ordine (audit non soddisfatto).
    aborted: bool = False
    blocked_reason: str = ""

    def of_kind(self, kind: CommandKind) -> list[CommandResult]:
        return [r for r in self.results if r.kind == kind]

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Dispatcher:
    """Esegue piani su un insieme ordinato di gateway."""

    def __init__(self, gateways: Sequence[Gateway], *,
                 observability: Optional[Observability] = None,
                 raise_on_error: bool = False,
                 require_persist: bool = False) -> None:
        self.gateways: list[Gateway] = list(gateways)
        self.observability = observability or Observability()
        self.raise_on_error = raise_on_error
        #: Opt-in (default invariato, fail-soft): blocca SOLO `place_order`
        #: finche' l'audit del piano non e' andato a buon fine.
        self.require_persist = bool(require_persist)

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
        """True se nessun gateway registrato esegue effetti reali.

        I gateway di **solo audit** (`audit_only`: ledger e convalida) vengono
        esclusi dal conteggio: scrivono telemetria, non ordinano e non
        notificano. Senza questa distinzione un giro in shadow mode con la
        persistenza attiva si dichiarerebbe "non shadow" pur non avendo
        eseguito nulla sul mondo.
        """
        if not self.gateways:
            return False
        executable = [gw for gw in self.gateways
                      if not bool(getattr(gw, AUDIT_ONLY_ATTR, False))]
        if not executable:
            return True                    # solo audit: nessun effetto eseguibile
        return all(bool(getattr(gateway, "dry_run", False)) for gateway in executable)

    @staticmethod
    def _audit_ok(result: CommandResult, reason: list[str]) -> bool:
        """L'audit del piano e' soddisfatto da questo `persist_decision`?

        `data["validated"]` esiste solo se il gateway ha eseguito la Shadow
        Validation (`ValidatingLedgerGateway`): senza quel campo l'assenza del
        verdetto non e' un fallimento — e' semplicemente un gateway che non
        convalida, e allora basta che il salvataggio sia riuscito.
        """
        if not result.ok:
            reason.append(f"persistenza fallita ({result.detail or 'errore'})")
            return False
        validated = result.data.get("validated", True)
        if validated is not True:
            state = result.data.get("decision_status") or "non convalidata"
            why = result.data.get("validation_reason") or "esito non positivo"
            reason.append(f"convalida {state} ({why})")
            return False
        return True

    # -- esecuzione ------------------------------------------------------
    def dispatch(self, plan: CommandPlan, *,
                 ctx: Optional[TraceContext] = None) -> DispatchReport:
        """Esegue i comandi del piano nell'ordine dichiarato (`Command.order`)."""
        scope = ctx or self.observability.new_trace()
        report = DispatchReport(plan_id=plan.plan_id, record_id=plan.record.record_id,
                                signal_id=plan.record.signal.signal_id,
                                shadow=self.shadow)
        self.observability.event(
            "plan.dispatch", ctx=scope, stage="dispatch",
            record_id=plan.record.record_id, signal_id=plan.record.signal.signal_id,
            verdict=plan.record.risk.verdict, reason=plan.record.risk.reason.value,
            mode=plan.record.mode, commands=plan.kinds(), shadow=self.shadow,
            require_persist=self.require_persist)

        # Audit del piano: None = nessun `persist_decision` incontrato finora
        # (con `require_persist` un ordine senza audit viene saltato).
        audit_ok: Optional[bool] = None
        audit_reason: list[str] = []

        for command in sorted(plan.commands, key=lambda c: c.order):
            if (command.kind == CommandKind.PLACE_ORDER and self.require_persist
                    and audit_ok is not True):
                detail = ("; ".join(audit_reason) if audit_reason
                          else "nessun persist_decision nel piano")
                report.skipped += 1
                report.aborted = True
                report.blocked_reason = detail
                self.observability.event(
                    "order.blocked", ctx=scope, stage="dispatch", outcome="blocked",
                    record_id=command.record_id, command_id=command.command_id,
                    dedup_key=command.dedup_key, reason=detail)
                continue

            gateway = self.gateway_for(command)
            if gateway is None:
                report.errors.append(f"{command.kind.value}: nessun gateway")
                report.skipped += 1
                if command.kind == CommandKind.PERSIST_DECISION:
                    audit_ok = False
                    audit_reason.append("persist_decision senza gateway")
                self.observability.event("command.unhandled", ctx=scope, stage="dispatch",
                                         command=command.kind.value,
                                         command_id=command.command_id,
                                         record_id=command.record_id,
                                         signal_id=command.signal_id)
                continue
            with self.observability.span(f"command.{command.kind.value}", ctx=scope,
                                         stage="dispatch", gateway=gateway.name,
                                         dedup_key=command.dedup_key,
                                         mode=command.mode,
                                         record_id=command.record_id,
                                         signal_id=command.signal_id) as span:
                result = gateway.execute(command, ctx=span, obs=self.observability)
            report.results.append(result)
            if command.kind == CommandKind.PERSIST_DECISION and audit_ok is not False:
                audit_ok = self._audit_ok(result, audit_reason)
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
                record_id=command.record_id, signal_id=command.signal_id,
                command=command.kind.value, command_id=command.command_id,
                gateway=result.gateway, ok=result.ok, status=result.status,
                duration_ms=result.duration_ms, detail=result.detail,
                dedup_key=command.dedup_key,
                validated=result.data.get("validated"))

        report.ok = not report.errors
        self.observability.event(
            "plan.dispatched", ctx=scope, stage="dispatch",
            outcome="blocked" if report.aborted else ("ok" if report.ok else "error"),
            record_id=plan.record.record_id, signal_id=plan.record.signal.signal_id,
            executed=report.executed, skipped=report.skipped,
            duplicated=report.duplicated, errors=len(report.errors),
            aborted=report.aborted, blocked_reason=report.blocked_reason,
            audit_ok=audit_ok)
        if report.errors and self.raise_on_error:
            raise GatewayError(report.errors, report.results)
        return report


def dispatch(plan: CommandPlan, gateways: Sequence[Gateway], *,
             observability: Optional[Observability] = None,
             ctx: Optional[TraceContext] = None,
             raise_on_error: bool = False,
             require_persist: bool = False) -> DispatchReport:
    """Scorciatoia: dispatcher usa-e-getta su un piano."""
    return Dispatcher(gateways, observability=observability,
                      raise_on_error=raise_on_error,
                      require_persist=require_persist).dispatch(plan, ctx=ctx)


__all__ = ["DispatchReport", "Dispatcher", "GatewayError", "dispatch"]
