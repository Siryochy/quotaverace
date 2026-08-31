"""
API JSON (stdlib, nessuna dipendenza) che espone i dati del bot QuotaVerace
ai frontend (webapp Next.js su Vercel).

Endpoint:
  GET /api/health            -> stato + crediti the-odds-api
  GET /api/dashboard         -> KPIs (bankroll, ROI, segnali oggi) + ultime value bet
  GET /api/storico           -> storico segnali + riepilogo 30gg
  GET /api/value             -> migliori value bet filtrate
  GET /api/schedina          -> schedina del giorno (picks + multipla)
  GET /api/scan              -> catalogo Betfair (cache job 8:45; ?live=1 per scan immediata)

Uso su Railway: dopo aver deployato il bot, crea un secondo servizio con
  startCommand = "python web_api.py"
oppure avvialo a fianco del bot. Porta di default 8000 (override via WEB_API_PORT).
"""

import json
import logging
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import DATA_DIR
from tracker import _get_conn, _create_results_table, get_performance_summary, get_signals

logger = logging.getLogger("web_api")

# Railway injects PORT; fall back to WEB_API_PORT, then 8000 for local use.
PORT = int(os.getenv("PORT") or os.getenv("WEB_API_PORT") or "8000")
DB = DATA_DIR / "quotaverace.db"


def _bankroll():
    return float(os.getenv("BANKROLL_DEFAULT", "100.0"))


def _odds_json(params=None):
    """Legge le quote dalla tabella 'signals' o match_analysis come asset curato."""
    conn = _get_conn()
    c = conn.cursor()
    rows = c.execute(
        "SELECT evento, esito, quota, probabilita, ev FROM signals "
        "WHERE esito_finale IS NULL ORDER BY id DESC LIMIT 10"
    ).fetchall()
    conn.close()
    return [
        {"evento": e, "esito": s, "quota": q, "probabilita": p, "ev": v}
        for e, s, q, p, v in rows if q
    ]


def _storico_json(params=None):
    signals = get_signals(limit=30)
    out = []
    for s in signals:
        if not s.quota:
            continue
        outcome = s.esito_finale or "pending"
        profit = s.profit if s.profit else None
        out.append({
            "id": s.id,
            "evento": s.evento,
            "esito": s.esito,
            "quota": s.quota,
            "probabilita": s.probabilita,
            "ev": s.ev,
            "risultato": outcome,
            "profit": profit,
            "data": s.timestamp,
        })
    summary = get_performance_summary(days=30)
    return {"segnali": out, "summary": summary}


def _dashboard_json(params=None):
    summary = get_performance_summary(days=30)
    value = _odds_json()
    # count segnali odierni
    conn = sqlite3.connect(str(DB))
    today_count = 0
    try:
        today_count = conn.cursor().execute(
            "SELECT COUNT(*) FROM signals WHERE date(timestamp) = date('now')"
        ).fetchone()[0]
    except Exception:
        today_count = 0
    conn.close()
    roi = summary.get("roi", 0.0)
    closed = summary.get("closed", 0)
    return {
        "bankroll": _bankroll(),
        "roi_30gg": roi,
        "segnali_oggi": today_count,
        "chiusi_30gg": closed,
        "hit_rate": summary.get("hit_rate", 0.0),
        "ultime_value": value,
    }


def _schedina_json(params=None):
    """Schedina del giorno: picks con valore + multipla prolungata."""
    try:
        from fixture_engine import get_value_picks_for_schedina, build_multipla
    except Exception as e:
        logger.warning("fixture_engine non disponibile: %s", e)
        return {"picks": [], "multipla": None, "bankroll": _bankroll()}

    picks = get_value_picks_for_schedina()
    out = []
    for p in picks:
        prob = p["ev"] + (1.0 / p["quota"]) if p["quota"] else 0.0
        out.append({
            "league": p["league"],
            "home": p["home"],
            "away": p["away"],
            "evento": p["evento"],
            "esito": p["esito"],
            "quota": p["quota"],
            "bookmaker": p["bookmaker"],
            "ev": p["ev"],
            "prob": prob,
        })

    mp = build_multipla(picks)
    multipla = None
    if mp:
        multipla = {
            "esiti": mp["esiti"],
            "quota": mp["quota"],
            "prob": mp["prob"],
            "ev": mp["ev"],
            "legs": [
                {"esito": l["esito"], "quota": l["quota"], "evento": l["evento"]}
                for l in mp["legs"]
            ],
        }

    return {"picks": out, "multipla": multipla, "bankroll": _bankroll()}


