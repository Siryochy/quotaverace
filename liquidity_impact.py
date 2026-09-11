"""liquidity_impact.py — Impatto delle soglie di liquidita' SX (11/09/2026).

Risponde a una domanda operativa: **quante puntate bloccheranno le nuove
soglie di liquidita'?** Rischiamo che il bot non scommetta piu' o i parametri
sono equilibrati?

Metodo: campione REALE dei mercati 1X2 calcio di SX Bet (API pubblica, zero
crediti, zero ordini) — gli stessi mercati letti da `sx_signals.scan`, ma
SENZA scrivere nulla sul ledger e senza piazzare alcun ordine — e imbuto dei
filtri, misurando DUE grandezze diverse che NON vanno confuse:

  * profondita' di MERCATO di un esito = somma di TUTTI i livelli del lato
    (e' quello che usa il filtro di `sx_signals.scan`);
  * size AL FLOOR = somma delle size al prezzo migliore (e' quello che usa
    il guardrail d'ordine `auto_bet._live_available_size` con
    `min_price = quota-segnale`).

Imbuto:

    eventi scoperti
      -> book completo
        -> coerenti (inv_sum 0.98-1.08)
          -> passano il filtro di MERCATO (totale + esiti minimi)
            -> hanno un FAVORITO giocabile (fascia quota + prob. mercato)
              -> size al floor >= soglia della leg giocata
                -> sono SEGNALI value del modello (EV/edge)

Poi la sensibilita' allo STAKE reale: per ogni stake tipico (1, 2, 5, 10,
20 USDC) si applica `auto_bet.required_depth` (= max(stake x 2.0,
SX_MIN_EXEC_DEPTH_USDC)) alla size al floor della leg giocata.

Output: tabella con conteggi/percentuali, confronto VECCHIE (15/5) e NUOVE
(25/5/25) soglie, motivi di blocco, ripartizione per lega e verdetto.

Uso:
    venv/bin/python liquidity_impact.py                    # campione live 24h
    venv/bin/python liquidity_impact.py --hours 72
    venv/bin/python liquidity_impact.py --json
    venv/bin/python liquidity_impact.py --save sample.json # congela il campione
    venv/bin/python liquidity_impact.py --from-cache sample.json

NON e' un modulo decisionale: nessuna funzione qui autorizza o blocca una
puntata. E' il complemento "ex ante" di liquidity_monitor (che misura gli
scarti gia' avvenuti).
"""
from __future__ import annotations

import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import DATA_DIR

logger = logging.getLogger("liquidity_impact")

# Soglie VECCHIE (prima della taratura 11/09) per il confronto before/after.
OLD_DEPTH_USDC = 15.0
OLD_LEG_DEPTH_USDC = 5.0

# Campione: limiti di cortesia verso l'API pubblica di SX (nessun credito
# the-odds-api coinvolto).
MAX_RAW_MARKETS = 1000
MAX_EVENTS = 200
HOURS_AHEAD = 24.0
CACHE_FILE = DATA_DIR / "execution" / "liquidity_impact_sample.json"

# Stakes tipici dell'esecuzione reale (USDC) per la sensibilita' al floor.
# Con STAKE_CAP_HARD e cap 1%/2% su wallet di 100-2000 USDC, gli stake
# realistici stanno in questa forbice (il cap 1% su 2000 USDC = 20 USDC).
STAKE_GRID = (1.0, 2.0, 5.0, 10.0, 20.0)


# ---------------------------------------------------------------------------
# Lettura book (pubblica, senza credenziali)
# ---------------------------------------------------------------------------

def _levels(prov, market_hash: str, sel: int = 1) -> List[Tuple[float, float]]:
    """Livelli BACK taker del lato: [(quota decimale, size USDC)] desc.

    A differenza di `sx_signals._book` (che aggrega e perde i livelli) qui
    servono TUTTI i livelli: la size al floor dipende dal prezzo.
    """
    from execution_engine import pct_scaled_to_decimal, sx_units_to_stake
    data = prov._get("orderbook-v3/snapshot", params={
        "marketHash": market_hash, "showTakerPerspective": "true"})
    d = data.get("data") or {}
    key = "outcomeOne" if int(sel) == 1 else "outcomeTwo"
    out: List[Tuple[float, float]] = []
    for lv in (d.get(key) or []):
        if not isinstance(lv, dict):
            continue
        q = pct_scaled_to_decimal(lv.get("percentageOdds"))
        s = sx_units_to_stake(lv.get("size"))
        if q and q > 1.0 and s:
            out.append((float(q), float(s)))
    out.sort(key=lambda x: -x[0])
    return out


