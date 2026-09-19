"""multi_market.py — Over/Under e Asian Handicap: dal book SX al ledger, alla corsia ordini.

Perche' questo modulo (19/09/2026): `sx_signals.py` copre SOLO il 1X2. SX Bet
pubblica anche i mercati a LINEA (type 2 = Over/Under, type 3 = Asian Handicap)
e il ledger multi-mercato `tracker.market_quotes` esiste dalla fase 1 — qui i
due pezzi si collegano e i calcoli di Poisson (`poisson_engine`) diventano
candidati giocabili.

Catena completa, in quattro passi:

1. **INGESTIONE** (`ingest`): discovery dei mercati SX type 2/3 (API PUBBLICA:
   zero chiavi, zero crediti, zero ordini), order book taker con la STESSA
   lettura di `sx_signals` (`_books_parallel`), righe validate dal CONTRATTO
   (`decision.market.parse_quote`, schema 2.0: mercato, linea, provenienza) e
   upsert sul ledger `tracker.market_quotes` (`save_market_quotes`).

2. **ANALISI** (`analyze_fixture`): per OGNI (mercato, linea) del ledger, la
   probabilita' del modello da Poisson — `ou_outcome_probs` per l'OU e
   `ah_outcome_probs` per l'AH, **push-aware** (linea intera con totale
   esattamente uguale alla linea = puntata restituita, P/L 0; quarter line =
   due mezze puntate) — devig a due esiti del mercato (`market_implied`) e
   blend modello+mercato come per il 1X2 (`adjusted_probability`).
   L'EV e' calcolato in modo esatto, non dalla probabilita' "efficace":
   `EV = p_win x (quota - 1) - p_lose` (il push vale 0).

3. **LEDGER** (`scan`): i candidati finiscono in `predictions` (mercato `OU` /
   `AH`, esito in formato ledger `Over 2.5` / `Home -0.75`) con lo stesso
   tiering del 1X2 (value / strong_value / rejected) — cosi' il settlement di
   `tracker` li salda e la calibrazione per mercato li misura.

4. **ORDINI** (`live_picks` + `order_target`): la corsia esecutiva legge dal
   ledger i soli pick APERTI in fascia, applica di nuovo il gate di lega e
   restituisce il bersaglio per `execution_engine.resolve_market_for`
   (mercato + LINEA + lato). `auto_bet` la concatena alla sua selezione 1X2:
   tutti i guardrail (T-60, stop-loss, cap, feed, dedup) valgono identici.

INTERRUTTORI LIVE PER MERCATO (direttiva del proprietario, 19/09/2026):

    ENABLE_LIVE_AH=1   -> l'Asian Handicap puo' piazzare ORDINI REALI;
    ENABLE_LIVE_OU=0   -> l'Over/Under resta in SHADOW/TELEMETRIA: i segnali
                          si generano, si registrano e si misurano, ma NON
                          diventano ordini (il leak storico sul mercato OU,
                          -6.8% su 924 bet, va rimisurato sulla corsia nuova
                          prima di rimetterci denaro).

Gli interruttori vivono SOLO qui e `live_picks` e' l'unico punto che li legge:
una corsia spente non puo' accendere ordini per distrazione di un chiamante.

Regole del modulo:

1. **Fail-closed**: senza prezzo, senza linea o senza lato riconoscibile non
   nasce alcun candidato (mai un segnale su un mercato ambiguo).
2. **Sola lettura verso l'exchange**: le scritture sul ledger passano dal CRUD
   di `tracker` (`save_market_quotes`, `save_prediction`), mai da SQL qui.
3. **Nessuna divergenza**: le soglie di liquidita' sono gli STESSI nomi di env
   di `sx_signals`/`auto_bet` (un tripwire verifica i default), la devig e il
   blend sono quelli di `market_calib`, il tier e' quello di `value_filter`.
"""

from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from market_calib import MARKET_EDGE_STRONG, market_implied
from poisson_engine import ah_outcome_probs, expected_goals, ou_outcome_probs
from value_filter import (
    EV_MIN,
    MIN_FAVOURITE_MARKET_PROB,
    ODDS_MAX,
    ODDS_MIN,
    adjusted_probability,
    compute_ev,
    get_signal_tier,
    is_sane,
    league_allowed,
)

logger = logging.getLogger("multi_market")


# ---------------------------------------------------------------------------
# Configurazione (tutto da env: nessun redeploy per cambiare una soglia)
# ---------------------------------------------------------------------------

def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


#: Interruttori LIVE per mercato (vedi docstring). AH acceso: e' il mercato
#: scelto dal proprietario per ripartire a monetizzare. OU spento: shadow.
ENABLE_LIVE_AH = _env_flag("ENABLE_LIVE_AH", True)
ENABLE_LIVE_OU = _env_flag("ENABLE_LIVE_OU", False)

#: Identita' del feed nel contratto (`gateway_id` non vuoto e' obbligatorio).
GATEWAY_ID = os.getenv("MM_GATEWAY_ID", "sxbet-multi")
SOURCE = "sxbet"

