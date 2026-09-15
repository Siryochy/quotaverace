"""decision/gateways.py — Chi esegue davvero i comandi (side effect isolati).

Il motore (`decision/engine.py`) emette comandi; qui ci sono i **gateway** che
li traducono in effetti. Ogni gateway:

- dichiara quali `CommandKind` sa gestire (`kinds`);
- implementa `_run(command, ctx, obs)` — il resto (tempi, eccezioni, risultato)
  e' gestito dal template di `BaseGateway`;
- **non solleva mai**: un errore diventa un `CommandResult` con `ok=False` e il
  dettaglio. Il `Dispatcher` decide se propagarlo come errore di piano.

Gateway di produzione:

| gateway | comando | effetto reale |
|---|---|---|
| `LedgerGateway` | `persist_decision` | riga sul ledger `decisions` (tracker) |
| `PlaceOrderGateway` | `place_order` | ordine su SX Bet via `auto_bet._live_fill` |
| `NotifyGateway` | `notify_operators` | messaggio Telegram a admin |
| `ShadowGateway` | TUTTI | scrive il comando sullo shadow ledger, non esegue |

`PlaceOrderGateway` **non reimplementa** l'esecuzione: delega al percorso gia'
testato di `auto_bet` (risoluzione mercato, floor EV, risk cap, monitor
liquidita'). Reimplementarlo qui sarebbe il modo piu' rapido per far divergere
il percorso nuovo da quello che gira in produzione.

`ShadowGateway` e' il cuore della **shadow mode** chiesta il 15/09/2026: gli
stessi comandi vengono registrati su un JSONL dedicato
(`DATA_DIR/decision/shadow_commands.jsonl`, env `DECISION_SHADOW_LOG`) con
deduplicazione per `dedup_key`, cosi' un job che gira ogni 60s non produce
migliaia di righe identiche. Non tocca il ledger, non chiama provider, non
invia nulla: e' un registro di cosa *sarebbe stato fatto*.

Per i test/lo sviluppo offline non servono fixture: si inietta un gateway
finto (una manciata di righe nel test) oppure si usa `ShadowGateway` con un
percorso temporaneo. **Nessun credito API viene consumato** da questo modulo.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from pydantic import BaseModel, Field

from .commands import Command, CommandKind
from .middleware import Observability, TraceContext

logger = logging.getLogger("decision.gateways")

SHADOW_LOG_ENV = "DECISION_SHADOW_LOG"
#: Righe lette in coda al file shadow per ricostruire le `dedup_key` gia' viste
#: (limite: il file puo' crescere, non si rilegge tutto a ogni giro).
SHADOW_TAIL_LINES = 5000


class CommandResult(BaseModel):
    """Esito dell'esecuzione di un comando (dato, non eccezione)."""

    kind: CommandKind
    command_id: str = ""
    gateway: str = ""
    ok: bool = False
    status: str = ""            # executed | recorded | duplicate | skipped | error
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = 0.0
    dry_run: bool = False

    @property
    def executed(self) -> bool:
        return self.ok and self.status in ("executed", "recorded")


class Gateway(Protocol):
    """Contratto: dichiara i tipi gestiti ed esegue il comando."""

    name: str
    kinds: tuple[CommandKind, ...]

    def handles(self, kind: CommandKind) -> bool:  # pragma: no cover - protocollo
        ...

    def execute(self, command: Command, *, ctx: Optional[TraceContext] = None,
                obs: Optional[Observability] = None) -> CommandResult:  # pragma: no cover
        ...


class BaseGateway:
    """Template: tempi + cattura eccezioni + risultato tipizzato."""

    name = "base"
    kinds: tuple[CommandKind, ...] = ()

    def handles(self, kind: CommandKind) -> bool:
        return kind in self.kinds

    def execute(self, command: Command, *, ctx: Optional[TraceContext] = None,
                obs: Optional[Observability] = None) -> CommandResult:
        started = time.perf_counter()
        try:
            result = self._run(command, ctx=ctx, obs=obs)
        except Exception as exc:                  # mai un'eccezione verso il dispatcher
            logger.warning("gateway %s: comando %s fallito (%s)",
                           self.name, command.kind.value, exc)
            result = CommandResult(kind=command.kind, command_id=command.command_id,
                                   gateway=self.name, ok=False, status="error",
                                   detail=f"{type(exc).__name__}: {exc}")
        result.duration_ms = round((time.perf_counter() - started) * 1000, 3)
        if not result.command_id:
            result.command_id = command.command_id
        if not result.gateway:
            result.gateway = self.name
        return result

    def _run(self, command: Command, *, ctx: Optional[TraceContext],
             obs: Optional[Observability]) -> CommandResult:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

