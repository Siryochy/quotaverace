"""auto_bet.py — Puntate automatiche giornaliere (SIM oppure LIVE via engine).

DAL 04/09 le puntate automatiche sono state rese SIMULATE (paper trading
con la quota del segnale) per alimentare ledger, CLV e dataset ML senza
conto Exchange. DAL 08/09 auto_bet puo' piazzare ORDINI REALI collegando i
segnali value a `execution_engine.py` (provider SX Bet V3, primo provider
abilitato, poi Smarkets/aggregatori): la modalita' si attiva con
`AUTO_BET_MODE=live` (o `real`) E un provider reale configurato
(EXECUTION_PROVIDER + credenziali). Senza quelle condizioni resta SIM
(default sicuro) — oppure non piazza nulla se il chiamante richiede il
fail-closed (allow_sim=False).

Flusso del mattino (job 08:50 UTC, dopo analisi):

1. Legge i segnali value/strong_value del giorno da match_analysis (quelli
   che battono il mercato, come la schedina);
2. Per ogni segnale calcola lo stake ADATTIVO (Kelly frazionato dinamico
   con drawdown protection e confidence weighting);
3. Esegue: in LIVE risolve il mercato dell'exchange (evento+esito),
   verifica che il prezzo disponibile sia >= quota del segnale (floor EV:
   mai riempirsi sotto la quota su cui e' stato calcolato l'edge) e piazza
   l'ordine; in SIM registra la puntata simulata con la quota del segnale;
4. Registra la puntata nella tabella `bets` (mode='live' con market_id /
   selection_id / bet_id reali, mode='sim' altrimenti) per tracking,
   settlement e riepilogo di fine giornata (settle_bets la salda come
   sempre). Un ordine reale NON riempito o saltato non lascia righe.

Regole prudenti di esecuzione (scelte per questo progetto):
- si scommette SOLO su segnali value/strong_value che battono il mercato;
- si salta una partita se manca < 15 minuti al calcio d'inizio;
- una sola puntata per (match, esito): la UNIQUE(match_id, esito) in `bets`
  impedisce di raddoppiare se il job viene rilanciato;
- correlation risk cap (30% bankroll per blocco correlato) + cap esposizione
  totale del giorno (40% bankroll), applicati PRIMA di salvare;
- in LIVE il market dell'exchange deve riferirsi alla STESSA partita del
  segnale (nomi squadre + kickoff): nessun ordine su eventi ambigui
  (fail-closed), e nessun riempimento sotto la quota-segnale (floor EV).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from config import DATA_DIR

logger = logging.getLogger("auto_bet")

BET_STAKE_DEFAULT_EUR = 5.0
MIN_MINUTES_TO_START = 15

# --- Esecuzione reale via execution_engine (wiring dal 08/09) ---
# AUTO_BET_MODE=live|real -> ordini REALI sul provider configurato
# (EXECUTION_PROVIDER=sxbet|smarkets|betinasia|mollybet + credenziali).
# Qualunque altro valore (default) -> SIM. Se il chiamante passa
# allow_sim=False e il live non e' disponibile, non si piazza nulla
# (fail-closed).
REAL_MODE_VALUES = ("live", "real", "1", "true", "on")

# --- Kill-switch Telegram (dal 08/09) ---
# Override persistente scritto dal comando /autobet: il file vive sul volume
# condiviso (data/execution/auto_bet_mode.json) quindi sopravvive ai
# redeploy. Valori:
#   "off"  -> STOP TOTALE: nessuna puntata (ne' reale ne' simulata);
#   "sim"  -> PAUSA ordini reali: resta solo il paper trading;
#   "live" -> ripristina AUTO_BET_MODE env (nessun override).
# In assenza del file vale AUTO_BET_MODE env (default "sim").
KILL_SWITCH_FILE = DATA_DIR / "execution" / "auto_bet_mode.json"
KILL_SWITCH_VALUES = ("off", "sim", "live")

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

# Staking 100% dinamico (08/09): NESSUN importo fisso. Lo stake lo decide
# il Kelly frazionato sul bankroll corrente (saldo reale del wallet in
# LIVE). Restano solo due vincoli di sicurezza:
#   - STAKE_STEP_EUR (default 0.01): arrotondamento fine, niente step fissi;
#   - MIN_STAKE_EUR (default 1.0): floor dell'exchange (SX Bet: 1 USDC).
STAKE_STEP_EUR = float(os.getenv("STAKE_STEP_EUR", "0.01"))
MIN_STAKE_EUR = float(os.getenv("MIN_STAKE_EUR", "1.0"))


def normalize_stake(stake: float) -> float:
    """Arrotonda allo step configurato (default 0.01) e forza il minimo
    (default 1.0 = minimo ordine SX Bet in USDC, env MIN_STAKE_EUR).
    Sotto il minimo: 0 (no bet)."""
    if stake <= 0:
        return 0.0
    stepped = round(stake / STAKE_STEP_EUR) * STAKE_STEP_EUR
    stepped = round(stepped, 2)
    if stepped < MIN_STAKE_EUR:
        return 0.0
    return stepped


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
    del segnale (the-odds-api) con quelli canonici. Fallback sicuro: se
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


_DRAW_NAMES = ("the draw", "draw", "pareggio")


def _canonical_esito(esito: str, home: str, away: str) -> dict | None:
    """Esito del segnale -> (mercato, esito_key) canonici per il ledger."""
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
    """Partite in programma nelle prossime 24h con segnale value/strong_value
    (esito canonico).

    Finestra MOBILE (now .. now+24h) invece del giorno calendario: a fine
    giornata UTC un match con kickoff poco dopo la mezzanotte cadrebbe nel
    giorno dopo e verrebbe perso dal filtro per data. Include market_edge,
    market_prob, best_ev e status per l'adaptive staking.
    """
    from tracker import _get_conn
    conn = _get_conn()
    c = conn.cursor()
    now_utc = datetime.now(timezone.utc)
    start = now_utc.isoformat().replace("+00:00", "Z")
    end = (now_utc + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    rows = c.execute('''SELECT m.id, m.home_team, m.away_team, m.commence_time,
                               m.league, a.best_esito, a.best_quota, a.market_edge,
                               a.market_prob, a.best_ev, a.status
                        FROM matches m JOIN match_analysis a ON m.id = a.match_id
                        WHERE m.commence_time >= ? AND m.commence_time < ?
                          AND a.status IN ('value','strong_value')
                        ORDER BY a.best_ev DESC''', (start, end)).fetchall()
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


def _too_close_to_start(start_time: str | None) -> bool:
    if not start_time:
        return False
    try:
        start = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
    except Exception:
        return False
    return start <= datetime.now(timezone.utc) + timedelta(minutes=MIN_MINUTES_TO_START)


def _kill_switch_override() -> str | None:
    """Override persistente scritto da /autobet (kill-switch Telegram).

    None se non impostato. Il file vive in data/execution/ (volume
    condiviso) per sopravvivere ai redeploy.
    """
    try:
        data = json.loads(KILL_SWITCH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    mode = str(data.get("mode", "")).strip().lower()
    return mode if mode in KILL_SWITCH_VALUES else None


def set_kill_switch(mode: str) -> dict:
    """Imposta l'override del kill-switch (comando Telegram /autobet).

    mode: "off" (stop totale), "sim" (pausa ordini reali),
    "live" (ripristina AUTO_BET_MODE env). Scrittura atomica sul volume.
    """
    mode = str(mode).strip().lower()
    if mode == "real":
        mode = "live"
    if mode not in KILL_SWITCH_VALUES:
        raise ValueError(
            f"modalita' non valida: {mode!r} (attese: off|sim|live)")
    KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"mode": mode,
            "updated_at": datetime.now(timezone.utc).isoformat()}
    tmp = KILL_SWITCH_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, KILL_SWITCH_FILE)
    return data


def clear_kill_switch() -> None:
    """Rimuove l'override: si torna ad AUTO_BET_MODE env."""
    try:
        KILL_SWITCH_FILE.unlink()
    except FileNotFoundError:
        pass


