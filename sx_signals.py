"""sx_signals.py — Segnali value 1X2 direttamente dai prezzi SX Bet.

Perche' questo modulo (09/09): il flusso storico nasce dalle quote
the-odds-api (fixture_engine.fetch_and_analyze_today) che serve ODDS_API_KEY
e brucia crediti; qui il MOTORE segue la stessa strategia (il valore esiste
solo se il modello batte il mercato devig, non un singolo bookmaker) usando
SOLO l'API PUBBLICA SX Bet (zero chiavi, zero crediti):

1. discovery: i 3 mercati binari "X vs Not X" (type 1, sportId 5) per
   partita -> si ricostruisce il 1X2 con i migliori prezzi BACK del book
   taker (stessi snapshot letti da scan_sx_live);
2. coerenza: inv_sum (somma inversi) 0.98-1.08 e quote sane, come il report
   di scan_sx_live — fuori range il match NON genera candidati;
3. modello: Poisson del progetto (poisson_engine.expected_goals/prob_1x2,
   rating dinamici inclusi) vs mercato fair (market_calib.market_implied,
   devig power su 1/X/2);
4. segnale: EV da prob finale blend (value_filter.adjusted_probability) e
   filtri sanità (is_sane: EV 3-15%, quote 1.50-5.00, edge vs mercato) —
   identici al flusso the-odds-api;
5. ledger: match/s match_analysis/predictions via tracker (save_match,
   save_analysis, save_prediction): da qui in poi auto_bet.run_today_bets
   li vede come QUALSIASI altro segnale value e — in AUTO_BET_MODE=live con
   provider SX configurato — piazza l'ordine reale sullo STESSO exchange
   che ha generato il prezzo (resolve_match_market matches per nomi+kickoff).

Settlement (senza ODDS_API_KEY): leggendo solo SX mancano i punteggi
finali; si recuperano da the-odds-api SE configurata (match per NOME+LEGA),
altrimenti da API-Football (football_hist) — lo strumento punteggi per le
bet SX e' _settle_sx_bets(). Finche' nessuna fonte e' disponibile le bet
restano aperte (nessuna chiusura errata: fail-closed).

CLI:
    venv/bin/python sx_signals.py scan        # scan + salvataggio segnali
    venv/bin/python sx_signals.py settle      # recupera punteggi e salda
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional

from execution_engine import SxBetProvider, pct_scaled_to_decimal, sx_units_to_stake
from market_calib import market_implied, MARKET_EDGE_STRONG
from poisson_engine import expected_goals, prob_1x2
from tracker import save_match, save_analysis, save_prediction
from value_filter import (compute_ev, is_sane, adjusted_probability,
                          eligible_favourites, favourites_gate_reason)

logger = logging.getLogger("sx_signals")

# --- Soglie di coerenza/liquidita' (identiche a scan_sx_live.py) -----------
MIN_INV_SUM, MAX_INV_SUM = 0.98, 1.08
MIN_DEPTH_USDC = 5.0        # liquidita' totale minima del match (taker)
MIN_LEG_DEPTH_USDC = 1.0    # minimo ordine SX Bet per singolo esito

# --- Finestra dei match candidati ------------------------------------------
HOURS_AHEAD = 24.0          # come auto_bet._today_value_picks (now..now+24h)
MIN_MINUTES_TO_START = 15   # auto_bet salta comunque i match vicini: qui
                            # non generiamo segnali gia' degni di salto
MAX_RAW_MARKETS = 300       # mercati binari da scansionare (100 partite)

_LEAGUE_MAP_CACHE: Dict[str, Optional[str]] = {}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _kickoff_utc_ms(game_time) -> Optional[int]:
    """Kickoff del mercato SX in ms epoch; None se assente/non valido.

    Accetta i formati osservati sull'API SX: gameTime in SECONDI epoch
    (int, es. 1788980400 = 2026-09-09 19:00 UTC), ms epoch, oppure date
    ISO (open_date del catalogo del provider).
    """
    if game_time is None:
        return None
    if isinstance(game_time, (int, float)):
        gt = int(game_time)
        if gt <= 0:
            return None
        # Secondi (~1.7e9) vs ms (~1.7e12): soglia 1e11 copre anni 1973-5138.
        return gt * 1000 if gt < 10 ** 11 else gt
    try:
        dt = datetime.fromisoformat(str(game_time).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _book(provider: SxBetProvider, market_id: str) -> dict:
    """Snapshot order book taker: best back per selection + profondita' USDC."""
    data = provider._get("orderbook-v3/snapshot", params={
        "marketHash": market_id, "showTakerPerspective": "true"})
    d = data.get("data") or {}
    out: dict = {}
    for key, sel in (("outcomeOne", 1), ("outcomeTwo", 2)):
        levels = d.get(key) or []
        best = None
        depth = 0.0
        for lv in levels:
            if not isinstance(lv, dict):
                continue
            q = pct_scaled_to_decimal(lv.get("percentageOdds"))
            size = sx_units_to_stake(lv.get("size"))
            if not q or q <= 1.0:
                continue
            depth += size
            if best is None or q > best["price"]:
                best = {"price": q, "size": size}
        out[sel] = {"best": best, "depth": round(depth, 2)}
    return out


