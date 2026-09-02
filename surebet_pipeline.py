"""
Surebet pipeline (dati reali)
=============================

Fonde i prezzi BACK reali del Betfair Exchange (job 8:45, data/scan_*.json)
con le quote the-odds-api (cache 24h per lega, costo zero dopo il job 6:00)
e lancia il detector di surebet_scanner sul DataFrame combinato.

Perche' servono DUE fonti: un singolo bookmaker non puo' produrre un
arbitraggio (la somma degli inversi di un mercato e' sempre > 1). L'edge
nasce quando le quote divergenti tra fonti superano il margine.

Fail-safe:
- senza catalogo Betfair -> nessun alert (None);
- senza seconda fonte (ODDS_API_KEY assente / cache vuota) -> alert [] con log;
- nessuna chiamata di rete: legge SOLO file gia' scaricati.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from daily_scanner import to_odds_records
from daily_scan_job import load_latest_scan
from surebet_scanner import DEFAULT_MIN_MARGIN, SUREBET_DB, scan_surebets

logger = logging.getLogger("surebet_pipeline")

REAL_SOURCE_LABEL = "reale (Betfair Exchange + the-odds-api)"
_MIN_SOURCES = 2


def _normalize_event(evento: str | None) -> str | None:
    """'Serie A – Roma vs Empoli' / 'Roma v Empoli' -> 'roma vs empoli'.

    Rimuove il prefisso campionato, separa le squadre e normalizza in
    minuscolo: la chiave deve coincidere TRA fonti per il groupby.
    """
    name = (evento or "").strip()
    if not name:
        return None
    # prefisso campionato: separatore " – " / " - " con spazi
    parts = re.split(r"\s+[–-]\s+", name)
    if len(parts) > 1:
        name = parts[-1]
    teams = re.split(r"\s+(?:vs|v)\s+", name, flags=re.IGNORECASE)
    if len(teams) != 2:
        return None
    home, away = teams[0].strip().lower(), teams[1].strip().lower()
    if not home or not away:
        return None
    return f"{home} vs {away}"


def merge_records(*row_lists: list[dict]) -> list[dict]:
    """Concatena le fonti e normalizza l'evento per il matching incrociato."""
    merged: list[dict] = []
    for rows in row_lists:
        for r in rows:
            key = _normalize_event(r.get("evento"))
            if not key:
                continue
            row = dict(r)
            row["evento"] = key
            merged.append(row)
    return merged


def _count_sources(rows: list[dict]) -> int:
    return len({r.get("bookmaker") for r in rows})


def find_surebets(betfair_rows: list[dict],
                  other_rows: list[dict] | None = None,
                  min_margin: float = DEFAULT_MIN_MARGIN) -> list:
    """Detector su dati reali. Ritorna [] se le fonti sono < 2 (onesto, no falsi)."""
    rows = merge_records(betfair_rows, other_rows or [])
    if not rows:
        return []
    if _count_sources(rows) < _MIN_SOURCES:
        logger.info("fonti insufficienti (%d bookmaker): surebet non valutabile",
                    _count_sources(rows))
        return []
    import pandas as pd
    df = pd.DataFrame(rows)
    return scan_surebets(df, min_margin)


def log_real_opportunity(opp) -> None:
    """Salva l'opportunita' con fonte REALI (schema identico a surebet_log.jsonl)."""
    d = opp.to_dict()
    d["fonte_dati"] = REAL_SOURCE_LABEL
    d["nota_limitazione"] = (
        "Prezzi reali al momento della scansione. Le quote possono variare "
        "prima della puntata; verifica la disponibilita' sul bookmaker."
    )
    SUREBET_DB.parent.mkdir(parents=True, exist_ok=True)
    with SUREBET_DB.open("a", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False) + "\n")


def format_alert(opportunities: list) -> str:
    """Messaggio Telegram per gli alert surebet su dati reali."""
    if not opportunities:
        return ""
    lines = [
        "⚡ *SUREBET — DATI REALI*",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]
    for i, opp in enumerate(opportunities[:5], 1):
        lines.append(f"#{i} 🏟 {opp.evento}")
        lines.append(f"   🎯 {opp.mercato} | margine {opp.margin * 100:+.2f}%")
        for esito, bookmaker, quota, alloc in zip(
                opp.esiti, opp.bookmakers, opp.quote, opp.allocazioni):
            lines.append(f"   • {esito}: {quota:.2f} ({bookmaker}) → {alloc:.2f}u")
        lines.append("")
    lines.extend([
        "⚠️ Quote reali al momento della scansione: possono cambiare prima "
        "della puntata. Verifica la disponibilità.",
        "",
        "🎲 *Gioca responsabilmente* — [www.adm.gov.it](https://www.adm.gov.it)",
    ])
    return "\n".join(lines)


def run_surebet_alert(target_date: str | None = None) -> list:
    """Pipeline completa: catalogo Betfair + quote odds-api -> surebets.

    Ritorna la lista di opportunita' ([]) se nessuna fonte o nessun edge.
    NON chiama mai la rete: solo file gia' scaricati dai job 6:00/8:45.
    """
    scan = load_latest_scan()
    if scan is None or not scan.get("opportunities"):
        # Con Betfair in stand-by (BETFAIR_ENABLED=0) lo skip è VOLUTO:
        # silenzioso, non un avviso (il catalogo non arriverà mai).
        try:
            from betfair_client import enabled as _bf_enabled
            loud = _bf_enabled()
        except Exception:
            loud = True
        if loud:
            logger.info("nessun catalogo Betfair in cache: surebet alert saltato")
        return []

    betfair_rows = to_odds_records(scan["opportunities"])
    if not betfair_rows:
        logger.info("catalogo Betfair senza prezzi validi: alert saltato")
        return []

    try:
        from odds_api import get_live_odds
        other_rows = get_live_odds()
    except Exception as e:
        logger.warning("seconda fonte non disponibile: %s", e)
        other_rows = []
    if not other_rows:
        logger.info("quote the-odds-api assenti (chiave/cache): alert saltato")
        return []

    opportunities = find_surebets(betfair_rows, other_rows)
    for opp in opportunities:
        log_real_opportunity(opp)
    if opportunities:
        logger.info("SUREBET su dati reali: %d opportunita'", len(opportunities))
    return opportunities


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ops = run_surebet_alert()
    if ops:
        print(format_alert(ops))
    else:
        print("Nessuna surebet su dati reali (o fonti insufficienti).")