def kill_switch_status() -> dict:
    """Stato del kill-switch per /autobet (Telegram)."""
    override = _kill_switch_override()
    env_mode = os.getenv("AUTO_BET_MODE", "sim").strip().lower()
    requested = override or env_mode
    if override == "off":
        effective = "off"
    elif requested in REAL_MODE_VALUES and _provider_ready():
        effective = "live"
    else:
        effective = "sim"
    return {
        "override": override,
        "env_mode": env_mode,
        "requested": requested,
        "effective": effective,
        "provider_ready": _provider_ready(),
        "file": str(KILL_SWITCH_FILE),
    }


def _requested_mode() -> str:
    """Modalita' richiesta: override del kill-switch Telegram se presente,
    altrimenti env AUTO_BET_MODE (default 'sim')."""
    override = _kill_switch_override()
    if override:
        return override
    return os.getenv("AUTO_BET_MODE", "sim").strip().lower()


def _provider_ready() -> bool:
    """True se execution_engine puo' piazzare ordini REALI (provider
    selezionato da EXECUTION_PROVIDER + credenziali, nessun DryRun)."""
    try:
        import execution_engine as ee
    except Exception:
        return False
    try:
        if ee.EXECUTION_DRY_RUN:
            return False
        if ee.EXECUTION_PROVIDER not in ("sxbet", "smarkets",
                                         "betinasia", "mollybet"):
            return False
        return bool(ee._creds_configured())
    except Exception:
        return False


