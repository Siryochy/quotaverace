"""decision — Catena di decisione: Signal -> Risk -> Stake, con kill switch e revisione.

Perche' esiste: descrivere in un solo posto *come nasce una puntata*, con
contratti espliciti invece di dizionari sparsi. La stratificazione riprende i
motori che il progetto ha gia' (modelli, filtri, staking), ma:

- separa **probabilita' calibrata** (modello) da **decisione di rischio** da
  **quanto puntare**: il Signal non contiene lo stake, cosi' "probabilita'
  calibrata" non puo' essere confusa con un invito a scommettere;
- mette il **Risk Engine** in mezzo, con tre uscite (`approve` / `review` /
  `reject`) e motivi machine-readable (`ReasonCode`) invece di prosa;
- mette il **kill switch** come autorita' superiore, con precedenza dichiarata
  (manuale > stop-loss giornaliero > pausa settlement);
- produce **un record unico** (input + decisione + stake + esito) per il
  feedback engine.

Uso tipico (offline, senza DB ne' rete):

    from decision import decide, RiskLimits, KillSwitchStatus, Signal

    record = decide(signal,
                    kills=KillSwitchStatus(mode="live", provider_ready=True),
                    limits=RiskLimits.from_env(),
                    bankroll=38.0)

    print(record.risk.verdict, record.risk.reason.value,
          record.stake.stake if record.stake else None)

Il record si scrive sul ledger `decisions` (tracker.py) con `persist(record)`:
le scritture sono **fail-safe** (mai un'eccezione: la telemetria non deve
fermare una puntata), le letture read-only (`snapshot`/`format_report`).

Dati di mercato: `market.py` definisce il **contratto** di una quota (validato
all'ingresso), `feeds.py` e' il **gateway** che le porta dentro — sorgente
PRIMARIA SX Bet, refresh forzato prima del Risk Engine e gate fail-closed
(`verify_feed`): senza quotatura fresca, conforme e **validata** la catena non
produce stake. Lo stesso gate e' applicato al percorso d'ordine di `auto_bet`.

⚠️ Questo pacchetto NON e' ancora collegato ad `auto_bet`: la produzione
continua a usare il percorso esistente finche' la parita' non e' dimostrata dai
test (`test_decision_pipeline.py` confronta i gate con `value_filter.is_sane`).
"""

from __future__ import annotations

from . import (
    commands, dispatcher, engine, feedback, feeds, gateways, guards, kill_switch,
    market, middleware, review_telegram, risk_engine, shadow, stake_engine,
    validation,
)
from .commands import Command, CommandKind, CommandPlan
from .dispatcher import DispatchReport, Dispatcher
from .engine import build_plan, emit_many, plan_for_resolved
from .feeds import (
    DEFAULT_GATEWAY_ID, SOURCE_REGISTRY, FeedGateResult, FeedSnapshot, FeedState,
    FeedUnavailable, MarketFeed, QuoteSource, SourceUnavailable, StaticSource,
    SxBetSource, build_sources, feed_enabled, feed_from_env, verify_feed,
)
from .feedback import attach_order, persist, persist_many, snapshot
from .gateways import (
    LedgerGateway, NotifyGateway, PlaceOrderGateway, ShadowGateway,
    ValidatingLedgerGateway,
)
from .guards import (
    STAGE_BETTING, STAGE_SETTLEMENT, SAFETY_CHAIN, SafetyBlock, SafetyBlockError,
    require_clear,
)
from .limits import RiskLimits, limits_from_env
from .market import (
    MARKET_SCHEMA_VERSION, MARKET_SELECTIONS, MIN_ODDS, SUPPORTED_MARKETS,
    SUPPORTED_SCHEMA_VERSIONS, MarketQuote, MarketQuoteError, QuoteBatch,
    QuoteErrorCode, QuoteIssue, QuoteRejection, log_issues, parse_quote,
    prepare_payload, validate_batch,
)
from .middleware import Observability, TraceContext, sink_from_env
from .models import (
    DECISION_STATUS_PENDING, DECISION_STATUS_REJECTED, DECISION_STATUS_VALIDATED,
    DECISION_STATUSES, DataQuality, DecisionRecord, DecisionStatus,
    KILL_SWITCH_PRECEDENCE, KillSwitchStatus, ReasonCode, RiskDecision, Signal,
    StakeDecision, make_signal_id, risk_approve, risk_reject, risk_review, utcnow,
)
from .validation import ValidationOutcome, validate_row
from .pipeline import decide, decide_many, pending, resolve_review, summary
from .review_queue import (
    ReviewQueue, STATUS_APPROVED, STATUS_EXPIRED, STATUS_PENDING, STATUS_REJECTED,
    default_path, format_report,
)
from .shadow import shadow_persist_enabled, SHADOW_PERSIST_ENV
from .review_telegram import (
    CallbackStore, HttpTelegramClient, ReviewCallback, ReviewOutcome,
    answer_callback, build_prompt, callback_id, callback_token, handle_callback,
    parse_callback, pending_prompts, send_prompts,
)