def _levels_parallel(prov, market_ids: List[str]) -> Dict[str, List[Tuple[float, float]]]:
    """Snapshot paralleli dei book taker (10 thread, come sx_signals)."""
    def _fetch(mid: str):
        try:
            return mid, _levels(prov, mid)
        except Exception as e:  # pragma: no cover - rete
            return mid, e

    out: Dict[str, List[Tuple[float, float]]] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_fetch, mid): mid for mid in market_ids}
        for fut in as_completed(futs):
            mid, res = fut.result()
            if isinstance(res, Exception):
                logger.warning("book %s non leggibile: %s", mid, res)
                continue
            out[mid] = res
    return out


def _sum_at_floor(levels: List[Tuple[float, float]]) -> float:
    """Size disponibile AL FLOOR: somma delle size al prezzo migliore.

    E' la grandezza che `auto_bet._live_available_size(min_price=floor)`
    somma quando il floor coincide col prezzo migliore (caso del segnale
    appena generato: l'ordine e' esattamente dove sta il mercato).
    """
    if not levels:
        return 0.0
    best = levels[0][0]
    return round(sum(s for q, s in levels if abs(q - best) < 1e-9), 2)


# ---------------------------------------------------------------------------
# Analisi di un evento
# ---------------------------------------------------------------------------

def _row_for_event(ev: dict, books: dict,
                   with_model: bool = False) -> Optional[dict]:
    """Riga di campione: prezzi, profondita', favorito giocabile, segnale.

    Pura lettura: replica i calcoli di `sx_signals.scan` senza toccare il
    ledger. Ritorna None se il book non e' completo/leggibile.

    ISOLAMENTO DELLE VARIABILI: con `with_model=False` (default) il modello
    NON viene nemmeno interrogato. La selezione del favorito dipende solo
    dal mercato (`market_implied` + fascia quota), quindi la misura di
    liquidita' resta completamente indipendente da ratings/EV: nessun
    numero del modello puo' inquinare il conteggio degli scarti.
    """
    import sx_signals
    from market_calib import market_implied
    from value_filter import (eligible_favourites, ODDS_MIN)

    home, away = ev["teams"]
    # Stessa risoluzione lega dello scan (il blend dinamico usa la chiave
    # SPORTS_MAP, non l'etichetta grezza SX).
    league_name = str(_league_sports(ev.get("league_label"))
                      or ev.get("league_label") or "")
    odds: Dict[str, float] = {}
    totals: Dict[str, float] = {}      # tutti i livelli (filtro di mercato)
    at_floor: Dict[str, float] = {}    # size al prezzo migliore (ordine)
    for leg in ev["legs"]:
        levels = books.get(leg["market_hash"])
        if not levels:
            return None
        odds[leg["esito"]] = levels[0][0]
        totals[leg["esito"]] = round(sum(s for _q, s in levels), 2)
        at_floor[leg["esito"]] = _sum_at_floor(levels)
    if len(odds) != 3:
        return None

    row: dict = {
        "match_id": f"sx-{ev['event_id']}",
        "home": home, "away": away,
        "league": ev.get("league_label") or "",
        "league_sports": league_name,
        "kickoff": datetime.fromtimestamp(
            ev["kickoff_ms"] / 1000.0, tz=timezone.utc).isoformat(),
        "odds": odds,
        "depths": totals,             # profondita' di mercato (scan)
        "at_floor": at_floor,         # size al floor (ordine)
        "total_depth": round(sum(totals.values()), 2),
        "min_leg_depth": round(min(totals.values()), 2),
        "inv_sum": round(sum(1.0 / o for o in odds.values()), 4),
    }
    row["coherent"] = 0.98 <= row["inv_sum"] <= 1.08

    market = market_implied(odds) or {}
    row["market_probs"] = {k: round(v, 4) for k, v in market.items()}
    # Favorito di MERCATO (solo mercato: nessun modello coinvolto).
    if all(k in market for k in ("1", "X", "2")):
        fav_m = max(("1", "X", "2"), key=lambda k: market[k])
        row["market_fav"] = fav_m
        row["market_fav_odds"] = odds[fav_m]
        row["market_fav_prob"] = round(market[fav_m], 4)
    else:
        row["market_fav"] = None

    # Selezione del favorito giocabile: dipende SOLO dal mercato (prob.
    # devigata + fascia quota), esattamente come `sx_signals.scan`.
    market_candidates = [
        {"esito": k, "quota": odds[k], "prob": market.get(k),
         "market_prob": market.get(k), "ev": None}
        for k in ("1", "X", "2")]
    shortlist = [c for c in eligible_favourites(market_candidates)
                 if float(c["quota"]) >= ODDS_MIN]
    row["is_favorite_candidate"] = bool(shortlist)
    row["favorite"] = shortlist[0]["esito"] if shortlist else None
    fav = row["favorite"]
    row["favorite_depth"] = totals.get(fav) if fav else None
    row["favorite_floor"] = at_floor.get(fav) if fav else None
    row["favorite_odds"] = odds.get(fav) if fav else None
    row["favorite_market_prob"] = market.get(fav) if fav else None
    row["value"] = False
    row["value_reason"] = "non valutato (misura solo-liquidita')"
    row["has_ratings"] = None
    row["fav_edge"] = None
    row["model_fav_prob"] = None
    if not with_model:
        return row

    # ---- Da qui in poi il MODELLO (opt-in: --with-model). ----
    from poisson_engine import expected_goals, prob_1x2
    from value_filter import compute_ev, is_sane, adjusted_probability
    # Copertura ratings: senza ratings reali per ENTRAMBE le squadre
    # `expected_goals` usa il profilo neutro di lega (= parere inutilizzabile).
    try:
        from rating_engine import get_rating
        row["has_ratings"] = bool(get_rating(home) and get_rating(away))
    except Exception:
        row["has_ratings"] = False
    try:
        lam_h, lam_a = expected_goals(home, away)
        p1, px, p2 = prob_1x2(lam_h, lam_a)
    except Exception:
        return row
    model_p = {"1": p1, "X": px, "2": p2}
    if row.get("market_fav"):
        row["model_fav_prob"] = round(model_p[row["market_fav"]], 4)
        row["fav_edge"] = round(
            model_p[row["market_fav"]] - market[row["market_fav"]], 4)
    candidates = []
    for mkey in ("1", "X", "2"):
        price = odds[mkey]
        market_prob = market.get(mkey)
        final_prob = adjusted_probability(model_p[mkey], market_prob, price,
                                          league=league_name)
        candidates.append({
            "esito": mkey, "quota": price, "prob": final_prob,
            "ev": compute_ev(final_prob, price),
            "market_prob": market_prob,
            "market_edge": (model_p[mkey] - market_prob)
            if market_prob is not None else None,
        })
    shortlist = [c for c in eligible_favourites(candidates)
                 if float(c["quota"]) >= ODDS_MIN]
    if shortlist:
        best_c = max(shortlist, key=lambda c: c["ev"])
        sane, reason = is_sane(best_c["prob"], best_c["quota"], best_c["ev"],
                               market_prob=best_c["market_prob"])
        row["value"] = bool(sane)
        row["value_reason"] = reason
        row["value_ev"] = round(best_c["ev"], 4)
        row["value_esito"] = best_c["esito"]
        row["value_odds"] = best_c["quota"]
        row["best_prob"] = best_c["prob"]
        row["best_ev"] = best_c["ev"]
        row["best_edge"] = best_c["market_edge"]
        row["best_market_prob"] = best_c["market_prob"]
        row["best_esito"] = best_c["esito"]
        row["best_odds"] = best_c["quota"]
    return row


