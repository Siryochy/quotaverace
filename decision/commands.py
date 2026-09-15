"""decision/commands.py — Il motore emette COMANDI, non effetti.

Perche' esiste (rifattorizzazione del 15/09/2026): la catena di decisione
sapeva *cosa* andava fatto, ma lo faceva da sola (o lo lasciava fare al
chiamante, in ordine sparso). Qui il motore produce **comandi leggeri** —
dati serializzabili che descrivono l'effetto desiderato — e il compito di
eseguirli passa a gateway dedicati (`decision/gateways.py`), instradati dal
`Dispatcher` (`decision/dispatcher.py`).

Vantaggi concreti, non teorici:

- **Testabilita'**: un piano si ispeziona senza DB, senza rete, senza ordini
  (`test_decision_commands.py` verifica esattamente questo).
- **Shadow mode**: lo stesso piano si puo' eseguire con gateway che NON
  eseguono (solo registrano) — e' il confronto misurato voluto per auto_bet.
- **Idempotenza**: ogni comando porta una `dedup_key` stabile (stesso
  effetto -> stessa chiave), cosi' un job che gira ogni 60s non produce
  duplicati quando non c'e' nulla di nuovo.
- **Audit**: il piano e' JSON, quindi finisce nei log strutturati senza
  conversioni (nessun `repr()` di oggetti vivi).

Regole del modulo:

1. **Purezza assoluta**: nessun import di `tracker`, `auto_bet`, provider o
   rete. Solo pydantic. Un comando non esegue, descrive.
2. **Leggerezza**: nel payload c'e' SOLO cio' che serve a eseguire. Il
   `DecisionRecord` completo viaggia dentro `PersistDecision` (e' un dato,
   non una dipendenza).
3. **Un comando, un effetto**: `PlaceOrder` non notifica, `NotifyOperators`
   non scrive sul ledger. L'ordine degli effetti e' responsabilita' del
   `Dispatcher`, che li esegue nell'ordine in cui sono emessi.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .models import DecisionRecord, Mode, Outcome, Signal, utcnow


class CommandKind(str, Enum):
    """Tipi di effetto che il motore puo' chiedere."""

    PERSIST_DECISION = "persist_decision"    # scrive la riga sul ledger
    PLACE_ORDER = "place_order"              # ordine sull'exchange (SOLO live)
    NOTIFY_OPERATORS = "notify_operators"    # Telegram a admin/iscritti


#: Ordine di esecuzione dei comandi quando il motore ne emette piu' di uno.
#: L'ordine e' deliberato: prima si registra la decisione (audit), poi si
#: esegue l'ordine, poi si avvisa. Un fallimento a valle non cancella l'audit.
COMMAND_ORDER = (CommandKind.PERSIST_DECISION, CommandKind.PLACE_ORDER,
                 CommandKind.NOTIFY_OPERATORS)