__all__ = [
    # contratti
    "Signal", "DataQuality", "RiskDecision", "StakeDecision", "DecisionRecord",
    "KillSwitchStatus", "ReasonCode", "KILL_SWITCH_PRECEDENCE", "make_signal_id",
    "risk_approve", "risk_review", "risk_reject", "utcnow",
    # limiti (fonte unica)
    "RiskLimits", "limits_from_env",
    # motori
    "kill_switch", "risk_engine", "stake_engine",
    # orchestrazione
    "decide", "decide_many", "resolve_review", "pending", "summary",
    # persistenza per il feedback engine (import pigro di tracker)
    "feedback", "persist", "persist_many", "attach_order", "snapshot",
    # Command pattern: il motore emette comandi, i gateway eseguono
    "commands", "engine", "gateways", "dispatcher", "shadow", "middleware",
    "guards", "Command", "CommandKind", "CommandPlan", "build_plan",
    "emit_many", "Dispatcher", "DispatchReport", "LedgerGateway",
    "NotifyGateway", "PlaceOrderGateway", "ShadowGateway",
    "ValidatingLedgerGateway", "validation", "ValidationOutcome", "validate_row",
    "DECISION_STATUSES", "DECISION_STATUS_PENDING", "DECISION_STATUS_VALIDATED",
    "DECISION_STATUS_REJECTED", "DecisionStatus",
    # fail-fast sui blocchi di sicurezza
    "SAFETY_CHAIN", "STAGE_BETTING", "STAGE_SETTLEMENT", "SafetyBlock",
    "SafetyBlockError", "require_clear",
    # osservabilita'
    "Observability", "TraceContext", "sink_from_env",
    # gateway di mercato (SX primaria, refresh forzato, gate fail-closed)
    "feeds", "MarketFeed", "FeedSnapshot", "FeedState", "FeedGateResult",
    "QuoteSource", "SxBetSource", "StaticSource", "SourceUnavailable", "FeedUnavailable",
    "verify_feed", "feed_from_env", "feed_enabled", "build_sources",
    "SOURCE_REGISTRY", "DEFAULT_GATEWAY_ID",
    # contratto di mercato (validato all'ingresso)
    "market", "MarketQuote", "MarketQuoteError", "QuoteIssue", "QuoteRejection",
    "QuoteBatch", "QuoteErrorCode", "parse_quote", "validate_batch",
    "prepare_payload", "log_issues", "MARKET_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS", "MIN_ODDS", "MARKET_SELECTIONS",
    "SUPPORTED_MARKETS",
    # coda revisioni
    "ReviewQueue", "default_path", "format_report", "STATUS_PENDING",
    "STATUS_APPROVED", "STATUS_REJECTED", "STATUS_EXPIRED",
    # revisioni su Telegram (callback idempotenti)
    "review_telegram", "build_prompt", "pending_prompts", "send_prompts",
    "handle_callback", "answer_callback", "parse_callback", "callback_id",
    "callback_token", "CallbackStore", "ReviewCallback", "ReviewOutcome",
    "HttpTelegramClient", "plan_for_resolved",
    # Shadow Validation (stato della riga persistita, opt-in nella shadow mode)
    "shadow_persist_enabled", "SHADOW_PERSIST_ENV",
]
