"""
API JSON (stdlib, nessuna dipendenza) che espone i dati del bot QuotaVerace
ai frontend (webapp Next.js su Vercel).

Endpoint:
  GET /api/health            -> stato + crediti the-odds-api
  GET /api/dashboard         -> KPIs (bankroll, ROI, segnali oggi) + ultime value bet
  GET /api/storico           -> storico segnali + riepilogo 30gg
  GET /api/value             -> migliori value bet filtrate

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

PORT = int(os.getenv("WEB_API_PORT", "8000"))
DB = Path(__file__).parent / "quotaverace.db"


def _bankroll():
    return float(os.getenv("BANKROLL_DEFAULT", "100.0"))


def _odds_json():
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


def _storico_json():
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


def _dashboard_json():
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


def _health_json():
    quota = None
    try:
        from odds_api import get_quota
        quota = get_quota()
    except Exception:
        quota = None
    creds = {"remaining": quota[0], "cache": quota[1]} if quota else None
    return {"status": "ok", "api_football_key": bool(os.getenv("API_FOOTBALL_KEY")), "quota": creds}


ROUTES = {
    "/api/health": _health_json,
    "/api/dashboard": _dashboard_json,
    "/api/storico": _storico_json,
    "/api/value": _odds_json,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        import urllib.parse
        path = urllib.parse.urlparse(self.path).path
        handler = ROUTES.get(path)
        if not handler:
            self._send(404, {"error": "not found", "routes": list(ROUTES.keys())})
            return
        try:
            self._send(200, handler())
        except Exception as e:
            logger.exception("errore endpoint %s", path)
            self._send(500, {"error": str(e)})

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