def _live_wallet_balance() -> float | None:
    """Saldo DISPONIBILE del provider reale (SX Bet: proxy wallet in USDC).

    None se non leggibile (dry-run, errore di rete, credenziali assenti):
    il chiamante ripiega sul bankroll cassa. Il saldo reale diventa il
    bankroll del Kelly in modalita' live: ogni stake e' dimensionato su
    quanto c'e' DAVVERO nel wallet.
    """
    try:
        import execution_engine as ee
        engine = ee.ExecutionEngine()
        if isinstance(engine.provider, ee.DryRunProvider):
            return None
        bal = engine.provider.get_balance()
        return float(bal.get("availableBalance") or 0.0)
    except Exception as e:
        logger.warning("auto_bet: lettura saldo wallet fallita: %s", e)
        return None


def _execution_mode(allow_sim: bool = True) -> str:
    """Modalita' effettiva del giro di puntate.

    'live' solo se AUTO_BET_MODE richiede l'esecuzione reale E il provider
    e' configurato (mai ordini reali senza configurazione esplicita).
    Altrimenti 'sim' (fallback di default) oppure 'off' (fail-closed
    richiesto dal chiamante).
    """
    mode = _requested_mode()
    if mode == "off":
        # Kill-switch /autobet off: STOP TOTALE, mai puntate (ne' reali ne'
        # simulate) finche' l'admin non riattiva.
        logger.warning("auto_bet: kill-switch OFF attivo — nessuna puntata")
        return "off"
    if mode in REAL_MODE_VALUES:
        if _provider_ready():
            return "live"
        logger.warning(
            "auto_bet: AUTO_BET_MODE=%s ma nessun provider reale configurato "
            "(EXECUTION_PROVIDER + credenziali) -> %s",
            mode, "SIM (fallback)" if allow_sim else "nessuna puntata")
        return "sim" if allow_sim else "off"
    return "sim"


