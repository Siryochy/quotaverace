"""decision/feeds.py — Il gateway di mercato: SX Bet come sorgente PRIMARIA.

Il contratto (`decision/market.py`) dice *cosa* e' una quota valida. Qui c'e'
**chi la porta dentro**: un gateway con sorgenti ordinate per priorita', un
refresh forzato prima del Risk Engine e un gate che **blocca** quando il
mercato non e' affidabile.

    SxBetSource (primaria)  ─┐
    ...altre sorgenti...     ├─▶  MarketFeed.refresh()  ─▶  FeedSnapshot
                             ┘        (forzato)              │
                                                              ▼
                                            verify_feed() ─▶ gate (fail-closed)
                                                              │
                                                     Risk Engine (mai prima)

Perche' SX e' la primaria: e' l'exchange su cui ordiniamo davvero
(`execution_engine` → `SxBetProvider`, ordini firmati), quindi la quota che
conta e' quella del SUO libro — letta in pubblico, senza credenziali e senza
crediti the-odds-api. Una fonte secondaria non la sostituisce: entra solo se la
primaria fallisce (e il fallimento resta scritto nella `errors` dello snapshot).

Perche' il refresh e' FORZATO: una quota vecchia e' peggio di nessuna quota.
Il gate confronta `refreshed_at` con `FEED_MAX_AGE_MINUTES` e, se non ha un
refresh recente **e conforme al contratto**, la catena si ferma. Non esiste un
percorso che arrivi al Risk Engine con dati di mercato vecchi.

Perche' esiste una finestra di riuso (`FEED_REFRESH_MIN_SECONDS`, default 600):
il job auto_bet gira ogni 60s e l'endpoint pubblico SX va rispettato — "forzato"
significa *non usare una cache vecchia*, non *martellare l'exchange*. Entro la
finestra lo snapshot precedente viene riusato (`reused=True`) e resta comunque
dentro il limite di freschezza (`max_age` = 20 min > finestra: il riuso non
puo' mascherare uno snapshot stantio). `reuse_seconds=0` (CLI `--force`) ignora
la finestra per una verifica puntuale.

**Validazione prima delle puntate** (direttiva del proprietario, 15/09/2026):
lo stop alle scommesse automatiche resta finche' il feed non ha dimostrato di
funzionare. Servono `FEED_MIN_REFRESHES` (default 3) refresh consecutivi ok —
"ok" = una sorgente ha risposto **e** nessuna quota ha violato il contratto —
**e** almeno una quota validata in totale (un feed vuoto non prova nulla).
Un fallimento azzera il contatore: la validazione e' una serie, non un timbro.
Lo stato vive sul volume (`DATA_DIR/decision/feed_state.json`), quindi
sopravvive ai redeploy; un file corrotto NON vale come validato (fail-closed:
qui l'incertezza non deve aprire le puntate).

Regole del modulo, coerenti col pacchetto:

1. **Import pigro**: `sx_signals`/`execution_engine` (che tirano dentro
   `tracker`, `poisson_engine`, ...) sono importati DENTRO `SxBetSource.fetch`.
   `import decision` resta leggero (tripwire in `test_decision_pipeline.py`).
2. **Mai un'eccezione dal refresh** (`ok=False` + `errors`): la lettura dati non
   rompe il job; a bloccare e' il gate, in modo esplicito e tracciabile.
3. **Tracciabilita' totale**: ogni refresh porta con se' `request_id`,
   `trace_id`, `gateway_id`, `schema_version` e `config_hash` — nei log
   strutturati, nello snapshot, nello stato su disco e nel piano dei comandi
   (`CommandPlan.market`), cosi' una decisione si lega alla quotatura che l'ha
   motivata.
4. **Zero ordini, zero crediti**: solo letture pubbliche SX. Nessun comando di
   questo modulo puo' piazzare qualcosa (l'esecuzione sta in `auto_bet`).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence

from pydantic import BaseModel, Field

from . import guards
from .market import (
    MARKET_SCHEMA_VERSION, MarketQuote, QuoteBatch, validate_batch,
)
from .middleware import Observability, TraceContext, config_hash, new_id
from .models import ReasonCode, utcnow

logger = logging.getLogger("decision.feeds")

#: Gateway di default: identifica CHI ha ingerito la quota (finisce in ogni
#: `MarketQuote.gateway_id` e in ogni evento/stato).
DEFAULT_GATEWAY_ID = "sxbet-feed"

ENABLED_ENV = "DECISION_FEED_ENABLED"
PRIMARY_ENV = "DECISION_FEED_PRIMARY"
STATE_ENV = "DECISION_FEED_STATE"
MAX_AGE_ENV = "DECISION_FEED_MAX_AGE_MIN"
MIN_REFRESHES_ENV = "DECISION_FEED_MIN_REFRESHES"
REUSE_ENV = "DECISION_FEED_REFRESH_MIN_SEC"

#: Freschezza massima accettata dal gate (minuti) e finestra di riuso.
DEFAULT_MAX_AGE_MINUTES = 20.0
DEFAULT_REUSE_SECONDS = 600.0
#: Refresh CONSECUTIVI ok necessari per considerare validato il feed.
DEFAULT_MIN_REFRESHES = 3

#: Errore di una sorgente: la primaria non ha risposto (o ha risposto male).
MAX_ERROR_CHARS = 200


class SourceUnavailable(RuntimeError):
    """La sorgente non ha potuto produrre quote (rete, API, mappatura)."""


# ---------------------------------------------------------------------------
# Sorgenti
# ---------------------------------------------------------------------------

class QuoteSource(Protocol):
    """Contratto di una sorgente: restituisce righe conformi al contratto.

    La sorgente mappa il proprio formato sui campi canonici
    (`event_id`, `market`, `selection`, `odds`, `timestamp`, `source`,
    `gateway_id`, ...). La validazione NON e' compito suo: la fa il feed, in un
    punto solo (`validate_batch`), cosi' nessuna sorgente puo' aggirarla.
    """

    name: str
    source_id: str

    def fetch(self, *, gateway_id: str, ctx: Optional[TraceContext] = None,
              obs: Optional[Observability] = None) -> list[dict]:  # pragma: no cover
        ...


class SxBetSource:
    """Sorgente PRIMARIA: mercati 1X2 calcio dall'API pubblica di SX Bet.

    Riusa la discovery di `sx_signals` (`_discover` + `_books_parallel`:
    stessa pagina di `/markets/active`, stesso raggruppamento dei 3 mercati
    binari "X vs Not X", stesso order book taker). Non la reimplementa: due
    discovery diverse divergerebbero, ed e' esattamente il modo in cui un
    percorso nuovo si allontana da quello in produzione (stessa scelta fatta in
    `gateways.PlaceOrderGateway` con `auto_bet._live_fill`).

    Nessuna credenziale: `SxBetProvider()` legge in pubblico (zero crediti
    the-odds-api). Il provider e' iniettabile — nei test e' un finto, quindi la
    suite resta OFFLINE.
    """

    name = "sxbet"
    source_id = "sxbet"

    def __init__(self, provider: Any = None, *, provider_factory: Optional[Callable[[], Any]] = None,
                 max_markets: Optional[int] = None, gateway_id: str = DEFAULT_GATEWAY_ID) -> None:
        self._provider = provider
        self._provider_factory = provider_factory
        self._max_markets = max_markets
        self.gateway_id = gateway_id
        self.fetches = 0                     # contatore (diagnostica/test)

    # -- provider ---------------------------------------------------------
    def provider(self) -> Any:
        if self._provider is not None:
            return self._provider
        if self._provider_factory is not None:
            return self._provider_factory()
        from execution_engine import SxBetProvider      # import pigro (pesante)
        self._provider = SxBetProvider()
        return self._provider

    # -- fetch ------------------------------------------------------------
    def fetch(self, *, gateway_id: str = DEFAULT_GATEWAY_ID,
              ctx: Optional[TraceContext] = None,
              obs: Optional[Observability] = None) -> list[dict]:
        """Quote 1X2 correnti dal libro SX, gia' in forma di contratto."""
        self.fetches += 1
        from sx_signals import _books_parallel, _discover, _kickoff_iso   # pigro

        provider = self.provider()
        try:
            events = _discover(provider) if self._max_markets is None \
                else _discover(provider, max_markets=self._max_markets)
        except Exception as exc:                 # rete/API/mappatura: sorgente giu'
            raise SourceUnavailable(f"discovery SX fallita: {exc}") from exc
        if not events:
            return []                            # nessun match in finestra: ok

        market_ids = [leg["market_hash"] for ev in events for leg in ev["legs"]]
        try:
            books = _books_parallel(provider, market_ids)
        except Exception as exc:
            raise SourceUnavailable(f"order book SX non leggibile: {exc}") from exc
        # `_books_parallel` e' fail-soft per singolo mercato (l'errore finisce
        # nel dict): se NESSUN book e' leggibile la sorgente e' giu' — non un
        # "mercato vuoto", che sarebbe indistinguibile da un feed senza partite.
        errored = [mid for mid in market_ids if (books.get(mid) or {}).get("error")]
        if errored and len(errored) == len(market_ids):
            primo = (books.get(errored[0]) or {}).get("error")
            raise SourceUnavailable(
                f"order book SX non leggibile: {len(errored)}/{len(market_ids)} "
                f"mercati in errore ({_short(str(primo))})")
        if errored:
            logger.warning("feed sxbet: %d/%d book non leggibili, gambe saltate",
                           len(errored), len(market_ids))

        observed = utcnow()
        rows: list[dict] = []
        skipped = 0
        for event in events:
            home, away = event["teams"]
            kickoff = _kickoff_iso(event["kickoff_ms"])
            prices: dict[str, float] = {}
            depths: dict[str, float] = {}
            for leg in event["legs"]:
                book = books.get(leg["market_hash"]) or {}
                if book.get("error"):
                    skipped += 1
                    continue
                # In TUTTI e 3 i mercati binari SX ("T1 vs Not T1", "Tie vs
                # Not tie", "T2 vs Not T2") l'esito scommesso e' outcomeOne,
                # che l'order book espone con la chiave INTERA 1 (la 2 e' il
                # lato complementare "Not X"). Stessa lettura di
                # `sx_signals.scan`: trovato provando un refresh reale, con la
                # chiave stringa dell'esito il feed risultava vuoto.
                side = book.get(1) or {}
                best = side.get("best") or {}
                price = best.get("price")
                if not price or price <= 1.0:
                    skipped += 1
                    continue
                prices[leg["esito"]] = float(price)
                depths[leg["esito"]] = float(side.get("depth") or 0.0)
            inv_sum = sum(1.0 / p for p in prices.values()) if prices else None
            for leg in event["legs"]:
                if leg["esito"] not in prices:
                    continue
                rows.append(_sx_row(
                    event=event, leg=leg, price=prices[leg["esito"]],
                    depth=depths.get(leg["esito"]), home=home, away=away,
                    kickoff=kickoff, observed=observed, gateway_id=gateway_id,
                    inv_sum=inv_sum, total_depth=sum(depths.values()),
                ))
        if skipped:
            logger.info("feed sxbet: %d gambe senza prezzo BACK utilizzabile "
                        "(book vuoto o in movimento)", skipped)
        return rows


