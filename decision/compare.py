"""decision/compare.py — Confronto MISURATO fra le due strade (shadow, sola lettura).

Perche' esiste (16/09/2026): dal 15/09 la catena `decision/` valuta in **shadow
mode** gli stessi segnali che la corsia `auto_bet` sta per giocare, e dal 16/09
la **Shadow Validation** ne persiste il verdetto sul ledger `decisions`. Il
confronto fra i due percorsi, pero', non era misurabile: due registri separati
(`decisions` = cosa DECIDE la catena, `bets` = cosa la corsia PIAZZA) e nessuna
giunzione. Qui la giunzione diventa un numero, cosi' il passo 3 (sostituire
l'esecuzione) puo' essere deciso su dati invece che su impressioni.

Le due strade, in una riga ciascuna:

    catena  : decisions   — verdetto + motivo (ReasonCode) + stato di convalida
    corsia  : bets        — la puntata ESISTE solo se e' stata piazzata (sim o live)

La chiave di giunzione e' **(match_id, esito canonico)**: l'esito della corsia
viene normalizzato con la STESSA funzione dell'adapter
(`decision.adapters.canonical_outcome`), quindi un ledger misto (nomi squadra
nelle righe vecchie, `1`/`X`/`2` in quelle nuove) non falsa il confronto.

I cinque casi (esaustivi, nessuna riga sparisce in silenzio):

| caso                 | catena                       | corsia           |
|---|---|---|
| `both_play`          | approva, stake eseguibile     | ha puntato       |
| `blocked_played`     | rifiuta / non eseguibile      | ha puntato       |
| `would_play_skipped` | approva, stake eseguibile     | NON ha puntato   |
| `agree_skip`         | rifiuta                       | non ha puntato   |
| `unobserved`         | nessuna riga                  | ha puntato       |

`blocked_played` e' il caso che conta di piu': **puntate reali che la catena
nuova avrebbe rifiutato**, col motivo del rifiuto e col P/L che hanno
realizzato. `would_play_skipped` e' il rovescio: **opportunita' che la catena
avrebbe giocato e la corsia ha saltato** (il motivo della corsia vive nei log
del giro e nel monitor liquidita': quando c'e' un evento di scarto per quella
partita, viene allegato come indizio).

⚠️ **Che cosa NON e' ancora misurabile.** Le righe di `decisions` esistono solo
per i segnali valutati mentre la shadow persistence era accesa (16/09 15:00 UTC):
le puntate della corsia senza riga corrispondente finiscono in `unobserved` e
**non** vengono contate come divergenze — non si puo' dire "la catena l'avrebbe
bloccata" di un segnale che la catena non ha mai visto. Il campo `caveat`
dichiara il campione; come per `league_gate_impact`, il verdetto sul P/L si
emette solo sopra `MIN_RELIABLE_CLOSED` chiusure.

Garanzie (verificate dai test):

- **sola lettura**: connessione SQLite `mode=ro`, nessun INSERT/UPDATE/DELETE
  nel sorgente, nessun ordine, nessuna riga scritta;
- **zero costi**: nessun accesso a the-odds-api/SX e nessun credito consumato
  (il modulo non importa `odds_api`, `sx_signals` ne' alcun provider);
- **fail-safe**: un DB assente/illeggibile o una tabella mancante (volume di un
  deploy precedente) ritornano un dizionario con `error`, mai un'eccezione.

Diagnostica, non decisionale: nessuna funzione qui autorizza, blocca o modifica
una puntata.

CLI: `venv/bin/python -m decision compare [--days N | --all] [--json]`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("decision.compare")

COMPARE_ENABLED_ENV = "DECISION_COMPARE_ENABLED"
COMPARE_DAYS_ENV = "DECISION_COMPARE_DAYS"

#: Finestra di default: la stessa del registro shadow (una settimana) — abbastanza
#: da coprire i segnali generati prima del kickoff, corta abbastanza da non
#: mescolare la misura di ieri con quella di un mese fa.
DEFAULT_WINDOW_DAYS = 7.0

#: Sotto questo numero di PUNTATE CHIUSE per cella l'P/L non e' conclusivo:
#: meglio dirlo che far credere a un ROI. Stessa soglia di `league_gate_impact`.
MIN_RELIABLE_CLOSED = 20

# Celle del confronto (chiavi stabili: finiscono nei report e nei test).
BOTH_PLAY = "both_play"
BLOCKED_PLAYED = "blocked_played"
WOULD_PLAY_SKIPPED = "would_play_skipped"
AGREE_SKIP = "agree_skip"
UNOBSERVED = "unobserved"
CELLS = (BOTH_PLAY, BLOCKED_PLAYED, WOULD_PLAY_SKIPPED, AGREE_SKIP, UNOBSERVED)


def compare_enabled(value: Optional[str] = None) -> bool:
    """Il confronto gira nel job periodico? (default si: e' sola lettura)."""
    raw = os.getenv(COMPARE_ENABLED_ENV) if value is None else value
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def window_days(value: Optional[Any] = None) -> float:
    """Finestra in giorni (env `DECISION_COMPARE_DAYS`, default 7)."""
    raw = os.getenv(COMPARE_DAYS_ENV) if value is None else value
    try:
        days = float(raw)                       # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS
    return days if days > 0 else DEFAULT_WINDOW_DAYS


def _db_path() -> Path:
    from config import DATA_DIR
    return Path(DATA_DIR) / "quotaverace.db"


def _connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    """Connessione **READ-ONLY**: questa misura non scrive mai."""
    target = Path(path) if path else _db_path()
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _cutoff(days: Optional[float]) -> Optional[str]:
    if not days:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=float(days))).isoformat()