def _books_parallel(provider: SxBetProvider, market_ids: List[str]) -> dict:
    """Snapshot paralleli (10 thread, come scan_sx_live)."""
    def _fetch(mid: str):
        try:
            return mid, _book(provider, mid)
        except Exception as e:
            return mid, {"error": str(e)}

    out: dict = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_fetch, mid): mid for mid in market_ids}
        for fut in as_completed(futs):
            mid, res = fut.result()
            out[mid] = res
    return out


def _discover(provider: SxBetProvider) -> List[dict]:
    """Partite 1X2 calcio SX nella finestra (now-1h .. now+HOURS_AHEAD).

    Discovery via endpoint RAW /markets/active (type 1 = mercati binari
    "X vs Not X"): a differenza del catalogo del provider include leagueId,
    leagueLabel, gameTime (ms) e sportXeventId, che servono per il ledger
    (lega -> SPORTS_MAP) e il match_id stabile. Raggruppa i 3 mercati per
    evento; ritorna [{event_id, league_label, kickoff_ms, teams, legs}].
    """
    raw: List[dict] = []
    pagination_key: Optional[str] = None
    while len(raw) < MAX_RAW_MARKETS:
        params: Dict = {"sportIds": "5", "type": "1", "pageSize": 100}
        if pagination_key:
            params["paginationKey"] = pagination_key
        data = provider._get("markets/active", params=params)
        d = data.get("data") if isinstance(data, dict) else {}
        markets = (d or {}).get("markets") or []
        raw.extend(markets)
        pagination_key = (d or {}).get("nextKey")
        if not pagination_key or not markets:
            break
    now = _now_ms()
    lo = now - 60 * 60 * 1000          # -1h: includo live appena iniziati
    hi = now + HOURS_AHEAD * 3600 * 1000
    by_event: Dict[str, dict] = {}
    for m in raw:
        if not isinstance(m, dict):
            continue
        ev_id = m.get("sportXeventId")
        ko = _kickoff_utc_ms(m.get("gameTime"))
        if not ev_id or ko is None or not (lo <= ko <= hi):
            continue
        t1 = str(m.get("teamOneName") or "").strip()
        t2 = str(m.get("teamTwoName") or "").strip()
        if not t1 or not t2:
            continue
        ev = by_event.setdefault(str(ev_id), {
            "event_id": str(ev_id),
            "league_label": m.get("leagueLabel") or "",
            "kickoff_ms": ko,
            "teams": (t1, t2),
            "legs": [],
        })
        o1 = str(m.get("outcomeOneName") or "").strip().lower()
        # I 3 mercati binari 1X2: "Tie vs Not tie" + "T1 vs Not T1" +
        # "T2 vs Not T2" (esito scommesso = outcomeOne, selection_id 1).
        if o1 in ("tie", "draw", "pareggio"):
            ev["legs"].append({"esito": "X", "market_hash": m["marketHash"]})
        elif o1 == t1.lower():
            ev["legs"].append({"esito": "1", "market_hash": m["marketHash"]})
        elif o1 == t2.lower():
            ev["legs"].append({"esito": "2", "market_hash": m["marketHash"]})
    # Completezza: servono TUTTI e 3 gli esiti per devigare il 1X2.
    out = []
    for ev in by_event.values():
        esiti = {leg["esito"] for leg in ev["legs"]}
        if esiti == {"1", "X", "2"}:
            out.append(ev)
    out.sort(key=lambda e: e["kickoff_ms"])
    return out


