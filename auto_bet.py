"""auto_bet.py — Puntate automatiche giornaliere su Betfair Exchange.

Flusso del mattino (job 08:50 UTC, dopo scansione e analisi):

1. Legge i segnali value/strong_value del giorno da match_analysis (quelli
   che battono il mercato, come la schedina);
2. Trova sul catalogo Betfair (cache scan_<giorno>.json) il mercato e il
   runner corrispondenti (MATCH_ODDS -> 1/X/2, OVER_UNDER_25 -> Over/Under 2.5);
3. Piazza un ordine BACK a limite con stake FISSO (BET_STAKE_EUR, default
   5.00 EUR; minimo Exchange Italia 2.00, step 0.50);
4. Registra l'ordine nella tabella `bets` per tracking, settlement e
   riepilogo di fine giornata.

Sicurezza (ereditata da betfair_client): DRY-RUN di default — nessun ordine
reale finche' BETFAIR_DRY_RUN=0 E BETFAIR_LIVE=1; kill-switch
(data/kill_switch) blocca tutto; ogni ordine finisce su data/orders.jsonl.

Regole prudenti di esecuzione (scelte per questo progetto):
- si scommette SOLO su segnali value/strong_value che battono il mercato;
- si salta una partita se manca < 15 minuti al calcio d'inizio;
- si salta se il prezzo back dell'Exchange e' oltre il 5% sotto la quota
  del segnale (l'edge si sarebbe eroso);
- una sola puntata per (match, esito): la UNIQUE(match_id, esito) in `bets`
  impedisce di raddoppiare se il job viene rilanciato.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from betfair_client import get_client, normalize_stake
from config import DATA_DIR
from daily_scanner import _split_teams, _DRAW_NAMES
from daily_scan_job import load_latest_scan

logger = logging.getLogger("auto_bet")

BET_STAKE_DEFAULT_EUR = 5.0
MIN_MINUTES_TO_START = 15
PRICE_GUARD = 0.95  # accetta solo prezzi >= 95% della quota del segnale

# --- Correlation risk cap ---
# Kelly assume indipendenza tra le puntate: due o piu' esiti correlati nello
# stesso blocco temporale (stessa partita, stessa lega con kickoff ravvicinati)
# moltiplicano la varianza reale. Ogni stake viene ridotto proporzionalmente
# quando l'esposizione totale del blocco supera il cap.
CORRELATION_CAP_PCT = 0.30     # max 30% di bankroll per blocco correlato
CORRELATION_WINDOW_MIN = 90    # kickoff entro 90' = stesso blocco temporale
# Cap di portafoglio: esposizione TOTALE del giorno (somma di tutti gli
# stake) <= 40% del bankroll. Kelly dimensiona ogni stake singolarmente;
# senza questo cap, 5+ segnali indipendenti sommano comunque un rischio
# complessivo che cresce col numero di pick (varianza additiva).
TOTAL_EXPOSURE_CAP_PCT = 0.40  # max 40% di bankroll per il portafoglio del giorno


def _kickoff_utc(commence: str | None):
    """Timestamp kickoff come datetime UTC naive (None se non parsabile)."""
    if not commence:
        return None
    try:
        return datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
    except Exception:
        return None


def apply_correlation_cap(candidates: list[dict], bankroll: float,
                          cap_pct: float = CORRELATION_CAP_PCT,
                          window_min: int = CORRELATION_WINDOW_MIN) -> list[dict]:
    """Riduce gli stake dei candidati correlati per proteggere il bankroll.

    Kelly calcola ogni stake come se le puntate fossero indipendenti: se il
    job piazza piu' esiti correlati (stessa partita, oppure stessa lega con
    kickoff nello stesso blocco temporale), la varianza reale del portafoglio
    e' piu' alta di quella modellata e il rischio di drawdown cresce. Questa
    funzione raggruppa i candidati per match / lega+finestra e, se
    l'esposizione totale del blocco supera cap_pct * bankroll, scala
    PROPORZIONALMENTE gli stake (mantiene il ranking EV, non taglia esiti).

    Regole di correlazione:
    - stesso match_id (es. 1X2 + Over sulla stessa partita): SEMPRE correlati;
    - stessa lega E kickoff entro window_min (stesso blocco temporale): il
      mercato si muove sugli stessi fattori -> varianza condivisa;
    - leghe diverse o kickoff lontani: indipendenti, nessun cap.

    Args:
        candidates: picks con almeno match_id, league (opz.), commence e stake.
        bankroll: bankroll corrente per il cap.
        cap_pct: frazione di bankroll massima per blocco correlato.
        window_min: finestra temporale (minuti) per raggruppare i kickoff.

    Returns:
        I candidati con stake scalati; aggiunge "corr_cap" (True se
        ridotto) e "corr_group" (descrizione del blocco) per il log.
    """
    if len(candidates) < 2 or bankroll <= 0:
        return candidates

    def _group_key(cand) -> str:
        # Gruppo per LEGA (default 'global' se assente): i match diversi
        # della stessa lega condividono la varianza di giornata/arbitri e
        # si separano in blocchi temporali sulla finestra window_min.
        # Lo stesso match_id ha sempre lo stesso kickoff, quindi esiti
        # multipli della stessa partita finiscono nello stesso blocco.
        return f"league:{cand.get('league') or 'global'}"

    def _same_match(a, b) -> bool:
        # Stesso match_id = SEMPRE correlati (es. 1X2 + Over stessa partita),
        # anche se i commence non sono parsabili in modo identico.
        mid = a.get("match_id")
        return bool(mid) and mid == b.get("match_id")

    # Raggruppa per lega; dentro ogni lega separa i blocchi temporali
    # disgiunti (finestra window_min). Algoritmo greedy: ogni candidato
    # entra nel primo blocco compatibile per tempo (o per match_id).
    groups: list[list[dict]] = []
    group_bounds: list[tuple] = []  # (min_kickoff, max_kickoff) UTC naive
    for cand in candidates:
        k = _kickoff_utc(cand.get("commence"))
        key = _group_key(cand)
        assigned = False
        for gi, g in enumerate(groups):
            if _group_key(g[0]) != key:
                continue
            gmin, gmax = group_bounds[gi]
            in_window = (k is not None and gmin is not None
                         and abs((k - gmin).total_seconds()) <= window_min * 60)
            if in_window or _same_match(g[0], cand) or (k is None and gmin is None):
                g.append(cand)
                if k is not None:
                    nmin = min(gmin, k) if gmin else k
                    nmax = max(gmax, k) if gmax else k
                    group_bounds[gi] = (nmin, nmax)
                assigned = True
                break
        if not assigned:
            groups.append([cand])
            group_bounds.append((k, k))

    cap = bankroll * cap_pct
    capped_total = 0.0
    capped_groups = 0
    for g in groups:
        total = sum(float(c.get("stake", 0) or 0) for c in g)
        if total <= cap:
            continue
        factor = cap / total
        for c in g:
            raw = float(c.get("stake", 0) or 0)
            c["stake"] = round(raw * factor, 2)
            c["corr_cap"] = True
            c["corr_group"] = (f"{len(g)} esiti correlati "
                               f"(esposizione €{total:.2f} > cap €{cap:.2f})")
        capped_total += total - cap
        capped_groups += 1

    if capped_groups:
        logger.info("auto_bet: correlation cap attivo — ridotti €%.2f di "
                    "stake correlati in %d blocchi sopra il cap",
                    capped_total, capped_groups)
    return candidates


def apply_total_exposure_cap(candidates: list[dict], bankroll: float,
                             cap_pct: float = TOTAL_EXPOSURE_CAP_PCT) -> list[dict]:
    """Cap di portafoglio: esposizione totale del giorno <= cap_pct del bankroll.

    Kelly dimensiona ogni stake come se fosse l'unica puntata: anche senza
    correlazione tra i segnali, la varianza del portafoglio cresce con il
    numero di pick. Se la somma di TUTTI gli stake supera cap_pct * bankroll,
    gli stake vengono scalati PROPORZIONALMENTE (mantiene il ranking EV e i
    rapporti tra le puntate, non taglia esiti).

    Args:
        candidates: picks con stake (gia' passati dal correlation cap).
        bankroll: bankroll corrente.
        cap_pct: frazione di bankroll massima per l'esposizione totale.

    Returns:
        I candidati con stake scalati; aggiunge "total_cap" (True se
        ridotto) e "total_cap_group" (descrizione) per il log.
    """
    if len(candidates) < 2 or bankroll <= 0:
        return candidates
    total = sum(float(c.get("stake", 0) or 0) for c in candidates)
    cap = bankroll * cap_pct
    if total <= cap:
        return candidates
    factor = cap / total
    for c in candidates:
        raw = float(c.get("stake", 0) or 0)
        c["stake"] = round(raw * factor, 2)
        c["total_cap"] = True
        c["total_cap_group"] = (f"esposizione totale €{total:.2f} > cap €{cap:.2f}")
    logger.info("auto_bet: cap esposizione totale attivo — ridotti €%.2f di "
                "stake (%d pick sopra il cap €%.2f)",
                total - cap, len(candidates), cap)
    return candidates


def _norm_team(name: str) -> str:
    from tracker import _norm_team as nt
    return nt(name)


_TEAM_ALIAS_CACHE: dict[str, str] = {}


def _resolve_team(name: str) -> str:
    """Normalizza un nome squadra E risolve gli alias comuni (TEAM_MAP).

    'AC Milan' -> 'milan', 'West Ham United' -> 'west ham': allinea i nomi
    del catalogo Betfair con quelli del segnale. Fallback sicuro: se
    TEAM_MAP non e' importabile, resta solo la normalizzazione base.
    """
    base = _norm_team(name)
    if base in _TEAM_ALIAS_CACHE:
        return _TEAM_ALIAS_CACHE[base]
    resolved = base
    try:
        from fixture_engine import TEAM_MAP
        resolved = _norm_team(TEAM_MAP.get(base, base))
    except Exception:
        pass
    _TEAM_ALIAS_CACHE[base] = resolved
    return resolved


def _runner_esito(market_type: str, selection_name: str,
                  event_teams: tuple) -> str | None:
    """Esito canonico del runner Betfair, con alias squadre.

    MATCH_ODDS: il runner deve essere il draw o UNA delle due squadre
    dell'evento (confronto con alias) -> '1'/'2'/'X'. Se non e'
    riconosciuto ritorna None: MAI piazzare su un runner ambiguo.
    OVER_UNDER_25: il nome del runner e' gia' 'Over 2.5'/'Under 2.5'.
    """
    sel = (selection_name or "").strip()
    if not sel:
        return None
    if market_type == "OVER_UNDER_25":
        low = sel.lower()
        if "over" in low:
            return "Over 2.5"
        if "under" in low:
            return "Under 2.5"
        return None
    # MATCH_ODDS
    low = sel.lower()
    if low in _DRAW_NAMES:
        return "X"
    home, away = event_teams
    sr = _resolve_team(sel)
    if sr == _resolve_team(home):
        return "1"
    if sr == _resolve_team(away):
        return "2"
    return None


def _canonical_esito(esito: str, home: str, away: str) -> dict | None:
    """Esito del segnale -> (mercato, esito_key) canonici per Betfair."""
    el = str(esito or "").lower().strip()
    if "over" in el:
        return {"mercato": "OU", "esito_key": "Over 2.5"}
    if "under" in el:
        return {"mercato": "OU", "esito_key": "Under 2.5"}
    if el in ("x", "draw", "pareggio"):
        return {"mercato": "1X2", "esito_key": "X"}
    if el == "1":
        return {"mercato": "1X2", "esito_key": "1"}
    if el == "2":
        return {"mercato": "1X2", "esito_key": "2"}
    hn, an, en = _resolve_team(home), _resolve_team(away), _resolve_team(el)
    if en == hn:
        return {"mercato": "1X2", "esito_key": "1"}
    if en == an:
        return {"mercato": "1X2", "esito_key": "2"}
    return None


def _today_value_picks() -> list[dict]:
    """Partite del giorno con segnale value/strong_value (esito canonico).

    Include market_edge, market_prob, best_ev e status per l'adaptive staking.
    """
    from tracker import _get_conn
    conn = _get_conn()
    c = conn.cursor()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = c.execute('''SELECT m.id, m.home_team, m.away_team, m.commence_time,
                               m.league, a.best_esito, a.best_quota, a.market_edge,
                               a.market_prob, a.best_ev, a.status
                        FROM matches m JOIN match_analysis a ON m.id = a.match_id
                        WHERE m.commence_time LIKE ? AND a.status IN ('value','strong_value')
                        ORDER BY a.best_ev DESC''', (f"{today}%",)).fetchall()
    conn.close()
    out = []
    for (mid, home, away, commence, league, esito, quota,
         m_edge, m_prob, ev, status) in rows:
        canon = _canonical_esito(esito, home, away)
        if not canon:
            continue
        out.append({"match_id": mid, "home": home, "away": away,
                    "commence": commence, "league": league or "",
                    "esito_raw": esito,
                    "quota": float(quota or 0),
                    "market_edge": float(m_edge) if m_edge is not None else None,
                    "market_prob": float(m_prob) if m_prob is not None else None,
                    "best_ev": float(ev) if ev is not None else 0.0,
                    "status": status or "value",
                    **canon})
    return out


def _find_opportunity(opps: list[dict], pick: dict) -> dict | None:
    """Trova il runner Betfair del segnale nel catalogo di scansione.

    Verifica INCROCIATA: le squadre dell'evento (risolte con gli alias)
    devono coincidere con quelle del segnale E il runner deve essere
    riconosciuto (draw o una delle due squadre) — mai un runner ambiguo.
    """
    mtype = "MATCH_ODDS" if pick["mercato"] == "1X2" else "OVER_UNDER_25"
    target = (_resolve_team(pick["home"]), _resolve_team(pick["away"]))
    best = None
    for o in opps:
        if o.get("market_type") != mtype:
            continue
        teams = _split_teams(o.get("event_name") or "")
        if not teams:
            continue
        if (_resolve_team(teams[0]), _resolve_team(teams[1])) != target:
            continue
        esito = _runner_esito(mtype, o.get("selection_name") or "", teams)
        if esito != pick["esito_key"]:
            continue
        price = o.get("price")
        if not price or float(price) <= 1.0:
            continue
        if best is None or float(price) > best["price"]:
            best = {**o, "price": float(price)}
    return best


def _too_close_to_start(start_time: str | None) -> bool:
    if not start_time:
        return False
    try:
        start = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
    except Exception:
        return False
    return start <= datetime.now(timezone.utc) + timedelta(minutes=MIN_MINUTES_TO_START)


def run_today_bets(client=None, stake_eur: float | None = None,
                    allow_sim: bool = False) -> list[dict]:
    """Piazza le puntate del giorno. Ritorna il riepilogo.

    - Betfair configurato: dry-run di default (ordini reali solo con
      BETFAIR_DRY_RUN=0 + BETFAIR_LIVE=1), prezzi dal catalogo Exchange.
    - allow_sim=True: se Betfair non e' disponibile (o manca il catalogo),
      piazza puntate SIMULATE con la quota del segnale (mode='sim') — paper
      test senza Exchange, per addestrare ledger/ML anche senza credenziali.

    Non lancia mai eccezioni verso il chiamante: ogni passo fallito viene
    loggato e saltato (fail-closed, come betfair_client).
    """
    if client is None:
        client = get_client()

    # Default stake fisso (fallback se adaptive staking non disponibile)
    stake_eur_default = stake_eur if stake_eur is not None else float(
        os.getenv("BET_STAKE_EUR", str(BET_STAKE_DEFAULT_EUR)))

    # Carica adaptive staking (lazy)
    try:
        from adaptive_staking import adaptive_stake, bankroll_stats
        _adaptive = True
        _bankroll_stats = bankroll_stats()
        # Cassa vuota (current=0) -> default €100, coerente con la dashboard
        # e con il comportamento pre-adaptive: mai puntare con bankroll 0.
        _bankroll = _bankroll_stats.get("current") or 100.0
        _peak = _bankroll_stats.get("peak") or _bankroll
    except ImportError:
        _adaptive = False
        _bankroll = 100.0
        _peak = 100.0
        logger.info("auto_bet: adaptive_staking non disponibile, uso stake fisso")

    # Carica CLV storico per la confidenza
    try:
        from tracker import _get_conn as _gc
        _conn = _gc()
        _clv_row = _conn.execute(
            "SELECT AVG(CASE WHEN closing_quota > 0 "
            "THEN signal_quota / closing_quota - 1.0 ELSE 0 END) "
            "FROM clv_history").fetchone()
        _conn.close()
        _avg_clv = float(_clv_row[0]) if _clv_row and _clv_row[0] else 0.0
    except Exception:
        _avg_clv = 0.0

    scan = load_latest_scan() if client else None
    sim = False
    if client is None:
        if not allow_sim:
            logger.info("auto_bet: Betfair non configurato (BETFAIR_APP_KEY assente), salto")
            return []
        sim = True
        logger.info("auto_bet: Betfair assente, modalita' SIM (paper senza Exchange)")
    elif not scan or not scan.get("opportunities"):
        if not allow_sim:
            logger.info("auto_bet: nessun catalogo Betfair in cache, salto")
            return []
        sim = True
        logger.info("auto_bet: catalogo Betfair assente, modalita' SIM")
    elif scan.get("day") != datetime.now(timezone.utc).strftime("%Y-%m-%d"):
        if not allow_sim:
            logger.info("auto_bet: catalogo del %s non di oggi, salto", scan.get("day"))
            return []
        sim = True
        logger.info("auto_bet: catalogo del %s non di oggi, modalita' SIM", scan.get("day"))

    from tracker import bet_exists_open

    # --- FASE 1: costruisci i candidati (guardie + stake, senza piazzare) ---
    candidates: list[dict] = []
    for pick in _today_value_picks():
        if bet_exists_open(pick["match_id"], pick["esito_key"]):
            logger.info("auto_bet: puntata gia' aperta per %s (%s), salto",
                        pick["match_id"], pick["esito_key"])
            continue

        price = None
        opp = None
        if sim:
            # Paper senza Exchange: quota del segnale, nessun catalogo.
            if _too_close_to_start(pick.get("commence")):
                logger.info("auto_bet: %s vs %s a meno di %d min dall'inizio, salto",
                            pick["home"], pick["away"], MIN_MINUTES_TO_START)
                continue
            price = float(pick["quota"] or 0)
            if price <= 1.0:
                logger.info("auto_bet: quota segnale non valida per %s, salto",
                            pick["match_id"])
                continue
        else:
            opp = _find_opportunity(scan["opportunities"], pick)
            if not opp:
                logger.info("auto_bet: nessun mercato Betfair per %s vs %s (%s)",
                            pick["home"], pick["away"], pick["esito_key"])
                continue
            if _too_close_to_start(opp.get("start_time")):
                logger.info("auto_bet: %s vs %s a meno di %d min dall'inizio, salto",
                            pick["home"], pick["away"], MIN_MINUTES_TO_START)
                continue
            price = opp["price"]
            if price < (pick["quota"] or 0) * PRICE_GUARD:
                logger.info("auto_bet: prezzo Exchange %.2f sotto il %.0f%% della quota "
                            "segnale %.2f — salto %s", price, PRICE_GUARD * 100,
                            pick["quota"], pick["esito_key"])
                continue

        # Adaptive staking: stake dinamico (identico per SIM e live)
        if _adaptive:
            as_result = adaptive_stake(
                bankroll=_bankroll, prob=pick.get("best_ev", 0.0) + 1.0 / price if price > 0 else 0.5,
                odds=price, market_edge=pick.get("market_edge"),
                status=pick.get("status", "value"),
                peak_bankroll=_peak,
                # CLV storico: conferma dell'edge -> stake piu' alto se
                # stiamo battendo la closing line (wiring del segnale CLV).
                has_clv_positive=(_avg_clv > 0.0))
            pick_stake = as_result["stake"]
            if pick_stake <= 0:
                logger.info("auto_bet: stake adaptive = 0 per %s (EV negativo), salto",
                            pick["match_id"])
                continue
            logger.info("auto_bet: stake adaptive €%.2f per %s (%s)",
                        pick_stake, pick["match_id"], as_result["reason"])
        else:
            pick_stake = normalize_stake(stake_eur_default)
        if pick_stake <= 0:
            logger.info("auto_bet: stake %.2f sotto il minimo per %s, salto",
                        pick_stake, pick["match_id"])
            continue

        candidates.append({
            **pick, "price": price, "stake": pick_stake, "opp": opp,
        })

    # --- FASE 2: risk capping (correlazione + esposizione totale) ---
    candidates = apply_correlation_cap(candidates, _bankroll)
    candidates = apply_total_exposure_cap(candidates, _bankroll)

    # --- FASE 3: piazza le puntate con gli stake finali ---
    from tracker import save_bet
    placed: list[dict] = []
    for cand in candidates:
        pick_stake = cand["stake"]
        price = cand["price"]
        if cand.get("corr_cap"):
            logger.info("auto_bet: stake ridotto da correlation cap per %s "
                        "(%s): €%.2f", cand["match_id"],
                        cand.get("corr_group", ""), pick_stake)
        if cand.get("total_cap"):
            logger.info("auto_bet: stake ridotto da cap esposizione totale per "
                        "%s (%s): €%.2f", cand["match_id"],
                        cand.get("total_cap_group", ""), pick_stake)
        if sim:
            record = {**cand, "market_id": None, "selection_id": None,
                      "status": "SUCCESS", "bet_id": None, "mode": "sim"}
            record.pop("opp", None)
            placed.append(record)
            try:
                save_bet(match_id=cand["match_id"], mercato=cand["mercato"],
                         esito=cand["esito_key"], market_id=None, selection_id=None,
                         price=price, stake=pick_stake, mode="sim", status="SUCCESS")
            except Exception as e:
                logger.warning("auto_bet: salvataggio sim %s: %s", cand["match_id"], e)
            continue

        opp = cand["opp"]
        ins = {"selectionId": opp["selection_id"], "side": "BACK",
               "orderType": "LIMIT",
               "limitOrder": {"size": pick_stake, "price": price,
                              "persistenceType": "LAPSE"}}
        try:
            result = client.place_orders(opp["market_id"], [ins],
                                         customer_ref=f"qv-{cand['match_id']}")
        except Exception as e:
            logger.warning("auto_bet: ordine fallito %s: %s", cand["match_id"], e)
            continue
        status = (result or {}).get("status")
        bet_id = (result or {}).get("betId")
        mode = "live" if not client.dry_run else "dry-run"
        record = {**cand, "market_id": opp["market_id"],
                  "selection_id": opp["selection_id"], "status": status,
                  "bet_id": bet_id, "mode": mode}
        record.pop("opp", None)
        placed.append(record)
        try:
            save_bet(match_id=cand["match_id"], mercato=cand["mercato"],
                     esito=cand["esito_key"], market_id=opp["market_id"],
                     selection_id=opp["selection_id"], price=price, stake=pick_stake,
                     mode=mode, status=status, bet_id=bet_id)
        except Exception as e:
            logger.warning("auto_bet: salvataggio ordine %s: %s", cand["match_id"], e)

    logger.info("auto_bet: %d puntate piazzate (%s)",
                len(placed), placed[0]["mode"] if placed else "nessuna")
    return placed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    res = run_today_bets()
    print(f"✅ {len(res)} puntate piazzate (modalità "
          f"{res[0]['mode'] if res else 'nessuna'})")
    for p in res:
        print(f"• {p['home']} vs {p['away']} — {p['esito_key']} @ {p['price']:.2f} "
              f"(€{p['stake']:.2f}) [{p['status']}]")