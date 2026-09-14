"""decision/models.py — Contratti della catena di decisione.

Tre stadi separati, ognuno con un contratto proprio:

    Signal  ──▶  RiskDecision  ──▶  StakeDecision  ──▶  DecisionRecord
    (cosa e'          (approve/          (quanto, SOLO      (feedback:
     successo)         review/reject)     se approvato)      input+esito)

Regole di ferro (verificate dai tripwire in `test_decision_pipeline.py`):

1. **Il Signal NON contiene lo stake.** Descrive un'opportunita' con la
   probabilita' calibrata: "probabilita' calibrata" non e' un invito a
   scommettere, e chi decide il rischio non e' chi calcola il modello.
2. **Il Risk Engine puo' solo stringere.** `RiskDecision.tightened` puo'
   ridurre un cap, mai alzarlo: nessun motore a valle puo' allargare cio' che
   un motore a monte ha ristretto.
3. **Motivi machine-readable.** Ogni verdetto porta un `ReasonCode`, mai solo
   prosa: il feedback engine aggrega i motivi, non interpreta frasi.
4. **Nessun effetto collaterale.** Questi modelli non toccano DB, rete o
   provider: sono dati.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

Market = Literal["1X2"]
Outcome = Literal["1", "X", "2"]
Tier = Literal["value", "strong_value", "moderate"]
Mode = Literal["off", "sim", "live"]
Verdict = Literal["approve", "review", "reject"]


def utcnow() -> datetime:
    """Ora UTC timezone-aware (mai naive: i kickoff sono UTC)."""
    return datetime.now(timezone.utc)


class ReasonCode(str, Enum):
    """Motivo di una decisione, aggregabile e confrontabile."""

    OK = "ok"
    # --- kill switch (in ordine di precedenza) ---
    KILL_SWITCH_OFF = "kill_switch_off"          # /autobet off: stop totale
    DAILY_STOP_LOSS = "daily_stop_loss"          # -5% in 24h: puntate bloccate
    SETTLEMENT_PAUSED = "settlement_paused"      # non blocca la bet, blocca il referto
    # --- Risk Engine ---
    LEAGUE_NOT_ALLOWED = "league_not_allowed"
    ODDS_TOO_LOW = "odds_too_low"
    ODDS_TOO_HIGH = "odds_too_high"
    NOT_FAVOURITE = "not_favourite"
    EV_TOO_LOW = "ev_too_low"
    EV_ANOMALOUS = "ev_anomalous"
    EDGE_TOO_LOW = "edge_too_low"
    DATA_QUALITY_LOW = "data_quality_low"    # modello cieco (senza ratings) -> umano
    CONFIDENCE_LOW = "confidence_low"
    LIQUIDITY_LOW = "liquidity_low"
    MARKET_INCOHERENT = "market_incoherent"
    ALREADY_EXPOSED = "already_exposed"
    # --- Stake Engine ---
    STAKE_BELOW_FLOOR = "stake_below_floor"      # cap severo: fail-closed
    RISK_TIGHTENED = "risk_tightened"
    # --- revisione umana ---
    REVIEW_PENDING = "review_pending"
    REVIEW_APPROVED = "review_approved"
    REVIEW_REJECTED = "review_rejected"
    REVIEW_EXPIRED = "review_expired"


class DataQuality(BaseModel):
    """Qualita' dei dati che sostengono il Signal (telemetria, non prosa).

    Serve al Risk Engine per distinguere "modello che non batte il mercato" da
    "modello cieco": senza ratings reali `expected_goals` usa il profilo neutro
    di lega e una probabilita' bassa e' ignoranza, non valore.
    """

    ratings_home_n: int = 0          # partite osservate per la squadra di casa
    ratings_away_n: int = 0
    model_coverage: float = Field(0.0, ge=0.0, le=1.0)   # 0.0 = entrambe senza rating
    calibrated: bool = False         # calibrazione isotonica attiva
    calibration_samples: int = 0
    market_coherent: bool = True     # inv_sum nella fascia 0.98-1.08
    inv_sum: Optional[float] = None
    depth_usdc: Optional[float] = None       # profondita' al floor della leg giocata
    snapshot_age_minutes: Optional[float] = None
    flags: list[str] = Field(default_factory=list)

    @property
    def is_blind(self) -> bool:
        """True se nessuna delle due squadre ha rating (modello neutro)."""
        return self.model_coverage <= 0.0


class Signal(BaseModel):
    """Un'opportunita' rilevata dal Signal Engine. NON contiene lo stake."""

    signal_id: str = ""
    match_id: str
    league: str = ""
    market: Market = "1X2"
    outcome: Outcome
    selection_label: str = ""
    kickoff: datetime
    price: float = Field(..., gt=1.0, description="quota giocabile")
    price_source: str = ""
    market_prob: float = Field(..., gt=0.0, lt=1.0)
    model_prob: float = Field(..., ge=0.0, le=1.0)
    blended_prob: float = Field(..., gt=0.0, lt=1.0)
    edge: Optional[float] = None       # blended - market (calcolato se assente)
    ev: Optional[float] = None         # blended*price - 1 (calcolato se assente)
    tier: Tier = "moderate"
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    data_quality: DataQuality = Field(default_factory=DataQuality)
    warnings: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _fill_derived(self):
        if self.edge is None:
            self.edge = self.blended_prob - self.market_prob
        if self.ev is None:
            self.ev = self.blended_prob * self.price - 1.0
        if not self.signal_id:
            self.signal_id = make_signal_id(self.match_id, self.market, self.outcome)
        if self.data_quality.is_blind and "ratings_assenti" not in self.warnings:
            self.warnings.append("ratings_assenti")
        return self

    @property
    def is_favourite(self) -> bool:
        return self.market_prob >= 0.50


def make_signal_id(match_id: str, market: str, outcome: str) -> str:
    """Id STABILE di un segnale (stesso match/mercato/esito -> stesso id)."""
    raw = f"{match_id}|{market}|{outcome}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

# Precedenza decisa dal proprietario il 14/09/2026:
#   kill switch manuale > stop-loss giornaliero > pausa settlement.
# Il primo blocco in quest'ordine e' il motivo riportato (e cio' che va
# rimosso per ripartire); gli altri restano visibili come avvisi.
KILL_SWITCH_PRECEDENCE = ("manual", "daily_stop", "settlement_pause")

_BLOCK_REASONS = {
    "manual": ReasonCode.KILL_SWITCH_OFF,
    "daily_stop": ReasonCode.DAILY_STOP_LOSS,
}


class KillSwitchStatus(BaseModel):
    """Istantanea dei blocchi attivi (letta da file/env, iniettabile nei test)."""

    mode: Mode = "off"
    env_mode: str = ""
    override: Optional[str] = None
    provider_ready: bool = False
    daily_stop_active: bool = False
    daily_stop_detail: str = ""
    settlement_paused: bool = False

    def betting_blocks(self) -> list[str]:
        """Blocchi che impediscono una puntata, in ordine di precedenza."""
        blocks: list[str] = []
        if self.mode == "off":
            blocks.append("manual")
        if self.daily_stop_active:
            blocks.append("daily_stop")
        return [name for name in KILL_SWITCH_PRECEDENCE if name in blocks]

    def advisories(self) -> list[str]:
        """Condizioni non bloccanti per la bet ma da mostrare (in precedenza)."""
        out: list[str] = []
        if self.settlement_paused:
            out.append("settlement_pause")
        return out

    def first_block(self) -> Optional[ReasonCode]:
        """Motivo del primo blocco in ordine di precedenza (None = libero)."""
        blocks = self.betting_blocks()
        return _BLOCK_REASONS.get(blocks[0]) if blocks else None

    @property
    def betting_allowed(self) -> bool:
        return not self.betting_blocks()


# ---------------------------------------------------------------------------
# Risk Engine
# ---------------------------------------------------------------------------

class RiskDecision(BaseModel):
    """Verdetto del Risk Engine: approve / review / reject + motivi."""

    verdict: Verdict
    reason: ReasonCode
    detail: str = ""
    reasons: list[ReasonCode] = Field(default_factory=list)
    advisories: list[ReasonCode] = Field(default_factory=list)
    checked: list[str] = Field(default_factory=list)     # ordine dei controlli
    tightened: dict[str, float] = Field(default_factory=dict)  # cap ridotti

    @property
    def allows_stake(self) -> bool:
        """Solo `approve` autorizza lo Stake Engine."""
        return self.verdict == "approve"


def risk_approve(*, checked: list[str], tightened: Optional[dict] = None,
                 advisories: Optional[list] = None) -> RiskDecision:
    return RiskDecision(verdict="approve", reason=ReasonCode.OK, detail="gate superati",
                        checked=list(checked), tightened=dict(tightened or {}),
                        advisories=list(advisories or []))


def risk_review(reason: ReasonCode, detail: str, *, checked: list[str],
                tightened: Optional[dict] = None) -> RiskDecision:
    return RiskDecision(verdict="review", reason=reason, detail=detail,
                        reasons=[reason], checked=list(checked),
                        tightened=dict(tightened or {}))


def risk_reject(reason: ReasonCode, detail: str, *,
                checked: list[str]) -> RiskDecision:
    return RiskDecision(verdict="reject", reason=reason, detail=detail,
                        reasons=[reason], checked=list(checked))


# ---------------------------------------------------------------------------
# Stake Engine
# ---------------------------------------------------------------------------

class StakeDecision(BaseModel):
    """Quanto puntare. Esiste SOLO a valle di un verdetto che autorizza."""

    bankroll: float
    stake: float = 0.0
    kelly_fraction: float = 0.0
    kelly_stake: float = 0.0          # stake prima dei cap (per l'audit)
    cap_pct: Optional[float] = None   # cap che ha morso (percentuale bankroll)
    cap_source: str = ""              # "tier" | "league" | "risk" | "none"
    floor: float = 0.0
    executable: bool = False
    reason: ReasonCode = ReasonCode.OK
    detail: str = ""
    mode: Mode = "sim"

    @property
    def skipped(self) -> bool:
        return not self.executable


# ---------------------------------------------------------------------------
# Feedback Engine (record unico)
# ---------------------------------------------------------------------------

class DecisionRecord(BaseModel):
    """La riga che lega input, previsione, decisione, stake e (dopo) esito."""

    record_id: str = ""
    signal: Signal
    kill_switch: KillSwitchStatus = Field(default_factory=KillSwitchStatus)
    risk: RiskDecision
    stake: Optional[StakeDecision] = None
    mode: Mode = "sim"
    provider: str = ""
    approved_by: Optional[str] = None      # revisione umana (Telegram)
    review_note: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    order: Optional[dict[str, Any]] = None       # riempito dall'esecuzione
    settlement: Optional[dict[str, Any]] = None  # riempito dal referto

    @model_validator(mode="after")
    def _fill_id(self):
        if not self.record_id:
            stamp = self.created_at.strftime("%Y%m%dT%H%M%S")
            self.record_id = f"{self.signal.signal_id}-{stamp}"
        return self

    def as_row(self) -> dict[str, Any]:
        """Riga piatta per il ledger del feedback engine."""
        row = {
            "record_id": self.record_id,
            "signal_id": self.signal.signal_id,
            "match_id": self.signal.match_id,
            "league": self.signal.league,
            "market": self.signal.market,
            "outcome": self.signal.outcome,
            "kickoff": self.signal.kickoff.isoformat(),
            "price": self.signal.price,
            "price_source": self.signal.price_source,
            "market_prob": self.signal.market_prob,
            "model_prob": self.signal.model_prob,
            "blended_prob": self.signal.blended_prob,
            "edge": self.signal.edge,
            "ev": self.signal.ev,
            "tier": self.signal.tier,
            "confidence": self.signal.confidence,
            "model_coverage": self.signal.data_quality.model_coverage,
            "calibrated": self.signal.data_quality.calibrated,
            "verdict": self.risk.verdict,
            "reason": self.risk.reason.value,
            "mode": self.mode,
            "created_at": self.created_at.isoformat(),
        }
        if self.stake is not None:
            row.update({
                "stake": self.stake.stake,
                "stake_executable": self.stake.executable,
                "kelly_fraction": self.stake.kelly_fraction,
                "cap_pct": self.stake.cap_pct,
                "cap_source": self.stake.cap_source,
            })
        if self.order:
            row["order_id"] = self.order.get("bet_id") or self.order.get("order_id")
            row["order_status"] = self.order.get("status")
        if self.settlement:
            row["outcome_final"] = self.settlement.get("esito_finale")
            row["profit"] = self.settlement.get("profit")
        return row


__all__ = [
    "DataQuality", "DecisionRecord", "KILL_SWITCH_PRECEDENCE", "KillSwitchStatus",
    "Market", "Mode", "Outcome", "ReasonCode", "RiskDecision", "Signal",
    "StakeDecision", "Tier", "Verdict", "make_signal_id", "risk_approve",
    "risk_reject", "risk_review", "utcnow",
]
