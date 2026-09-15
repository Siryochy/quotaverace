"""decision/market.py — Il contratto di mercato: una quota valida, o niente.

Perche' esiste: finora una quota entrava nel sistema come **dict anonimo** —
chiavi diverse per provider diversi, tipi non controllati, timestamp a volte
senza fuso orario (il caso classico: il punteggio di una partita in corso usato
come finale, vedi `STALE_INPLAY_HOURS`). L'errore si scopriva a valle, quando
costava: un CLV calcolato su un timestamp ambiguo, un confronto fra quote in
unita' diverse, un gate di liquidita' che leggeva una stringa.

Qui la quota diventa un **tipo**: `MarketQuote`. Chi la produce (feed SX Bet,
the-odds-api, un import manuale) deve rispettare il contratto; chi la consuma
riceve campi garantiti, normalizzati e confrontabili.

I campi chiave (tutti obbligatori):

    schema_version  versione del contratto dichiarata DAL PRODUTTORE
    event_id        identificativo dell'evento (es. `sx-L19947936`)
    market          mercato canonico, normalizzato dagli alias del provider
    selection       esito canonico (`1`/`X`/`2` per 1X2, `over`/`under` per OU)
    odds            quota decimale europea, **minimo 0.1**
    timestamp       istante della rilevazione (obbligatoriamente UTC aware)
    source          chi ha prodotto la quota (provider)
    gateway_id      quale gateway l'ha ingerita (tracciabilita' all'ingresso)

Cosa valida il contratto e cosa NO (la separazione conta):

- **Struttura**: campi presenti, tipi, quote finite e >= 0.1, timestamp con
  fuso orario, coerenza mercato/selezione (`1X2` + `over` = rifiuto), versione
  dello schema supportata. Tutto QUI.
- **Strategia**: fascia quote 1.30-1.80, EV minimo, edge minimo, cap di stake.
  Tutto nel **Risk Engine** (`decision/risk_engine.py`), che legge le soglie da
  `value_filter`/`market_calib`. Il contratto non conosce la strategia: se la
  conoscesse, ogni cambio di soglia sarebbe un cambio di schema.

Come si valida all'ingresso:

    quote = parse_quote(row, gateway_id="sx-feed")     # strict: solleva
    batch = validate_batch(rows, gateway_id="sx-feed") # mai un'eccezione

`parse_quote` solleva `MarketQuoteError` (con `issues` machine-readable) e
**logga ogni errore**: una riga `logger.error` per diagnostica umana piu' un
evento JSON `market.quote_rejected` sul sink di osservabilita', con
`error_code`, campo colpevole e gateway. `validate_batch` fa la stessa cosa in
modo non bloccante (accettate + respinte + conteggi per codice) per gli import
a lotti: una riga rotta non deve fermare le altre.

Regole del modulo:

1. **Purezza**: solo pydantic + stdlib + il middleware. Nessun import di
   `tracker`/`auto_bet`/provider a livello di modulo.
2. **Fail-closed sulla struttura, fail-open sulla telemetria**: un dato ambiguo
   viene respinto; un sink di log rotto non ferma l'ingresso.
3. **Nessun payload intero nei log**: solo nome del campo e valore troncato
   (`TRUNCATE`, 80 char). Un feed non deve poter scrivere segreti nei log.
4. **Nessun indovinello**: gli alias di chiave/mercato/selezione sono tabelle
   esplicite; cio' che non e' in tabella e' un rifiuto, mai una scelta silenziosa.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from .middleware import Observability, TraceContext

logger = logging.getLogger("decision.market")

#: Versione del contratto. Cambia quando cambia la FORMA (campi obbligatori,
#: unita' di misura, semantica di un campo), non quando cambiano le soglie.
MARKET_SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS = (MARKET_SCHEMA_VERSION,)

#: Quota minima ammessa dal contratto (decimale europeo, 1.0 = pari).
MIN_ODDS = 0.1

#: Lunghezza massima del valore riportato nei log (mai il payload intero).
TRUNCATE = 80

#: Eventi di rifiuto emessi da `validate_batch` prima di passare al solo
#: riepilogo: un lotto di 500 righe rotte non deve scrivere 500 eventi.
DEFAULT_MAX_REJECTION_EVENTS = 20

#: Chiavi accettate al posto dei nomi canonici (i feed usano nomi propri).
#: Il nome canonico VINCE sempre: nessuna sovrascrittura silenziosa.
KEY_ALIASES = {
    "eventid": "event_id",
    "event": "event_id",
    "matchid": "event_id",
    "match_id": "event_id",
    "fixture_id": "event_id",
    "markettype": "market",
    "market_name": "market",
    "marketkey": "market",
    "outcome": "selection",
    "selectionid": "selection",
    "selection_id": "selection",
    "side": "selection",
    "odd": "odds",
    "price": "odds",
    "quote": "odds",
    "decimal_odds": "odds",
    "ts": "timestamp",
    "time": "timestamp",
    "updated_at": "timestamp",
    "observed_at": "timestamp",
    "gateway": "gateway_id",
    "gatewayid": "gateway_id",
    "feed": "gateway_id",
    "provider": "source",
    "bookmaker": "source",
    "version": "schema_version",
    "schemaversion": "schema_version",
}

#: Alias dei mercati -> mercato canonico (chiavi normalizzate).
MARKET_ALIASES = {
    "1x2": "1X2",
    "1X2": "1X2",
    "12": "1X2",
    "h2h": "1X2",
    "headtohead": "1X2",
    "match_odds": "1X2",
    "matchwinner": "1X2",
    "moneyline": "1X2",
    "money_line": "1X2",
    "winner": "1X2",
    "ou": "OU",
    "o/u": "OU",
    "overunder": "OU",
    "over_under": "OU",
    "total": "OU",
    "totals": "OU",
}
SUPPORTED_MARKETS = ("1X2", "OU")

#: Alias degli esiti -> esito canonico (chiavi normalizzate).
SELECTION_ALIASES = {
    "1": "1", "home": "1", "h": "1", "casa": "1", "team1": "1", "t1": "1",
    "x": "X", "draw": "X", "tie": "X", "pareggio": "X", "d": "X",
    "2": "2", "away": "2", "a": "2", "trasferta": "2", "team2": "2", "t2": "2",
    "over": "over", "o": "over", "piu": "over",
    "under": "under", "u": "under", "meno": "under",
}

#: Selezione ammessa per ogni mercato canonico (validazione INCROCIATA): e' il
#: controllo che a valle costa di piu' — un esito che non appartiene al mercato
#: e' il modo in cui un `over` finisce saldato su un 1X2 (vedi il caso
#: 'Blackburn Rovers' del 09/09, chiuso come Over 2.5).
MARKET_SELECTIONS = {"1X2": ("1", "X", "2"), "OU": ("over", "under")}


class QuoteErrorCode(str, Enum):
    """Perche' una quota e' stata respinta (machine-readable, aggregabile)."""

    MISSING_FIELD = "missing_field"
    EMPTY_FIELD = "empty_field"
    INVALID_TYPE = "invalid_type"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    UNKNOWN_MARKET = "unknown_market"
    UNKNOWN_SELECTION = "unknown_selection"
    SELECTION_MARKET_MISMATCH = "selection_market_mismatch"
    ODDS_BELOW_MIN = "odds_below_min"
    ODDS_NOT_FINITE = "odds_not_finite"
    TIMESTAMP_NAIVE = "timestamp_naive"


#: Tipi di errore di pydantic -> codice del contratto (i validator usano il
#: formato "<codice>: <dettaglio>", questo e' il ripiego per gli altri).
_PYDANTIC_CODES = {
    "missing": QuoteErrorCode.MISSING_FIELD,
    "greater_than_equal": QuoteErrorCode.ODDS_BELOW_MIN,
    "less_than": QuoteErrorCode.INVALID_TYPE,
    "string_type": QuoteErrorCode.INVALID_TYPE,
    "int_type": QuoteErrorCode.INVALID_TYPE,
    "float_type": QuoteErrorCode.INVALID_TYPE,
    "float_parsing": QuoteErrorCode.INVALID_TYPE,
    "datetime_type": QuoteErrorCode.INVALID_TYPE,
    "datetime_parsing": QuoteErrorCode.INVALID_TYPE,
    "string_too_short": QuoteErrorCode.EMPTY_FIELD,
}

_CODE_RE = re.compile("|".join(sorted((code.value for code in QuoteErrorCode),
                                      key=len, reverse=True)))
_ISO_Z = re.compile(r"[Zz]$")
_NOT_ALNUM = re.compile(r"[^a-z0-9]")


def _norm_key(value: Any) -> str:
    """Chiave di confronto degli alias: minuscola, senza separatori.

    Cosi' 'Match Odds', 'match_odds' e 'match-odds' sono la stessa cosa senza
    dover elencare ogni variante nella tabella.
    """
    return _NOT_ALNUM.sub("", str(value).strip().lower())


#: Tabelle di alias con la chiave NORMALIZZATA (una sola forma di confronto).
_MARKET_LOOKUP = {_norm_key(key): value for key, value in MARKET_ALIASES.items()}
_SELECTION_LOOKUP = {_norm_key(key): value for key, value in SELECTION_ALIASES.items()}


# ---------------------------------------------------------------------------
# Problem reporting
# ---------------------------------------------------------------------------

class QuoteIssue(BaseModel):
    """Un problema rilevato all'ingresso (campo + motivo, mai prosa libera)."""

    code: QuoteErrorCode
    field: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "field": self.field, "detail": self.detail}

    def __str__(self) -> str:
        where = f" [{self.field}]" if self.field else ""
        return f"{self.code.value}{where}: {self.detail}"