def _league_sx_to_sports_map(league_label: str) -> Optional[str]:
    """leagueLabel SX -> nome lega SPORTS_MAP (per il settlement the-odds-api).

    Fuzzy con cache: 'Italy Serie A'/'Serie A (IT)' -> 'Serie A'. None se
    nessuna corrispondenza sufficiente (settlement via fonte alternativa).
    """
    if league_label in _LEAGUE_MAP_CACHE:
        return _LEAGUE_MAP_CACHE[league_label]
    try:
        from difflib import SequenceMatcher
        from odds_api import SPORTS_MAP
        from execution_engine import _name_key
        best, best_sim = None, 0.0
        target = _name_key(league_label)
        for lg in SPORTS_MAP:
            sim = SequenceMatcher(None, target, _name_key(lg)).ratio()
            if sim > best_sim:
                best, best_sim = lg, sim
        result = best if best_sim >= 0.55 else None
    except Exception:
        result = None
    _LEAGUE_MAP_CACHE[league_label] = result
    return result


def _kickoff_iso(kickoff_ms: int) -> str:
    """ms epoch -> ISO UTC con Z (formato commence_time del ledger)."""
    return datetime.fromtimestamp(kickoff_ms / 1000.0,
                                  tz=timezone.utc).isoformat().replace("+00:00", "Z")


def scan(provider: Optional[SxBetProvider] = None) -> List[dict]:
    """Un giro di scan: SX -> segnali value nel ledger. Ritorna i salvati.

    Fail-soft: una partita con errori viene saltata, il giro continua.
    """
    prov = provider or SxBetProvider()   # letture pubbliche: nessuna chiave
    try:
        events = _discover(prov)
    except Exception as e:
        logger.warning("sx_signals: discovery fallita: %s", e)
        return []
    if not events:
        logger.info("sx_signals: nessun match 1X2 completo nella finestra %dh",
                    HOURS_AHEAD)
        return []

    # Snapshot paralleli di tutti i book PRIMA di valutare.
    all_ids = [leg["market_hash"] for ev in events for leg in ev["legs"]]
    books = _books_parallel(prov, all_ids)

    saved: List[dict] = []
    now_ms = _now_ms()
    for ev in events:
        home, away = ev["teams"]
        kickoff_iso = _kickoff_iso(ev["kickoff_ms"])
        if ev["kickoff_ms"] <= now_ms + MIN_MINUTES_TO_START * 60 * 1000:
            continue  # troppo vicino al kickoff: l'auto-bet lo salterebbe
        try:
            lam_h, lam_a = expected_goals(home, away)
            p1, px, p2 = prob_1x2(lam_h, lam_a)
        except Exception as e:
            logger.warning("sx_signals: modello fallito su %s vs %s: %s",
                           home, away, e)
            continue

        # Prezzi SX per esito + profondita' + coerenza del mercato.
        odds: Dict[str, float] = {}
        depths: Dict[str, float] = {}
        coherent = True
        for leg in ev["legs"]:
            b = books.get(leg["market_hash"]) or {}
            if "error" in b:
                coherent = False
                break
            # In tutti e 3 i mercati binari ("Tie vs Not tie", "T1 vs Not T1",
            # "T2 vs Not T2") l'esito scommesso e' outcomeOne -> selection 1
            # (la selection 2 e' il lato complementare "Not X").
            info = b.get(1) or {}
            best = info.get("best")
            if not best:
                coherent = False
                break
            odds[leg["esito"]] = float(best["price"])
            depths[leg["esito"]] = float(info.get("depth") or 0.0)
        if not coherent or len(odds) != 3:
            continue
        inv_sum = sum(1.0 / o for o in odds.values())
        total_depth = sum(depths.values())
        if not (MIN_INV_SUM <= inv_sum <= MAX_INV_SUM):
            logger.info("sx_signals: %s vs %s inv_sum %.3f fuori range, skip",
                        home, away, inv_sum)
            continue
        if (total_depth < MIN_DEPTH_USDC
                or any(d < MIN_LEG_DEPTH_USDC for d in depths.values())):
            logger.info("sx_signals: %s vs %s liquidita' %.1f USDC insufficiente, skip",
                        home, away, total_depth)
            continue

        match_id = f"sx-{ev['event_id']}"
        league_label = ev["league_label"]
        league_sports = _league_sx_to_sports_map(league_label)
        league_name = league_sports or league_label

        market = market_implied(odds)
        if not market:
            continue
        candidates = []
        for mkey, model_prob, price in (
                ("1", p1, odds["1"]), ("X", px, odds["X"]), ("2", p2, odds["2"])):
            market_prob = market.get(mkey)
            final_prob = adjusted_probability(model_prob, market_prob, price,
                                              league=league_name)
            ev_val = compute_ev(final_prob, price)
            edge = (model_prob - market_prob) if market_prob is not None else None
            candidates.append({
                "esito": mkey, "quota": price, "prob": final_prob, "ev": ev_val,
                "prob_model": model_prob, "market_prob": market_prob,
                "market_edge": edge,
            })

        # match_id deterministico: save_match fa INSERT OR REPLACE,
        # save_analysis sostituisce e save_prediction aggiorna le
        # predizioni non ancora saldate (idempotenza tra giri).
        # STRATEGIA SOLO FAVORITI (11/09): si gioca solo il miglior EV tra i
        # favoriti netti; senza favoriti il match non genera segnali.
        shortlist = eligible_favourites(candidates)
        if shortlist:
            best_c = max(shortlist, key=lambda c: c["ev"])
        else:
            best_c = max(candidates,
                         key=lambda c: (c.get("market_prob") or 0.0))
        save_match(match_id, league_name, home, away, kickoff_iso)
        if shortlist:
            sane, reason = is_sane(best_c["prob"], best_c["quota"],
                                   best_c["ev"],
                                   market_prob=best_c["market_prob"])
        else:
            sane, reason = False, favourites_gate_reason()
        if not sane:
            status = "rejected"
        elif best_c["ev"] > 0.08 and (best_c["market_edge"] is None
                                      or best_c["market_edge"] >= MARKET_EDGE_STRONG):
            status = "strong_value"
        elif best_c["ev"] > 0.03:
            status = "value"
        else:
            status = "no_value"
        save_analysis(match_id, lam_h, lam_a, p1, px, p2, None,
                      best_c["ev"], best_c["esito"], best_c["quota"],
                      "SX Bet", status,
                      market_prob=best_c["market_prob"],
                      market_edge=best_c["market_edge"])
        for cand in shortlist:
            # Stessa classificazione per-candidato del flusso the-odds-api
            # (_candidate_status): nel ledger finiscono anche i no_value.
            csane, _ = is_sane(cand["prob"], cand["quota"], cand["ev"],
                               market_prob=cand["market_prob"])
            if csane:
                if cand["ev"] > 0.08 and (cand["market_edge"] is None
                                          or cand["market_edge"] >= MARKET_EDGE_STRONG):
                    st = "strong_value"
                elif cand["ev"] > 0.03:
                    st = "value"
                else:
                    st = "no_value"
            else:
                st = "rejected"
            save_prediction(match_id, "1X2", cand["esito"], cand["quota"],
                            cand["prob"], cand["ev"],
                            market_prob=cand["market_prob"],
                            market_edge=cand["market_edge"], status=st)
        if status in ("value", "strong_value"):
            saved.append({
                "match_id": match_id, "home": home, "away": away,
                "commence": kickoff_iso, "league": league_name,
                "esito": best_c["esito"], "quota": best_c["quota"],
                "ev": best_c["ev"], "status": status,
            })
        logger.info("sx_signals: %s vs %s (%s) %s — best %s @ %.2f EV %.1f%%",
                    home, away, league_label, status,
                    best_c["esito"], best_c["quota"], best_c["ev"] * 100)
    logger.info("sx_signals: %d partite analizzate, %d segnali value salvati",
                len(events), len(saved))
    return saved


