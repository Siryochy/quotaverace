"""ml_audit.py — Audit di qualita' del dataset di addestramento ML.

Prima di addestrare il modello, il dataset (predictions + bets chiuse)
deve essere pulito: un errore nel dataset viene IMPARATO dal modello come
verita'. Questo script controlla ogni riga e segnala le anomalie.

CLI:
  venv/bin/python ml_audit.py                # audit su predictions+bets
  venv/bin/python ml_audit.py --source bets  # solo puntate auto
  venv/bin/python ml_audit.py --source predictions

Exit code: 0 = dataset pulito, 1 = trovati problemi.
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict, List

from ml_dataset import build_training_rows

VALID_OUTCOMES = {"won", "lost", "push"}

# Per 1X2 l'esito puo' essere un nome squadra (best_esito della schedina):
# nessun check stretto. I mercati derivati hanno esiti strutturati.
CANONICAL_ESITI = {
    "OU": {"over", "under"},
    "AH": {"home", "away"},
    "BTTS": {"yes", "no", "gol gol", "si", "no gol"},
}


def audit_training_rows(rows: List[Dict]) -> List[Dict]:
    """Controlla le righe di addestramento. Ritorna la lista dei problemi.

    Ogni problema: {"riga": n, "match_id", "mercato", "esito", "tipo", "msg"}.
    Una riga puo' generare piu' problemi (uno per tipo).
    """
    problems: List[Dict] = []
    seen: set = set()

    for n, r in enumerate(rows, 1):
        mid = r.get("match_id") or ""
        mercato = r.get("mercato") or ""
        esito = r.get("esito") or ""
        outcome = r.get("esito_finale")
        label = r.get("label_ml")
        quota = r.get("quota")
        prob = r.get("prob")
        profit = r.get("profit")

        base = {"riga": n, "match_id": mid, "mercato": mercato, "esito": esito}

        # 1. match_id obbligatorio
        if not mid:
            problems.append({**base, "tipo": "match_id_mancante",
                             "msg": "match_id vuoto"})

        # 2. esito finale valido
        if outcome not in VALID_OUTCOMES:
            problems.append({**base, "tipo": "esito_finale_invalido",
                             "msg": f"esito_finale={outcome!r} non in {sorted(VALID_OUTCOMES)}"})

        # 3. label_ml coerente con esito_finale
        expected_label = 1 if outcome == "won" else 0
        if label != expected_label:
            problems.append({**base, "tipo": "label_incoerente",
                             "msg": f"esito_finale={outcome!r} -> label_ml deve essere "
                                    f"{expected_label}, trovato {label!r}"})

        # 4. quota valida (una quota di scommessa e' sempre > 1.0)
        if quota is None:
            problems.append({**base, "tipo": "quota_mancante", "msg": "quota None"})
        elif quota <= 1.0:
            problems.append({**base, "tipo": "quota_invalida",
                             "msg": f"quota {quota} <= 1.0 (quota di scommessa non valida)"})

        # 5. prob blend nel range [0,1] (assente per le puntate: ok)
        if prob is not None and not (0.0 <= prob <= 1.0):
            problems.append({**base, "tipo": "prob_fuori_range",
                             "msg": f"prob {prob} fuori da [0,1]"})

        # 6. profit coerente col segno dell'esito
        if profit is not None and outcome == "won" and profit <= 0:
            problems.append({**base, "tipo": "profit_incoerente",
                             "msg": f"esito won ma profit {profit} <= 0"})
        if profit is not None and outcome == "lost" and profit >= 0:
            problems.append({**base, "tipo": "profit_incoerente",
                             "msg": f"esito lost ma profit {profit} >= 0"})
        if profit is not None and outcome == "push" and abs(profit) > 1e-9:
            problems.append({**base, "tipo": "profit_incoerente",
                             "msg": f"esito push ma profit {profit} != 0"})

        # 7. esito strutturalmente valido per i mercati derivati.
        #    OU: deve contenere over/under; AH: deve iniziare con home/away;
        #    BTTS: yes/no/gol gol. 1X2 accetta anche i nomi squadra.
        key = mercato.upper()
        if key == "OU" and not any(t in esito.lower() for t in ("over", "under")):
            problems.append({**base, "tipo": "esito_non_canonico",
                             "msg": f"esito {esito!r} non e' un esito Over/Under"})
        elif key == "AH" and not esito.lower().lstrip().startswith(("home", "away")):
            problems.append({**base, "tipo": "esito_non_canonico",
                             "msg": f"esito {esito!r} non inizia con Home/Away (Asian Handicap)"})
        elif key == "BTTS" and esito.lower() not in {"yes", "no", "gol gol", "si", "no gol"}:
            problems.append({**base, "tipo": "esito_non_canonico",
                             "msg": f"esito {esito!r} non e' Yes/No (BTTS)"})

        # 8. duplicati sulla CHIAVE NORMALIZZATA (stessa chiave della pipeline:
        # ml_dataset.dedupe_training_rows). "Over 2.5" e "over" sono lo stesso
        # segnale; una previsione del ledger e la relativa puntata auto con lo
        # stesso match+esito sono la stessa scommessa (1 riga sola).
        norm = r.get("esito_norm")
        if norm is None:
            from ml_dataset import esito_norm
            norm = esito_norm(mercato, esito,
                              r.get("home") or "", r.get("away") or "")
        dup_key = (mid, mercato.strip().upper(), norm)
        if dup_key in seen:
            problems.append({**base, "tipo": "duplicato",
                             "msg": f"riga duplicata per ({mid}, {mercato}, {esito})"})
        seen.add(dup_key)

    return problems


def summarize(problems: List[Dict]) -> Dict:
    """Conteggio problemi per tipo (ordinato per frequenza decrescente)."""
    by_type: Dict[str, int] = {}
    for p in problems:
        by_type[p["tipo"]] = by_type.get(p["tipo"], 0) + 1
    return dict(sorted(by_type.items(), key=lambda kv: -kv[1]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Audit qualita' dataset ML")
    ap.add_argument("--source", choices=["all", "predictions", "bets"],
                    default="all", help="Fonte delle righe (default: all)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Massimo numero di righe da controllare")
    args = ap.parse_args(argv)

    rows = build_training_rows(limit=args.limit, source=args.source)
    print(f"🔎 Audit dataset ML ({args.source}): {len(rows)} righe chiuse")
    if not rows:
        print("ℹ️  Nessuna riga chiusa nel dataset: niente da controllare.")
        return 0

    problems = audit_training_rows(rows)
    if not problems:
        print("✅ Dataset pulito: nessun problema trovato.")
        return 0

    print(f"❌ {len(problems)} problemi trovati su {len(rows)} righe:")
    for tipo, n in summarize(problems).items():
        print(f"   • {tipo}: {n}")
    print("\nDettaglio (prime 30):")
    for p in problems[:30]:
        print(f"   riga {p['riga']} [{p['match_id']}] {p['mercato']} "
              f"{p['esito']}: {p['tipo']} — {p['msg']}")
    if len(problems) > 30:
        print(f"   … e altri {len(problems) - 30} problemi (vedi --limit per ampliare)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