class MarketQuoteError(ValueError):
    """Contratto di mercato violato. Porta TUTTI i problemi, non solo il primo."""

    def __init__(self, issues: Iterable[QuoteIssue], *, gateway_id: str = "",
                 source: str = "", raw_keys: Optional[Iterable[str]] = None) -> None:
        self.issues = list(issues)
        self.gateway_id = gateway_id
        self.source = source
        self.raw_keys = list(raw_keys or [])
        codes = ", ".join(sorted({issue.code.value for issue in self.issues}))
        super().__init__(f"contratto di mercato non valido ({len(self.issues)} "
                         f"problemi: {codes})")

    def codes(self) -> list[str]:
        return [issue.code.value for issue in self.issues]


class QuoteRejection(BaseModel):
    """Rifiuto registrato da `validate_batch` (una riga per problema)."""

    index: int = 0
    code: QuoteErrorCode
    field: str = ""
    detail: str = ""
    event_id: str = ""
    source: str = ""
    gateway_id: str = ""
    raw_keys: list[str] = Field(default_factory=list)

    @classmethod
    def from_issue(cls, issue: QuoteIssue, *, index: int = 0, event_id: str = "",
                   source: str = "", gateway_id: str = "",
                   raw_keys: Optional[Iterable[str]] = None) -> "QuoteRejection":
        return cls(index=index, code=issue.code, field=issue.field,
                   detail=issue.detail, event_id=event_id, source=source,
                   gateway_id=gateway_id, raw_keys=list(raw_keys or []))

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Il contratto
# ---------------------------------------------------------------------------