def key_of(match_id: Any, outcome: Any) -> str:
    """Chiave di giunzione del confronto (match + esito canonico)."""
    return f"{str(match_id or '').strip()}|{str(outcome or '').strip().upper()}"


def chain_would_play(row: Dict[str, Any]) -> bool:
    """La catena avrebbe piazzato l'ordine per questa riga?

    E' la STESSA condizione che `engine.plan_for_resolved` usa per emettere
    `place_order`: verdetto `approve` **e** stake eseguibile. Il `mode` non
    entra di proposito: qui si misura se il GATE avrebbe fatto passare il
    segnale, non quale comando sarebbe stato emesso in simulazione.
    """
    return (str(row.get("verdict") or "") == "approve"
            and bool(row.get("stake_executable")))


# ---------------------------------------------------------------------------
# Lettura dei due ledger (read-only)
# ---------------------------------------------------------------------------

def _chain_rows(conn: sqlite3.Connection, days: Optional[float]) -> List[dict]:
    """Righe del ledger `decisions` (una per segnale valutato dalla catena)."""
    since = _cutoff(days)
    sql = ("SELECT d.record_id, d.signal_id, d.match_id, d.league, d.outcome, "
           "d.selection_label, d.price, d.ev, d.stake, d.stake_executable, "
           "d.verdict, d.reason, d.status, d.mode, d.confidence, "
           "d.esito_finale, d.profit, d.created_at, "
           "COALESCE(m.commence_time, d.kickoff, d.created_at) AS when_ts, "
           "m.home_team, m.away_team "
           "FROM decisions d LEFT JOIN matches m ON m.id = d.match_id "
           "WHERE (d.market = '1X2' OR d.market IS NULL OR d.market = '')")
    args: List[Any] = []
    if since:
        sql += " AND COALESCE(m.commence_time, d.kickoff, d.created_at) >= ?"
        args.append(since)
    out: List[dict] = []
    for r in conn.execute(sql, args).fetchall():
        row = dict(r)
        row["would_play"] = chain_would_play(row)
        row["closed"] = row.get("esito_finale") is not None
        out.append(row)
    return out