def _sx_row(*, event: Mapping[str, Any], leg: Mapping[str, Any], price: float,
            depth: Optional[float], home: str, away: str, kickoff: str,
            observed: datetime, gateway_id: str, inv_sum: Optional[float],
            total_depth: float) -> dict:
    """Una gamba SX in forma di contratto (i campi extra restano nel quote)."""
    esito = str(leg["esito"])
    label = {"1": home, "X": "Draw", "2": away}.get(esito, esito)
    return {
        "schema_version": MARKET_SCHEMA_VERSION,
        "event_id": f"sx-{event['event_id']}",
        "market": "1X2",
        "selection": esito,
        "odds": price,
        "timestamp": observed.isoformat(),
        "source": f"sxbet",
        "gateway_id": gateway_id,
        "event_name": f"{home} - {away}",
        "league": event.get("league_label") or "",
        "home": home,
        "away": away,
        "kickoff": kickoff,
        "selection_label": label,
        "depth_usdc": depth,
        # Campi extra (ammessi dal contratto, non lo allargano): servono al
        # percorso d'ordine e alla diagnosi.
        "market_hash": leg["market_hash"],
        "sport_x_event_id": event["event_id"],
        "inv_sum": round(inv_sum, 4) if inv_sum is not None else None,
        "total_depth_usdc": round(total_depth, 2),
    }


