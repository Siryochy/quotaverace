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

from tracker import _get_conn, _create_results_table, get_performance_summary, get_signals

logger = logging.getLogger("web_api")

# Railway injects PORT; fall back to WEB_API_PORT, then 8000 for local use.
PORT = int(os.getenv("PORT") or os.getenv("WEB_API_PORT") or "8000")
DB = Path(__file__).parent / "quotaverace.db"


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
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        handler = ROUTES.get(path)
        if not handler:
            self._send(404, {"error": "not found", "routes": list(ROUTES.keys())})
            return
        try:
            result = handler(params)
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