def _lane_rows(conn: sqlite3.Connection, days: Optional[float]) -> List[dict]:
    """Puntate della corsia (`bets`), esito NORMALIZZATO a 1/X/2.

    `bets.esito` porta l'esito canonico dal 09/09, ma le righe piu' vecchie
    possono contenere il nome della squadra (o `Over 2.5`): si normalizza con
    `adapters.canonical_outcome` (import pigro, stessa funzione dell'adapter) e
    si scartano le righe senza esito decidibile **contandole** (`undecidable`),
    cosi' un buco non passa per assenza di puntate.
    """
    from .adapters import canonical_outcome     # pigro: vedi docstring modulo

    since = _cutoff(days)
    sql = ("SELECT b.id, b.match_id, b.esito, b.price, b.stake, b.mode, "
           "b.status, b.profit, b.esito_finale, b.created_at, "
           "COALESCE(m.commence_time, b.created_at) AS when_ts, "
           "m.home_team, m.away_team, m.league "
           "FROM bets b LEFT JOIN matches m ON m.id = b.match_id "
           "WHERE (b.mercato = '1X2' OR b.mercato IS NULL OR b.mercato = '')")
    args: List[Any] = []
    if since:
        sql += " AND COALESCE(m.commence_time, b.created_at) >= ?"
        args.append(since)
    out: List[dict] = []
    for r in conn.execute(sql, args).fetchall():
        row = dict(r)
        outcome = canonical_outcome(str(row.get("esito") or ""),
                                    str(row.get("home_team") or ""),
                                    str(row.get("away_team") or ""))
        row["outcome"] = outcome or ""
        row["decidable"] = outcome is not None
        row["closed"] = row.get("esito_finale") is not None
        row["stake"] = float(row.get("stake") or 0.0)
        row["profit"] = float(row.get("profit") or 0.0)
        out.append(row)
    return out


def _assert_readable(conn: sqlite3.Connection) -> None:
    """Probe di lettura: un file che NON e' un database SQLite fallisce qui.

    Senza questo controllo `_skipped_tables` (tollerante per definizione: il
    volume di un deploy precedente puo' non avere ancora `decisions`) tratterebbe
    un file corrotto come "nessuna tabella" e la misura sembrerebbe VUOTA invece
    che rotta — il tipo di silenzio che falsa una fase di misura.
    """
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()


def _skipped_tables(conn: sqlite3.Connection) -> List[str]:
    """Tabelle mancanti fra quelle che servono (DB di un deploy precedente)."""
    missing = []
    for table in ("decisions", "bets", "matches"):
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
        except sqlite3.Error:
            missing.append(table)
            continue
        if row is None:
            missing.append(table)
    return missing


# ---------------------------------------------------------------------------
# Aggregati
# ---------------------------------------------------------------------------

def _chain_bucket(rows: List[dict]) -> dict:
    """Aggregato di righe della CATENA: `profit` e' per UNITA' di stake.

    Stessa convenzione di `tracker._decision_bucket`: `pnl_units` e' la somma
    dei P/L per unita' (ROI flat), `pnl_staked` pesa ogni riga per lo stake
    pianificato (ROI sul capitale che sarebbe stato investito).
    """
    closed = [r for r in rows if r.get("closed")]
    won = [r for r in closed if str(r.get("esito_finale")) == "won"]
    stake = sum(float(r.get("stake") or 0.0) for r in closed)
    pnl_units = sum(float(r.get("profit") or 0.0) for r in closed)
    pnl_staked = sum(float(r.get("profit") or 0.0) * float(r.get("stake") or 0.0)
                     for r in closed)
    ev_sum = sum(float(r.get("ev") or 0.0) for r in closed)
    return {
        "n": len(rows),
        "closed": len(closed),
        "open": len(rows) - len(closed),
        "won": len(won),
        "lost": len(closed) - len(won),
        "hit_rate": round(len(won) / len(closed) * 100, 2) if closed else None,
        "stake": round(stake, 2),
        "pnl_units": round(pnl_units, 4),
        "pnl_staked": round(pnl_staked, 4),
        "roi_flat": round(pnl_units / len(closed) * 100, 2) if closed else None,
        "roi_staked": round(pnl_staked / stake * 100, 2) if stake else None,
        "avg_ev": round(ev_sum / len(closed) * 100, 2) if closed else None,
        "unit": "per_unità_di_stake",
    }