def _digest(*parts: Any) -> str:
    """Digest stabile a 12 hex per le chiavi di deduplicazione."""
    raw = "|".join("" if p is None else str(p) for p in parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Payload tipizzati (validati all'EMISSIONE: un comando malformato non nasce)
# ---------------------------------------------------------------------------

class PlaceOrderPayload(BaseModel):
    """Cio' che serve a un gateway d'esecuzione. Niente logica, solo dati."""

    match_id: str
    league: str = ""
    home: str = ""
    away: str = ""
    market: str = "1X2"
    outcome: Outcome
    selection_label: str = ""
    kickoff: datetime
    #: Quota del segnale: e' il BOUND dell'ordine (mai peggio di cosi').
    price: float = Field(..., gt=1.0)
    stake: float = Field(..., gt=0.0)
    mode: Mode = "live"
    provider: str = ""
    time_in_force: str = "IOC"


class PersistDecisionPayload(BaseModel):
    """La riga piatta del ledger (`DecisionRecord.as_row()`)."""

    row: dict[str, Any]
    record_id: str = ""


class NotifyPayload(BaseModel):
    """Messaggio agli operatori, con chiave anti-spam."""

    kind: Literal["blocked", "review_pending", "order_placed", "info"] = "info"
    text: str
    dedup_key: str = ""
    targets: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Comando
# ---------------------------------------------------------------------------

class Command(BaseModel):
    """Un effetto richiesto dal motore, in forma di dato."""

    kind: CommandKind
    payload: dict[str, Any] = Field(default_factory=dict)
    command_id: str = ""
    #: Chiave STABILE dell'effetto (stesso effetto -> stessa chiave): i
    #: gateway la usano per non ripetere cio' che e' gia' stato fatto.
    dedup_key: str = ""
    signal_id: str = ""
    record_id: str = ""
    mode: Mode = "sim"
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _fill_ids(self):
        if not self.command_id:
            self.command_id = _digest(self.kind.value, self.record_id or self.signal_id,
                                      self.created_at.strftime("%Y%m%dT%H%M%S"))
        return self

    @property
    def order(self) -> int:
        """Posizione del comando nell'ordine di esecuzione dichiarato."""
        try:
            return COMMAND_ORDER.index(self.kind)
        except ValueError:                          # kind sconosciuto: in coda
            return len(COMMAND_ORDER)

    def as_json(self) -> dict[str, Any]:
        """Rappresentazione JSON-safe (per log strutturati e shadow ledger)."""
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Fabbriche: costruiscono E validano il payload all'emissione
# ---------------------------------------------------------------------------

def persist_decision_command(record: DecisionRecord) -> Command:
    """Comando che scrive la decisione sul ledger (`decisions`)."""
    row = record.as_row()
    return Command(
        kind=CommandKind.PERSIST_DECISION,
        payload=PersistDecisionPayload(row=row, record_id=record.record_id).model_dump(mode="json"),
        dedup_key=_digest("persist", record.record_id),
        signal_id=record.signal.signal_id,
        record_id=record.record_id,
        mode=record.mode,
    )


def place_order_command(record: DecisionRecord, *, provider: str = "",
                        home: str = "", away: str = "",
                        stake: Optional[float] = None) -> Command:
    """Comando d'ordine per un segnale APPROVATO ed ESEGUIBILE.

    La quota nel payload e' quella del segnale: il gateway non puo' riempire a
    un prezzo peggiore (floor EV, come in `auto_bet._live_fill`). La
    `dedup_key` rende l'ordine idempotente per (match, esito).
    """
    stake_value = float(stake if stake is not None else (record.stake.stake if record.stake else 0.0))
    payload = PlaceOrderPayload(
        match_id=record.signal.match_id,
        league=record.signal.league,
        home=home, away=away,
        market=record.signal.market,
        outcome=record.signal.outcome,
        selection_label=record.signal.selection_label,
        kickoff=record.signal.kickoff,
        price=record.signal.price,
        stake=stake_value,
        mode=record.mode,
        provider=provider or record.provider,
    )
    return Command(
        kind=CommandKind.PLACE_ORDER,
        payload=payload.model_dump(mode="json"),
        dedup_key=_digest("order", record.signal.match_id, record.signal.outcome),
        signal_id=record.signal.signal_id,
        record_id=record.record_id,
        mode=record.mode,
    )


def notify_command(record: DecisionRecord, *, kind: str, text: str,
                   targets: Optional[list[str]] = None,
                   scope: str = "") -> Command:
    """Comando di notifica, con chiave anti-spam.

    `scope` decide la granularita' della deduplicazione: vuoto = per decisione
    (una notifica per segnale, caso `review_pending`); valorizzato in modo
    stabile nel tempo (es. il giorno) = UNA notifica per quel tipo, come
    l'anti-spam del kill switch che avvisava 1440 volte al giorno col job ogni
    60s (chiave `KS_OFF` del 09/09).
    """
    day = record.created_at.strftime("%Y%m%d")
    key = _digest("notify", kind, scope) if scope else _digest("notify", kind,
                                                               record.record_id, day)
    return Command(
        kind=CommandKind.NOTIFY_OPERATORS,
        payload=NotifyPayload(kind=kind, text=text,           # type: ignore[arg-type]
                              dedup_key=key,
                              targets=list(targets or [])).model_dump(mode="json"),
        dedup_key=key,
        signal_id=record.signal.signal_id,
        record_id=record.record_id,
        mode=record.mode,
    )


# ---------------------------------------------------------------------------
# Piano
# ---------------------------------------------------------------------------

class CommandPlan(BaseModel):
    """L'uscita del motore: un record + i comandi che lo traducono in effetti.

    Il piano NON esegue niente. `dispatcher.dispatch(plan)` lo fa, con i
    gateway scelti (reali, shadow, finti nei test).
    """

    plan_id: str = ""
    record: DecisionRecord
    commands: list[Command] = Field(default_factory=list)
    #: Motivo del fail-fast, se un blocco di sicurezza ha fermato la catena
    #: PRIMA di qualunque calcolo (`decision/guards.py`).
    blocked: Optional[dict[str, Any]] = None
    #: Identita' del feed DI MERCATO che ha preceduto il Risk Engine
    #: (`decision/feeds.py`): request_id, trace_id, gateway_id, schema_version,
    #: config_hash, source, refreshed_at. E' cio' che lega una decisione alla
    #: quotatura su cui e' stata presa — senza, "perche' questo ordine?" non ha
    #: risposta verificabile.
    market: Optional[dict[str, Any]] = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _fill_plan(self):
        if not self.plan_id:
            self.plan_id = _digest("plan", self.record.record_id)
        self.commands.sort(key=lambda c: c.order)
        return self

    def kinds(self) -> list[str]:
        return [c.kind.value for c in self.commands]

    def of_kind(self, kind: CommandKind) -> list[Command]:
        return [c for c in self.commands if c.kind == kind]

    @property
    def places_order(self) -> bool:
        return bool(self.of_kind(CommandKind.PLACE_ORDER))

    def as_json(self) -> dict[str, Any]:
        """Piano serializzato: e' cio' che finisce nei log e nello shadow ledger."""
        return {
            "plan_id": self.plan_id,
            "record_id": self.record.record_id,
            "signal_id": self.record.signal.signal_id,
            "verdict": self.record.risk.verdict,
            "reason": self.record.risk.reason.value,
            "mode": self.record.mode,
            "commands": [c.as_json() for c in self.commands],
            "blocked": self.blocked,
            "market": self.market,
            "created_at": self.created_at.isoformat(),
        }


def plan_for_record(record: DecisionRecord, commands: list[Command], *,
                    blocked: Optional[dict] = None,
                    market: Optional[dict] = None) -> CommandPlan:
    """Piano da record + comandi (i comandi restano ordinati per `order`)."""
    return CommandPlan(record=record, commands=list(commands), blocked=blocked,
                       market=market)


__all__ = [
    "COMMAND_ORDER", "Command", "CommandKind", "CommandPlan", "NotifyPayload",
    "PersistDecisionPayload", "PlaceOrderPayload", "notify_command",
    "persist_decision_command", "place_order_command", "plan_for_record",
]