def _health_json(params=None):
    quota = None
    try:
        from odds_api import get_quota
        quota = get_quota()
    except Exception:
        quota = None
    creds = {"remaining": quota[0], "cache": quota[1]} if quota else None
    return {"status": "ok", "api_football_key": bool(os.getenv("API_FOOTBALL_KEY")), "quota": creds}


def _segnali_json(params=None):
    """Segnale completo per una partita (usato dal calcolatore del sito).

    GET /api/segnali?home=Inter&away=Napoli
    Ritorna expected goals, probabilita' 1X2/Over/Under/BTTS, miglior esito
    con EV, stake Kelly (1/4, cap 3%) e, se disponibili, le quote di
    mercato reali dal calendario.
    """
    from poisson_engine import expected_goals, prob_1x2, prob_over_under, prob_btts
    from value_filter import compute_ev, kelly_fraction, get_pro_stake, is_sane
    from leagues_data import ALL_LEAGUES

    home = (params.get("home") or "").strip()
    away = (params.get("away") or "").strip()
    if not home or not away:
        return 400, {"error": "Specifica home e away", "esempio": "/api/segnali?home=Inter&away=Napoli"}
    try:
        lam_h, lam_a = expected_goals(home, away)
    except Exception as e:
        return 404, {"error": str(e), "home": home, "away": away}

    p1, px, p2 = prob_1x2(lam_h, lam_a)
    p_over, p_under = prob_over_under(lam_h, lam_a)
    p_btts = prob_btts(lam_h, lam_a)

    # Quote di riferimento: miglior prezzo dal calendario (se gia' analizzato)
    league = None
    for l, teams in ALL_LEAGUES.items():
        if home in teams:
            league = l
            break
    market_prob = market_edge = None
    ref_odds = {}
    if league:
        try:
            from tracker import _get_conn
            conn = _get_conn(); c = conn.cursor()
            c.execute("SELECT a.best_quota, a.market_prob, a.market_edge, a.best_esito "
                      "FROM matches m JOIN match_analysis a ON a.match_id = m.id "
                      "WHERE m.home_team=? AND m.away_team=? ORDER BY a.id DESC LIMIT 1",
                      (home, away))
            row = c.fetchone(); conn.close()
            if row:
                ref_odds["quota"] = row[0]
                market_prob = row[1]
                market_edge = row[2]
        except Exception:
            pass

    candidates = [
        {"esito": "1", "label": f"Vittoria {home}", "prob": p1, "quota": 2.0},
        {"esito": "X", "label": "Pareggio", "prob": px, "quota": 3.2},
        {"esito": "2", "label": f"Vittoria {away}", "prob": p2, "quota": 2.0},
        {"esito": "Over 2.5", "label": "Over 2.5 Gol", "prob": p_over, "quota": ref_odds.get("quota", 2.10)},
    ]
    for cand in candidates:
        cand["ev"] = compute_ev(cand["prob"], cand["quota"])
    best = max(candidates, key=lambda c: c["ev"])
    pro = get_pro_stake(100.0, best["prob"], best["quota"])
    sane, reason = is_sane(best["prob"], best["quota"], best["ev"], market_prob=market_prob)

    return {
        "home": home, "away": away, "league": league or "",
        "lam_h": round(lam_h, 3), "lam_a": round(lam_a, 3),
        "p1": round(p1, 4), "pX": round(px, 4), "p2": round(p2, 4),
        "p_over": round(p_over, 4), "p_under": round(p_under, 4),
        "p_btts": round(p_btts, 4),
        "best": {"esito": best["esito"], "label": best["label"],
                  "quota": best["quota"], "prob": round(best["prob"], 4),
                  "ev": round(best["ev"], 4)},
        "stake_kelly_pct": round(pro["stake_pct_of_bankroll"], 2),
        "sane": sane, "sane_reason": reason,
        "market": ({"prob": round(market_prob, 4), "edge": round(market_edge, 4)}
                    if market_prob is not None else None),
    }