class MarketQuote(BaseModel):
    """Una quota che rispetta il contratto: tipi garantiti, unita' dichiarate.

    `extra="allow"`: i campi sconosciuti del feed NON vengono persi (restano in
    `extra_fields`), cosi' si puo' ispezionare cosa manda davvero un provider
    senza allargare il contratto.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    # -- identita' del contratto -----------------------------------------
    schema_version: str = Field(..., description="versione dello schema dichiarata dal produttore")
    event_id: str = Field(..., description="identificativo dell'evento")
    market: str = Field(..., description="mercato canonico (1X2, OU)")
    selection: str = Field(..., description="esito canonico (1/X/2, over/under)")
    odds: float = Field(..., ge=MIN_ODDS, allow_inf_nan=False,
                        description=f"quota decimale europea (minimo {MIN_ODDS})")
    timestamp: datetime = Field(..., description="istante della rilevazione (UTC)")
    source: str = Field(..., description="provider che ha prodotto la quota")
    gateway_id: str = Field(..., description="gateway che ha ingerito la quota")

    # -- contesto (facoltativo: arricchisce, non identifica) -------------
    event_name: str = ""
    league: str = ""
    home: str = ""
    away: str = ""
    kickoff: Optional[datetime] = None
    selection_label: str = ""
    depth_usdc: Optional[float] = Field(None, ge=0.0,
                                        description="profondita' al floor della selezione")

    # -- validatori ------------------------------------------------------
    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise _fail(QuoteErrorCode.EMPTY_FIELD, "'schema_version' vuoto")
        if text not in SUPPORTED_SCHEMA_VERSIONS:
            raise _fail(QuoteErrorCode.UNSUPPORTED_SCHEMA,
                        f"versione '{text}' non supportata (supportate: "
                        f"{', '.join(SUPPORTED_SCHEMA_VERSIONS)})")
        return text

    @field_validator("event_id", "source", "gateway_id")
    @classmethod
    def _non_empty(cls, value: str, info: ValidationInfo) -> str:
        text = (value or "").strip()
        if not text:
            raise _fail(QuoteErrorCode.EMPTY_FIELD, f"'{info.field_name}' vuoto")
        return text

    @field_validator("market", mode="before")
    @classmethod
    def _normalize_market(cls, value: Any) -> str:
        key = _alias_key(value)
        if key is None:
            raise _fail(QuoteErrorCode.INVALID_TYPE, f"mercato non testuale: {_short(value)}")
        if not key:
            raise _fail(QuoteErrorCode.EMPTY_FIELD, "'market' vuoto")
        canonical = _MARKET_LOOKUP.get(_norm_key(key))
        if canonical is None:
            canonical = key if key.upper() in SUPPORTED_MARKETS else None
        if canonical is None:
            raise _fail(QuoteErrorCode.UNKNOWN_MARKET,
                        f"mercato '{_short(value)}' non riconosciuto (supportati: "
                        f"{', '.join(SUPPORTED_MARKETS)})")
        return canonical

    @field_validator("selection", mode="before")
    @classmethod
    def _normalize_selection(cls, value: Any, info: ValidationInfo) -> str:
        key = _alias_key(value)
        if key is None:
            raise _fail(QuoteErrorCode.INVALID_TYPE, f"selezione non testuale: {_short(value)}")
        if not key:
            raise _fail(QuoteErrorCode.EMPTY_FIELD, "'selection' vuota")
        canonical = _SELECTION_LOOKUP.get(_norm_key(key))
        if canonical is None:
            raise _fail(QuoteErrorCode.UNKNOWN_SELECTION,
                        f"selezione '{_short(value)}' non riconosciuta (1X2: 1/X/2, "
                        f"OU: over/under)")
        market = info.data.get("market")
        allowed = MARKET_SELECTIONS.get(str(market or ""))
        if allowed and canonical not in allowed:
            raise _fail(QuoteErrorCode.SELECTION_MARKET_MISMATCH,
                        f"selezione '{canonical}' non appartiene al mercato "
                        f"'{market}' (ammesse: {', '.join(allowed)})")
        return canonical

    @field_validator("odds", mode="before")
    @classmethod
    def _check_odds(cls, value: Any) -> float:
        number = _as_float(value)
        if number is None:
            raise _fail(QuoteErrorCode.INVALID_TYPE, f"quota non numerica: {_short(value)}")
        if not math.isfinite(number):
            raise _fail(QuoteErrorCode.ODDS_NOT_FINITE, f"quota non finita: {number}")
        if number < MIN_ODDS:
            raise _fail(QuoteErrorCode.ODDS_BELOW_MIN,
                        f"quota {number} sotto il minimo {MIN_ODDS}")
        return number

    @field_validator("timestamp", "kickoff", mode="before")
    @classmethod
    def _check_timestamp(cls, value: Any, info: ValidationInfo) -> Optional[datetime]:
        if value is None:
            return None                      # `timestamp` mancante lo dice pydantic
        moment = _as_datetime(value)
        if moment is None:
            raise _fail(QuoteErrorCode.INVALID_TYPE,
                        f"'{info.field_name}' non e' una data ISO-8601: {_short(value)}")
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise _fail(QuoteErrorCode.TIMESTAMP_NAIVE,
                        f"'{info.field_name}' senza fuso orario (serve UTC; usa "
                        f"assume_utc=True se la fonte e' UTC)")
        return moment.astimezone(timezone.utc)

    # -- derivati --------------------------------------------------------
    @property
    def extra_fields(self) -> dict[str, Any]:
        """Campi non previsti dal contratto, come arrivati dal feed."""
        return dict(self.model_extra or {})

    @property
    def quote_id(self) -> str:
        """Id della RILEVAZIONE (cambia quando la quota cambia nel tempo)."""
        return _digest("quote", self.event_id, self.market, self.selection,
                       self.gateway_id, self.source, self.timestamp.isoformat())

    @property
    def identity_key(self) -> str:
        """Chiave STABILE di (evento, mercato, esito): non dipende dal tempo."""
        return _digest("identity", self.event_id, self.market, self.selection)

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        """Eta' della rilevazione in secondi (negativa se nel futuro)."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return (current - self.timestamp).total_seconds()

    def to_signal_fields(self) -> dict[str, Any]:
        """La parte di `Signal` che il mercato puo' riempire (le probabilita' no).

        `outcome` compare SOLO per il mercato 1X2: il contratto non inventa un
        esito che il `Signal` non accetta (l'OU e' escluso dalle selezioni dal
        06/09, resta nel ledger come telemetria).
        """
        fields: dict[str, Any] = {
            "match_id": self.event_id,
            "league": self.league,
            "market": self.market,
            "selection_label": self.selection_label or self.selection,
            "kickoff": self.kickoff or self.timestamp,
            "price": self.odds,
            "price_source": self.source,
        }
        if self.market == "1X2":
            fields["outcome"] = self.selection
        return fields