# ---------------------------------------------------------------------------
# Settlement per le bet SX (match_id = "sx-<eventId>")
# ---------------------------------------------------------------------------

def _sx_open_matches() -> Dict[str, dict]:
    """Meta delle partite SX con bet aperte: {match_id: info}.

    info = {home, away, league, kickoff} — serve per abbinare i risultati
    per NOME+LEGA (le fonti esterne non conoscono i match_id sx-*).
    """
    from tracker import _get_conn
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT b.match_id, m.home_team, m.away_team, m.league, "
        "m.commence_time FROM bets b JOIN matches m ON m.id = b.match_id "
        "WHERE b.esito_finale IS NULL AND b.match_id LIKE 'sx-%'").fetchall()
    conn.close()
    return {r[0]: {"home": r[1], "away": r[2], "league": r[3],
                   "kickoff": r[4]} for r in rows}


def _results_from_the_odds_api(meta: Dict[str, dict]) -> int:
    """Punteggi via the-odds-api (fetch_scores, match per NOME+LEGA).

    Stessa logica di bot._update_results (match_scores_by_name per evitare
    inversioni casa/trasferta). Ritorna il numero di match_results salvati.
    """
    from tracker import _norm_team, save_result
    from odds_api import fetch_scores, match_scores_by_name, SPORTS_MAP
    leagues = {info.get("league") for info in meta.values()}
    leagues.discard(None)
    saved = 0
    for lg in sorted(leagues):
        sport = SPORTS_MAP.get(lg)
        if not sport:
            continue
        try:
            scores = fetch_scores(sport, days_from=2)
        except Exception as e:
            logger.warning("sx_signals: fetch_scores %s fallita: %s", lg, e)
            continue
        for m in scores:
            parsed = match_scores_by_name(m)
            if parsed is None:
                continue
            sh, sa = parsed
            mh = _norm_team(m.get("home_team", ""))
            ma = _norm_team(m.get("away_team", ""))
            for mid, info in meta.items():
                if info.get("league") != lg:
                    continue
                if (_norm_team(info["home"]) == mh
                        and _norm_team(info["away"]) == ma):
                    save_result(mid, lg, info["home"], info["away"], sh, sa, "")
                    saved += 1
                    break
    return saved


