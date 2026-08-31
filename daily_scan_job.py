"""
Job giornaliero Betfair
=======================

Esegue la scansione giornaliera (daily_scanner.scan_day) e salva il catalogo
completo su data/scan_<YYYY-MM-DD>.json.

- Nessun ordine: livello "discovery" solo lettura.
- Se BETFAIR_APP_KEY manca, il job salta silenziosamente (nessun errore).
- Il file salvato alimenta GET /api/scan (cache) e il flusso surebet
  (to_odds_records) senza consumare API a ogni richiesta frontend.

Uso:
  python daily_scan_job.py            # scan di oggi
  python daily_scan_job.py 2026-09-01 # scan di una data specifica
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from betfair_client import get_client
from config import DATA_DIR
from daily_scanner import scan_day

logger = logging.getLogger("daily_scan_job")

SCAN_DIR = DATA_DIR


def run_daily_scan(target_date: str | None = None) -> dict | None:
    """Lancia scan_day e persiste il catalogo. None se Betfair non configurato."""
    client = get_client()
    if client is None:
        logger.info("BETFAIR_APP_KEY assente: scansione giornaliera saltata")
        return None
    result = scan_day(client, target_date)
    day = result.get("day") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    path = SCAN_DIR / f"scan_{day}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **result,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    logger.info("Catalogo salvato: %s (%d eventi, %d mercati, %d prezzi)",
                path.name, result.get("events", 0), result.get("markets", 0),
                len(result.get("opportunities", [])))
    return payload


def load_latest_scan() -> dict | None:
    """Carica la scansione salvata più recente (nome file scan_<giorno>.json).

    Il nome è sortabile lessicograficamente per data ISO: l'ultimo file è
    il più recente. File corrotti vengono ignorati.
    """
    files = sorted(SCAN_DIR.glob("scan_*.json"))
    for path in reversed(files):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("scan file corrotto (%s): %s", path.name, e)
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    target = sys.argv[1] if len(sys.argv) > 1 else None
    payload = run_daily_scan(target)
    if payload is None:
        print("❌ Betfair non configurato (BETFAIR_APP_KEY assente).")
        sys.exit(1)
    print(f"✅ {payload['day']}: {payload['events']} eventi, "
          f"{payload['markets']} mercati, "
          f"{len(payload['opportunities'])} prezzi back")