class LedgerGateway(BaseGateway):
    """Scrive la decisione sul ledger `decisions` (via `decision.feedback`)."""

    name = "ledger"
    kinds = (CommandKind.PERSIST_DECISION,)

    def __init__(self, persist: Optional[Callable[[Any], dict]] = None) -> None:
        self._persist = persist

    def _run(self, command: Command, *, ctx, obs) -> CommandResult:
        from .feedback import persist as default_persist
        writer = self._persist or default_persist
        row = dict(command.payload.get("row") or {})
        if not row:
            return CommandResult(kind=command.kind, ok=False, status="error",
                                 detail="payload senza riga di decisione")
        out = writer(row)
        if not out.get("saved"):
            return CommandResult(kind=command.kind, ok=False, status="error",
                                 detail=out.get("error") or "salvataggio fallito",
                                 data={"record_id": out.get("record_id", "")})
        return CommandResult(kind=command.kind, ok=True, status="executed",
                             detail="decisione registrata",
                             data={"record_id": out.get("record_id", "")})


# ---------------------------------------------------------------------------
# Esecuzione ordini
# ---------------------------------------------------------------------------

class PlaceOrderGateway(BaseGateway):
    """Piazza l'ordine reale DELEGANDO ad `auto_bet._live_fill`.

    Con `dry_run=True` non chiama nulla: registra e basta (usato dai test e per
    ispezionare il percorso). In modo `sim` non si ordina: il ledger SIM resta
    di competenza di `auto_bet`.
    """

    name = "place_order"
    kinds = (CommandKind.PLACE_ORDER,)

    def __init__(self, *, dry_run: bool = False,
                 fill: Optional[Callable[[dict, float, float], Optional[dict]]] = None) -> None:
        self.dry_run = dry_run
        self._fill = fill

    def _run(self, command: Command, *, ctx, obs) -> CommandResult:
        payload = dict(command.payload)
        price = float(payload.get("price") or 0.0)
        stake = float(payload.get("stake") or 0.0)
        if payload.get("mode") != "live":
            return CommandResult(kind=command.kind, ok=True, status="skipped",
                                 detail=f"modo '{payload.get('mode')}': nessun ordine reale",
                                 dry_run=self.dry_run)
        if stake <= 0 or price <= 1.0:
            return CommandResult(kind=command.kind, ok=False, status="error",
                                 detail="stake o quota non validi nel comando")
        if self.dry_run:
            return CommandResult(kind=command.kind, ok=True, status="recorded",
                                 detail="dry-run: comando non eseguito",
                                 data={"price": price, "stake": stake}, dry_run=True)

        if self._fill is None:
            from auto_bet import _live_fill            # import pigro (pesante)
            fill = _live_fill
        else:
            fill = self._fill

        pick = {
            "match_id": payload.get("match_id"),
            "mercato": payload.get("market", "1X2"),
            "esito_key": payload.get("outcome"),
            "home": payload.get("home", ""),
            "away": payload.get("away", ""),
            "commence": payload.get("kickoff"),
            "quota": price,
            "league": payload.get("league", ""),
        }
        filled = fill(pick, stake, price)
        if filled is None:
            return CommandResult(kind=command.kind, ok=True, status="skipped",
                                 detail="ordine saltato (mercato assente, prezzo sotto "
                                        "il floor EV o errore di rete)")
        if not filled.get("ok"):
            return CommandResult(kind=command.kind, ok=False, status="error",
                                 detail=str(filled.get("error") or filled.get("status") or "non piazzato"),
                                 data=dict(filled))
        return CommandResult(kind=command.kind, ok=True, status="executed",
                             detail=str(filled.get("status") or "SUCCESS"),
                             data=dict(filled))


# ---------------------------------------------------------------------------
# Notifiche
# ---------------------------------------------------------------------------

def admin_targets() -> list[str]:
    """Chat ID degli operatori (`ADMIN_CHAT_ID`, fallback `TELEGRAM_CHAT_IDS`).

    Fonte unica per le notifiche di decisione e per i prompt di revisione
    (`decision/review_telegram.py`): l'elenco dei destinatari non va duplicato
    in due moduli che devono restare d'accordo.
    """
    raw = os.getenv("ADMIN_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_IDS", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _send_telegram(text: str, targets: list[str]) -> bool:
    """POST diretto all'API Telegram (stessa convenzione di `surebet_engine`)."""
    token = os.getenv("QUOTAVERACE_BOT_TOKEN", "")
    if not token or not targets:
        logger.warning("notify: token o destinatari mancanti, notifica saltata")
        return False
    import requests
    ok = True
    for chat_id in targets:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=20)
            if response.status_code != 200:
                logger.warning("notify %s: HTTP %s", chat_id, response.status_code)
                ok = False
        except Exception as exc:
            logger.warning("notify %s: %s", chat_id, exc)
            ok = False
    return ok