class StaticSource:
    """Sorgente da righe gia' pronte (test, import manuali, `--file`).

    Serve a validare il feed senza rete e a provare il fallback: si comporta
    come una sorgente vera (compreso `fail` per simulare una fonte giu').
    """

    def __init__(self, rows: Iterable[Mapping[str, Any]] = (), *,
                 name: str = "static", source_id: Optional[str] = None,
                 fail: Optional[str] = None) -> None:
        self.name = name
        self.source_id = source_id or name
        self._rows = [dict(row) for row in rows]
        self._fail = fail
        self.fetches = 0

    def fetch(self, *, gateway_id: str = DEFAULT_GATEWAY_ID,
              ctx: Optional[TraceContext] = None,
              obs: Optional[Observability] = None) -> list[dict]:
        self.fetches += 1
        if self._fail:
            raise SourceUnavailable(self._fail)
        return [dict(row) for row in self._rows]


#: Registro delle sorgenti disponibili per nome (estendibile senza toccare il
#: feed). La PRIMARIA si sceglie con `DECISION_FEED_PRIMARY` e deve stare qui:
#: un nome sconosciuto NON ripiega su un'altra fonte (fail-closed).
SOURCE_REGISTRY: dict[str, Callable[..., QuoteSource]] = {
    "sxbet": SxBetSource,
}


def feed_enabled(value: Optional[str] = None) -> bool:
    """Feed attivo? Default SI': e' la sorgente primaria dei dati di mercato."""
    raw = os.getenv(ENABLED_ENV) if value is None else value
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def primary_name(value: Optional[str] = None) -> str:
    raw = (os.getenv(PRIMARY_ENV) if value is None else value) or ""
    return (raw.strip().lower() or "sxbet")


def default_state_path() -> Path:
    override = os.getenv(STATE_ENV)
    if override:
        return Path(override)
    try:
        from config import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:
        base = Path("data")
    return base / "decision" / "feed_state.json"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


def build_sources(primary: Optional[str] = None,
                  sources: Optional[Sequence[QuoteSource]] = None) -> list[QuoteSource]:
    """Sorgenti ordinate: la PRIMARIA per prima, il resto come ripiego."""
    if sources is not None:
        return list(sources)
    name = primary_name(primary)
    factory = SOURCE_REGISTRY.get(name)
    if factory is None:
        logger.error("feed: sorgente primaria '%s' sconosciuta (registro: %s) — "
                     "nessuna fonte configurata (fail-closed)",
                     name, ", ".join(sorted(SOURCE_REGISTRY)))
        return []
    return [factory()]


# ---------------------------------------------------------------------------
# Stato del feed (sopravvive ai redeploy: e' il contatore della validazione)
# ---------------------------------------------------------------------------