#: Finestra dei fixture candidati (identica a sx_signals/auto_bet: 24h).
HOURS_AHEAD = _env_float("MM_HOURS_AHEAD", 24.0)
MIN_MINUTES_TO_START = _env_int("MM_MIN_MINUTES_TO_START", 15)
MAX_RAW_MARKETS = _env_int("MM_MAX_RAW_MARKETS", 400)
#: Quante linee diverse analizzare per mercato e partita. SX ne offre molte
#: (OU 0.5..8.5): analizzarle tutte riempirebbe il ledger di righe che nessuno
#: gioca. Le linee principali/gia' piu' liquide vengono prima.
MAX_LINES_PER_MARKET = _env_int("MM_MAX_LINES_PER_MARKET", 12)

#: Soglie di liquidita' (STESSI nomi/env di sx_signals e auto_bet: la taratura
#: dell'11/09 e' una sola in tutto il progetto).
MIN_DEPTH_USDC = _env_float("SX_MIN_DEPTH_USDC", 25.0)
MIN_LEG_DEPTH_USDC = _env_float("SX_MIN_LEG_DEPTH_USDC", 5.0)
MIN_EXEC_DEPTH_USDC = _env_float("SX_MIN_EXEC_DEPTH_USDC", 25.0)
MIN_INV_SUM, MAX_INV_SUM = 0.98, 1.08

#: I mercati gestiti qui e il type id nativo SX (vedi decision.market).
MARKETS: Tuple[str, ...] = ("OU", "AH")
SX_TYPE_IDS: Dict[str, str] = {"OU": "2", "AH": "3"}
_NATIVE_TYPE_TO_MARKET = {v: k for k, v in SX_TYPE_IDS.items()}


def live_markets() -> Tuple[str, ...]:
    """I mercati che possono piazzare ORDINI REALI adesso (interruttori)."""
    out = []
    if ENABLE_LIVE_AH:
        out.append("AH")
    if ENABLE_LIVE_OU:
        out.append("OU")
    return tuple(out)


# ---------------------------------------------------------------------------
# Helper di formato: linea, esito di ledger, lato
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def line_key(line: Any) -> str:
    """Chiave canonica della linea per il ledger (`2.5`, `-0.75`)."""
    try:
        return f"{float(line):g}"
    except (TypeError, ValueError):
        return ""