def _calendario_json(params=None):
    """Partite del giorno con analisi (come /calendario del bot)."""
    from tracker import get_today_matches, get_analysis_for_match
    rows = get_today_matches()
    out = []
    for row in rows:
        mid, league, home, away, commence, status, _ = row
        item = {"league": league, "home": home, "away": away,
                "commence": commence, "status": status, "match_id": mid}
        ana = get_analysis_for_match(mid)
        if ana:
            (_, _, lam_h, lam_a, p1, px, p2, p_over, best_ev, best_esito,
             best_quota, best_bookmaker, a_status, _, market_prob, market_edge) = ana
            item.update({
                "lam_h": lam_h, "lam_a": lam_a, "prob_1": p1, "prob_X": px,
                "prob_2": p2, "prob_over": p_over,
                "best_esito": best_esito, "best_quota": best_quota,
                "best_bookmaker": best_bookmaker, "best_ev": best_ev,
                "analisi_status": a_status, "market_prob": market_prob,
                "market_edge": market_edge,
            })
        out.append(item)
    return {"partite": out, "n": len(out)}


def _backtest_json(params=None):
    """Statistiche di calibrazione modello vs mercato."""
    from backtest import backtest_stats
    try:
        return backtest_stats()
    except Exception as e:
        return 500, {"error": str(e)}


def _campionati_json(params=None):
    """Lista campionati e squadre per la ricerca del calcolatore."""
    from leagues_data import ALL_LEAGUES
    return {"campionati": [{"nome": name, "squadre": sorted(teams.keys())}
                            for name, teams in ALL_LEAGUES.items()]}


def _cassa_get(params=None):
    from tracker import get_cassa, cassa_totals
    entries = get_cassa()
    return {"scommesse": entries, "totali": cassa_totals(entries)}


def _cassa_post(params=None, body=None):
    from tracker import save_cassa_entry, get_cassa, cassa_totals
    data = body or {}
    partita = (data.get("partita") or "").strip()
    esito = (data.get("esito") or "").strip()
    quota = float(data.get("quota") or 0)
    importo = float(data.get("importo") or 0)
    if not partita or not esito or quota <= 1.0 or importo <= 0:
        return 400, {"error": "Campi mancanti o non validi: servono partita, esito, quota>1, importo>0"}
    ev = float(data.get("ev") or 0)
    save_cassa_entry(partita, esito, quota, importo, ev=ev, data=data.get("data"))
    entries = get_cassa()
    return {"ok": True, "scommesse": entries, "totali": cassa_totals(entries)}


def _cassa_delete(params=None):
    from tracker import clear_cassa
    clear_cassa()
    return {"ok": True, "scommesse": [], "totali": {"n": 0, "totale_speso": 0,
                                                    "vincita_potenziale": 0, "profit_potenziale": 0}}


def _analisi_json(params=None, body=None):
    """Rigenera calendario + analisi con la strategia corrente (come /analisi del bot).

    POST /api/analisi — esegue fetch_and_analyze_today e ritorna il riepilogo.
    """
    try:
        from fixture_engine import fetch_and_analyze_today
        total, value = fetch_and_analyze_today()
        return {"ok": True, "partite": total, "value": value}
    except Exception as e:
        logger.exception("errore analisi")
        return 500, {"error": str(e)}