def _lane_bucket(rows: List[dict]) -> dict:
    """Aggregato di PUNTATE della corsia: `profit` e' in VALUTA (USDC)."""
    closed = [r for r in rows if r.get("closed")]
    won = [r for r in closed if str(r.get("esito_finale")) == "won"]
    stake = sum(r["stake"] for r in closed)
    pnl = sum(r["profit"] for r in closed)
    return {
        "n": len(rows),
        "closed": len(closed),
        "open": len(rows) - len(closed),
        "won": len(won),
        "lost": len(closed) - len(won),
        "hit_rate": round(len(won) / len(closed) * 100, 2) if closed else None,
        "stake": round(stake, 2),
        "pnl": round(pnl, 4),
        "roi": round(pnl / stake * 100, 2) if stake else None,
        "unit": "valuta",
    }


def _counts(rows: List[dict], field: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        value = str(row.get(field) or "?")
        out[value] = out.get(value, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Indizi sul perche' la corsia ha saltato (monitor liquidita', sola lettura)
# ---------------------------------------------------------------------------

def lane_skip_hints(keys: set, days: Optional[float]) -> Dict[str, str]:
    """{chiave: motivo} per le partite di cui il monitor liquidita' ha traccia.

    La corsia che salta un ordine **non scrive nulla** nel ledger delle puntate
    (una riga in `bets` esiste solo se la posizione e' stata presa): senza
    questo indizio resterebbe un buco muto. Il monitor degli scarti
    (`liquidity_monitor`, JSONL sul volume) registra invece il motivo
    (`depth_vs_stake` per book sottile, `depth_exec_*` allo scan): qui si
    aggancia, fail-safe (nessun import o log illeggibile -> nessun indizio).
    """
    if not keys:
        return {}
    try:
        from liquidity_monitor import iter_events
        events = iter_events(days)
    except Exception as exc:                    # indizio, mai un blocco
        logger.debug("compare: indizi di scarto non leggibili (%s)", exc)
        return {}
    hints: Dict[str, str] = {}
    for event in events:                        # dal piu' recente
        key = key_of(event.get("match_id"), event.get("esito"))
        if key in keys and key not in hints:
            hints[key] = str(event.get("reason") or event.get("kind") or "?")
    return hints


# ---------------------------------------------------------------------------
# Misura
# ---------------------------------------------------------------------------

def measure(days: Optional[float] = None, conn: Optional[sqlite3.Connection] = None,
            path: Optional[str | Path] = None, *, with_hints: bool = True) -> dict:
    """Confronta le due strade. NON solleva: su errore ritorna `error`.

    `days=None` usa la finestra di default (`DECISION_COMPARE_DAYS`, 7 giorni);
    `days=0` (o negativo) significa "tutto lo storico" (`_cutoff` non taglia).
    """
    if days is None:
        days = window_days()
    out: Dict[str, Any] = {
        "readonly": True,
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cells": list(CELLS),
        "min_reliable_closed": MIN_RELIABLE_CLOSED,
    }
    own = conn is None
    try:
        conn = conn or _connect(path)
        _assert_readable(conn)                   # file corrotto -> `error`
        missing = _skipped_tables(conn)
        out["missing_tables"] = missing
        chain = _chain_rows(conn, days) if "decisions" not in missing else []
        lane = _lane_rows(conn, days) if "bets" not in missing else []

        # Indice della catena per chiave: piu' righe sulla stessa chiave sono
        # un dato (dedup per segnale fallita) e vanno VISIBILI, non fuse.
        chain_by_key: Dict[str, List[dict]] = {}
        for row in chain:
            chain_by_key.setdefault(key_of(row.get("match_id"), row.get("outcome")),
                                    []).append(row)
        duplicate_keys = sum(1 for group in chain_by_key.values() if len(group) > 1)

        cells: Dict[str, List[dict]] = {name: [] for name in CELLS}
        matched_keys: set = set()
        for row in lane:
            if not row.get("decidable"):
                continue
            key = key_of(row.get("match_id"), row.get("outcome"))
            group = chain_by_key.get(key) or []
            matched_keys.add(key)
            if not group:
                # La catena non ha valutato questo segnale: non e' una
                # divergenza, e' un'osservazione mancante (vedi caveat).
                cells[UNOBSERVED].append(row)
                continue
            chain_row = _best_chain_row(group)
            if chain_row["would_play"]:
                cells[BOTH_PLAY].append(row)
            else:
                cells[BLOCKED_PLAYED].append({**row, "chain": chain_row})
        for key, group in chain_by_key.items():
            if key in matched_keys:
                continue
            chain_row = _best_chain_row(group)
            if chain_row["would_play"]:
                cells[WOULD_PLAY_SKIPPED].append(chain_row)
            else:
                cells[AGREE_SKIP].append(chain_row)

        would_play_skipped = cells[WOULD_PLAY_SKIPPED]
        hints: Dict[str, str] = {}
        if with_hints and would_play_skipped:
            # `days=0` significa "tutto lo storico" e va tradotto in None anche
            # per il monitor (la' una finestra a 0 taglierebbe ogni evento).
            hints = lane_skip_hints(
                {key_of(r.get("match_id"), r.get("outcome"))
                 for r in would_play_skipped},
                days if days and float(days) > 0 else None)

        both_play = cells[BOTH_PLAY]
        blocked = cells[BLOCKED_PLAYED]
        compared = len(both_play) + len(blocked) + len(would_play_skipped) + \
            len(cells[AGREE_SKIP])
        divergences = len(blocked) + len(would_play_skipped)

        out["chain"] = {
            "rows": len(chain),
            "by_verdict": _counts(chain, "verdict"),
            "by_reason": _counts(chain, "reason"),
            "by_status": _counts(chain, "status"),
            "would_play": sum(1 for r in chain if r["would_play"]),
            "duplicate_keys": duplicate_keys,
        }
        out["lane"] = {
            "bets": len(lane),
            "undecidable": sum(1 for r in lane if not r.get("decidable")),
            "by_mode": _counts(lane, "mode"),
            "by_status": _counts(lane, "status"),
        }
        out["agreement"] = {
            **{name: len(cells[name]) for name in CELLS},
            "compared": compared,
            "divergences": divergences,
            "divergence_rate": round(divergences / compared, 4) if compared else None,
        }
        out["blocked_played"] = [
            {
                "match_id": r.get("match_id"), "outcome": r.get("outcome"),
                "league": (r.get("chain") or {}).get("league") or r.get("league") or "",
                "verdict": (r.get("chain") or {}).get("verdict"),
                "reason": (r.get("chain") or {}).get("reason"),
                "status": (r.get("chain") or {}).get("status"),
                "price": r.get("price"), "stake": r.get("stake"),
                "mode": r.get("mode"), "closed": r.get("closed"),
                "esito_finale": r.get("esito_finale"), "profit": r.get("profit"),
            }
            for r in sorted(blocked, key=lambda r: -r["stake"])
        ]
        out["would_play_skipped"] = [
            {
                "match_id": r.get("match_id"), "outcome": r.get("outcome"),
                "league": r.get("league") or "", "price": r.get("price"),
                "stake": r.get("stake"), "ev": r.get("ev"),
                "status": r.get("status"), "reason": r.get("reason"),
                "closed": r.get("closed"), "esito_finale": r.get("esito_finale"),
                "profit": r.get("profit"),
                "hint": hints.get(key_of(r.get("match_id"), r.get("outcome")), ""),
            }
            for r in sorted(would_play_skipped,
                            key=lambda r: -(float(r.get("ev") or 0.0)))
        ]
        # Costo del gate per MOTIVO: e' la tabella che serve per decidere se
        # allineare o no la corsia alla catena.
        by_reason: Dict[str, dict] = {}
        for r in blocked:
            reason = str((r.get("chain") or {}).get("reason") or "?")
            by_reason.setdefault(reason, []).append(r)
        out["by_reason"] = {
            reason: _lane_bucket(group) for reason, group in sorted(by_reason.items())
        }
        out["settled"] = {
            BOTH_PLAY: _lane_bucket(both_play),
            BLOCKED_PLAYED: _lane_bucket(blocked),
            # Non giocate dalla corsia: il P/L e' quello che la catena avrebbe
            # realizzato, per UNITA' di stake (nessun denaro e' stato messo).
            WOULD_PLAY_SKIPPED: _chain_bucket(would_play_skipped),
        }
        closed_blocked = out["settled"][BLOCKED_PLAYED]["closed"]
        closed_both = out["settled"][BOTH_PLAY]["closed"]
        out["reliable"] = (closed_blocked + closed_both) >= MIN_RELIABLE_CLOSED
        unobserved = len(cells[UNOBSERVED])
        out["caveat"] = (
            f"campione: {closed_both} puntate chiuse concordate, "
            f"{closed_blocked} chiuse che la catena avrebbe rifiutato "
            f"({len(blocked)} in tutto) — "
            + ("sopra" if out["reliable"] else "sotto")
            + f" la soglia di affidabilita' ({MIN_RELIABLE_CLOSED}): "
            + ("il P/L e' utilizzabile." if out["reliable"] else
               "il P/L NON e' conclusivo.")
            + (f" {unobserved} puntate della corsia non hanno una riga nella "
               "catena (valutate fuori dalla finestra di persistenza shadow): "
               "restano fuori dal confronto." if unobserved else "")
        )
        return out
    except Exception as exc:                    # mai un'eccezione al chiamante
        logger.warning("compare: misura non disponibile (%s)", exc)
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _best_chain_row(group: List[dict]) -> dict:
    """La riga piu' recente del gruppo (l'ultima valutazione della catena)."""
    return sorted(group, key=lambda r: str(r.get("created_at") or ""))[-1]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt_bucket(bucket: dict) -> str:
    """Riga compatta di un aggregato (`unit` decide l'unita' di misura)."""
    if not bucket.get("n"):
        return "nessuna riga"
    if bucket.get("unit") == "valuta":
        roi = "n/d" if bucket.get("roi") is None else f"{bucket['roi']:+.1f}%"
        head = (f"n={bucket['n']} (chiuse {bucket['closed']}) "
                f"P/L {bucket.get('pnl', 0.0):+.2f} USDC | ROI {roi}")
    else:
        roi = "n/d" if bucket.get("roi_flat") is None else f"{bucket['roi_flat']:+.1f}%"
        head = (f"n={bucket['n']} (chiuse {bucket['closed']}) "
                f"P/L {bucket.get('pnl_units', 0.0):+.2f} per unita' | ROI flat {roi}")
    hit = bucket.get("hit_rate")
    return head + (f" | hit {hit:.0f}%" if hit is not None else "")


def format_report(data: Optional[dict] = None) -> str:
    """Report Telegram-friendly del confronto (nessun ordine, sola lettura)."""
    d = data if isinstance(data, dict) else measure()
    if d.get("error"):
        return f"⚠️ Confronto shadow non disponibile ({d['error']})"
    window = d.get("days")
    label = f"ultimi {int(window)}g" if window else "tutto lo storico"
    chain = d.get("chain") or {}
    lane = d.get("lane") or {}
    agr = d.get("agreement") or {}
    lines = [f"👥 *Confronto shadow* catena ↔ corsia ({label}, sola lettura)", ""]
    lines.append(f"• Catena: {chain.get('rows', 0)} righe | verdetti "
                 + " | ".join(f"{k} {v}" for k, v in sorted((chain.get('by_verdict') or {}).items()))
                 + f" | avrebbe giocato {chain.get('would_play', 0)}")
    statuses = chain.get("by_status") or {}
    if statuses:
        lines.append("  convalida: " + " | ".join(
            f"{k} {v}" for k, v in sorted(statuses.items())))
    lines.append(f"• Corsia: {lane.get('bets', 0)} puntate ("
                 + " | ".join(f"{k} {v}" for k, v in sorted((lane.get('by_mode') or {}).items()))
                 + ")" + (f" | {lane['undecidable']} esito non decidibile"
                          if lane.get("undecidable") else ""))
    lines.append(f"• Accordo: entrambe giocano *{agr.get(BOTH_PLAY, 0)}* | "
                 f"catena blocca ma corsia gioca *{agr.get(BLOCKED_PLAYED, 0)}* | "
                 f"catena gioca ma corsia salta *{agr.get(WOULD_PLAY_SKIPPED, 0)}* | "
                 f"entrambe fuori {agr.get(AGREE_SKIP, 0)}")
    if agr.get(UNOBSERVED):
        lines.append(f"  non confrontabili: {agr[UNOBSERVED]} puntate senza riga "
                     "nella catena")
    rate = agr.get("divergence_rate")
    if rate is not None:
        lines.append(f"  divergenza: {agr.get('divergences', 0)}/"
                     f"{agr.get('compared', 0)} ({rate * 100:.0f}% dei casi confrontabili)")

    settled = d.get("settled") or {}
    lines.append("")
    lines.append(f"• Celle chiuse — entrambe giocano: "
                 f"{_fmt_bucket(settled.get(BOTH_PLAY) or {})}")
    lines.append(f"• La catena avrebbe rifiutato (puntate REALI): "
                 f"{_fmt_bucket(settled.get(BLOCKED_PLAYED) or {})}")
    reasons = d.get("by_reason") or {}
    for reason, bucket in sorted(reasons.items(),
                                 key=lambda kv: -kv[1].get("n", 0))[:6]:
        lines.append(f"    - {reason}: {_fmt_bucket(bucket)}")
    skipped = d.get("would_play_skipped") or []
    lines.append(f"• La corsia ha saltato (la catena avrebbe giocato): "
                 f"{_fmt_bucket(settled.get(WOULD_PLAY_SKIPPED) or {})}")
    for row in skipped[:5]:
        hint = f" — {row['hint']}" if row.get("hint") else ""
        lines.append(f"    - {row.get('match_id')} {row.get('outcome')} "
                     f"@ {row.get('price')} (EV {float(row.get('ev') or 0) * 100:+.1f}%)"
                     f"{hint}")
    missing = d.get("missing_tables") or []
    if missing:
        lines.append(f"  ⚠️ tabelle assenti nel DB: {', '.join(missing)}")
    lines.append("")
    lines.append(f"  {'✅' if d.get('reliable') else '⚠️'} {d.get('caveat', '')}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Confronto shadow catena ↔ corsia (read-only, zero crediti)")
    parser.add_argument("--days", type=float, default=None,
                        help=f"finestra in giorni (default: {DEFAULT_WINDOW_DAYS:.0f}, "
                             f"env {COMPARE_DAYS_ENV})")
    parser.add_argument("--all", action="store_true",
                        help="tutto lo storico invece della finestra")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    data = measure(days=0 if args.all else args.days)
    print(json.dumps(data, indent=1, ensure_ascii=False) if args.json
          else format_report(data))
    return 1 if data.get("error") else 0


__all__ = [
    "AGREE_SKIP", "BLOCKED_PLAYED", "BOTH_PLAY", "CELLS", "COMPARE_DAYS_ENV",
    "COMPARE_ENABLED_ENV", "DEFAULT_WINDOW_DAYS", "MIN_RELIABLE_CLOSED",
    "UNOBSERVED", "WOULD_PLAY_SKIPPED", "chain_would_play", "compare_enabled",
    "format_report", "key_of", "lane_skip_hints", "main", "measure",
    "window_days",
]


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