class QuoteBatch(BaseModel):
    """Esito di una validazione a lotti: cosa e' entrato e cosa e' stato respinto."""

    gateway_id: str = ""
    source: str = ""
    accepted: list[MarketQuote] = Field(default_factory=list)
    rejected: list[QuoteRejection] = Field(default_factory=list)
    total: int = 0
    #: Rifiuti che NON hanno emesso un evento (protezione anti-flood).
    suppressed_events: int = 0

    @property
    def ok(self) -> bool:
        return not self.rejected

    @property
    def rejected_rows(self) -> int:
        """Righe respinte: una riga puo' violare piu' regole insieme."""
        return len({rejection.index for rejection in self.rejected})

    @property
    def issues(self) -> int:
        """Problemi totali (>= righe respinte)."""
        return len(self.rejected)

    def by_code(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejected:
            counts[rejection.code.value] = counts.get(rejection.code.value, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def as_dict(self) -> dict[str, Any]:
        return {
            "gateway_id": self.gateway_id,
            "source": self.source,
            "total": self.total,
            "accepted": len(self.accepted),
            "rejected_rows": self.rejected_rows,
            "issues": self.issues,
            "rejected": len(self.rejected),
            "by_code": self.by_code(),
            "suppressed_events": self.suppressed_events,
        }


# ---------------------------------------------------------------------------
# Helper di conversione e formato
# ---------------------------------------------------------------------------

def _fail(code: QuoteErrorCode, detail: str) -> ValueError:
    """Errore di validazione col formato '<codice>: <dettaglio>'."""
    return ValueError(f"{code.value}: {detail}")


def _short(value: Any) -> str:
    """Valore troncato per i log (mai un payload intero, mai un valore lungo)."""
    text = repr(value) if not isinstance(value, str) else value
    text = text.replace("\n", " ")
    return text if len(text) <= TRUNCATE else text[:TRUNCATE] + "…"


def _digest(*parts: Any) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _alias_key(value: Any) -> Optional[str]:
    """Chiave di alias di mercato/selezione (str ripulita, int -> str)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        return None
    return re.sub(r"\s+", "", value.strip())


def _as_float(value: Any) -> Optional[float]:
    """Numero da int/float/stringa numerica (virgola decimale ammessa)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", ".")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_datetime(value: Any) -> Optional[datetime]:
    """Data da datetime o stringa ISO-8601 (accetta il suffisso `Z`)."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = _ISO_Z.sub("+00:00", value.strip())
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _issue_from_error(error: Mapping[str, Any]) -> QuoteIssue:
    """`ValidationError` di pydantic -> `QuoteIssue` col codice del contratto."""
    location = [str(part) for part in error.get("loc", ()) if part != "__root__"]
    message = str(error.get("msg", ""))
    match = _CODE_RE.search(message)
    if match:
        code = QuoteErrorCode(match.group(0))
        detail = message[match.end():].lstrip(": ").strip()
    else:
        code = _PYDANTIC_CODES.get(str(error.get("type")), QuoteErrorCode.INVALID_TYPE)
        # Messaggio di pydantic in inglese: tradotto per i campi obbligatori,
        # cosi' un log di rifiuto si legge senza conoscere la libreria.
        detail = message
        if code is QuoteErrorCode.MISSING_FIELD and message == "Field required":
            detail = f"'{'.'.join(location)}' obbligatorio" if location else "campo obbligatorio"
    return QuoteIssue(code=code, field=".".join(location), detail=detail)


def _issues_from_validation_error(error: ValidationError) -> list[QuoteIssue]:
    return [_issue_from_error(item) for item in error.errors()]


def _safe_keys(row: Any) -> list[str]:
    """Chiavi della riga, senza fidarsi della riga.

    Una riga puo' essere un `Mapping` ostile (un `.get` che solleva): la
    contabilita' del lotto non deve mai dipendere dal comportamento del dato.
    """
    try:
        return sorted(str(key) for key in row.keys())
    except Exception:
        return []


def _safe_get(row: Any, *keys: str) -> str:
    """Primo valore non vuoto fra le chiavi date, con la stessa prudenza."""
    for key in keys:
        try:
            value = row.get(key)
        except Exception:
            continue
        if value not in (None, ""):
            return str(value)
    return ""


# ---------------------------------------------------------------------------
# Validazione ALL'INGRESSO
# ---------------------------------------------------------------------------

def prepare_payload(data: Any, *, gateway_id: Optional[str] = None,
                    source: Optional[str] = None,
                    schema_version: Optional[str] = None,
                    assume_utc: bool = False) -> tuple[dict[str, Any], list[QuoteIssue]]:
    """Normalizza un payload grezzo PRIMA della validazione del contratto.

    Fa solo cio' che e' deterministico: applica gli alias di chiave (il nome
    canonico vince), completa i default di feed (`gateway_id`/`source`/
    `schema_version` **solo se assenti**), e — con `assume_utc=True` — dichiara
    UTC i timestamp senza fuso orario. Non corregge valori: un rifiuto esplicito
    e' meglio di un dato sistemato a mano.
    """
    if not isinstance(data, Mapping):
        return {}, [QuoteIssue(code=QuoteErrorCode.INVALID_TYPE, field="",
                               detail=f"payload non e' un oggetto: {_short(data)}")]
    payload: dict[str, Any] = dict(data)
    for key in list(payload):
        canonical = KEY_ALIASES.get(re.sub(r"[^a-z0-9]", "", str(key).lower()))
        if canonical and canonical not in payload:
            payload[canonical] = payload[key]
    if gateway_id and not payload.get("gateway_id"):
        payload["gateway_id"] = gateway_id
    if source and not payload.get("source"):
        payload["source"] = source
    if schema_version and not payload.get("schema_version"):
        payload["schema_version"] = schema_version
    if assume_utc:
        for key in ("timestamp", "kickoff"):
            value = payload.get(key)
            if value is None:
                continue
            moment = _as_datetime(value)
            if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
                payload[key] = moment.replace(tzinfo=timezone.utc)
    return payload, []


def log_issues(issues: Iterable[QuoteIssue], *, gateway_id: str = "", source: str = "",
               event_id: str = "", obs: Optional[Observability] = None,
               ctx: Optional[TraceContext] = None, stage: str = "market") -> None:
    """Logga i problemi di ingresso: una riga di log e un evento, per ognuno."""
    for issue in issues:
        logger.error("contratto di mercato respinto [%s] campo=%s: %s "
                     "(gateway=%s, source=%s, event=%s)",
                     issue.code.value, issue.field or "-", issue.detail,
                     gateway_id or "-", source or "-", event_id or "-")
        if obs is not None:
            obs.event("market.quote_rejected", ctx=ctx, stage=stage, outcome="rejected",
                      error_code=issue.code.value, field=issue.field or "",
                      detail=issue.detail, gateway_id=gateway_id, source=source,
                      event_id=event_id)


def parse_quote(data: Any, *, gateway_id: Optional[str] = None,
                source: Optional[str] = None,
                schema_version: Optional[str] = None,
                assume_utc: bool = False,
                obs: Optional[Observability] = None,
                ctx: Optional[TraceContext] = None,
                log_accepted: bool = False) -> MarketQuote:
    """Valida UNA quota all'ingresso. Solleva `MarketQuoteError` se non conforme.

    Ogni problema viene loggato prima di sollevare: chi cattura l'eccezione ha
    gia' la diagnostica nei log (e nel sink di osservabilita', se fornito).
    """
    payload, issues = prepare_payload(data, gateway_id=gateway_id, source=source,
                                      schema_version=schema_version, assume_utc=assume_utc)
    raw_keys = _safe_keys(data) if isinstance(data, Mapping) else []
    event_id = str(payload.get("event_id") or "")
    if issues:
        log_issues(issues, gateway_id=gateway_id or "", source=source or "",
                   event_id=event_id, obs=obs, ctx=ctx)
        raise MarketQuoteError(issues, gateway_id=gateway_id or "", source=source or "",
                               raw_keys=raw_keys)
    try:
        quote = MarketQuote(**payload)
    except ValidationError as error:
        issues = _issues_from_validation_error(error)
        log_issues(issues, gateway_id=payload.get("gateway_id") or gateway_id or "",
                   source=payload.get("source") or source or "", event_id=event_id,
                   obs=obs, ctx=ctx)
        raise MarketQuoteError(issues, gateway_id=payload.get("gateway_id") or gateway_id or "",
                               source=payload.get("source") or source or "",
                               raw_keys=raw_keys) from error
    if log_accepted:
        logger.debug("contratto di mercato valido [%s] %s %s @ %s (gateway=%s)",
                     quote.quote_id, quote.event_id, quote.selection, quote.odds,
                     quote.gateway_id)
        if obs is not None:
            obs.event("market.quote_accepted", ctx=ctx, stage="market", outcome="ok",
                      quote_id=quote.quote_id, event_id=quote.event_id,
                      market=quote.market, selection=quote.selection, odds=quote.odds,
                      source=quote.source, gateway_id=quote.gateway_id)
    return quote


def validate_batch(rows: Optional[Iterable[Any]], *, gateway_id: str = "",
                   source: str = "", assume_utc: bool = False,
                   obs: Optional[Observability] = None,
                   ctx: Optional[TraceContext] = None,
                   max_events: int = DEFAULT_MAX_REJECTION_EVENTS) -> QuoteBatch:
    """Valida un lotto **senza mai sollevare**: accettate + respinte + conteggi.

    E' il punto d'ingresso per gli import: una riga rotta non ferma le altre e
    il rifiuto e' contabilizzato (non silenzioso). Gli eventi di rifiuto sono
    limitati a `max_events` per non inondare il sink; le righe di log restano
    per ogni problema e i rifiuti oltre il limite finiscono in
    `suppressed_events`.
    """
    batch = QuoteBatch(gateway_id=gateway_id, source=source)
    try:
        items = list(rows or [])
    except TypeError:
        items = []
        batch.rejected.append(QuoteRejection(
            code=QuoteErrorCode.INVALID_TYPE, field="",
            detail=f"lotto non iterabile: {_short(rows)}", gateway_id=gateway_id,
            source=source))
    batch.total = len(items)
    emitted = 0
    for index, row in enumerate(items):
        probe = obs if emitted < max_events else None
        try:
            quote = parse_quote(row, gateway_id=gateway_id, source=source,
                                assume_utc=assume_utc, obs=probe, ctx=ctx)
        except MarketQuoteError as error:
            raw_keys = _safe_keys(row) if isinstance(row, Mapping) else []
            event_id = _safe_get(row, "event_id", "match_id") if isinstance(row, Mapping) else ""
            for issue in error.issues:
                batch.rejected.append(QuoteRejection.from_issue(
                    issue, index=index, event_id=event_id,
                    source=str(error.source or source), gateway_id=str(error.gateway_id or gateway_id),
                    raw_keys=raw_keys))
            if probe is not None:
                emitted += len(error.issues)
            else:
                batch.suppressed_events += len(error.issues)
            continue
        except Exception as exc:                 # qualunque sorpresa: mai un'eccezione
            batch.rejected.append(QuoteRejection(
                code=QuoteErrorCode.INVALID_TYPE, index=index,
                detail=f"{type(exc).__name__}: {_short(exc)}",
                gateway_id=gateway_id, source=source))
            continue
        batch.accepted.append(quote)
    _log_batch(batch, obs=obs, ctx=ctx)
    return batch


def _log_batch(batch: QuoteBatch, *, obs: Optional[Observability] = None,
               ctx: Optional[TraceContext] = None) -> None:
    """Riepilogo del lotto: una riga di log + un evento (fail-safe)."""
    summary = batch.as_dict()
    try:
        if batch.rejected:
            logger.warning("contratto di mercato: %d/%d quote respinte, %d problemi "
                           "(gateway=%s, source=%s, motivi=%s)",
                           batch.rejected_rows, batch.total, batch.issues,
                           batch.gateway_id or "-", batch.source or "-",
                           batch.by_code())
        else:
            logger.info("contratto di mercato: %d/%d quote valide (gateway=%s)",
                        len(batch.accepted), batch.total, batch.gateway_id or "-")
        if obs is not None:
            obs.event("market.batch_validated", ctx=ctx, stage="market",
                      outcome="ok" if batch.ok else "rejected", **summary)
    except Exception as exc:                     # la telemetria non blocca l'ingresso
        logger.warning("contratto di mercato: riepilogo non registrato (%s)", exc)


__all__ = [
    "DEFAULT_MAX_REJECTION_EVENTS", "KEY_ALIASES", "MARKET_ALIASES", "MARKET_SCHEMA_VERSION",
    "MARKET_SELECTIONS", "MIN_ODDS", "MarketQuote", "MarketQuoteError", "QuoteBatch",
    "QuoteErrorCode", "QuoteIssue", "QuoteRejection", "SELECTION_ALIASES",
    "SUPPORTED_MARKETS", "SUPPORTED_SCHEMA_VERSIONS", "TRUNCATE",
    "log_issues", "parse_quote", "prepare_payload", "validate_batch",
]