def _db_stats_json(params=None):
    """Diagnostica: conteggi del DB (ratings, risultati, analisi)."""
    conn = sqlite3.connect(str(DB))
    c = conn.cursor()
    out = {}
    for table, q in [
        ("match_results", "SELECT COUNT(*) FROM match_results"),
        ("team_ratings", "SELECT COUNT(*) FROM team_ratings"),
        ("match_analysis", "SELECT COUNT(*) FROM match_analysis"),
        ("signals", "SELECT COUNT(*) FROM signals"),
        ("cassa", "SELECT COUNT(*) FROM cassa"),
    ]:
        try:
            out[table] = c.execute(q).fetchone()[0]
        except Exception:
            out[table] = None
    try:
        out["ultima_analisi"] = c.execute(
            "SELECT timestamp FROM match_analysis ORDER BY id DESC LIMIT 1").fetchone()[0]
    except Exception:
        out["ultima_analisi"] = None
    try:
        out["ratings_sotto_soglia"] = c.execute(
            "SELECT COUNT(*) FROM team_ratings WHERE (n_home + n_away) < 6").fetchone()[0]
    except Exception:
        out["ratings_sotto_soglia"] = None
    conn.close()
    return out


def _ratings_json(params=None):
    """Coefficienti di rating per squadre specifiche (?teams=Roma,Barcelona)."""
    teams = [t.strip() for t in (params.get("teams") or "").split(",") if t.strip()]
    conn = sqlite3.connect(str(DB))
    c = conn.cursor()
    out = []
    for t in teams:
        try:
            row = c.execute("SELECT team, league, attack_home, defense_home, attack_away, defense_away, n_home, n_away "
                            "FROM team_ratings WHERE team=?", (t,)).fetchone()
        except Exception:
            row = None
        out.append({"team": t, "rating": row})
    conn.close()
    return {"ratings": out}


def _scan_json(params=None):
    """Catalogo Betfair: cache del job giornaliero, o scan live con ?live=1.

    Ritorna (status_code, payload) — il codice HTTP riflette lo stato:
    200 = ok, 503 = nessuna cache o Betfair non configurato.
    """
    params = params or {}
    from daily_scan_job import load_latest_scan, run_daily_scan
    if params.get("live") == "1":
        # ?date=YYYY-MM-DD per scansionare un giorno specifico (default: oggi UTC)
        payload = run_daily_scan(params.get("date") or None)
        if payload is None:
            return 503, {
                "error": "betfair_not_configured",
                "message": "Configura BETFAIR_APP_KEY, BETFAIR_USERNAME, "
                           "BETFAIR_PASSWORD, BETFAIR_CERT_PATH",
            }
        return 200, payload
    cached = load_latest_scan()
    if cached is None:
        return 503, {
            "error": "no_scan_cache",
            "message": "Nessuna scansione salvata: usa /scan nel bot "
                       "o GET /api/scan?live=1",
        }
    return 200, cached


ROUTES = {
    "/api/health": _health_json,
    "/api/dashboard": _dashboard_json,
    "/api/storico": _storico_json,
    "/api/value": _odds_json,
    "/api/schedina": _schedina_json,
    "/api/scan": _scan_json,
    "/api/segnali": _segnali_json,
    "/api/calendario": _calendario_json,
    "/api/backtest": _backtest_json,
    "/api/campionati": _campionati_json,
    "/api/cassa": _cassa_get,
    "/api/db_stats": _db_stats_json,
    "/api/ratings": _ratings_json,
}

POST_ROUTES = {
    "/api/cassa": _cassa_post,
    "/api/analisi": _analisi_json,
}

DELETE_ROUTES = {
    "/api/cassa": _cassa_delete,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._dispatch(ROUTES, None)

    def do_POST(self):
        self._dispatch(POST_ROUTES, self._read_body())

    def do_DELETE(self):
        self._dispatch(DELETE_ROUTES, None)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _dispatch(self, routes, body):
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        handler = routes.get(path)
        if not handler:
            self._send(404, {"error": "not found", "routes": list(ROUTES.keys())})
            return
        try:
            result = handler(params, body) if body is not None else handler(params)
        except Exception as e:
            logger.exception("errore endpoint %s", path)
            self._send(500, {"error": str(e)})
            return
        if isinstance(result, tuple) and len(result) == 2:
            self._send(*result)
        else:
            self._send(200, result)

    def _send(self, code, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    _get_conn().close()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logger.info("QuotaVerace Web API in ascolto su :%s", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()