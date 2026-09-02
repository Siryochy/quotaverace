"""market_signals.py — Aggregatore segnali di mercato RLM/steam/crollo.

Unico punto di aggregazione dei movimenti di linea (price_snapshots) per
report Telegram e webapp: classifica ogni segnale value attivo con i
VERI rilevatori di line_movement.py / rlm_alert.py (niente proxy euristici
sul singolo consumo):

  - steam  (🔥 urgent):  movimento > 6% in < 30 min
  - crash  (🚨 urgent):  calo >= 5% dal primo snapshot (edge in erosione)
  - rlm    (⚠️ warning): Reverse Line Movement (movimento contro il pubblico)

CLI:
  venv/bin/python market_signals.py           # report testuale
  venv/bin/python market_signals.py --json    # output JSON
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Dict, List

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"urgent": 0, "warning": 1, "info": 2}
TYPE_EMOJI = {"steam": "🔥", "crash": "🚨", "rlm": "⚠️"}
TYPE_LABEL = {"steam": "STEAM", "crash": "CROLLO QUOTA", "rlm": "RLM"}


def collect_market_signals(max_matches: int = 20) -> List[Dict]:
    """Analizza i segnali value/strong_value attivi e li classifica.

    Returns:
        Lista di dict ordinati per severita' (urgent prima) e poi per
        |movimento|: match_id, evento, league, esito, quota, ev, status,
        alert_type, severity, total_move_pct, first_price, last_price,
        n_snapshots, commence.
    """
    from rlm_alert import check_rlm_for_signal, get_active_value_signals

    alerts: List[Dict] = []
    signals = get_active_value_signals()
    for sig in signals[:max_matches]:
        try:
            alert = check_rlm_for_signal(sig)
        except Exception as e:
            logger.debug("check_rlm_for_signal fallito per %s: %s",
                         sig.get("match_id"), e)
            continue
        if alert:
            alerts.append(alert)

    # Piu' urgenti prima, poi movimento maggiore
    alerts.sort(key=lambda a: (SEVERITY_ORDER.get(a.get("severity", "info"), 2),
                               -abs(a.get("total_move_pct", 0.0))))
    return alerts


def summarize_market_signals(alerts: List[Dict]) -> Dict:
    """Conteggi per tipo/severita' per badge e dashboard."""
    by_type = {"steam": 0, "crash": 0, "rlm": 0}
    for a in alerts:
        t = a.get("alert_type")
        if t in by_type:
            by_type[t] += 1
    return {
        "total": len(alerts),
        "urgent": sum(1 for a in alerts if a.get("severity") == "urgent"),
        "by_type": by_type,
    }


def format_market_signals_report(alerts: List[Dict],
                                 max_lines: int = 5) -> List[str]:
    """Righe Telegram per il report giornaliero (senza titolo di sezione).

    Ritorna [] se nessun segnale: il chiamante decide se mostrare la
    sezione (mostrare sempre la sezione = rumore).
    """
    if not alerts:
        return []
    s = summarize_market_signals(alerts)
    lines = [f"📊 *Line Movement (24h):* {s['total']} segnali monitorati"]
    parts = []
    if s["by_type"]["steam"]:
        parts.append(f"🔥 Steam: {s['by_type']['steam']}")
    if s["by_type"]["crash"]:
        parts.append(f"🚨 Crollo: {s['by_type']['crash']}")
    if s["by_type"]["rlm"]:
        parts.append(f"⚠️ RLM: {s['by_type']['rlm']}")
    if parts:
        lines.append("   " + " | ".join(parts))
    for a in alerts[:max_lines]:
        move = a.get("total_move_pct", 0.0)
        arrow = "↗" if move > 0 else "↙"
        lines.append(
            f"   {TYPE_EMOJI.get(a['alert_type'], '•')} {arrow} "
            f"{a.get('evento', '?')} — {a.get('esito', '?')} "
            f"@ {a.get('quota', 0):.2f} ({move:+.1f}%, "
            f"{a.get('n_snapshots', 0)} snap)")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Segnali di mercato RLM/steam/crollo")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--max", type=int, default=20,
                    help="numero massimo di segnali analizzati")
    args = ap.parse_args(argv)

    alerts = collect_market_signals(max_matches=args.max)
    if args.json:
        print(json.dumps({
            "summary": summarize_market_signals(alerts),
            "signals": alerts,
        }, indent=2, ensure_ascii=False))
    elif not alerts:
        print("✅ Nessun movimento rilevante sui segnali attivi.")
    else:
        print("\n".join(format_market_signals_report(alerts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