class FeedState(BaseModel):
    """Cosa sappiamo del feed: freschezza, validazione, tracciabilita'."""

    gateway_id: str = DEFAULT_GATEWAY_ID
    source: str = ""
    primary: str = "sxbet"
    sources: list[str] = Field(default_factory=list)
    schema_version: str = MARKET_SCHEMA_VERSION
    config_hash: str = ""
    request_id: str = ""
    trace_id: str = ""
    refreshed_at: Optional[datetime] = None
    ok: bool = False
    accepted: int = 0
    rejected: int = 0
    by_code: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0
    #: Refresh consecutivi conformi (un fallimento azzera) e quote validate in
    #: totale: insieme decidono se il feed e' "validato".
    consecutive_ok: int = 0
    validated_quotes_total: int = 0
    total_refreshes: int = 0
    total_failures: int = 0
    #: Soglie in vigore quando lo stato e' stato scritto (audit: con quali
    #: regole questo feed e' stato dichiarato validato).
    min_refreshes: int = DEFAULT_MIN_REFRESHES
    max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES
    reuse_seconds: float = DEFAULT_REUSE_SECONDS
    updated_at: Optional[datetime] = None
    #: Motivo dell'ultimo stato non-ok (diagnostica rapida).
    last_error: str = ""

    @property
    def validated(self) -> bool:
        """Feed validato: serie di refresh ok E almeno una quota vista."""
        return (self.consecutive_ok >= self.min_refreshes
                and self.validated_quotes_total >= 1)

    def identity(self) -> dict[str, Any]:
        """I cinque identificatori tracciati + cosa e' stato letto."""
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "gateway_id": self.gateway_id,
            "schema_version": self.schema_version,
            "config_hash": self.config_hash,
            "source": self.source,
            "refreshed_at": self.refreshed_at.isoformat() if self.refreshed_at else None,
            "ok": self.ok,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "consecutive_ok": self.consecutive_ok,
            "verified": self.validated,
        }


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

class FeedSnapshot(BaseModel):
    """L'esito di un refresh: quote conformi + identita' del giro.

    `ok` significa: **una sorgente ha risposto e ogni quota ha rispettato il
    contratto**. Un feed vuoto (nessun match in finestra) e' ok con zero quote —
    ma non contribuisce alla validazione (che richiede almeno una quota vista).
    """

    gateway_id: str = DEFAULT_GATEWAY_ID
    source: str = ""
    primary: str = "sxbet"
    sources_tried: list[str] = Field(default_factory=list)
    request_id: str = ""
    trace_id: str = ""
    schema_version: str = MARKET_SCHEMA_VERSION
    config_hash: str = ""
    refreshed_at: datetime = Field(default_factory=utcnow)
    forced: bool = True
    reused: bool = False
    #: False quando lo snapshot e' riusato DAL FILE DI STATO: il file e' un
    #: contatore di validazione, non un archivio di quote. Chi ha bisogno della
    #: quotatura deve forzare il refresh (`refresh(reuse_seconds=0)`).
    quotes_cached: bool = True
    ok: bool = False
    duration_ms: float = 0.0
    quotes: list[MarketQuote] = Field(default_factory=list)
    accepted: int = 0
    rejected: int = 0
    by_code: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    consecutive_ok: int = 0
    validated_quotes_total: int = 0
    min_refreshes: int = DEFAULT_MIN_REFRESHES
    max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES

    # -- letture ---------------------------------------------------------
    def age_seconds(self, now: Optional[datetime] = None) -> float:
        current = _aware(now) or utcnow()
        return (current - self.refreshed_at).total_seconds()

    @property
    def is_validated(self) -> bool:
        return (self.consecutive_ok >= self.min_refreshes
                and self.validated_quotes_total >= 1)

    def identity(self) -> dict[str, Any]:
        """Identita' tracciabile del giro (finisce nei piani e nei log)."""
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "gateway_id": self.gateway_id,
            "schema_version": self.schema_version,
            "config_hash": self.config_hash,
            "source": self.source,
            "refreshed_at": self.refreshed_at.isoformat(),
            "reused": self.reused,
            "quotes_cached": self.quotes_cached,
            "ok": self.ok,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "verified": self.is_validated,
        }

    def by_event(self) -> dict[str, list[MarketQuote]]:
        out: dict[str, list[MarketQuote]] = {}
        for quote in self.quotes:
            out.setdefault(quote.event_id, []).append(quote)
        return out

    def quote_for(self, event_id: str, market: str = "1X2",
                  selection: str = "") -> Optional[MarketQuote]:
        """Quota dello snapshot per (evento, mercato, esito), se presente."""
        for quote in self.quotes:
            if quote.event_id != event_id or quote.market != market:
                continue
            if not selection or quote.selection == selection:
                return quote
        return None

    def as_state(self) -> FeedState:
        return FeedState(
            gateway_id=self.gateway_id, source=self.source, primary=self.primary,
            sources=self.sources_tried, schema_version=self.schema_version,
            config_hash=self.config_hash, request_id=self.request_id,
            trace_id=self.trace_id, refreshed_at=self.refreshed_at, ok=self.ok,
            accepted=self.accepted, rejected=self.rejected, by_code=dict(self.by_code),
            errors=list(self.errors), duration_ms=self.duration_ms,
            consecutive_ok=self.consecutive_ok,
            validated_quotes_total=self.validated_quotes_total,
            min_refreshes=self.min_refreshes, max_age_minutes=self.max_age_minutes,
            updated_at=utcnow(),
            last_error=(self.errors[0] if (self.errors and not self.ok) else ""),
        )

    def describe(self) -> str:
        state = "ok" if self.ok else "FALLITO"
        detail = f"feed {state} da {self.source or self.primary} ({self.accepted} quote"
        if self.rejected:
            detail += f", {self.rejected} respinte"
        detail += f", {self.age_seconds():.0f}s fa)"
        if not self.is_validated:
            detail += (f" — NON validato ({self.consecutive_ok}/{self.min_refreshes} "
                       f"refresh ok consecutivi, {self.validated_quotes_total} quote validate)")
        return detail


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Gate: l'ingresso non e' opzionale
# ---------------------------------------------------------------------------

