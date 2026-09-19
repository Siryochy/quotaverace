"""decision/clv.py — Valutatore CLV PURO: calcola la misura, emette il comando.

Perche' esiste (16/09/2026): il Closing Line Value e' la metrica che dice se
il sistema batte il mercato (STRATEGY.md: "CLV e' il segnale, il record
vittorie/sconfitte e' rumore"). Finora il campione finiva sul ledger
(`tracker.save_clv`) da chiamate sparse dentro l'analisi: qui la valutazione
diventa un PASSO DELLA CATENA DI DECISIONE, con lo stesso schema degli altri
stadi:

    ClvInput ──▶ evaluate_clv() ──▶ WriteCLVCommand ──▶ ClvGateway ──▶ clv_history
    (osservazione)  (PURO: misura)    (contratto)        (DB, iniettabile)

Regole del modulo:

1. **Purezza**: qui non si tocca NESSUN database. `evaluate_clv` calcola la
   differenza di quota e RESTITUISCE all'orchestratore l'istanza di
   `WriteCLVCommand` (contratto immutabile in `decision/commands.py`): la
   scrittura su `clv_history` e' un effetto e spetta al gateway
   (`decision/gateways.ClvGateway`, che delega a `tracker.save_clv` — il
   percorso di produzione gia' testato, mai reimplementato).
2. **Una sola formula**: la differenza di quota e' `market_calib.clv_raw`
   (signal/closing - 1, import pigro — stesso pattern di `decision/limits.py`).
   Nessuna copia della formula: se la calibrazione cambia, cambia qui e basta.
3. **Un comando malformato non nasce**: quote <= 1.0 o `market_id` vuoto
   violano il contratto -> l'esito e' `rejected` con motivo, MAI un'eccezione
   verso l'orchestratore e MAI un comando "corretto" in silenzio.
4. **Skipped != rejected**: senza closing non c'e' ancora niente da misurare
   (`skipped`, il normale prima del fischio); dati malformati sono un
   problema (`rejected`). In entrambi i casi il comando non viene emesso.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .commands import Command, WriteCLVCommand, write_clv_command
from .models import utcnow

logger = logging.getLogger("decision.clv")

# --- esiti della valutazione (machine-readable, aggregabili) -----------------
STATUS_OK = "ok"                    # comando emesso
STATUS_SKIPPED = "skipped"          # niente da misurare (ancora): NON e' un errore
STATUS_REJECTED = "rejected"        # dati malformati: il comando NON nasce

REASON_OK = "ok"
REASON_CLOSING_MISSING = "closing_missing"     # closing line non ancora disponibile
REASON_SIGNAL_MISSING = "signal_missing"       # quota del segnale assente
REASON_MARKET_ID_MISSING = "market_id_missing"  # senza match non si registra nulla
REASON_ODDS_INVALID = "odds_invalid"           # quote <= 1.0: contratto violato
REASON_CONTRACT_INVALID = "contract_invalid"   # payload rifiutato dalla validazione

#: Flag informativi (non bloccano l'emissione, arricchiscono l'audit).
FLAG_SINGLE_SAMPLE = "single_sample"   # closing == segnale: CLV finto (eco), il
#                                        report lo esclude gia' dalle medie.


class ClvInput(BaseModel):
    """L'osservazione di mercato che il valutatore riceve (solo dati).

    `market_id` e' l'id del match sul ledger (chiave di `tracker.save_clv`);
    `signal_odds` e' la quota presa al momento del segnale, `closing_odds` la
    closing line dello stesso esito. Entrambe opzionali: l'osservazione puo'
    arrivare prima che la closing esista.
    """

    model_config = {"frozen": True}

    signal_id: str = ""
    market_id: str = ""
    outcome: str = ""
    signal_odds: Optional[float] = None
    closing_odds: Optional[float] = None
    timestamp: Optional[datetime] = None
    source: str = ""


class ClvEvaluation(BaseModel):
    """Esito della valutazione: la misura + il comando per l'orchestratore.

    `command` e' l'istanza di `WriteCLVCommand` (il contratto richiesto);
    `dispatch` e' lo stesso comando nella forma generica che il `Dispatcher`
    instrada al gateway. Sono costruiti INSIEME dagli stessi valori, cosi' non
    possono divergere. Su `skipped`/`rejected` entrambi sono None.
    """

    model_config = {"frozen": True}

    status: Literal["ok", "skipped", "rejected"]
    reason: str
    clv_diff: Optional[float] = None       # signal/closing - 1 (None se non misurabile)
    command: Optional[WriteCLVCommand] = None
    dispatch: Optional[Command] = None
    flags: list[str] = Field(default_factory=list)
    detail: str = ""

    @property
    def emitted(self) -> bool:
        """True se un comando e' stato emesso all'orchestratore."""
        return self.status == STATUS_OK and self.command is not None


def clv_diff(signal_odds: float, closing_odds: float) -> Optional[float]:
    """Differenza di quota in modo PURO: signal/closing - 1.

    Formula UNICA del progetto (`market_calib.clv_raw`, import pigro per non
    appesantire `import decision`): None se una delle due quote non e' valida.
    Per il CLV vig-free (devig della closing) si continua a usare
    `market_calib.clv_vig_free` nei report: qui si misura il campione grezzo
    che alimenta `clv_history`.
    """
    from market_calib import clv_raw            # import pigro (fonte unica)
    return clv_raw(float(signal_odds), float(closing_odds))


def evaluate_clv(observation: ClvInput, *, now: Optional[datetime] = None) -> ClvEvaluation:
    """Valuta l'osservazione e RESTITUISCE il comando (non scrive nulla).

    Puro: nessun DB, nessuna rete, nessun side effect. L'orchestratore passa
    `evaluation.dispatch` al `Dispatcher` con un `ClvGateway` registrato; il
    valutatore non sa come il comando verra' eseguito (ne' SE verra' eseguito:
    in shadow mode resta solo nel registro).
    """
    obs = observation

    # --- guardie PRIMA di costruire il comando: un comando malformato non nasce
    if not (obs.market_id or "").strip():
        return ClvEvaluation(status=STATUS_REJECTED, reason=REASON_MARKET_ID_MISSING,
                             detail="market_id vuoto: senza match non si registra nulla")
    if obs.signal_odds is None:
        return ClvEvaluation(status=STATUS_SKIPPED, reason=REASON_SIGNAL_MISSING,
                             detail="quota del segnale assente: niente da confrontare")
    if obs.closing_odds is None:
        return ClvEvaluation(status=STATUS_SKIPPED, reason=REASON_CLOSING_MISSING,
                             detail="closing line non ancora disponibile")

    try:
        sig, clos = float(obs.signal_odds), float(obs.closing_odds)
    except (TypeError, ValueError):
        return ClvEvaluation(status=STATUS_REJECTED, reason=REASON_ODDS_INVALID,
                             detail="quote non numeriche")
    if sig <= 1.0 or clos <= 1.0:
        # Il contratto richiede quote > 1.0: rifiutato, mai corretto in silenzio
        # (una quota <= 1 e' dati malformati, non un mercato).
        return ClvEvaluation(status=STATUS_REJECTED, reason=REASON_ODDS_INVALID,
                             detail=f"quote fuori contratto (signal={sig}, closing={clos})")

    diff = clv_diff(sig, clos)
    if diff is None:
        # Formula di progetto refuse: stesso trattamento dei dati malformati.
        return ClvEvaluation(status=STATUS_REJECTED, reason=REASON_ODDS_INVALID,
                             detail="formula CLV: quote non valide")

    flags = [FLAG_SINGLE_SAMPLE] if sig == clos else []

    timestamp = obs.timestamp or now or utcnow()
    try:
        command = WriteCLVCommand(
            signal_id=obs.signal_id,
            market_id=obs.market_id,
            signal_odds=sig,
            closing_odds=clos,
            timestamp=timestamp,
            source=obs.source,
        )
        dispatch = write_clv_command(
            match_id=command.market_id,
            outcome=obs.outcome or "",
            signal_odds=command.signal_odds,
            closing_odds=command.closing_odds,
            timestamp=command.timestamp,
            source=command.source,
            signal_id=command.signal_id,
        )
    except Exception as exc:
        # Un payload che viola il contratto e' RIFIUTATO, mai corretto in
        # silenzio: l'orchestratore riceve un esito leggibile, non un'eccezione.
        logger.warning("decision.clv: comando rifiutato dal contratto (%s)", exc)
        return ClvEvaluation(status=STATUS_REJECTED, reason=REASON_CONTRACT_INVALID,
                             detail=f"contratto violato: {exc}")
    return ClvEvaluation(status=STATUS_OK, reason=REASON_OK, clv_diff=diff,
                         command=command, dispatch=dispatch, flags=flags)


def evaluate_clv_many(observations, *, now: Optional[datetime] = None) -> list[ClvEvaluation]:
    """Valuta un lotto: MAI un'eccezione (un'osservazione ostile = rejected)."""
    out: list[ClvEvaluation] = []
    for obs in observations or []:
        try:
            out.append(evaluate_clv(obs, now=now))
        except Exception as exc:                  # osservazione ostile/non conforme
            logger.warning("decision.clv: valutazione fallita (%s)", exc)
            out.append(ClvEvaluation(status=STATUS_REJECTED, reason=REASON_ODDS_INVALID,
                                     detail=f"{type(exc).__name__}: {exc}"))
    return out


__all__ = [
    "ClvEvaluation", "ClvInput", "FLAG_SINGLE_SAMPLE", "REASON_CLOSING_MISSING",
    "REASON_CONTRACT_INVALID", "REASON_MARKET_ID_MISSING", "REASON_ODDS_INVALID",
    "REASON_OK", "REASON_SIGNAL_MISSING", "STATUS_OK", "STATUS_REJECTED",
    "STATUS_SKIPPED", "clv_diff", "evaluate_clv", "evaluate_clv_many",
]