def _live_fill(pick: dict, stake: float, floor: float) -> dict | None:
    """Piazza un ordine REALE per il segnale e ritorna l'esito.

    Flusso (fail-closed, nessuna eccezione verso il chiamante):
    1. engine reale (provider da env, mai DryRun);
    2. risoluzione del mercato exchange per la STESSA partita ed esito
       (execution_engine.resolve_match_market: nomi squadre + kickoff,
       univocita' — niente ordini su eventi ambigui);
    3. floor EV: se il miglior prezzo disponibile e' sotto la quota del
       segnale la puntata non e' piu' +EV -> salto;
    4. ordine IOC con bound = quota segnale (riempimento a quella quota o
       meglio: l'edge e' stato calcolato su quella quota).

    Ritorna None per i salti (mercato non trovato/ambiguo, prezzo sotto il
    floor, errore di rete) e per gli ordini riusciti un dict con
    {ok, market_id, selection_id, bet_id, status, price, stake}.
    """
    try:
        import execution_engine as ee
    except Exception as e:
        logger.warning("auto_bet: execution_engine non disponibile (%s), "
                       "salto %s", e, pick.get("match_id"))
        return None
    try:
        engine = ee.ExecutionEngine()
    except Exception as e:
        logger.warning("auto_bet: ExecutionEngine non avviato (%s), salto %s",
                       e, pick.get("match_id"))
        return None
    if isinstance(engine.provider, ee.DryRunProvider):
        logger.warning("auto_bet: provider in DryRun, nessun ordine reale "
                       "per %s (%s)", pick.get("match_id"),
                       pick.get("esito_key"))
        return None
    prov = engine.provider

    try:
        mkt = ee.resolve_match_market(
            prov, pick["home"], pick["away"], pick["esito_key"],
            pick.get("commence"))
    except Exception as e:
        logger.warning("auto_bet: risoluzione mercato %s fallita: %s",
                       pick.get("match_id"), e)
        return None
    if not mkt:
        logger.info("auto_bet: mercato non trovato/ambiguo su %s per "
                    "%s vs %s (%s), salto",
                    getattr(prov, "name", "?"), pick["home"],
                    pick["away"], pick["esito_key"])
        return None
    market_id, sel = mkt["market_id"], mkt["selection_id"]

    best = None
    try:
        best = prov.best_back_price(market_id, sel)
    except Exception:
        best = None
    if best is not None and best < float(floor) - 1e-9:
        logger.info("auto_bet: %s vs %s (%s): best %s %.2f < floor segnale "
                    "%.2f, salto (EV perso)", pick["home"], pick["away"],
                    pick["esito_key"], getattr(prov, "name", "?"),
                    best, float(floor))
        return None

    try:
        order = prov.place_limit_order(market_id, sel, "BACK",
                                       float(floor), float(stake))
    except Exception as e:
        logger.warning("auto_bet: ordine reale %s fallito (%s vs %s): %s",
                       market_id, pick["home"], pick["away"], e)
        return None

    matched_price = (float(order.price_matched)
                     if order.price_matched else float(floor))
    matched_stake = float(order.size_matched or 0.0)
    if not order.ok or matched_stake <= 0:
        logger.warning("auto_bet: ordine reale %s non riempito (%s vs %s, "
                       "%s): %s", market_id, pick["home"], pick["away"],
                       order.status, order.error or "nessun match")
        return {"ok": False, "market_id": market_id,
                "selection_id": sel, "status": order.status or "FAILURE",
                "error": order.error}
    logger.info("auto_bet: ORDINE REALE riempito %s (%s vs %s, %s) @ %.2f "
                "per €%.2f [%s]", market_id, pick["home"], pick["away"],
                pick["esito_key"], matched_price, matched_stake, order.status)
    return {"ok": True, "market_id": market_id, "selection_id": sel,
            "bet_id": order.bet_id, "status": order.status or "SUCCESS",
            "price": matched_price, "stake": matched_stake}


