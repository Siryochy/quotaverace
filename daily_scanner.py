"""
Daily total scanner
===================

Varre TODOS os eventos de futebol (eventTypeId=1) no Betfair Exchange para
um dia, extrai os melhores precos back disponiveis para cada mercado
(MATCH_ODDS, OVER_UNDER_25, OVER_UNDER_15, OVER_UNDER_35, BTTS) e devolve
o catalogo completo de (evento, mercado, selecao, preco, stake minimo).

E o nivel "descoberta" do fluxo de automacao: nada aqui pega ordem —
apenas lista oportunidades com preco de mercado real.

Include anche un ponte verso il contratto normalizzato di odds_ingest
(to_odds_records): converte le opportunita' in righe
{bookmaker, evento, sport, esito, quota_decimale, timestamp} cosi'
surebet_scanner puo' consumare prezzi REALI dell'Exchange invece dei mock.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from betfair_client import BetfairClient, EVENT_TYPE_SOCCER

logger = logging.getLogger("daily_scanner")

MARKET_TYPES = [
    "MATCH_ODDS",
    "OVER_UNDER_25",
    "OVER_UNDER_15",
    "OVER_UNDER_35",
    "BOTH_TEAMS_TO_SCORE",
]

BATCH_SIZE = 100  # maxResults por chamada listMarketCatalogue


def _day_window(target_date: str | None = None) -> tuple[str, str]:
    """ISO UTC window [start, end) do dia alvo (default: hoje UTC)."""
    if target_date:
        day = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = day.isoformat().replace("+00:00", "Z")
    end = (day + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    return start, end


def scan_day(client: BetfairClient, target_date: str | None = None) -> dict:
    """Varre todos os mercados de futebol do dia e retorna o catalogo.

    Estrutura de retorno:
    {
      "day": "2026-08-31",
      "events": n, "markets": n,
      "opportunities": [
        {"event_id", "event_name", "market_id", "market_type",
         "selection_id", "selection_name", "side": "BACK",
         "price", "price_size", "start_time"},
      ],
    }
    """
    start, end = _day_window(target_date)
    market_filter = {
        "eventTypeIds": [EVENT_TYPE_SOCCER],
        "marketStartTime": {"from": start, "to": end},
        "marketTypeCodes": MARKET_TYPES,
    }

    catalogue = []
    for mtype in MARKET_TYPES:
        mf = dict(market_filter)
        mf["marketTypeCodes"] = [mtype]
        batch = client.list_market_catalogue(
            mf, max_results=BATCH_SIZE,
            market_projection=["EVENT", "RUNNER_DESCRIPTION", "MARKET_START_TIME"])
        catalogue.extend(batch)
        if len(batch) == BATCH_SIZE:
            # mais de 100 mercados desse tipo: paginar (best-effort 2 paginas)
            logger.warning("mercado %s paginou (>= %d) — resultado possivelmente parcial", mtype, BATCH_SIZE)

    opportunities = []
    market_ids = [m["marketId"] for m in catalogue]
    books = {}
    for i in range(0, len(market_ids), 200):
        try:
            books.update({b["marketId"]: b for b in client.list_market_book(market_ids[i:i + 200])})
        except Exception as e:
            logger.warning("listMarketBook lote %d falhou: %s", i, e)

    for m in catalogue:
        book = books.get(m["marketId"])
        if not book:
            continue
        for runner in book.get("runners", []):
            ex = runner.get("ex") or {}
            back = (ex.get("availableToBack") or [None])[0]
            if not back:
                continue
            name_map = {r.get("selectionId"): r.get("runnerName") for r in m.get("runners", [])}
            opportunities.append({
                "event_id": (m.get("event") or {}).get("id"),
                "event_name": (m.get("event") or {}).get("name"),
                "market_id": m["marketId"],
                "market_type": m.get("marketType"),
                "selection_id": runner["selectionId"],
                "selection_name": name_map.get(runner["selectionId"]),
                "side": "BACK",
                "price": back.get("price"),
                "price_size": back.get("size"),
                "start_time": m.get("marketStartTime"),
            })

    return {
        "day": start[:10],
        "events": len({o["event_id"] for o in opportunities if o["event_id"]}),
        "markets": len(catalogue),
        "opportunities": opportunities,
    }


def group_same_start(opportunities: list[dict], tolerance_min: int = 0) -> dict[str, list[dict]]:
    """Agrupa oportunidades por kickoff (partidas que comecam no mesmo momento)."""
    groups: dict[str, list[dict]] = {}
    for o in opportunities:
        key = (o.get("start_time") or "")[:16]  # minuto
        groups.setdefault(key, []).append(o)
    return groups


# ---------------------------------------------------------------------------
# Ponte verso il contratto odds_ingest (per surebet_scanner)
# ---------------------------------------------------------------------------

BETFAIR_BOOKMAKER = "Betfair Exchange"

_TEAM_SEPARATORS = (" vs ", " v ", " - ", " – ")
_DRAW_NAMES = ("the draw", "draw", "pareggio")


def _split_teams(event_name: str) -> tuple[str, str] | None:
    """'Roma v Empoli' -> ('Roma', 'Empoli'). None se nessun separatore noto."""
    name = (event_name or "").strip()
    for sep in _TEAM_SEPARATORS:
        if sep in name:
            home, away = name.split(sep, 1)
            return home.strip(), away.strip()
    return None


def _esito_from_selection(market_type: str, selection_name: str,
                          event_name: str) -> str | None:
    """Mappa il runner Betfair sull'esito del contratto normalizzato.

    - MATCH_ODDS: 'Roma'->'1', 'Empoli'->'2', 'The Draw'->'X'
      (nomi squadre estratti dall'evento, confronto case-insensitive;
       fallback: nome secco del runner).
    - OVER_UNDER_* / BOTH_TEAMS_TO_SCORE: il nome runner e' gia' un esito
      ('Over 2.5', 'Under 2.5', 'Yes', 'No') e viene lasciato com'e'.
    """
    sel = (selection_name or "").strip()
    if not sel:
        return None
    if market_type == "MATCH_ODDS":
        low = sel.lower()
        if low in _DRAW_NAMES:
            return "X"
        teams = _split_teams(event_name or "")
        if teams:
            home, away = teams
            if low == home.lower():
                return "1"
            if low == away.lower():
                return "2"
        return sel
    return sel


def to_odds_records(opportunities: list[dict],
                    bookmaker: str = BETFAIR_BOOKMAKER) -> list[dict]:
    """Converte le opportunita' di scan_day nel contratto di odds_ingest.

    Righe pronte per pd.DataFrame(...).dropna() e poi scan_surebets():
    {bookmaker, evento, sport, esito, quota_decimale, timestamp}.

    Nota: un solo bookmaker per evento non produce MAI surebet (la somma
    degli inversi di un singolo mercato e' sempre > 1). Il ponte serve per
    mescolare i prezzi REALI dell'Exchange con altre fonti nel DataFrame.
    """
    rows: list[dict] = []
    for o in opportunities:
        price = o.get("price")
        if price is None or float(price) <= 1.0:
            continue
        esito = _esito_from_selection(
            o.get("market_type") or "",
            o.get("selection_name") or "",
            o.get("event_name") or "",
        )
        if not esito:
            continue
        rows.append({
            "bookmaker": bookmaker,
            "evento": o.get("event_name") or "Sconosciuto",
            "sport": "calcio",
            "esito": esito,
            "quota_decimale": float(price),
            "timestamp": o.get("start_time")
                         or datetime.now(timezone.utc).isoformat(),
        })
    return rows