class FeedGateResult(BaseModel):
    """Verdetto del gate di mercato: si punta solo con un feed fresco e validato."""

    allowed: bool
    reason: ReasonCode
    detail: str = ""
    block: Optional[guards.SafetyBlock] = None
    identity: dict[str, Any] = Field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _feed_block(detail: str) -> guards.SafetyBlock:
    """Il blocco in forma di regola di sicurezza (stessa struttura delle altre).

    Precedenza 4: viene DOPO la catena delle autorita' (kill switch > stop-loss
    > pausa settlement) — un'autorita' umana non viene mai scavalcata da un
    problema tecnico di dati, e chi legge il blocco deve poterlo vedere.
    """
    return guards.SafetyBlock(
        reason=ReasonCode.FEED_UNAVAILABLE, stage="market", name="market_feed",
        label="gateway di mercato non affidabile", detail=detail, precedence=4,
        hint="verifica il feed (python -m decision feed --refresh --force): "
             "lo stop resta finche' il feed non e' validato",
    )


_FEED_BLOCK_HINTS = {
    ReasonCode.FEED_MISSING: "nessun refresh eseguito in questo giro: la catena "
                             "non valuta senza dati di mercato (fail-closed)",
    ReasonCode.FEED_UNAVAILABLE: "il refresh del gateway e' fallito: nessuna "
                                 "sorgente ha risposto con quote conformi",
    ReasonCode.FEED_STALE: "l'ultimo refresh e' piu' vecchio della soglia di "
                           "freschezza: la catena non usa quotature stantie",
    ReasonCode.FEED_NOT_VALIDATED: "il feed non ha ancora la serie di refresh "
                                   "conformi richiesta: le puntate restano ferme",
}


def verify_feed(snapshot: Optional[FeedSnapshot], *, now: Optional[datetime] = None,
                max_age_minutes: Optional[float] = None,
                min_refreshes: Optional[int] = None,
                required: bool = True) -> FeedGateResult:
    """Fail-closed: prima dello snapshot assente, poi i dati, poi la validazione.

    Ordine dei controlli (il primo che scatta e' il motivo riportato):

    1. nessuno snapshot / feed non richiesto  -> FEED_MISSING (o passa);
    2. refresh non ok (sorgenti giu' o quote non conformi) -> FEED_UNAVAILABLE;
    3. snapshot piu' vecchio di `max_age`     -> FEED_STALE;
    4. serie di refresh conformi incompleta   -> FEED_NOT_VALIDATED.
    """
    if not required:
        return FeedGateResult(allowed=True, reason=ReasonCode.OK,
                              detail="gate di mercato non richiesto (feed disattivato)")
    if snapshot is None:
        return _blocked(ReasonCode.FEED_MISSING, _FEED_BLOCK_HINTS[ReasonCode.FEED_MISSING],
                        identity={})
    identity = snapshot.identity()
    if not snapshot.ok:
        detail = snapshot.errors[0] if snapshot.errors else (
            f"refresh non conforme: {snapshot.rejected} quote respinte dal contratto")
        if snapshot.rejected and snapshot.errors:
            detail += f" ({snapshot.rejected} quote respinte)"
        return _blocked(ReasonCode.FEED_UNAVAILABLE, detail, identity=identity,
                        snapshot=snapshot)
    limit = float(max_age_minutes if max_age_minutes is not None
                  else snapshot.max_age_minutes)
    age_s = snapshot.age_seconds(now)
    if age_s > limit * 60.0:
        return _blocked(ReasonCode.FEED_STALE,
                        f"ultimo refresh {age_s/60.0:.1f} minuti fa (limite {limit:.0f} min)",
                        identity=identity, snapshot=snapshot)
    needed = int(min_refreshes if min_refreshes is not None else snapshot.min_refreshes)
    if snapshot.consecutive_ok < needed or snapshot.validated_quotes_total < 1:
        return _blocked(ReasonCode.FEED_NOT_VALIDATED,
                        f"{snapshot.consecutive_ok}/{needed} refresh conformi consecutivi, "
                        f"{snapshot.validated_quotes_total} quote validate "
                        f"(gateway {snapshot.gateway_id})",
                        identity=identity, snapshot=snapshot)
    return FeedGateResult(allowed=True, reason=ReasonCode.OK,
                          detail=(f"feed valido: {snapshot.accepted} quote da "
                                  f"{snapshot.source} ({snapshot.age_seconds(now):.0f}s fa)"),
                          identity=identity)


def _blocked(reason: ReasonCode, detail: str, *, identity: dict,
             snapshot: Optional[FeedSnapshot] = None) -> FeedGateResult:
    block = _feed_block(detail)
    block.reason = reason
    block.hint = _FEED_BLOCK_HINTS.get(reason, block.hint)
    return FeedGateResult(allowed=False, reason=reason, detail=detail, block=block,
                          identity=identity)


