"""decision/validation.py — Shadow Validation: la convalida della riga PERSISTITA.

Perche' esiste (16/09/2026): **"persistito" non e' "approvato"**. Prima di
questo modulo l'unica garanzia della catena era che la riga finisse sul ledger;
l'ordine, pero', non dipendeva dalla riuscita di quella scrittura. Qui il ciclo
di vita diventa esplicito e a tre stati:

    1. il gateway di storage SCRIVE la riga con stato `pending`;
    2. il motore di convalida la RILEGGE dal ledger e la esamina;
    3. solo `validated` autorizza l'ordine reale (`require_persist` del
       `Dispatcher`, che blocca l'ordine — non l'audit ne' le notifiche).

Perche' la convalida legge la RIGA e non l'oggetto in memoria: se esaminasse
l'oggetto, una scrittura fallita non si vedrebbe e la catena autorizzerebbe un
ordine in nome di una decisione che sul ledger non esiste. Leggendo cio' che e'
stato scritto, "prima persistere, poi convalidare" diventa una proprieta'
strutturale invece di una promessa.

Cosa NON fa: non ricalcola EV, edge, stake o gate di strategia. Le soglie
vivono nel Risk/Stake Engine e il verdetto e' gia' scritto nella riga — qui si
traduce una riga in uno stato, senza copiare nessun numero (il giorno in cui la
strategia cambia, questo file non cambia).

Regole, in ordine (nessun fuzzy: si legge il verdetto, non lo si interpreta):

| riga persistita                          | stato     | motivo                |
|---|---|---|
| verdict `reject`                         | rejected  | il motivo del rifiuto  |
| verdict `review`                         | pending   | `review_pending`       |
| verdict `approve` + stake eseguibile     | validated | `ok`                   |
| verdict `approve` senza stake eseguibile | rejected  | `stake_not_executable` |
| verdict assente/ignoto                   | pending   | `validation_incomplete`|

`pending` blocca l'ordine esattamente come `rejected`: l'unica differenza e'
che `pending` puo' ancora diventare `validated` (una revisione umana approvata),
mentre `rejected` e' definitivo per quella riga.

Il modulo e' **puro**: nessun DB, nessuna rete, nessun import di `tracker` o di
`auto_bet`. Si testa offline in millisecondi e si puo' chiamare da qualunque
gateway.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from pydantic import BaseModel

from .models import (
    DECISION_STATUS_PENDING,
    DECISION_STATUS_REJECTED,
    DECISION_STATUS_VALIDATED,
    DecisionStatus,
    ReasonCode,
)

#: Verdetto del Risk Engine che rifiuta (fonte: `models.Verdict`).
REJECT_VERDICT = "reject"
#: Verdetti che non decidono da soli: serve un umano (`ReviewQueue`).
PENDING_VERDICTS = ("review",)
#: Verdetto che, con uno stake eseguibile, autorizza l'ordine.
APPROVE_VERDICT = "approve"


class ValidationOutcome(BaseModel):
    """Esito della convalida di una riga (dato serializzabile, non eccezione)."""

    status: DecisionStatus
    reason: ReasonCode
    detail: str = ""

    @property
    def validated(self) -> bool:
        """Convalida positiva: e' l'unico caso che apre l'ordine reale."""
        return self.status == DECISION_STATUS_VALIDATED

    @property
    def order_allowed(self) -> bool:
        """Alias esplicito di `validated` (fail-closed per tutto il resto)."""
        return self.validated

    @property
    def decided(self) -> bool:
        """True se lo stato e' definitivo (non potra' piu' cambiare da solo)."""
        return self.status in (DECISION_STATUS_VALIDATED, DECISION_STATUS_REJECTED)

    def as_json(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason.value,
                "detail": self.detail, "validated": self.validated}


def _reason_code(value: Any) -> Optional[ReasonCode]:
    """`ReasonCode` dal valore scritto sul ledger (None se ignoto).

    Un motivo illeggibile non e' un motivo inventato: si scarta.
    """
    try:
        return ReasonCode(str(value))
    except ValueError:
        return None


def validate_row(row: Optional[Mapping[str, Any]]) -> ValidationOutcome:
    """Convalida una riga del ledger `decisions`. PURA: nessun effetto.

    Una riga assente (`None` o vuota) NON e' un'eccezione: e' "non c'e' niente
    da convalidare" -> `pending` con `validation_incomplete`, che blocca
    l'ordine (fail-closed) lasciando aperto un secondo tentativo dopo il
    salvataggio. Una riga senza `record_id` non e' identificabile e vale come
    assente: senza identita' non si puo' nulla.
    """
    data = dict(row or {})
    if not data.get("record_id"):
        return ValidationOutcome(
            status=DECISION_STATUS_PENDING,
            reason=ReasonCode.VALIDATION_INCOMPLETE,
            detail="riga assente o senza record_id: niente da convalidare")

    verdict = str(data.get("verdict") or "").strip().lower()

    if verdict == REJECT_VERDICT:
        reason = _reason_code(data.get("reason")) or ReasonCode.VALIDATION_INCOMPLETE
        return ValidationOutcome(
            status=DECISION_STATUS_REJECTED, reason=reason,
            detail=f"rifiuto ({reason.value}): nessun ordine")

    if verdict in PENDING_VERDICTS:
        return ValidationOutcome(
            status=DECISION_STATUS_PENDING, reason=ReasonCode.REVIEW_PENDING,
            detail="in attesa di revisione umana: l'ordine resta bloccato")

    if verdict == APPROVE_VERDICT:
        if data.get("stake_executable"):
            return ValidationOutcome(
                status=DECISION_STATUS_VALIDATED, reason=ReasonCode.OK,
                detail="approvata con stake eseguibile")
        return ValidationOutcome(
            status=DECISION_STATUS_REJECTED, reason=ReasonCode.STAKE_NOT_EXECUTABLE,
            detail="stake assente o cappato sotto il floor: ordine bloccato")

    return ValidationOutcome(
        status=DECISION_STATUS_PENDING, reason=ReasonCode.VALIDATION_INCOMPLETE,
        detail=f"verdetto assente o ignoto ({verdict or '?'}): niente da convalidare")


__all__ = [
    "APPROVE_VERDICT", "PENDING_VERDICTS", "REJECT_VERDICT", "ValidationOutcome",
    "validate_row",
]