def _league_sports(label: Optional[str]) -> Optional[str]:
    """Etichetta SX -> chiave SPORTS_MAP (fallback: l'etichetta stessa)."""
    try:
        import sx_signals
        return sx_signals._league_sx_to_sports_map(label or "")
    except Exception:
        return None


def collect(provider=None, max_events: int = MAX_EVENTS,
            max_markets: int = MAX_RAW_MARKETS,
            hours: float = HOURS_AHEAD,
            with_model: bool = False) -> List[dict]:
    """Campione live: discovery + snapshot dei book (nessuna scrittura)."""
    import sx_signals
    from execution_engine import SxBetProvider
    prov = provider or SxBetProvider()   # letture pubbliche, nessuna chiave
    old_hours = sx_signals.HOURS_AHEAD
    sx_signals.HOURS_AHEAD = hours
    try:
        events = sx_signals._discover(prov, max_markets=max_markets)
    finally:
        sx_signals.HOURS_AHEAD = old_hours
    if not events:
        return []
    events = sorted(events, key=lambda e: e["kickoff_ms"])[:max_events]
    ids = [leg["market_hash"] for ev in events for leg in ev["legs"]]
    books = _levels_parallel(prov, ids)
    rows: List[dict] = []
    for ev in events:
        row = _row_for_event(ev, books, with_model=with_model)
        if row is not None:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Aggregazione
# ---------------------------------------------------------------------------

def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 1) if den else 0.0


def _market_ok(row: dict, total_min: float, leg_min: float) -> bool:
    """Filtro di MERCATO di `sx_signals.scan` (profondita' totale per esito)."""
    if row["total_depth"] < total_min:
        return False
    return not any(row["depths"].get(k, 0.0) < leg_min for k in ("1", "X", "2"))