# ---------------------------------------------------------------------------
# Il gateway
# ---------------------------------------------------------------------------

class MarketFeed:
    """Gateway di mercato: refresh forzato, contratto all'ingresso, stato su disco."""

    def __init__(self, sources: Optional[Sequence[QuoteSource]] = None, *,
                 gateway_id: str = DEFAULT_GATEWAY_ID,
                 observability: Optional[Observability] = None,
                 state_path: Optional[str | Path] = None,
                 primary: Optional[str] = None,
                 reuse_seconds: Optional[float] = None,
                 max_age_minutes: Optional[float] = None,
                 min_refreshes: Optional[int] = None) -> None:
        self.sources: list[QuoteSource] = build_sources(primary, sources)
        self.gateway_id = gateway_id
        self.observability = observability or Observability(component="decision.feed")
        self.state_path = Path(state_path) if state_path else default_state_path()
        self.reuse_seconds = (float(reuse_seconds) if reuse_seconds is not None
                              else _env_float(REUSE_ENV, DEFAULT_REUSE_SECONDS))
        self.max_age_minutes = (float(max_age_minutes) if max_age_minutes is not None
                                else _env_float(MAX_AGE_ENV, DEFAULT_MAX_AGE_MINUTES))
        self.min_refreshes = (int(min_refreshes) if min_refreshes is not None
                              else _env_int(MIN_REFRESHES_ENV, DEFAULT_MIN_REFRESHES))
        self._last: Optional[FeedSnapshot] = None
        self._state: FeedState = self.load_state()

    # -- sorgenti ---------------------------------------------------------
    @property
    def has_sources(self) -> bool:
        return bool(self.sources)

    def source_names(self) -> list[str]:
        return [getattr(source, "name", "?") for source in self.sources]

    # -- stato ------------------------------------------------------------
    def load_state(self) -> FeedState:
        """Stato dal volume. File assente o corrotto -> stato NON validato."""
        if not self.state_path.exists():
            return FeedState(gateway_id=self.gateway_id, primary=primary_name(),
                             sources=self.source_names(),
                             min_refreshes=self.min_refreshes,
                             max_age_minutes=self.max_age_minutes,
                             reuse_seconds=self.reuse_seconds)
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("stato non e' un oggetto")
            return FeedState.model_validate(payload)
        except Exception as exc:
            logger.warning("feed: stato illeggibile in %s (%s) — il feed risulta "
                           "NON validato (fail-closed)", self.state_path, exc)
            return FeedState(gateway_id=self.gateway_id, primary=primary_name(),
                             sources=self.source_names(),
                             min_refreshes=self.min_refreshes,
                             max_age_minutes=self.max_age_minutes,
                             reuse_seconds=self.reuse_seconds)

    def state(self) -> FeedState:
        return self._state

    def _save_state(self, state: FeedState) -> None:
        """Scrittura atomica, fail-safe: lo stato non blocca mai un refresh."""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = state.model_dump(mode="json")
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                             dir=str(self.state_path.parent),
                                             prefix=".feed_state-",
                                             suffix=".tmp") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                tmp_name = handle.name
            os.replace(tmp_name, self.state_path)
        except Exception as exc:
            logger.warning("feed: stato non salvato (%s)", exc)

    def last_snapshot(self) -> Optional[FeedSnapshot]:
        return self._last

    # -- refresh ----------------------------------------------------------
    def refresh(self, *, request_id: str = "", ctx: Optional[TraceContext] = None,
                reuse_seconds: Optional[float] = None,
                force: bool = False) -> FeedSnapshot:
        """Forza un refresh del gateway e restituisce lo snapshot.

        `force=True` (o `reuse_seconds=0`) ignora la finestra di riuso. Non
        solleva MAI: un fallimento e' `snapshot.ok = False` con `errors`
        popolate — a bloccare e' `verify_feed`, con un motivo tracciabile.
        """
        obs = self.observability
        scope = ctx or obs.new_trace(request_id=request_id)
        window = 0.0 if force else float(self.reuse_seconds if reuse_seconds is None
                                         else reuse_seconds)
        # Finestra di riuso: rispetta l'exchange (il job gira ogni 60s).
        from_process = self._last is not None
        previous = self._last or self._snapshot_from_state()
        if previous is not None and window > 0 and previous.age_seconds() < window:
            reused = previous.model_copy(update={"reused": True, "forced": False,
                                                 "quotes_cached": from_process,
                                                 "request_id": scope.request_id,
                                                 "trace_id": scope.trace_id})
            if not from_process and reused.accepted:
                # Riuso dallo stato: il contatore dice che c'erano quote, ma il
                # file non le conserva (per non trasformarlo in un archivio).
                logger.info("feed: riuso dello stato per %s (%d quote non "
                            "disponibili in questo processo)",
                            self.gateway_id, reused.accepted)
            obs.event("feed.reused", ctx=scope, stage="market",
                      gateway_id=self.gateway_id, source=previous.source,
                      schema_version=previous.schema_version,
                      age_s=round(previous.age_seconds(), 1),
                      window_s=window, accepted=previous.accepted,
                      quotes_cached=from_process)
            self._last = reused
            return reused

        started = time.perf_counter()
        snapshot = FeedSnapshot(
            gateway_id=self.gateway_id, primary=primary_name(),
            sources_tried=self.source_names(), request_id=scope.request_id,
            trace_id=scope.trace_id, schema_version=MARKET_SCHEMA_VERSION,
            config_hash=obs.config_fingerprint, refreshed_at=utcnow(), forced=True,
            min_refreshes=self.min_refreshes, max_age_minutes=self.max_age_minutes,
        )
        with obs.span("feed.refresh", ctx=scope, stage="market", forced=True,
                      gateway_id=self.gateway_id, primary=snapshot.primary,
                      schema_version=MARKET_SCHEMA_VERSION,
                      config_hash=snapshot.config_hash) as span:
            source_name, batch = self._fetch_first()
            snapshot.source = source_name
            snapshot.quotes = batch.accepted
            snapshot.accepted = len(batch.accepted)
            snapshot.rejected = len(batch.rejected)
            snapshot.by_code = dict(batch.by_code)
            snapshot.errors = list(batch.errors)
            snapshot.ok = bool(source_name) and batch.responded and not batch.rejected
            snapshot.duration_ms = round((time.perf_counter() - started) * 1000, 3)
            previous_ok = self._state.consecutive_ok
            snapshot.consecutive_ok = previous_ok + 1 if snapshot.ok else 0
            snapshot.validated_quotes_total = (self._state.validated_quotes_total
                                               + snapshot.accepted)
            if snapshot.ok and not snapshot.accepted:
                # Feed vuoto: refresh valido, ma non prova nulla sul contratto.
                logger.info("feed: %s ha risposto senza quote (nessun match in "
                            "finestra) — refresh valido ma non validante", source_name)

        state = snapshot.as_state()
        state.consecutive_ok = snapshot.consecutive_ok
        state.validated_quotes_total = snapshot.validated_quotes_total
        state.total_refreshes = self._state.total_refreshes + 1
        state.total_failures = self._state.total_failures + (0 if snapshot.ok else 1)
        state.reuse_seconds = self.reuse_seconds
        self._state = state
        self._last = snapshot
        self._save_state(state)

        self._event(obs, span, snapshot)
        if not snapshot.ok:
            logger.error("feed: refresh FALLITO (gateway %s, sorgenti %s, request %s): %s",
                         snapshot.gateway_id, ", ".join(snapshot.sources_tried) or "-",
                         snapshot.request_id, snapshot.errors or "quote non conformi")
        return snapshot

    def refresh_or_raise(self, **kwargs) -> FeedSnapshot:
        """Come `refresh`, ma solleva `FeedUnavailable` se lo snapshot non e' ok.

        Comodo per chi vuole la semantica fail-fast a monte (job, script di
        validazione); la catena usa `refresh` + `verify_feed`.
        """
        snapshot = self.refresh(**kwargs)
        if not snapshot.ok:
            raise FeedUnavailable(snapshot.describe(), snapshot=snapshot)
        return snapshot

    def _fetch_first(self) -> tuple[str, _SourceBatch]:
        """Prima sorgente che risponde (la primaria per prima). Fail-soft."""
        tried: list[str] = []
        errors: list[str] = []
        for source in self.sources:
            name = getattr(source, "name", "?")
            tried.append(name)
            ctx = self.observability.new_trace()
            try:
                rows = source.fetch(gateway_id=self.gateway_id, ctx=ctx,
                                    obs=self.observability)
            except SourceUnavailable as exc:
                errors.append(f"{name}: {_short(str(exc))}")
                logger.warning("feed: sorgente '%s' non disponibile (%s)", name, exc)
                continue
            except Exception as exc:                 # sorgente rotta: come giu'
                errors.append(f"{name}: {type(exc).__name__}: {_short(str(exc))}")
                logger.warning("feed: sorgente '%s' fallita (%s)", name, exc)
                continue
            batch = self._validate(rows, source, ctx=ctx)
            batch.errors = batch.errors + errors      # fallimenti PRECEDENTI
            return name, batch
        return "", _SourceBatch(responded=False, errors=errors or
                                ["nessuna sorgente configurata"])

    def _validate(self, rows: Any, source: QuoteSource, *,
                  ctx: Optional[TraceContext]) -> "_SourceBatch":
        """Contratto all'ingresso: `validate_batch` (mai un'eccezione)."""
        batch = validate_batch(rows, gateway_id=self.gateway_id,
                               source=getattr(source, "source_id", None) or source.name,
                               obs=self.observability, ctx=ctx)
        return _SourceBatch(responded=True, accepted=batch.accepted,
                            rejected=batch.rejected, by_code=batch.by_code(),
                            errors=[])

    def _snapshot_from_state(self) -> Optional[FeedSnapshot]:
        """Snapshot ricostruito dallo stato su disco (per la finestra di riuso)."""
        state = self._state
        if state.refreshed_at is None or not state.request_id:
            return None
        if state.gateway_id != self.gateway_id:
            return None
        return FeedSnapshot(
            gateway_id=state.gateway_id, source=state.source, primary=state.primary,
            sources_tried=list(state.sources), request_id=state.request_id,
            trace_id=state.trace_id, schema_version=state.schema_version,
            config_hash=state.config_hash, refreshed_at=state.refreshed_at,
            ok=state.ok, accepted=state.accepted, rejected=state.rejected,
            by_code=dict(state.by_code), errors=list(state.errors),
            consecutive_ok=state.consecutive_ok,
            validated_quotes_total=state.validated_quotes_total,
            min_refreshes=state.min_refreshes, max_age_minutes=state.max_age_minutes,
        )

    def _event(self, obs: Observability, ctx: TraceContext,
               snapshot: FeedSnapshot) -> None:
        """Evento di refresh con TUTTI gli identificatori richiesti."""
        obs.event("feed.refreshed" if snapshot.ok else "feed.failed", ctx=ctx,
                  stage="market", outcome="ok" if snapshot.ok else "error",
                  gateway_id=snapshot.gateway_id, source=snapshot.source,
                  schema_version=snapshot.schema_version,
                  config_hash=snapshot.config_hash, request_id=snapshot.request_id,
                  trace_id=snapshot.trace_id, accepted=snapshot.accepted,
                  rejected=snapshot.rejected, by_code=snapshot.by_code,
                  sources_tried=snapshot.sources_tried,
                  duration_ms=snapshot.duration_ms,
                  consecutive_ok=snapshot.consecutive_ok,
                  validated=snapshot.is_validated,
                  errors=snapshot.errors)

    # -- gate -------------------------------------------------------------
    def gate(self, snapshot: Optional[FeedSnapshot] = None, *,
             now: Optional[datetime] = None, required: bool = True) -> FeedGateResult:
        """Verdetto sul feed (default: l'ultimo refresh di questa istanza)."""
        return verify_feed(snapshot or self._last, now=now, required=required,
                           max_age_minutes=self.max_age_minutes,
                           min_refreshes=self.min_refreshes)

    def is_validated(self) -> bool:
        return self._state.validated

    def describe(self) -> str:
        state = self._state
        lines = [f"📡 Feed di mercato (gateway {state.gateway_id}, primaria {state.primary})",
                 f"  sorgenti: {', '.join(state.sources) or 'nessuna'}",
                 f"  stato: {'VALIDATO' if state.validated else 'NON validato'} "
                 f"({state.consecutive_ok}/{state.min_refreshes} refresh conformi "
                 f"consecutivi, {state.validated_quotes_total} quote validate)"]
        if state.refreshed_at:
            freshness = (utcnow() - _aware(state.refreshed_at)).total_seconds()
            lines.append(f"  ultimo refresh: {state.refreshed_at.isoformat()} "
                         f"({freshness:.0f}s fa) — ok={state.ok}, "
                         f"{state.accepted} quote, {state.rejected} respinte")
        if state.last_error:
            lines.append(f"  ultimo errore: {state.last_error}")
        lines.append(f"  refresh totali {state.total_refreshes} "
                     f"(falliti {state.total_failures})")
        lines.append(f"  tracciabilita': request {state.request_id or '-'} | "
                     f"trace {state.trace_id or '-'} | schema {state.schema_version} | "
                     f"config {state.config_hash or '-'}")
        return "\n".join(lines)