def parse_line(value: Any) -> Optional[float]:
    """Prima linea numerica nel testo ('Over 3.25' -> 3.25). None se assente.

    Zero E' una linea valida per l'Asian Handicap (handicap pari): il valore 0
    non viene mai confuso con "assente".
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    match = _LINE_RE.search(str(value))
    if not match:
        return None
    try:
        return float(match.group(0))
    except (TypeError, ValueError):
        return None


def ledger_esito(market_type: str, selection: str, line: Any) -> str:
    """Esito nel formato che `tracker` sa saldare (e `ml_audit` riconosce).

    OU -> 'Over 2.5' / 'Under 3.25' (la linea si legge dall'esito: il
    settlement e' line-aware dal 19/09).
    AH -> 'Home -0.75' / 'Away +0.25': la linea e' quella del LATO (l'AH del
    ledger e' sempre dal punto di vista della squadra giocata).
    """
    value = float(line)
    if str(market_type).upper() == "OU":
        side = "Over" if str(selection) == "over" else "Under"
        return f"{side} {value:g}"
    home = str(selection) == "1"
    side_line = value if home else -value
    sign = "+" if side_line >= 0 else ""
    return f"{'Home' if home else 'Away'} {sign}{side_line:g}"


def sx_line_of_esito(esito: str) -> Optional[float]:
    """Linea dal punto di vista di teamOne a partire dall'esito di ledger.

    Inversa di `ledger_esito` per l'AH: 'Home -0.75' -> -0.75,
    'Away +0.25' -> -0.25.
    """
    parts = str(esito or "").split()
    if len(parts) < 2:
        return parse_line(esito)
    line = parse_line(parts[1])
    if line is None:
        return None
    side = parts[0].strip().lower()
    if side.startswith("away"):
        return -line
    if side.startswith("home"):
        return line
    return None


def order_target(pick: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Bersaglio d'ordine per `execution_engine.resolve_market_for`.

    Ritorna {"market_type", "line" (dal punto di vista di teamOne), "side"}
    dove per l'OU `side` e' 'over'/'under' e per l'AH 'home'/'away'. None se
    il pick non e' riconducibile a un mercato a linea (fail-closed).
    """
    market_type = str(pick.get("mercato") or pick.get("market") or "").upper()
    esito = str(pick.get("esito_key") or pick.get("esito") or "")
    if market_type == "OU":
        side = "over" if esito.strip().lower().startswith("over") else (
            "under" if esito.strip().lower().startswith("under") else None)
        line = parse_line(esito)
        if side is None or line is None:
            return None
        return {"market_type": "OU", "line": line, "side": side}
    if market_type == "AH":
        line = sx_line_of_esito(esito)
        if line is None:
            return None
        return {"market_type": "AH", "line": line,
                "side": "home" if esito.strip().lower().startswith("home")
                else "away"}
    return None


def _clean_team(name: Any) -> str:
    """Nome squadra senza la linea attaccata ('Cagliari -0.75' -> 'Cagliari')."""
    return _LINE_RE.sub(" ", str(name or "")).strip(" -+")


def _same_team(a: str, b: str) -> bool:
    """Confronto squadre con il resolver unico del progetto (fail-closed)."""
    if not a or not b:
        return False
    try:
        from team_names import same_team
        return bool(same_team(a, b))
    except Exception:
        return a.strip().lower() == b.strip().lower()


def outcome_sides(market_type: str, outcome_one: Any,
                  home: str, away: str) -> Optional[Tuple[str, str]]:
    """Esiti canonici di outcomeOne/outcomeTwo nel mercato binario SX.

    OU: il nome dell'esito dice il lato ('Over 2.5' / 'Under 2.5').
    AH: outcomeOne e' la squadra a cui si applica la linea; la selection 1 e'
    teamOne, la 2 teamTwo. Senza riconoscimento -> None (mai indovinare).
    """
    mt = str(market_type).upper()
    o1 = str(outcome_one or "")
    if mt == "OU":
        low = o1.strip().lower()
        if low.startswith("over") or " over" in low:
            return "over", "under"
        if low.startswith("under") or " under" in low:
            return "under", "over"
        return None
    clean = _clean_team(o1)
    if _same_team(clean, home):
        return "1", "2"
    if _same_team(clean, away):
        return "2", "1"
    return None


# ---------------------------------------------------------------------------
# 1. INGESTIONE: mercati SX type 2/3 -> contratto -> ledger `market_quotes`
# ---------------------------------------------------------------------------

#: Limiti di plausibilita' di una linea presa da un CAMPO della fonte (non dai
#: nomi): SX potrebbe esprimerla in unita' scalate, quindi si accetta solo un
#: valore che assomigli a una linea di gol (|v| <= 12). Oltre -> si ignora.
_LINE_FIELD_MAX = 12.0


def _market_line(m: Dict[str, Any]) -> Optional[float]:
    """Linea di un mercato SX: PRIMA dai nomi degli esiti, poi dal campo.

    I nomi sono la fonte piu' affidabile osservata sull'API pubblica
    ('Over 2.5' / 'Cagliari -0.75'): il campo esplicito si usa solo se i nomi
    non portano numeri e il valore e' plausibile. Zero e' una linea valida
    (handicap pari): mai confuso con 'assente'.
    """
    for key in ("outcomeOneName", "outcomeTwoName"):
        value = parse_line(m.get(key))
        if value is not None:
            return value
    for key in ("line", "lineValue", "line_value", "handicap"):
        raw = m.get(key)
        if raw is None:
            continue
        value = parse_line(raw)
        if value is not None and abs(value) <= _LINE_FIELD_MAX:
            return value
    return None


def _discover_type(provider: Any, type_id: str,
                   max_markets: int) -> List[Dict[str, Any]]:
    """Una pagina (o piu') di /markets/active per UN type id (come sx_signals)."""
    out: List[Dict[str, Any]] = []
    pagination_key: Optional[str] = None
    while len(out) < max_markets:
        params: Dict[str, Any] = {"sportIds": "5", "type": str(type_id),
                                  "pageSize": 100}
        if pagination_key:
            params["paginationKey"] = pagination_key
        try:
            data = provider._get("markets/active", params=params)
        except Exception as exc:
            logger.warning("multi_market: discovery type %s fallita: %s",
                           type_id, exc)
            break
        d = data.get("data") if isinstance(data, dict) else {}
        markets = (d or {}).get("markets") or []
        for m in markets:
            if isinstance(m, dict):
                out.append(m)
        pagination_key = (d or {}).get("nextKey")
        if not pagination_key or not markets:
            break
    return out[:max_markets]


def discover(provider: Any, *, types: Optional[Sequence[str]] = None,
             max_markets: int = MAX_RAW_MARKETS,
             now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Mercati OU/AH calcio SX nella finestra, normalizzati per l'analisi.

    Ogni record porta: evento (sportXeventId), kickoff, squadre, lega, mercato,
    LINEA, mainLine e il market hash. Fail-soft: un record malformato viene
    contato e saltato, il giro continua.
    """
    from sx_signals import _kickoff_utc_ms          # import pigro (riuso)

    wanted = tuple(types or MARKETS)
    per_type = max(1, max_markets // max(1, len(wanted)))
    now_ms = int((now or datetime.now(timezone.utc)).timestamp() * 1000)
    lo = now_ms - 60 * 60 * 1000                     # -1h: live appena iniziati
    hi = now_ms + HOURS_AHEAD * 3600 * 1000
    records: List[Dict[str, Any]] = []
    skipped = 0
    for market_type in wanted:
        type_id = SX_TYPE_IDS.get(str(market_type).upper())
        if not type_id:
            continue
        for m in _discover_type(provider, type_id, per_type):
            event_id = m.get("sportXeventId")
            kickoff_ms = _kickoff_utc_ms(m.get("gameTime"))
            home = str(m.get("teamOneName") or "").strip()
            away = str(m.get("teamTwoName") or "").strip()
            market_hash = m.get("marketHash")
            line = _market_line(m)
            if not (event_id and market_hash and home and away) \
                    or kickoff_ms is None or line is None \
                    or not (lo <= kickoff_ms <= hi):
                skipped += 1
                continue
            records.append({
                "event_id": str(event_id),
                "league_label": m.get("leagueLabel") or "",
                "kickoff_ms": kickoff_ms,
                "home": home, "away": away,
                "market_type": str(market_type).upper(),
                "line": float(line),
                "main_line": bool(m.get("mainLine")),
                "market_hash": str(market_hash),
                "outcome_one": m.get("outcomeOneName"),
                "outcome_two": m.get("outcomeTwoName"),
            })
    if skipped:
        logger.info("multi_market: %d mercati scartati in discovery "
                    "(linea/squadre/kickoff non utilizzabili)", skipped)
    records.sort(key=lambda r: r["kickoff_ms"])
    return records


def build_quote_rows(records: Sequence[Dict[str, Any]],
                     books: Dict[str, Any], *,
                     observed: Optional[datetime] = None,
                     gateway_id: str = GATEWAY_ID
                     ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Record discovery + order book -> righe validate dal CONTRATTO 2.0.

    Una riga per ESITO (2 per mercato binario). Il contratto (`parse_quote`) e'
    l'unico posto dove una quota viene validata: mercato/linea/coerenza, linea
    obbligatoria dove serve, versione schema. Una riga respinta non entra.

    Ritorna (righe, contatori). Non solleva mai: il feed non deve poter
    fermare il giro.
    """
    from decision.market import MARKET_SCHEMA_VERSION, parse_quote
    from sx_signals import _kickoff_iso

    when = observed or datetime.now(timezone.utc)
    rows: List[Dict[str, Any]] = []
    stats = {"built": 0, "rejected": 0, "no_book": 0, "no_side": 0,
             "incoherent": 0}
    for rec in records:
        sides = outcome_sides(rec["market_type"], rec.get("outcome_one"),
                              rec["home"], rec["away"])
        if sides is None:
            stats["no_side"] += 1
            continue
        book = books.get(rec["market_hash"]) or {}
        if book.get("error"):
            stats["no_book"] += 1
            continue
        sel_one, sel_two = sides
        kickoff = _kickoff_iso(rec["kickoff_ms"])
        prices: Dict[str, float] = {}
        for selection, index in ((sel_one, 1), (sel_two, 2)):
            info = book.get(index) or {}
            best = info.get("best") or {}
            price = best.get("price")
            if not price or float(price) <= 1.0:
                continue
            prices[selection] = float(price)
            row = {
                "schema_version": MARKET_SCHEMA_VERSION,
                "event_id": f"sx-{rec['event_id']}",
                "market": rec["market_type"],
                "market_type": rec["market_type"],
                "selection": selection,
                "odds": float(price),
                "timestamp": when.isoformat(),
                "source": SOURCE,
                "gateway_id": gateway_id,
                "line": rec["line"],
                "main_line": rec.get("main_line"),
                "origin": "native",
                "event_name": f"{rec['home']} - {rec['away']}",
                "league": rec.get("league_label") or "",
                "home": rec["home"], "away": rec["away"],
                "kickoff": kickoff,
                "selection_label": ledger_esito(rec["market_type"],
                                                selection, rec["line"]),
                "depth_usdc": float(info.get("depth") or 0.0),
                # Campi extra (ammessi dal contratto, non lo allargano):
                # servono al percorso d'ordine (market hash) e alla diagnosi.
                "market_hash": rec["market_hash"],
                "sport_x_event_id": rec["event_id"],
            }
            if len(prices) == 2:
                inv = sum(1.0 / p for p in prices.values())
                row["inv_sum"] = round(inv, 4)
                row["total_depth_usdc"] = round(
                    float((book.get(1) or {}).get("depth") or 0.0)
                    + float((book.get(2) or {}).get("depth") or 0.0), 2)
            try:
                quote = parse_quote(row, gateway_id=gateway_id)
            except Exception as exc:                 # riga non conforme
                stats["rejected"] += 1
                logger.debug("multi_market: quota respinta dal contratto "
                             "(%s %s %s): %s", rec["market_type"],
                             rec["line"], selection, exc)
                continue
            rows.append(quote.as_row())
            stats["built"] += 1
        if len(prices) == 2:
            inv = sum(1.0 / p for p in prices.values())
            if not (MIN_INV_SUM <= inv <= MAX_INV_SUM):
                # Mercato non coerente (book sporco o in movimento): le due
                # righe appena costruite restano fuori dal ledger.
                stats["incoherent"] += 1
                rows = rows[:-2]
                stats["built"] -= 2
    return rows, stats


def ingest(provider: Any = None, *, types: Optional[Sequence[str]] = None,
           max_markets: int = MAX_RAW_MARKETS,
           observed: Optional[datetime] = None, save: bool = True) -> Dict[str, Any]:
    """Discovery + book + contratto + upsert sul ledger `market_quotes`.

    Sola lettura verso SX (API pubblica: zero chiavi, zero crediti, zero
    ordini). Non solleva mai: un mercato sporco non deve fermare il giro.
    """
    from sx_signals import _books_parallel            # import pigro (riuso)

    summary: Dict[str, Any] = {"records": 0, "quotes": 0, "saved": 0,
                               "skipped": 0, "fixtures": 0, "error": None}
    try:
        if provider is None:
            from execution_engine import SxBetProvider
            provider = SxBetProvider()
        records = discover(provider, types=types, max_markets=max_markets,
                          now=observed)
        summary["records"] = len(records)
        if not records:
            logger.info("multi_market: nessun mercato OU/AH nella finestra")
            return summary
        hashes = [r["market_hash"] for r in records]
        books = _books_parallel(provider, hashes)
        rows, stats = build_quote_rows(records, books, observed=observed)
        summary["quotes"] = stats["built"]
        summary["skipped"] = (stats["rejected"] + stats["no_book"]
                              + stats["no_side"] + stats["incoherent"])
        if save and rows:
            from tracker import save_market_quotes
            result = save_market_quotes(rows) or {}
            summary["saved"] = int(result.get("saved") or 0)
            summary["fixtures"] = int(result.get("fixtures") or 0)
            summary["error"] = result.get("error")
        logger.info("multi_market: ingest %d mercati -> %d quote salvate "
                    "(%d scartate, %d fixture)", summary["records"],
                    summary["saved"], summary["skipped"], summary["fixtures"])
    except Exception as exc:                        # rete/discovery/DB
        logger.warning("multi_market: ingest fallita (%s)", exc)
        summary["error"] = str(exc)
    return summary


# ---------------------------------------------------------------------------
# 2. ANALISI: Poisson (push-aware) + devig + blend -> candidati
# ---------------------------------------------------------------------------

def _model_side(market_type: str, lam_h: float, lam_a: float, line: float,
                selection: str):
    """(p_win, p_push, p_lose) del modello per UN lato. None se non calcolabile.

    OU -> `ou_outcome_probs` (side 'over'/'under');
    AH -> `ah_outcome_probs` (linea dal punto di vista di teamOne, `side`
    della squadra giocata) come gia' fa la telemetria AH del progetto.
    """
    try:
        if str(market_type).upper() == "OU":
            return ou_outcome_probs(lam_h, lam_a, float(line),
                                    side=str(selection))
        return ah_outcome_probs(lam_h, lam_a, float(line),
                                side="home" if str(selection) == "1" else "away")
    except Exception as exc:
        logger.debug("multi_market: modello fallito (%s %s %s): %s",
                     market_type, line, selection, exc)
        return None


def _quotes_for(fixture_id: str) -> List[Dict[str, Any]]:
    """Righe del ledger per la partita (fail-safe: [] se il DB non risponde)."""
    try:
        from tracker import get_market_quotes
        return list(get_market_quotes(fixture_id=fixture_id) or [])
    except Exception as exc:
        logger.debug("multi_market: lettura ledger %s fallita: %s",
                     fixture_id, exc)
        return []


def _group_key(row: Dict[str, Any]) -> Tuple[str, str]:
    return (str(row.get("market_type") or "").upper(),
            str(row.get("line_key") or ""))


def _group_rank(group: List[Dict[str, Any]]) -> Tuple[int, float, float]:
    """Ordine dei gruppi: linea principale prima, poi la piu' liquida."""
    main = 1 if any(r.get("main_line") for r in group) else 0
    depth = max((float(r.get("liquidity") or 0.0) for r in group), default=0.0)
    line = float(group[0].get("line") or 0.0)
    return (-main, -depth, abs(line))


def analyze_fixture(fixture_id: str, lam_h: float, lam_a: float, *,
                    quotes: Optional[Sequence[Dict[str, Any]]] = None,
                    league: str = "",
                    max_lines: Optional[int] = None
                    ) -> List[Dict[str, Any]]:
    """Candidati OU/AH di UNA partita dalle quote del ledger `market_quotes`.

    Per OGNI (mercato, linea): devig a due esiti (`market_implied`), probabilita'
    del modello push-aware, blend (`adjusted_probability`) e filtri di sanita'
    (`is_sane`) — gli STESSI del 1X2, con `favourites_only=False` perche' il
    lato favorito qui e' gia' selezionato sotto (mercato a 2 esiti).

    `playable=True` solo per il lato scelto (favorito di mercato, quota in
    fascia, edge/EV minimi, libro profondo a sufficienza): e' l'unico che la
    corsia d'ordine considerera'.
    """
    rows = list(quotes) if quotes is not None else _quotes_for(fixture_id)
    limit = int(max_lines if max_lines is not None else MAX_LINES_PER_MARKET)
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        market_type, _key = _group_key(row)
        if market_type not in MARKETS or row.get("price") is None:
            continue
        groups.setdefault((market_type, _key), []).append(row)
    out: List[Dict[str, Any]] = []
    used: Dict[str, int] = {}
    for (market_type, _key), group in sorted(groups.items(),
                                             key=lambda item: _group_rank(item[1])):
        if used.get(market_type, 0) >= limit:
            continue
        line = group[0].get("line")
        if line is None:
            continue
        prices = {str(r.get("selection")): float(r["price"])
                  for r in group if r.get("price")}
        if len(prices) != 2:
            continue
        market = market_implied(prices)
        if not market:
            continue
        used[market_type] = used.get(market_type, 0) + 1
        depths = {str(r.get("selection")): float(r.get("liquidity") or 0.0)
                  for r in group}
        total_depth = sum(depths.values())
        market_ok = (total_depth >= MIN_DEPTH_USDC
                     and all(d >= MIN_LEG_DEPTH_USDC for d in depths.values()))
        cands: List[Dict[str, Any]] = []
        for selection, price in prices.items():
            probs = _model_side(market_type, lam_h, lam_a, float(line), selection)
            if probs is None:
                continue
            p_win, p_push, p_lose = probs
            market_prob = market.get(selection)
            # Probabilita' "efficace" (push = mezza vincita): e' la grandezza
            # confrontabile con la prob. fair devigata del mercato.
            model_eff = p_win + 0.5 * p_push
            prob = adjusted_probability(model_eff, market_prob, price,
                                        league=league)
            # EV ESATTO: il push restituisce lo stake (P/L 0), non e' una vincita.
            ev = p_win * (price - 1.0) - p_lose
            edge = (model_eff - market_prob) if market_prob is not None else None
            cands.append({
                "fixture_id": fixture_id, "market_type": market_type,
                "mercato": market_type, "line": float(line),
                "esito_key": ledger_esito(market_type, selection, line),
                "selection": selection,
                "quota": price, "price": price,
                "prob": prob, "prob_model": model_eff,
                "p_win": p_win, "p_push": p_push, "p_lose": p_lose,
                "market_prob": market_prob, "market_edge": edge, "ev": ev,
                "depth": depths.get(selection, 0.0),
                "total_depth": total_depth, "market_ok": market_ok,
                "league": league,
            })
        if len(cands) != 2:
            continue
        eligible = [c for c in cands
                    if c["market_prob"] is not None
                    and c["market_prob"] >= MIN_FAVOURITE_MARKET_PROB
                    and ODDS_MIN <= c["quota"] <= ODDS_MAX]
        chosen = max(eligible, key=lambda c: c["ev"]) if eligible else None
        for cand in cands:
            sane, reason = is_sane(cand["prob"], cand["quota"], cand["ev"],
                                   market_prob=cand["market_prob"],
                                   league=league, favourites_only=False)
            depth_ok = (market_ok and cand["depth"] >= MIN_EXEC_DEPTH_USDC)
            if cand is chosen and sane and depth_ok:
                cand["tier"] = get_signal_tier(cand["ev"], cand["market_edge"])
                cand["status"] = cand["tier"]
                cand["playable"] = cand["status"] in ("value", "strong_value",
                                                       "moderate")
            else:
                cand["playable"] = False
                cand["status"] = "rejected"
                cand["tier"] = "rejected"
                if not sane:
                    cand["reason"] = reason
                elif not depth_ok:
                    cand["reason"] = ("liquidita' insufficiente ("
                                      f"{cand['depth']:.1f} < "
                                      f"{MIN_EXEC_DEPTH_USDC:.1f} USDC al floor)")
                elif cand is not chosen:
                    cand["reason"] = "non e' il lato favorito della linea"
            out.append(cand)
    return out


# ---------------------------------------------------------------------------
# 3. LEDGER: candidati -> `predictions` (telemetria + settlement + calibrazione)
# ---------------------------------------------------------------------------

#: Quante righe del ledger multi-mercato leggere per la scansione. La tabella
#: e' potata (`prune_market_quotes`) e aggiornata in upsert: un tetto alto
#: basta e impedisce di leggere in memoria un volume patologico.
SCAN_LIMIT = _env_int("MM_SCAN_LIMIT", 5000)


def _window_iso(now: datetime) -> Tuple[str, str]:
    start = now.isoformat().replace("+00:00", "Z")
    end = (now + timedelta(hours=HOURS_AHEAD)).isoformat().replace("+00:00", "Z")
    return start, end


def _fixtures_with_quotes(now: datetime) -> List[Dict[str, Any]]:
    """Fixture con quote multi-mercato nella finestra (dal ledger, non da SX)."""
    try:
        from tracker import get_market_quotes
    except Exception as exc:
        logger.warning("multi_market: tracker non disponibile (%s)", exc)
        return []
    rows = get_market_quotes(limit=SCAN_LIMIT) or []
    start, end = _window_iso(now)
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        market_type = str(row.get("market_type") or "").upper()
        if market_type not in MARKETS or row.get("price") is None:
            continue
        fixture_id = str(row.get("fixture_id") or "")
        if not fixture_id:
            continue
        kickoff = str(row.get("kickoff") or "")
        if not kickoff or not (start <= kickoff < end):
            continue
        entry = out.setdefault(fixture_id, {
            "id": fixture_id,
            "home": row.get("home") or "", "away": row.get("away") or "",
            "commence": kickoff, "league": row.get("league") or "",
        })
        if not entry["league"] and row.get("league"):
            entry["league"] = row["league"]
    return sorted(out.values(), key=lambda f: f["commence"])


def _ensure_match(fixture: Dict[str, Any]) -> None:
    """Riga in `matches` se manca (il ledger p_vuole una partita per il referto).

    Non SOVRASCRIVE una riga esistente: `save_match` fa INSERT OR REPLACE e
    riscrivere una partita gia' saldata (status/altri campi) sarebbe un danno
    silenzioso.
    """
    try:
        from tracker import _get_conn, save_match
        conn = _get_conn()
        row = conn.execute("SELECT 1 FROM matches WHERE id = ?",
                           (fixture["id"],)).fetchone()
        conn.close()
        if row:
            return
        save_match(fixture["id"], fixture.get("league") or "",
                   fixture.get("home") or "", fixture.get("away") or "",
                   fixture.get("commence"))
    except Exception as exc:
        logger.debug("multi_market: save_match %s saltata: %s",
                     fixture.get("id"), exc)


def _lam_for(match_id: str, home: str, away: str) -> Tuple[float, float]:
    """(lam_h, lam_a) del modello: dal ledger analisi, altrimenti dal motore."""
    try:
        from tracker import _get_conn
        conn = _get_conn()
        row = conn.execute("SELECT lam_h, lam_a FROM match_analysis "
                           "WHERE match_id = ? ORDER BY id DESC LIMIT 1",
                           (match_id,)).fetchone()
        conn.close()
        if row and row[0] is not None and row[1] is not None:
            return float(row[0]), float(row[1])
    except Exception:
        pass
    return expected_goals(home, away)


def _ledger_rows(cands: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Righe da registrare in `predictions` (le sole linee che contano).

    Il ledger delle previsioni e' fatto per i segnali AZIONABILI: registrare
    ogni linea (fino a 12 x 2 per mercato) lo riempirebbe di migliaia di righe
    al giorno. Lo snapshot completo di tutti i mercati vive in
    `market_quotes`; qui entra la linea giocabile (coi suoi due lati, cosi' il
    tiering per-candidato resta confrontabile col 1X2) e, se nessuna linea e'
    giocabile, il solo candidato con l'EV piu' alto (diagnostica del perche').
    """
    by_group: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for cand in cands:
        by_group.setdefault((cand["mercato"], line_key(cand["line"])), []).append(cand)
    out: List[Dict[str, Any]] = []
    for market_type in MARKETS:
        groups = [g for (mt, _k), g in by_group.items() if mt == market_type]
        if not groups:
            continue
        playable = [g for g in groups if any(c.get("playable") for c in g)]
        if playable:
            out.extend(playable[0])
        else:
            best = max(groups, key=lambda g: max(float(c["ev"]) for c in g))
            out.append(max(best, key=lambda c: float(c["ev"])))
    return out


def _persist(fixture: Dict[str, Any], cands: Sequence[Dict[str, Any]]) -> int:
    """Scrive i candidati scelti in `predictions` (fail-safe, mai un'eccezione)."""
    written = 0
    try:
        from tracker import save_prediction
        for cand in _ledger_rows(cands):
            save_prediction(fixture["id"], cand["mercato"], cand["esito_key"],
                            cand["quota"], cand["prob"], cand["ev"],
                            market_prob=cand.get("market_prob"),
                            market_edge=cand.get("market_edge"),
                            status=cand.get("status") or "rejected")
            written += 1
    except Exception as exc:
        logger.warning("multi_market: salvataggio previsioni %s: %s",
                       fixture.get("id"), exc)
    return written


def scan(provider: Any = None, *, ingest_quotes: bool = True,
         persist: bool = True, now: Optional[datetime] = None
         ) -> List[Dict[str, Any]]:
    """Un giro completo: ingest SX -> analisi -> ledger `predictions`.

    Ritorna i segnali giocabili trovati (forma compatta: e' la telemetria del
    giro). Fail-soft: una partita che esplode viene saltata, il giro continua.
    """
    when = now or datetime.now(timezone.utc)
    if ingest_quotes:
        ingest(provider, observed=when)
    saved: List[Dict[str, Any]] = []
    fixtures = _fixtures_with_quotes(when)
    for fixture in fixtures:
        try:
            quotes = _quotes_for(fixture["id"])
            if not quotes:
                continue
            _ensure_match(fixture)
            lam_h, lam_a = _lam_for(fixture["id"], fixture["home"],
                                    fixture["away"])
            cands = analyze_fixture(fixture["id"], lam_h, lam_a, quotes=quotes,
                                    league=fixture.get("league") or "")
            if not cands:
                continue
            if persist:
                _persist(fixture, cands)
            for cand in cands:
                if not cand.get("playable"):
                    continue
                saved.append({
                    "match_id": fixture["id"], "home": fixture["home"],
                    "away": fixture["away"], "commence": fixture["commence"],
                    "league": fixture.get("league") or "",
                    "mercato": cand["mercato"], "esito": cand["esito_key"],
                    "quota": cand["quota"], "ev": cand["ev"],
                    "status": cand["status"], "line": cand["line"],
                    "live": cand["mercato"] in live_markets(),
                })
        except Exception as exc:                     # partita ostile
            logger.warning("multi_market: analisi %s saltata (%s)",
                           fixture.get("id"), exc)
    live_n = sum(1 for s in saved if s.get("live"))
    logger.info("multi_market: %d fixture con quote, %d segnali giocabili "
                "(%d nelle corsie live %s)", len(fixtures), len(saved), live_n,
                ",".join(live_markets()) or "nessuna")
    return saved


# ---------------------------------------------------------------------------
# 4. CORSAIA ORDINI: pick aperti del ledger multi-mercato (interruttori live)
# ---------------------------------------------------------------------------

def live_picks(*, hours: float = HOURS_AHEAD,
               now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Pick OU/AH pronti per l'ordine, dai SOLI mercati accesi.

    Il gate di lega e la fascia quota sono RIPETUTI qui (difesa in profondita',
    come in `auto_bet._today_value_picks`): una riga storica scritta prima di
    un cambio di strategia non puo' diventare un ordine. FAIL-CLOSED sulla
    lega assente: senza sapere cosa si sta giocando non si ordina.
    """
    markets = live_markets()
    if not markets:
        return []
    when = now or datetime.now(timezone.utc)
    start = when.isoformat().replace("+00:00", "Z")
    end = (when + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    try:
        from tracker import _get_conn
        conn = _get_conn()
        marks = ",".join("?" * len(markets))
        rows = conn.execute(
            f'''SELECT p.match_id, m.home_team, m.away_team, m.commence_time,
                       m.league, p.mercato, p.esito, p.quota, p.market_edge,
                       p.market_prob, p.ev, p.status
                  FROM predictions p JOIN matches m ON m.id = p.match_id
                 WHERE p.mercato IN ({marks})
                   AND p.status IN ('value','strong_value','moderate')
                   AND p.esito_finale IS NULL
                   AND m.commence_time >= ? AND m.commence_time < ?
                 ORDER BY p.ev DESC''',
            tuple(markets) + (start, end)).fetchall()
        conn.close()
    except Exception as exc:
        logger.warning("multi_market: lettura pick fallita (%s)", exc)
        return []
    out: List[Dict[str, Any]] = []
    for (match_id, home, away, commence, league, mercato, esito, quota,
         edge, market_prob, ev, status) in rows:
        league_name = (league or "").strip()
        if not league_name:
            logger.info("multi_market: skip %s %s (lega assente)",
                        match_id, esito)
            continue
        if not league_allowed(league_name):
            logger.info("multi_market: skip %s %s (lega '%s' fuori dai "
                        "campionati vincenti)", match_id, esito, league_name)
            continue
        try:
            price = float(quota)
        except (TypeError, ValueError):
            continue
        if price < ODDS_MIN or price > ODDS_MAX:
            logger.info("multi_market: skip %s %s (quota %.2f fuori fascia "
                        "%.2f-%.2f)", match_id, esito, price, ODDS_MIN, ODDS_MAX)
            continue
        if market_prob is not None and float(market_prob) < MIN_FAVOURITE_MARKET_PROB:
            continue
        target = order_target({"mercato": mercato, "esito_key": esito})
        if target is None or target.get("line") is None:
            logger.info("multi_market: skip %s %s (mercato/linea non "
                        "riconoscibili per l'ordine)", match_id, esito)
            continue
        out.append({
            "match_id": match_id, "home": home, "away": away,
            "commence": commence, "league": league_name,
            "mercato": str(mercato).upper(), "esito_key": esito,
            "esito_raw": esito, "quota": price, "price": price,
            "market_line": target["line"], "order_side": target["side"],
            "market_edge": float(edge) if edge is not None else None,
            "market_prob": float(market_prob) if market_prob is not None else None,
            "best_ev": float(ev) if ev is not None else 0.0,
            "status": status,
        })
    return out


def shadow_report(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Riepilogo della corsia multi-mercato (shadow OU + live AH), sola lettura.

    Serve a MISURARE prima di allargare: quante previsioni, quante chiuse,
    quante vinte e il P/L per unita' di stake, per mercato. Zero crediti:
    legge solo il ledger locale.
    """
    report: Dict[str, Any] = {"live_markets": list(live_markets()), "markets": {}}
    for market_type in MARKETS:
        entry: Dict[str, Any] = {"open": 0, "closed": 0, "won": 0,
                                 "lost": 0, "push": 0, "profit": 0.0,
                                 "roi": None, "quotes": 0}
        try:
            from tracker import get_predictions
            rows = get_predictions(mercato=market_type, limit=5000)
        except Exception as exc:
            logger.debug("multi_market: report %s fallito: %s", market_type, exc)
            rows = []
        for row in rows:
            if row.get("esito_finale") is None:
                entry["open"] += 1
                continue
            entry["closed"] += 1
            verdict = str(row.get("esito_finale") or "").lower()
            if verdict in entry:
                entry[verdict] += 1
            try:
                entry["profit"] += float(row.get("profit") or 0.0)
            except (TypeError, ValueError):
                pass
        if entry["closed"]:
            entry["profit"] = round(entry["profit"], 4)
            entry["roi"] = round(entry["profit"] / entry["closed"], 4)
        try:
            from tracker import get_market_quotes
            entry["quotes"] = len(get_market_quotes(market_type=market_type) or [])
        except Exception:
            pass
        report["markets"][market_type] = entry
    return report


def format_report(data: Optional[Dict[str, Any]] = None) -> str:
    """Riepilogo leggibile (CLI/Telegram), mai un'eccezione."""
    rep = data if data is not None else shadow_report()
    lines = ["📐 MULTI-MERCATO (OU/AH)",
             "Corsie LIVE: " + (", ".join(rep.get("live_markets") or [])
                                or "nessuna (tutto shadow)")]
    for market_type, entry in (rep.get("markets") or {}).items():
        roi = entry.get("roi")
        lines.append(
            f"• {market_type}: {entry.get('quotes', 0)} quote sul ledger | "
            f"{entry.get('closed', 0)} chiuse "
            f"({entry.get('won', 0)}V/{entry.get('lost', 0)}P/"
            f"{entry.get('push', 0)}push) | {entry.get('open', 0)} aperte | "
            f"ROI {'n/d' if roi is None else f'{roi*100:+.2f}%'}")
    return "\n".join(lines)


if __name__ == "__main__":                        # pragma: no cover
    import argparse
    import json as _json

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Multi-mercato OU/AH (SX Bet)")
    parser.add_argument("command", nargs="?", default="report",
                        choices=("ingest", "scan", "picks", "report"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command == "ingest":
        result = ingest()
        print(_json.dumps(result, indent=2, default=str) if args.json
              else f"ingest: {result}")
    elif args.command == "scan":
        found = scan()
        print(_json.dumps(found, indent=2, default=str) if args.json
              else f"scan: {len(found)} segnali giocabili")
    elif args.command == "picks":
        picks = live_picks()
        print(_json.dumps(picks, indent=2, default=str) if args.json
              else f"picks live: {len(picks)}")
    else:
        data = shadow_report()
        print(_json.dumps(data, indent=2, default=str) if args.json
              else format_report(data))