def _results_from_api_football(meta: Dict[str, dict]) -> int:
    """Punteggi via API-Football per i match ancora senza risultato.

    Una query per (lega, giorno) su /fixtures (league+season+from+to) con
    LEAGUE_IDS di football_hist; le leghe non mappate vengono saltate.
    Ritorna il numero di match_results salvati.
    """
    if not os.getenv("API_FOOTBALL_KEY"):
        return 0
    from tracker import _get_conn, save_result, _norm_team
    import football_hist as fh
    # Salta i match che hanno GIA' un risultato (evita query inutili).
    conn = _get_conn()
    have = {r[0] for r in conn.execute(
        "SELECT match_id FROM match_results").fetchall()}
    conn.close()
    todo = {mid: info for mid, info in meta.items()
            if mid not in have and info.get("league") in fh.LEAGUE_IDS}
    if not todo:
        return 0
    # Raggruppa per (league_id, data kickoff YYYY-MM-DD): una query ciascuno.
    groups: Dict[tuple, list] = {}
    for mid, info in todo.items():
        day = str(info.get("kickoff") or "")[:10]
        lid = fh.LEAGUE_IDS[info["league"]]
        if day:
            groups.setdefault((info["league"], lid, day), []).append((mid, info))
    saved = 0
    year = datetime.now(timezone.utc).year
    for (league, lid, day), items in groups.items():
        for season in (year, year - 1):
            body = fh._api_get("fixtures", {
                "league": lid, "season": season,
                "from": day, "to": day})
            for fx in (body or {}).get("response") or []:
                parsed = fh._parse_fixture(fx, league)
                if not parsed:
                    continue
                _fx_id, home_db, away_db, sh, sa, _date = parsed
                for mid, info in items:
                    if (_norm_team(info["home"]) == _norm_team(home_db)
                            and _norm_team(info["away"]) == _norm_team(away_db)):
                        save_result(mid, info["league"], info["home"],
                                    info["away"], sh, sa, "")
                        saved += 1
            # Se abbiamo coperto tutti i match del gruppo, stop (risparmio
            # crediti piano free: niente query sull'anno precedente).
            conn = _get_conn()
            still = conn.execute(
                "SELECT COUNT(*) FROM match_results WHERE match_id IN "
                "(%s)" % ",".join("?" * len(items)),
                tuple(mid for mid, _ in items)).fetchone()[0]
            conn.close()
            if still >= len(items):
                break
    return saved


def settle_sx_bets() -> dict:
    """Salda le bet SX aperte: risultati (fonti esterne) + settle_bets.

    Le bet senza risultato disponibile restano aperte (fail-closed: mai
    chiudere un verdetto senza punteggio reale).
    """
    from tracker import settle_bets
    meta = _sx_open_matches()
    if not meta:
        return {"open": 0, "results": 0, "settled": 0, "source": None}
    results = 0
    source = None
    if os.getenv("ODDS_API_KEY"):
        try:
            results = _results_from_the_odds_api(meta)
            source = "the-odds-api" if results else source
        except Exception as e:
            logger.warning("sx_signals: settlement the-odds-api fallito: %s", e)
    if not results and os.getenv("API_FOOTBALL_KEY"):
        try:
            results = _results_from_api_football(meta)
            source = "api-football" if results else source
        except Exception as e:
            logger.warning("sx_signals: settlement api-football fallito: %s", e)
    settled, pushes = settle_bets()
    return {"open": len(meta), "results": results, "settled": settled,
            "pushes": pushes, "source": source}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "settle":
        res = settle_sx_bets()
        print(f"✅ settlement SX: {res}")
    else:
        sig = scan()
        print(f"✅ {len(sig)} segnali value salvati")
        for s in sig:
            print(f"• {s['home']} vs {s['away']} — {s['esito']} @ "
                  f"{s['quota']:.2f} (EV {s['ev'] * 100:+.1f}%) [{s['status']}]")
