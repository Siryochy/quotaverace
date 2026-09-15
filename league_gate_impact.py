"""league_gate_impact.py — impatto del gate LEGHE sulla corsia auto-bet.

Direttiva del proprietario (15/09/2026) sul gate "solo campionati vincenti"
(`value_filter.STRATEGY_LEAGUES`): **prima misuro**, poi decido.

Il problema che il gate dovrebbe chiudere: i candidati di `fixture_engine` e
`sx_signals` NON portano la chiave `league`, quindi `is_sane(league="")`
tratta la lega vuota come AMMESSA e la strategia "solo campionati vincenti"
di fatto non viene applicata alla corsia auto-bet (la catena `decision/` la
applica e infatti rifiuta con `league_not_allowed`).

Questo modulo NON decide e NON cambia nulla: misura sul ledger REALE cosa
succederebbe se il gate venisse applicato alla corsia auto-bet — quante
puntate sarebbero bloccate e con quale P/L realizzato. Lettura SOLA (SQLite
`mode=ro`), nessun ordine, zero crediti the-odds-api.

Attenzione a una differenza che falsa i confronti se ignorata:

  * `bets.profit` e' P/L in VALUTA (stake x (quota-1) se vinta, -stake se
    persa) -> ROI = somma(profit) / somma(stake);
  * `predictions.profit` e' P/L **per unita' di stake** ((quota-1) / -1
    dalla `_prediction_outcome`) -> ROI = media(profit).

Le righe senza riga in `matches` (lega sconosciuta) NON sono "ammesse": sono
contate in un bucket `unknown` separato, perche' e' proprio il caso che oggi
passa il gate per un difetto di propagazione, non per una scelta.

Uso:
    venv/bin/python league_gate_impact.py                    # puntate, tutto lo storico
    venv/bin/python league_gate_impact.py --source all
    venv/bin/python league_gate_impact.py --days 14 --json

E' il complemento "ex ante" di `decision.feedback.shadow` (che misura le
decisioni della catena nuova sui segnali vivi).
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import DATA_DIR
from value_filter import STRATEGY_LEAGUES, league_allowed

logger = logging.getLogger("league_gate_impact")

# Sotto questo numero di righe CHIUSE il verdetto non e' affidabile: 11/09
# il ledger aveva ~20 chiusure totali. Meglio dirlo che far credere a un ROI.
MIN_RELIABLE_CLOSED = 30

ALLOWED = "allowed"
BLOCKED = "blocked"
UNKNOWN = "unknown"


def _db_path() -> Path:
    return Path(DATA_DIR) / "quotaverace.db"


def _connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    """Connessione READ-ONLY: questa diagnostica non scrive mai."""
    target = Path(path) if path else _db_path()
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _cutoff(days: Optional[float]) -> Optional[str]:
    if not days:
        return None
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=float(days))).isoformat()


def _bets_rows(conn: sqlite3.Connection, days: Optional[float]) -> List[dict]:
    since = _cutoff(days)
    sql = ("SELECT b.id, b.match_id, b.mode, b.esito, b.price, b.stake, "
           "b.profit, b.esito_finale, b.created_at, m.league, m.commence_time "
           "FROM bets b LEFT JOIN matches m ON m.id = b.match_id")
    args: List[Any] = []
    if since:
        # La finestra si applica alla DATA DEL MATCH (fallback: la data di
        # registrazione): si misura la partita, non quando e' stata scritta
        # la riga.
        sql += " WHERE COALESCE(m.commence_time, b.created_at) >= ?"
        args.append(since)
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        out.append({
            "source": "bets", "id": r["id"], "match_id": r["match_id"],
            "mode": r["mode"], "league": r["league"], "outcome": r["esito"],
            "price": r["price"], "stake": float(r["stake"] or 0.0),
            "profit": float(r["profit"] or 0.0),
            # `esito_finale IS NULL` = ancora in gioco (o mai refertata).
            "closed": r["esito_finale"] is not None,
            "verdict": r["esito_finale"], "kickoff": r["commence_time"],
            "created_at": r["created_at"],
        })
    return out


def _predictions_rows(conn: sqlite3.Connection, days: Optional[float]) -> List[dict]:
    since = _cutoff(days)
    sql = ("SELECT p.id, p.match_id, p.mercato, p.esito, p.quota, p.prob, p.ev, "
           "p.status, p.profit, p.esito_finale, p.created_at, "
           "m.league, m.commence_time "
           "FROM predictions p LEFT JOIN matches m ON m.id = p.match_id "
           "WHERE p.mercato = '1X2'")
    args: List[Any] = []
    if since:
        sql += " AND COALESCE(m.commence_time, p.created_at) >= ?"
        args.append(since)
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        out.append({
            "source": "predictions", "id": r["id"], "match_id": r["match_id"],
            "mode": r["status"], "league": r["league"], "outcome": r["esito"],
            "price": r["quota"], "stake": 1.0,      # unita' di stake
            "profit": float(r["profit"] or 0.0),
            "closed": r["esito_finale"] is not None,
            "verdict": r["esito_finale"], "kickoff": r["commence_time"],
            "created_at": r["created_at"], "ev": r["ev"],
        })
    return out


def _bucket(rows: List[dict]) -> dict:
    """Aggregati di un gruppo di righe.

    Il ROI si calcola sulla valuta delle PUNTATE (profit/stake sommati) e sulla
    media per le PREVISIONI (profit per unita' di stake): vedi il docstring del
    modulo. Qui si tiene conto di entrambe le fonti e si dichiara quale si sta
    usando, cosi' un numero non viene confrontato con l'altro per errore.
    """
    closed = [r for r in rows if r["closed"]]
    open_rows = [r for r in rows if not r["closed"]]
    won = [r for r in closed if r["verdict"] == "won"]
    sources = {r["source"] for r in rows}
    if sources == {"predictions"}:
        unit_roi = True
        staked = float(len(closed))
        pnl = round(sum(r["profit"] for r in closed), 4)
    else:
        unit_roi = False
        staked = round(sum(r["stake"] for r in closed), 4)
        pnl = round(sum(r["profit"] for r in closed), 4)
    return {
        "n": len(rows),
        "open": len(open_rows),
        "closed": len(closed),
        "won": len(won),
        "lost": len(closed) - len(won),
        "staked": staked,
        "pnl": pnl,
        "roi": round(pnl / staked, 4) if staked else None,
        "hit_rate": round(len(won) / len(closed), 4) if closed else None,
        "unit_roi": unit_roi,
        "open_stake": round(sum(r["stake"] for r in open_rows), 4),
        "sources": sorted(sources),
    }


def _classify(row: dict) -> str:
    league = (row.get("league") or "").strip()
    if not league:
        # Nessuna riga in `matches` (o lega vuota): oggi passa il gate per un
        # difetto di propagazione, non per una decisione. Bucket a parte.
        return UNKNOWN
    return ALLOWED if league_allowed(league) else BLOCKED


def _coverage(conn: sqlite3.Connection, days: Optional[float]) -> dict:
    """Impatto sul FLUSSO: quante righe e quanti segnali giocabili cadono
    nelle leghe ammesse dalla strategia (finestra sui match analizzati).

    E' la domanda operativa opposta a quella del P/L: se il gate ammette il 13%
    delle leghe ma lo 0% dei segnali giocabili, applicarlo spegne la corsia —
    e questo si misura subito, senza aspettare centinaia di chiusure.

    La finestra e' la PRODUZIONE del segnale (`predictions.created_at`), non
    la data del match: le previsioni nascono 1-3 giorni prima del kickoff.
    """
    since = _cutoff(days) if days else None
    sql = ("SELECT m.league, COUNT(*) n, "
           "SUM(CASE WHEN p.status IN ('value','strong_value','moderate') "
           "THEN 1 ELSE 0 END) playable, "
           "SUM(CASE WHEN p.esito_finale IS NULL THEN 1 ELSE 0 END) open_rows "
           "FROM matches m JOIN predictions p ON p.match_id = m.id "
           "WHERE p.mercato = '1X2'")
    args: List[Any] = []
    if since:
        sql += " AND p.created_at >= ?"
        args.append(since)
    sql += " GROUP BY m.league"
    groups = {ALLOWED: {"n": 0, "playable": 0, "open": 0, "leagues": 0},
              BLOCKED: {"n": 0, "playable": 0, "open": 0, "leagues": 0}}
    by_league: List[dict] = []
    for r in conn.execute(sql, args).fetchall():
        league = (r["league"] or "").strip()
        name = ALLOWED if league_allowed(league) else BLOCKED
        groups[name]["n"] += r["n"]
        groups[name]["playable"] += r["playable"] or 0
        groups[name]["open"] += r["open_rows"] or 0
        groups[name]["leagues"] += 1
        by_league.append({"league": league, "allowed": name == ALLOWED,
                          "n": r["n"], "playable": r["playable"] or 0,
                          "open": r["open_rows"] or 0})
    total_n = groups[ALLOWED]["n"] + groups[BLOCKED]["n"]
    total_playable = groups[ALLOWED]["playable"] + groups[BLOCKED]["playable"]
    for group in groups.values():
        group["share_rows"] = round(group["n"] / total_n, 4) if total_n else None
        group["share_playable"] = (round(group["playable"] / total_playable, 4)
                                   if total_playable else None)
    return {"window_days": days, "total_rows": total_n,
            "total_playable": total_playable, "groups": groups,
            "by_league": sorted(by_league, key=lambda d: (-d["playable"], -d["n"],
                                                          d["league"]))}


def measure(days: Optional[float] = None, source: str = "bets",
            conn: Optional[sqlite3.Connection] = None,
            path: Optional[str | Path] = None) -> dict:
    """Misura l'impatto del gate leghe. NON solleva: su errore ritorna `error`."""
    out: Dict[str, Any] = {
        "readonly": True, "source": source, "days": days,
        "strategy_leagues": sorted(STRATEGY_LEAGUES),
        "min_reliable_closed": MIN_RELIABLE_CLOSED,
    }
    own = conn is None
    try:
        conn = conn or _connect(path)
        rows: List[dict] = []
        if source in ("bets", "all"):
            rows += _bets_rows(conn, days)
        if source in ("predictions", "all"):
            rows += _predictions_rows(conn, days)
        buckets: Dict[str, List[dict]] = {ALLOWED: [], BLOCKED: [], UNKNOWN: []}
        for row in rows:
            buckets[_classify(row)].append(row)
        out["buckets"] = {name: _bucket(group) for name, group in buckets.items()}
        # Dettaglio per lega bloccata: e' la lista che serve per decidere.
        per_league: Dict[str, List[dict]] = {}
        for row in buckets[BLOCKED]:
            per_league.setdefault(row["league"], []).append(row)
        out["blocked_by_league"] = sorted(
            ({"league": lg, **_bucket(group)} for lg, group in per_league.items()),
            key=lambda d: (-d["n"], d["league"]))
        # Cosa il gate bloccherebbe ADESSO (righe ancora in gioco).
        try:
            out["coverage"] = _coverage(conn, days)
        except Exception as exc:                  # la sezione non fa fallire la misura
            logger.warning("league_gate_impact: copertura non calcolabile (%s)", exc)
            out["coverage"] = None
        out["blocked_open"] = [{
            "source": r["source"], "match_id": r["match_id"], "league": r["league"],
            "outcome": r["outcome"], "stake": r["stake"], "price": r["price"],
            "kickoff": r["kickoff"],
        } for r in sorted(buckets[BLOCKED], key=lambda r: str(r["kickoff"] or ""))
              if not r["closed"]]
        closed_blocked = out["buckets"][BLOCKED]["closed"]
        closed_allowed = out["buckets"][ALLOWED]["closed"]
        out["reliable"] = closed_blocked >= MIN_RELIABLE_CLOSED
        out["caveat"] = (
            f"campione: {closed_allowed} chiusure nelle leghe ammesse, "
            f"{closed_blocked} in quelle bloccate — "
            + ("sotto" if closed_blocked < MIN_RELIABLE_CLOSED else "sopra")
            + f" la soglia di affidabilita' ({MIN_RELIABLE_CLOSED}): "
            + ("il P/L delle bloccate NON e' conclusivo."
               if closed_blocked < MIN_RELIABLE_CLOSED
               else "il confronto e' utilizzabile."))
        return out
    except Exception as exc:                      # mai un'eccezione al chiamante
        logger.warning("league_gate_impact: misura fallita (%s)", exc)
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def format_report(data: Optional[dict] = None) -> str:
    """Report Telegram-friendly: cosa il gate leghe bloccherebbe e con che P/L."""
    d = data if isinstance(data, dict) else measure()
    if d.get("error"):
        return f"⚠️ Impatto gate leghe: misura non disponibile ({d['error']})"
    lines = ["🚦 Gate LEGHE sulla corsia auto-bet (misura, nessuna modifica)",
             f"  leghe ammesse dalla strategia: "
             f"{', '.join(d.get('strategy_leagues') or []) or '(nessuna)'}"]
    names = {ALLOWED: "ammesse", BLOCKED: "bloccate", UNKNOWN: "senza lega"}
    for name in (ALLOWED, BLOCKED, UNKNOWN):
        b = d["buckets"][name]
        roi = "n/d" if b["roi"] is None else f"{b['roi']*100:+.1f}%"
        lines.append(
            f"  {names[name]:12} righe {b['n']:4} (in gioco {b['open']:3}, "
            f"chiuse {b['closed']:3}) | P/L {b['pnl']:+.2f} | ROI {roi} "
            f"| hit {'n/d' if b['hit_rate'] is None else format(b['hit_rate']*100, '.0f') + '%'}")
    blocked = d.get("blocked_by_league") or []
    if blocked:
        lines.append("  leghe che il gate vieterebbe:")
        for entry in blocked[:10]:
            roi = "n/d" if entry["roi"] is None else f"{entry['roi']*100:+.1f}%"
            lines.append(f"    - {entry['league']}: {entry['n']} righe "
                         f"(chiuse {entry['closed']}), P/L {entry['pnl']:+.2f}, ROI {roi}")
    cov = d.get("coverage") or {}
    if cov.get("total_rows"):
        allowed = cov["groups"][ALLOWED]
        blocked = cov["groups"][BLOCKED]
        lines.append(
            f"  flusso analizzato ({cov['total_rows']} righe, "
            f"{cov['total_playable']} giocabili): ammesse {allowed['n']} righe "
            f"({(allowed['share_rows'] or 0)*100:.0f}%) con {allowed['playable']} giocabili "
            f"| bloccate {blocked['n']} righe con {blocked['playable']} giocabili")
        if allowed["playable"] == 0 and blocked["playable"] > 0:
            lines.append("  🔴 il gate spegnerebbe la corsia: nessun segnale giocabile "
                         "nelle leghe ammesse")
    open_rows = d.get("blocked_open") or []
    if open_rows:
        lines.append(f"  ⏳ in gioco e bloccate subito: {len(open_rows)}")
        for r in open_rows[:5]:
            lines.append(f"    - {r['league']}: {r['match_id']} esito {r['outcome']} "
                         f"@ {r['price']} stake {r['stake']}")
    lines.append(f"  {'✅' if d.get('reliable') else '⚠️'} {d.get('caveat', '')}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Misura l'impatto del gate leghe sulla corsia auto-bet (read-only)")
    parser.add_argument("--days", type=float, default=None,
                        help="finestra in giorni (default: tutto lo storico)")
    parser.add_argument("--source", choices=("bets", "predictions", "all"),
                        default="bets")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    data = measure(days=args.days, source=args.source)
    print(json.dumps(data, indent=1, ensure_ascii=False) if args.json
          else format_report(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
