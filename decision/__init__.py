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

⚠️ Questo pacchetto NON e' ancora collegato ad `auto_bet`: la produzione
continua a usare il percorso esistente finche' la parita' non e' dimostrata dai
test (`test_decision_pipeline.py` confronta i gate con `value_filter.is_sane`).
"""

from __future__ import annotations

from . import kill_switch, risk_engine, stake_engine
from .limits import RiskLimits, limits_from_env
from .models import (
    DataQuality, DecisionRecord, KILL_SWITCH_PRECEDENCE, KillSwitchStatus,
    ReasonCode, RiskDecision, Signal, StakeDecision, make_signal_id,
    risk_approve, risk_reject, risk_review, utcnow,
)
from .pipeline import decide, decide_many, pending, resolve_review, summary
from .review_queue import (
    ReviewQueue, STATUS_APPROVED, STATUS_EXPIRED, STATUS_PENDING, STATUS_REJECTED,
    default_path, format_report,
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
    # coda revisioni
    "ReviewQueue", "default_path", "format_report", "STATUS_PENDING",
    "STATUS_APPROVED", "STATUS_REJECTED", "STATUS_EXPIRED",
]