class FeedUnavailable(RuntimeError):
    """Sollevata da `refresh_or_raise` quando il refresh non e' conforme."""

    def __init__(self, message: str, *, snapshot: Optional[FeedSnapshot] = None) -> None:
        super().__init__(message)
        self.snapshot = snapshot


class _SourceBatch:
    """Esito interno di una sorgente (righe validate o fallimento)."""

    __slots__ = ("responded", "accepted", "rejected", "by_code", "errors")

    def __init__(self, *, responded: bool, accepted: Optional[list] = None,
                 rejected: Optional[list] = None, by_code: Optional[dict] = None,
                 errors: Optional[list] = None) -> None:
        self.responded = responded
        self.accepted = list(accepted or [])
        self.rejected = list(rejected or [])
        self.by_code = dict(by_code or {})
        self.errors = list(errors or [])


def _short(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def feed_from_env(sources: Optional[Sequence[QuoteSource]] = None, *,
                  observability: Optional[Observability] = None,
                  gateway_id: str = DEFAULT_GATEWAY_ID,
                  state_path: Optional[str | Path] = None) -> Optional[MarketFeed]:
    """Feed configurato dall'ambiente, o `None` se disattivato.

    Chiamarlo non tocca la rete: il refresh e' esplicito (`refresh`). E' cosi'
    che i job possono costruire il gateway senza side effect inattesi.
    """
    if not feed_enabled():
        return None
    return MarketFeed(sources, observability=observability, gateway_id=gateway_id,
                      state_path=state_path)


__all__ = [
    "DEFAULT_GATEWAY_ID", "DEFAULT_MAX_AGE_MINUTES", "DEFAULT_MIN_REFRESHES",
    "DEFAULT_REUSE_SECONDS", "ENABLED_ENV", "FeedGateResult", "FeedSnapshot",
    "FeedState", "FeedUnavailable", "MarketFeed", "MAX_AGE_ENV", "MIN_REFRESHES_ENV",
    "PRIMARY_ENV", "QuoteSource", "REUSE_ENV", "SOURCE_REGISTRY", "STATE_ENV",
    "SourceUnavailable", "StaticSource", "SxBetSource", "build_sources",
    "feed_enabled", "feed_from_env", "primary_name", "verify_feed",
]