class NotifyGateway(BaseGateway):
    """Messaggio all'operatore (Telegram). `sender` iniettabile nei test."""

    name = "notify"
    kinds = (CommandKind.NOTIFY_OPERATORS,)

    def __init__(self, sender: Optional[Callable[[str, list[str]], bool]] = None,
                 targets: Optional[list[str]] = None) -> None:
        self._sender = sender
        self._targets = targets

    def _run(self, command: Command, *, ctx, obs) -> CommandResult:
        payload = dict(command.payload)
        text = str(payload.get("text") or "")
        if not text:
            return CommandResult(kind=command.kind, ok=False, status="error",
                                 detail="notifica senza testo")
        targets = list(self._targets if self._targets is not None
                       else (payload.get("targets") or admin_targets()))
        sender = self._sender or _send_telegram
        if not targets:
            return CommandResult(kind=command.kind, ok=True, status="skipped",
                                 detail="nessun destinatario configurato")
        delivered = bool(sender(text, targets))
        return CommandResult(kind=command.kind, ok=delivered,
                             status="executed" if delivered else "error",
                             detail="notifica inviata" if delivered else "invio fallito",
                             data={"kind": payload.get("kind"), "targets": targets})


# ---------------------------------------------------------------------------
# Shadow
# ---------------------------------------------------------------------------

def shadow_log_path() -> Path:
    override = os.getenv(SHADOW_LOG_ENV)
    if override:
        return Path(override)
    try:
        from config import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:
        base = Path("data")
    return base / "decision" / "shadow_commands.jsonl"


class ShadowGateway(BaseGateway):
    """Registra il comando e NON esegue nulla (shadow mode).

    Deduplica per `dedup_key` (letto dalla coda del file): lo stesso effetto
    richiesto dal job ogni 60s finisce UNA volta sola nel registro. Scrittura
    atomica in append e fail-safe: un file non scrivibile non ferma niente.
    """

    name = "shadow"
    kinds = tuple(CommandKind)
    #: Dichiarazione esplicita: questo gateway NON esegue. Il `Dispatcher` la
    #: usa per dire "nessun effetto reale in questo giro" (`report.shadow`).
    dry_run = True

    def __init__(self, path: Optional[str | Path] = None, *, remember: bool = True) -> None:
        self.path = Path(path) if path else shadow_log_path()
        self.remember = remember
        self._seen: set[str] = set()
        if remember:
            self._seen = self._load_seen()

    # -- registro --------------------------------------------------------
    def _load_seen(self) -> set[str]:
        seen: set[str] = set()
        try:
            if not self.path.exists():
                return seen
            with open(self.path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-SHADOW_TAIL_LINES:]
            for line in lines:
                try:
                    key = json.loads(line).get("dedup_key")
                except Exception:
                    continue
                if key:
                    seen.add(str(key))
        except Exception as exc:
            logger.warning("shadow: registro non leggibile (%s)", exc)
        return seen

    def _append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def _run(self, command: Command, *, ctx, obs) -> CommandResult:
        key = command.dedup_key or command.command_id
        if self.remember and key in self._seen:
            return CommandResult(kind=command.kind, ok=True, status="duplicate",
                                 detail="comando gia' registrato (dedup_key)",
                                 data={"dedup_key": key}, dry_run=True)
        entry = {
            "ts": command.created_at.isoformat(),
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "dedup_key": key,
            "command": command.as_json(),
        }
        if ctx is not None:
            entry["trace"] = ctx.as_dict()
        if obs is not None:
            entry["config_hash"] = obs.config_fingerprint
        self._append(entry)
        if self.remember:
            self._seen.add(key)
        return CommandResult(kind=command.kind, ok=True, status="recorded",
                             detail="comando registrato (nessuna esecuzione)",
                             data={"dedup_key": key, "path": str(self.path)},
                             dry_run=True)


__all__ = [
    "BaseGateway", "CommandResult", "Gateway", "LedgerGateway", "NotifyGateway",
    "PlaceOrderGateway", "SHADOW_LOG_ENV", "ShadowGateway", "admin_targets",
    "shadow_log_path",
]