def _played_ok(row: dict, floor_min: float) -> bool:
    """Soglia sulla LEG GIOCATA, misurata sulla size al floor."""
    if not row.get("favorite"):
        return False
    return float(row.get("favorite_floor") or 0.0) >= floor_min


def _depth_block_reason(row: dict, total_min: float, leg_min: float) -> str:
    if row["total_depth"] < total_min:
        return "depth_totale"
    return "depth_esito"


def _percentiles(values: List[float], ps=(10, 25, 50, 75, 90),
                 digits: int = 2) -> dict:
    if not values:
        return {}
    v = sorted(values)
    out = {}
    for p in ps:
        idx = min(len(v) - 1, int(round(p / 100.0 * (len(v) - 1))))
        out[f"p{p}"] = round(v[idx], digits)
    return out


def analyse(rows: List[dict], with_model: bool = False) -> dict:
    """Imbuto + sensibilita' allo stake + confronto vecchie/nuove soglie.

    Con `with_model=False` (default) il risultato contiene SOLO la misura di
    liquidita': nessun campo dipendente da ratings/EV. Cosi' la variabile in
    esame resta isolata e leggibile (e un DB senza ratings non puo' produrre
    numeri fasulli).
    """
    import auto_bet
    import sx_signals

    n = len(rows)
    coherent = [r for r in rows if r["coherent"]]
    old_ok = [r for r in coherent if _market_ok(r, OLD_DEPTH_USDC,
                                                OLD_LEG_DEPTH_USDC)]
    new_ok = [r for r in coherent if _market_ok(
        r, sx_signals.MIN_DEPTH_USDC, sx_signals.MIN_LEG_DEPTH_USDC)]
    depth_blocked = [r for r in coherent if not _market_ok(
        r, sx_signals.MIN_DEPTH_USDC, sx_signals.MIN_LEG_DEPTH_USDC)]

    favs = [r for r in coherent if r.get("is_favorite_candidate")]
    # Eseguibile = favorito che passa ENTRAMBI i guardrail nuovi (filtro di
    # mercato + size al floor della leg giocata).
    def _executable(r: dict) -> bool:
        return (_market_ok(r, sx_signals.MIN_DEPTH_USDC,
                           sx_signals.MIN_LEG_DEPTH_USDC)
                and _played_ok(r, sx_signals.MIN_EXEC_DEPTH_USDC))

    favs_played_ok = [r for r in favs if _executable(r)]

    reasons: Dict[str, int] = {}
    for r in depth_blocked:
        k = _depth_block_reason(r, sx_signals.MIN_DEPTH_USDC,
                                sx_signals.MIN_LEG_DEPTH_USDC)
        reasons[k] = reasons.get(k, 0) + 1

    # Sensibilita' allo stake: size al floor della leg giocata vs
    # required_depth(stake) = max(stake x SX_DEPTH_MULTIPLIER,
    #                            SX_MIN_EXEC_DEPTH_USDC).
    stake_grid = []
    for stake in STAKE_GRID:
        need = auto_bet.required_depth(stake)
        keep = [r for r in favs
                if float(r.get("favorite_floor") or 0.0) >= need
                and _market_ok(r, sx_signals.MIN_DEPTH_USDC,
                               sx_signals.MIN_LEG_DEPTH_USDC)]
        stake_grid.append({
            "stake": stake, "required_depth": round(need, 2),
            "passed": len(keep), "tested": len(favs),
            "pct": _pct(len(keep), len(favs)),
        })

    # Quanto costerebbe IRRIGIDIRE la soglia della leg giocata: e' la
    # domanda opposta ("i parametri sono equilibrati anche in alto?").
    exec_sensitivity = []
    for t in (5.0, 10.0, 25.0, 50.0, 100.0):
        keep = [r for r in favs if _market_ok(
            r, sx_signals.MIN_DEPTH_USDC, sx_signals.MIN_LEG_DEPTH_USDC)
            and float(r.get("favorite_floor") or 0.0) >= t]
        exec_sensitivity.append({"floor_min": t, "passed": len(keep),
                                 "tested": len(favs),
                                 "pct": _pct(len(keep), len(favs))})

    floors = [float(r.get("favorite_floor") or 0.0) for r in favs]
    markets = [float(r["depths"].get("1", 0.0)) for r in coherent]

    # ---- (opt-in) GATE MODELLO: copertura ratings + edge sul favorito. ----
    model_gate: dict = {}
    gate_sensitivity: List[dict] = []
    favorite_ev: dict = {}
    favorite_edge: dict = {}
    if with_model:
        with_ratings = [r for r in coherent if r.get("has_ratings")]
        edges_all = [r["fav_edge"] for r in coherent
                     if r.get("fav_edge") is not None]
        edges_rated = [r["fav_edge"] for r in with_ratings
                       if r.get("fav_edge") is not None]
        edges_unrated = [r["fav_edge"] for r in coherent
                         if r.get("fav_edge") is not None
                         and not r.get("has_ratings")]
        model_gate = {
            "coherent": len(coherent),
            "with_ratings": len(with_ratings),
            "with_ratings_pct": _pct(len(with_ratings), len(coherent)),
            "fav_edge_all": _percentiles(edges_all, digits=4),
            "fav_edge_rated": _percentiles(edges_rated, digits=4),
            "fav_edge_unrated": _percentiles(edges_unrated, digits=4),
            "positive_edges": sum(1 for e in edges_all if e > 0),
            # Se NON c'e' nessuna riga con ratings, il parere del modello e'
            # un profilo neutro: il gate non e' misurabile in questo ambiente.
            "measurable": bool(with_ratings),
            "league_edge": [],
        }
        _by_lg: Dict[str, list] = {}
        for r in coherent:
            if r.get("fav_edge") is None:
                continue
            _by_lg.setdefault(r.get("league_sports") or r["league"] or "?",
                              []).append(r["fav_edge"])
        model_gate["league_edge"] = [
            {"league": lg, "n": len(v),
             "edge_median": round(sorted(v)[len(v) // 2], 4)}
            for lg, v in sorted(_by_lg.items(), key=lambda kv: -len(kv[1]))]

        # Sensibilita' del gate: quanti favoriti passerebbero con una soglia
        # di edge diversa (l'EV_MIN resta quello di produzione).
        from value_filter import is_sane
        for edge_min in (0.0, 0.02, 0.03, 0.05):
            ok = 0
            for r in favs:
                if r.get("best_prob") is None:
                    continue
                sane, _ = is_sane(
                    r["best_prob"], r["best_odds"], r["best_ev"],
                    market_prob=r["best_market_prob"],
                    market_edge_min=edge_min)
                ok += int(bool(sane))
            gate_sensitivity.append({"edge_min": edge_min, "signals": ok,
                                     "tested": len(favs),
                                     "pct": _pct(ok, len(favs))})
        favorite_ev = _percentiles(
            [r["best_ev"] for r in favs if r.get("best_ev") is not None],
            digits=4)
        favorite_edge = _percentiles(
            [r["best_edge"] for r in favs if r.get("best_edge") is not None],
            digits=4)
        values = [r for r in favs if r.get("value")]
        values_ok = [r for r in values if _executable(r)]
    else:
        values = []
        values_ok = []

    # Ripartizione per lega (solo leghe con >= 2 partite coerenti).
    by_league: Dict[str, dict] = {}
    for r in coherent:
        lg = r["league"] or "?"
        e = by_league.setdefault(lg, {"events": 0, "market_ok": 0,
                                      "favorites": 0,
                                      "favorites_playable": 0})
        e["events"] += 1
        if _market_ok(r, sx_signals.MIN_DEPTH_USDC,
                      sx_signals.MIN_LEG_DEPTH_USDC):
            e["market_ok"] += 1
        if r.get("is_favorite_candidate"):
            e["favorites"] += 1
            if (_market_ok(r, sx_signals.MIN_DEPTH_USDC,
                           sx_signals.MIN_LEG_DEPTH_USDC)
                    and _played_ok(r, sx_signals.MIN_EXEC_DEPTH_USDC)):
                e["favorites_playable"] += 1
    leagues = [{"league": k, **v} for k, v in by_league.items() if v["events"] >= 2]
    leagues.sort(key=lambda e: -e["events"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "old": {"depth": OLD_DEPTH_USDC, "leg": OLD_LEG_DEPTH_USDC},
            "new": {
                "depth": sx_signals.MIN_DEPTH_USDC,
                "leg": sx_signals.MIN_LEG_DEPTH_USDC,
                "exec": sx_signals.MIN_EXEC_DEPTH_USDC,
                "order_multiplier": auto_bet.SX_DEPTH_MULTIPLIER,
                "order_min": auto_bet.MIN_EXEC_DEPTH_USDC,
            },
        },
        "funnel": {
            "events": n,
            "with_book": n,
            "coherent": len(coherent),
            "old_market_ok": len(old_ok),
            "old_market_pct": _pct(len(old_ok), len(coherent)),
            "new_market_ok": len(new_ok),
            "new_market_pct": _pct(len(new_ok), len(coherent)),
            "depth_blocked": len(depth_blocked),
            "favorites": len(favs),
            "favorites_market_ok": sum(1 for r in favs if _market_ok(
                r, sx_signals.MIN_DEPTH_USDC, sx_signals.MIN_LEG_DEPTH_USDC)),
            "favorites_playable": len(favs_played_ok),
            "favorites_playable_pct": _pct(len(favs_played_ok), len(favs)),
            # "Scommesse perse per liquidita'" = candidati favoriti NON
            # eseguibili perbook sottile (il complemento, esplicito).
            "lost_to_liquidity": len(favs) - len(favs_played_ok),
            "lost_to_liquidity_pct": _pct(len(favs) - len(favs_played_ok),
                                          len(favs)),
        },
        "misc": {
            "with_model": bool(with_model),
            "stake_grid": list(STAKE_GRID),
        },
        "block_reasons": reasons,
        "stake_sensitivity": stake_grid,
        "exec_sensitivity": exec_sensitivity,
        "favorite_floor": _percentiles(floors),
        "market_depth": _percentiles(markets),
        "leagues": leagues,
        "rows": rows,
        **(({"value_signals": len(values),
            "value_signals_playable": len(values_ok),
            "gate_sensitivity": gate_sensitivity,
            "model_gate": model_gate,
            "favorite_ev": favorite_ev,
            "favorite_edge": favorite_edge}) if with_model else {}),
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt_med(d: Optional[dict]) -> str:
    if not d:
        return "n/d"
    return f"{d.get('p50', 0.0) * 100:+.1f}pp (p10 {d.get('p10', 0.0)*100:+.1f} / p90 {d.get('p90', 0.0)*100:+.1f})"


def _bar(pct: float, width: int = 20) -> str:
    filled = int(round(width * max(0.0, min(100.0, pct)) / 100.0))
    return "█" * filled + "░" * (width - filled)


def format_report(data: dict) -> str:
    f, t = data["funnel"], data["thresholds"]
    _wm = bool(data.get("misc", {}).get("with_model"))
    s_dist = 4 if _wm else 3          # numerazione dinamica delle sezioni
    s_stake = s_dist + 1
    s_league = s_stake + 1
    L: List[str] = []
    L.append("═" * 72)
    L.append("  IMPATTO SOGLIE LIQUIDITA' SX — campione reale (API pubblica)")
    L.append("═" * 72)
    L.append(f"  Generato: {data['generated_at']}")
    L.append(f"  VECCHIE: totale {t['old']['depth']:.0f} | esito {t['old']['leg']:.0f}")
    L.append(f"  NUOVE:   totale {t['new']['depth']:.0f} | esito {t['new']['leg']:.0f} "
             f"| leg giocata {t['new']['exec']:.0f} (size al floor)")
    L.append(f"  ORDINE:  richiesto = max(stake x {t['new']['order_multiplier']:.1f}, "
             f"{t['new']['order_min']:.0f}) USDC")
    L.append("")
    L.append("  1) FILTRO DI MERCATO (profondita', tutti i livelli)")
    L.append(f"     partite con book completo ................ {f['events']}")
    L.append(f"     coerenti (inv_sum 0.98-1.08) ............. {f['coherent']}")
    L.append(f"     passano le soglie VECCHIE ................ "
             f"{f['old_market_ok']} ({f['old_market_pct']:.1f}%)  "
             f"{_bar(f['old_market_pct'])}")
    L.append(f"     passano le soglie NUOVE .................. "
             f"{f['new_market_ok']} ({f['new_market_pct']:.1f}%)  "
             f"{_bar(f['new_market_pct'])}")
    L.append(f"     BLOCCATE dal filtro nuove ................ {f['depth_blocked']} "
             f"({_pct(f['depth_blocked'], f['coherent']):.1f}%)")
    if data["block_reasons"]:
        for k, v in sorted(data["block_reasons"].items(), key=lambda kv: -kv[1]):
            L.append(f"        · {k:<16} {v}")
    L.append("")
    L.append("  2) STRATEGIA E LIQUIDITA' DELLA LEG GIOCATA (size al floor)")
    L.append(f"     candidati FAVORITI (fascia quota+prob) ... {f['favorites']} "
             f"su {f['coherent']} coerenti")
    L.append(f"     di cui passano il filtro di mercato ...... "
             f"{f['favorites_market_ok']}/{f['favorites']}  "
             f"({_pct(f['favorites_market_ok'], f['favorites']):.1f}%)")
    L.append(f"     di cui ESEGUIBILI (floor >= {t['new']['exec']:.0f} USDC) ......... "
             f"{f['favorites_playable']}/{f['favorites']}  "
             f"({f['favorites_playable_pct']:.1f}%)  {_bar(f['favorites_playable_pct'])}")
    L.append(f"     ► SCOMMESSE PERSE PER LIQUIDITA' ......... "
             f"{f['lost_to_liquidity']}/{f['favorites']}  "
             f"({f['lost_to_liquidity_pct']:.1f}%)")
    L.append("")
    if not data.get("misc", {}).get("with_model"):
        L.append("  (misura SOLO-liquidita': il gate modello e' escluso per "
                 "isolamento — usare --with-model per includerlo)")
        L.append("")
    else:
        L.append("  3) GATE MODELLO (opt-in, variabile separata)")
        mg = data.get("model_gate") or {}
        L.append(f"     segnali value ............................ "
                 f"{f.get('value_signals', 0)}")
        L.append(f"     di cui eseguibili ........................ "
                 f"{f.get('value_signals_playable', 0)}")
        if mg:
            L.append(f"     partite con RATINGS reali (entrambe le squadre) .. "
                     f"{mg['with_ratings']}/{mg['coherent']} "
                     f"({mg['with_ratings_pct']:.1f}%)")
            L.append(f"     edge modello sul favorito di mercato (mediana) .. "
                     f"{_fmt_med(mg.get('fav_edge_all'))}")
            if not mg.get("measurable"):
                L.append("     ⚠️  NESSUN rating nel DB locale: il modello usa il "
                         "profilo neutro di lega.")
                L.append("         Il numero di segnali value NON e' "
                         "rappresentativo della produzione.")
        if data.get("gate_sensitivity"):
            for g in data["gate_sensitivity"]:
                star = "  <- attuale" if abs(g["edge_min"] - 0.03) < 1e-9 else ""
                L.append(f"        edge >= {g['edge_min']*100:>4.0f}pp -> "
                         f"{g['signals']:>3}/{g['tested']} segnali "
                         f"({g['pct']:.1f}%){star}")
        L.append("")
    L.append(f"  {s_dist}) DISTRIBUZIONI (USDC)")
    def _fmt_pct(d):
        return " | ".join(f"{k} {v:.2f}" for k, v in d.items()) if d else "n/d"
    L.append(f"     profondita' di mercato (tutti i livelli) . {_fmt_pct(data['market_depth'])}")
    L.append(f"     size al floor, leg giocata ............... {_fmt_pct(data['favorite_floor'])}")
    L.append("")
    L.append(f"  {s_stake}) SENSIBILITA' ALLO STAKE (guardrail d'ordine)")
    L.append("     stake   richiesto   eseguibili        %")
    for s in data["stake_sensitivity"]:
        L.append(f"     {s['stake']:>5.1f}   {s['required_depth']:>8.2f}   "
                 f"{s['passed']:>6}/{s['tested']:<6}  {s['pct']:>5.1f}%  "
                 f"{_bar(s['pct'], 12)}")
    L.append("")
    L.append(f"  {s_stake}b) SE IRRIGIDISSIMO la soglia della leg giocata "
             "(USDC al floor)")
    L.append("     soglia   eseguibili        %")
    for s in data.get("exec_sensitivity", []):
        tag = "  <- attuale" if abs(s["floor_min"] - t["new"]["exec"]) < 1e-9 else ""
        L.append(f"     {s['floor_min']:>6.0f}   {s['passed']:>6}/{s['tested']:<6}  "
                 f"{s['pct']:>5.1f}%  {_bar(s['pct'], 12)}{tag}")
    L.append("")
    if data["leagues"]:
        L.append(f"  {s_league}) PER LEGA (>= 2 partite coerenti)")
        L.append("     lega                                      n  mercato  favoriti  eseguibili")
        for e in data["leagues"][:15]:
            L.append(f"     {e['league'][:38]:<38} {e['events']:>3}  "
                     f"{e['market_ok']:>7}  {e['favorites']:>8}  "
                     f"{e['favorites_playable']:>10}")
        L.append("")
    L.append("  VERDETTO")
    for line in verdict(data):
        L.append(f"    {line}")
    L.append("═" * 72)
    return "\n".join(L)


def verdict(data: dict) -> List[str]:
    """Lettura sintetica: il bot rischia di non scommettere piu'?"""
    f = data["funnel"]
    out: List[str] = []
    if not f["coherent"]:
        return ["Campione senza partite coerenti: allarga il campione."]
    out.append("MISURA SOLO-LIQUIDITA' (modello escluso): nessun numero "
               "dipende da ratings/EV.")
    out.append(f"Filtro di MERCATO: le nuove soglie bloccano "
               f"{f['depth_blocked']} partite su {f['coherent']} "
               f"({_pct(f['depth_blocked'], f['coherent']):.1f}%).")
    if f["favorites"]:
        pct = f["favorites_playable_pct"]
        if pct >= 90:
            out.append(f"ESEGUIBILITA' {pct:.1f}% sui candidati favoriti: la "
                       "liquidita' NON e' il collo di bottiglia.")
        elif pct >= 50:
            out.append(f"ESEGUIBILITA' {pct:.1f}% sui candidati favoriti: il "
                       "flusso si riduce ma resta operativo.")
        else:
            out.append(f"ESEGUIBILITA' {pct:.1f}% sui candidati favoriti: "
                       "soglie troppo severe per questa liquidita' — "
                       "rivedere SX_MIN_EXEC_DEPTH_USDC / SX_DEPTH_MULTIPLIER.")
    else:
        out.append("Nessun candidato favorito nel campione: il vincolo e' la "
                   "FASCIA QUOTA (1.30-1.80 + prob. di mercato), non la "
                   "liquidita'.")
    out.append(f"PERDITA PER LIQUIDITA': {f['lost_to_liquidity']} candidati "
               f"favoriti su {f['favorites']} "
               f"({f['lost_to_liquidity_pct']:.1f}%) vengono scartati per book "
               f"sottile.")
    if not data.get("misc", {}).get("with_model"):
        return out
    mg = data.get("model_gate") or {}
    if f.get("value_signals", 0) == 0:
        if mg.get("measurable"):
            out.append("Zero segnali value con il GATE MODELLO attuale (EV +3pp): "
                       "il collo di bottiglia NON e' la liquidita'. "
                       f"Edge mediano del modello sul favorito di mercato: "
                       f"{_fmt_med(mg.get('fav_edge_all'))}.")
            gs = data.get("gate_sensitivity") or []
            cur = next((g for g in gs if abs(g["edge_min"] - 0.03) < 1e-9), None)
            loose = next((g for g in gs if abs(g["edge_min"] - 0.02) < 1e-9), None)
            if cur and loose:
                out.append(f"Con edge >=3pp (attuale) i segnali sono "
                           f"{cur['signals']}/{cur['tested']}; con l'edge >=2pp "
                           f"del 10/09 sarebbero {loose['signals']}/"
                           f"{loose['tested']} ({loose['pct']:.1f}%).")
        else:
            out.append("GATE MODELLO NON MISURABILE in questo ambiente: nel DB "
                       "locale non c'e' nessun rating (0 righe team_ratings), "
                       "quindi il modello usa il profilo neutro. Il conteggio "
                       "dei segnali value VA MISURATO SUL CONTAINER.")
    else:
        out.append(f"Segnali value: {f['value_signals']} "
                   f"({f['value_signals_playable']} eseguibili).")
    return out


# ---------------------------------------------------------------------------
# Cache / CLI
# ---------------------------------------------------------------------------

def save_cache(rows: List[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cache(path: Path) -> List[dict]:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return d.get("rows") or []


def main() -> None:  # pragma: no cover - CLI
    p = argparse.ArgumentParser(
        description="Misura l'impatto delle soglie di liquidita' SX Bet")
    p.add_argument("--max-events", type=int, default=MAX_EVENTS,
                   help=f"partite analizzate (default {MAX_EVENTS})")
    p.add_argument("--max-markets", type=int, default=MAX_RAW_MARKETS,
                   help=f"mercati binari da scoprire (default {MAX_RAW_MARKETS})")
    p.add_argument("--hours", type=float, default=HOURS_AHEAD,
                   help=f"finestra di discovery in ore (default {HOURS_AHEAD})")
    p.add_argument("--save", type=str, default=None,
                   help="salva il campione in un file JSON")
    p.add_argument("--from-cache", type=str, default=None,
                   help="analizza un campione salvato (nessuna rete)")
    p.add_argument("--with-model", action="store_true",
                   help="include anche il gate modello (default: misura "
                        "SOLO-liquidita', variabile isolata)")
    p.add_argument("--json", action="store_true", help="output JSON")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.from_cache:
        rows = load_cache(Path(args.from_cache))
    else:
        rows = collect(max_events=args.max_events,
                       max_markets=args.max_markets, hours=args.hours,
                       with_model=args.with_model)
        if args.save and rows:
            save_cache(rows, Path(args.save))
    if not rows:
        print("Nessun mercato 1X2 calcio nel campione (nessuna analisi).")
        return
    data = analyse(rows, with_model=args.with_model)
    if args.json:
        data.pop("rows", None)
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    print(format_report(data))


if __name__ == "__main__":  # pragma: no cover
    main()