def run_today_bets(stake_eur: float | None = None,
                   allow_sim: bool = True) -> list[dict]:
    """Piazza le puntate del giorno (SIM di default, LIVE con
    AUTO_BET_MODE=live + provider reale). Ritorna il riepilogo.

    In SIM usa la quota del segnale (paper trading) e registra in `bets`
    con mode='sim'. In LIVE risolve il mercato exchange e piazza un ordine
    reale (mode='live' con market_id/selection_id/bet_id); gli ordini non
    riempiti o i salti (mercato assente, prezzo sotto il floor EV) non
    lasciano righe sul ledger. Il saldo a fine partita e' sempre il solito
    (settle_bets via the-odds-api).

    Non lancia mai eccezioni verso il chiamante: ogni passo fallito viene
    loggato e saltato (fail-closed).

    Args:
        stake_eur: stake fisso di fallback (se adaptive_staking non
            disponibile). Default BET_STAKE_EUR env o 5.00.
        allow_sim: True -> in assenza di provider reale si ripiega sulla
            simulazione; False -> fail-closed (nessuna puntata).
    """
    # Default stake fisso (fallback se adaptive staking non disponibile)
    stake_eur_default = stake_eur if stake_eur is not None else float(
        os.getenv("BET_STAKE_EUR", str(BET_STAKE_DEFAULT_EUR)))

    # --- Modalita' effettiva del giro (prima dei candidati: decide il
    # --- bankroll del Kelly).
    mode = _execution_mode(allow_sim)
    if mode == "off":
        # Kill-switch /autobet off oppure fail-closed senza provider reale.
        logger.error("auto_bet: modalita' 'off' (kill-switch o fail-closed): "
                     "nessuna puntata")
        return []

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

    # LIVE: il bankroll del Kelly e' il saldo REALE del wallet exchange
    # (disponibile per gli ordini), non la cassa simulata. Se il wallet e'
    # sotto il minimo ordine non si piazza nulla (fail-closed).
    _wallet_balance: float | None = None
    if mode == "live":
        _wallet_balance = _live_wallet_balance()
        if _wallet_balance is None:
            logger.warning("auto_bet: saldo wallet non disponibile, uso "
                           "bankroll cassa €%.2f", _bankroll)
        elif _wallet_balance < MIN_STAKE_EUR:
            logger.error("auto_bet: wallet sotto il minimo ordine "
                         "(%.2f USDC < %.2f): nessuna puntata",
                         _wallet_balance, MIN_STAKE_EUR)
            return []
        else:
            _bankroll = _wallet_balance
            _peak = _wallet_balance  # drawdown vs saldo attuale (nessuno storico)
            logger.info("auto_bet: bankroll LIVE = saldo wallet %.2f USDC",
                        _wallet_balance)

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

    from tracker import bet_exists_open

    # --- FASE 1: costruisci i candidati (guardie + stake, senza salvare) ---
    candidates: list[dict] = []
    for pick in _today_value_picks():
        if bet_exists_open(pick["match_id"], pick["esito_key"]):
            logger.info("auto_bet: puntata gia' aperta per %s (%s), salto",
                        pick["match_id"], pick["esito_key"])
            continue

        # SIM: quota del segnale, nessun catalogo.
        if _too_close_to_start(pick.get("commence")):
            logger.info("auto_bet: %s vs %s a meno di %d min dall'inizio, salto",
                        pick["home"], pick["away"], MIN_MINUTES_TO_START)
            continue
        price = float(pick["quota"] or 0)
        if price <= 1.0:
            logger.info("auto_bet: quota segnale non valida per %s, salto",
                        pick["match_id"])
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

        # LIVE: clamp al floor dell'exchange (minimo ordine 1 USDC) e mai
        # oltre il saldo disponibile del wallet (i fondi sono li').
        if mode == "live":
            pick_stake = max(pick_stake, MIN_STAKE_EUR)
            pick_stake = min(pick_stake, _bankroll)
            pick_stake = round(pick_stake, 2)

        candidates.append({
            **pick, "price": price, "stake": pick_stake,
        })

    # --- FASE 2: risk capping (correlazione + esposizione totale) ---
    candidates = apply_correlation_cap(candidates, _bankroll)
    candidates = apply_total_exposure_cap(candidates, _bankroll)

    # --- FASE 3: esegui e registra (LIVE via execution_engine oppure SIM) ---
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

        if mode == "live":
            filled = _live_fill(cand, pick_stake, price)
            if filled is None:
                # Saltata (mercato assente/ambiguo, prezzo sotto il floor EV,
                # errore di rete): nessun ordine, nessuna riga sul ledger.
                continue
            if not filled.get("ok"):
                # Ordine rifiutato/non riempito dall'exchange: niente riga
                # (un FAILED sul ledger verrebbe saldato come perdita reale).
                logger.warning("auto_bet: ordine live non piazzato per %s "
                               "(%s @ %.2f): %s", cand["match_id"],
                               cand["esito_key"], price,
                               filled.get("error") or filled.get("status"))
                continue
            matched_price = float(filled["price"] or price)
            matched_stake = float(filled["stake"] or pick_stake)
            record = {**cand, "market_id": filled["market_id"],
                      "selection_id": filled["selection_id"],
                      "status": filled.get("status") or "SUCCESS",
                      "bet_id": filled.get("bet_id"), "mode": "live",
                      "price": matched_price, "stake": matched_stake}
            placed.append(record)
            try:
                save_bet(match_id=cand["match_id"], mercato=cand["mercato"],
                         esito=cand["esito_key"],
                         market_id=filled["market_id"],
                         selection_id=filled["selection_id"],
                         price=matched_price, stake=matched_stake,
                         mode="live",
                         status=filled.get("status") or "SUCCESS",
                         bet_id=filled.get("bet_id"))
            except Exception as e:
                logger.warning("auto_bet: salvataggio live %s: %s",
                               cand["match_id"], e)
            continue

        # SIM (default): paper trading con la quota del segnale.
        record = {**cand, "market_id": None, "selection_id": None,
                  "status": "SUCCESS", "bet_id": None, "mode": "sim"}
        placed.append(record)
        try:
            save_bet(match_id=cand["match_id"], mercato=cand["mercato"],
                     esito=cand["esito_key"], market_id=None, selection_id=None,
                     price=price, stake=pick_stake, mode="sim", status="SUCCESS")
        except Exception as e:
            logger.warning("auto_bet: salvataggio sim %s: %s", cand["match_id"], e)

    logger.info("auto_bet: %d puntate piazzate (%s)", len(placed), mode)
    return placed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    res = run_today_bets()
    mode = res[0]['mode'] if res else 'nessuna'
    print(f"✅ {len(res)} puntate ({mode})")
    for p in res:
        print(f"• {p['home']} vs {p['away']} — {p['esito_key']} @ {p['price']:.2f} "
              f"(€{p['stake']:.2f}) [{p['status']}]")