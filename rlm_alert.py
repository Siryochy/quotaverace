"""rlm_alert.py — Alert RLM real-time per Telegram.

Quando un segnale value/strong_value mostra Reverse Line Movement
(il prezzo si muove contro il pubblico → money sharp entra), invia
un alert immediato su Telegram per esecuzione rapida.

Flusso:
1. Controlla i price_snapshots per ogni segnale value attivo
2. Rileva RLM (movimento > soglia nella direzione opposta al pubblico)
3. Formatta un messaggio Telegram con dettagli per l'esecuzione
4. Invia agli admin + iscritti premium

CLI:
  venv/bin/python rlm_alert.py              # controlla e mostra alert
  venv/bin/python rlm_alert.py --check      # solo check, nessun invio
  venv/bin/python rlm_alert.py --json       # output JSON
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Soglie alert
RLM_ALERT_THRESHOLD = 3.0     # movimento minimo per alert (3%)
STEAM_ALERT_THRESHOLD = 6.0   # steam move per alert urgente (6%)
CRASH_ALERT_THRESHOLD = -5.0  # CROLLO quota: calo >= 5% dal primo snapshot
MIN_SNAPSHOTS = 3             # almeno 3 snapshot per valutare
ALERT_COOLDOWN_MINUTES = 60   # no alert ripetuti per lo stesso match entro 60 min


def _get_conn():
    from tracker import _get_conn
    return _get_conn()


def get_active_value_signals() -> List[Dict]:
    """Recupera i segnali value/strong_value attivi (non ancora iniziati o in corso)."""
    conn = _get_conn()
    try:
        now = datetime.now().isoformat()
        rows = conn.execute(
            "SELECT m.id, m.home_team, m.away_team, m.commence_time, "
            "m.league, a.best_esito, a.best_quota, a.best_ev, "
            "a.market_edge, a.status "
            "FROM matches m "
            "JOIN match_analysis a ON m.id = a.match_id "
            "WHERE a.status IN ('value', 'strong_value') "
            "AND m.commence_time >= ? "
            "ORDER BY a.best_ev DESC LIMIT 20",
            (now,)).fetchall()
    finally:
        conn.close()

    return [{
        "match_id": r[0], "home": r[1], "away": r[2],
        "commence": r[3], "league": r[4],
        "esito": r[5], "quota": float(r[6] or 0),
        "ev": float(r[7] or 0),
        "market_edge": float(r[8]) if r[8] is not None else None,
        "status": r[9],
    } for r in rows]


def check_rlm_for_signal(signal: Dict) -> Optional[Dict]:
    """Controlla RLM/steam per un singolo segnale.

    Returns:
        Dict con info alert o None se nessun movimento rilevante.
    """
    from line_movement import detect_rlm, detect_steam, get_snapshots

    match_id = signal["match_id"]
    esito = signal["esito"]

    # Mappa esito -> chiave snapshot
    esito_map = {"1": "1", "X": "X", "2": "2",
                 "Over 2.5": "Over 2.5", "Under 2.5": "Under 2.5"}
    snap_key = esito_map.get(esito, esito)

    snapshots = get_snapshots(match_id, snap_key)
    # 2 snapshot bastano per il CROLLO quota (alert di velocita'): attenderne
    # 3 ritarderebbe il segnale di un ciclo (5 min). RLM/steam restano
    # conservativi (>= MIN_SNAPSHOTS) per evitare falsi positivi da rumore.
    if len(snapshots) < 2:
        return None

    # Calcola movimento totale
    first_price = snapshots[0]["price"]
    last_price = snapshots[-1]["price"]
    if first_price <= 0:
        return None

    total_move_pct = (last_price / first_price - 1.0) * 100

    # Controlla RLM
    rlm = detect_rlm(match_id, snap_key)
    steam = detect_steam(match_id, snap_key)

    alert_type = None
    severity = "info"
    if steam:
        alert_type = "steam"
        severity = "urgent"
    elif total_move_pct <= CRASH_ALERT_THRESHOLD:
        # CROLLO quota (>= 5% in discesa): edge in erosione, esegui o rivaluta.
        # Copre i movimenti rapidi che detect_rlm (basato su probabilita')
        # puo' non classificare come RLM.
        alert_type = "crash"
        severity = "urgent"
    elif rlm and abs(total_move_pct) >= RLM_ALERT_THRESHOLD:
        alert_type = "rlm"
        severity = "warning"

    if not alert_type:
        return None

    # Dati per l'alert
    direction = "↗ SALITA" if total_move_pct > 0 else "↘ DISCESA"
    sharp_move = total_move_pct < 0  # prezzo scende = sharp money sull'esito

    return {
        "match_id": match_id,
        "evento": f"{signal.get('home', '?')} vs {signal.get('away', '?')}",
        "league": signal.get("league", ""),
        "esito": esito,
        "quota": signal["quota"],
        "ev": signal["ev"],
        "market_edge": signal.get("market_edge"),
        "status": signal["status"],
        "commence": signal.get("commence", ""),
        "alert_type": alert_type,
        "severity": severity,
        "total_move_pct": round(total_move_pct, 2),
        "direction": direction,
        "sharp_move": sharp_move,
        "first_price": round(first_price, 3),
        "last_price": round(last_price, 3),
        "n_snapshots": len(snapshots),
    }


def format_rlm_alert(alert: Dict) -> str:
    """Formatta un alert RLM per Telegram (Markdown)."""
    severity_emoji = {"urgent": "🔥", "warning": "⚠️", "info": "📊"}
    emoji = severity_emoji.get(alert["severity"], "📊")
    alert_label = {"steam": "STEAM MOVE", "crash": "CROLLO QUOTA",
                   "rlm": "RLM"}.get(alert["alert_type"], "RLM")

    edge_txt = ""
    if alert.get("market_edge") is not None:
        edge_txt = f" | edge {alert['market_edge']*100:+.1f}pp"

    lines = [
        f"{emoji} *ALERT {alert_label}* — {alert['severity'].upper()}",
        f"",
        f"⚽ *{alert['evento']}* [{alert['league']}]",
        f"🎯 {alert['esito']} @ {alert['quota']:.2f} ({alert['status']})",
        f"📈 EV: +{alert['ev']*100:.1f}%{edge_txt}",
        f"",
        f"📊 Movimento: {alert['direction']} {alert['total_move_pct']:+.1f}%",
        f"   {alert['first_price']:.2f} → {alert['last_price']:.2f} "
        f"({alert['n_snapshots']} snapshots)",
        f"",
    ]

    if alert["alert_type"] == "steam":
        lines.append("🔥 *STEAM*: movimento improvviso! Sharp money in entrata.")
        lines.append("⚡ Esegui ORA prima che il mercato si aggiusti.")
    elif alert["alert_type"] == "crash":
        lines.append("🚨 *CROLLO QUOTA*: -5% o oltre dal primo rilevamento.")
        lines.append("⚡ Edge in erosione: esegui subito o scarta il segnale.")
    else:
        if alert["sharp_move"]:
            lines.append("📉 Il prezzo scende → i sharps stanno coprendo l'esito.")
            lines.append("⚡ Valuta l'esecuzione prima della chiusura.")
        else:
            lines.append("📈 Il prezzo sale → il mercato si muove a favore.")
            lines.append("⚡ Edge potenzialmente migliorato.")

    # Quota iniziale vs attuale
    if alert["quota"] > 0:
        edge_vs_signal = (alert["last_price"] / alert["quota"] - 1.0) * 100
        if edge_vs_signal > 0:
            lines.append(f"\n💰 Quota segnale: {alert['quota']:.2f} → "
                         f"attuale: {alert['last_price']:.2f} "
                         f"(+{edge_vs_signal:.1f}% dal segnale)")
        elif edge_vs_signal < -2:
            lines.append(f"\n⚠️ Quota segnale: {alert['quota']:.2f} → "
                         f"attuale: {alert['last_price']:.2f} "
                         f"({edge_vs_signal:.1f}% dal segnale — edge eroso!)")

    return "\n".join(lines)


def check_all_alerts() -> List[Dict]:
    """Controlla tutti i segnali attivi e ritorna gli alert RLM."""
    signals = get_active_value_signals()
    alerts = []
    for sig in signals:
        alert = check_rlm_for_signal(sig)
        if alert:
            alerts.append(alert)
    return alerts


async def send_rlm_alerts(context, alerts: List[Dict]) -> int:
    """Invia gli alert RLM su Telegram. Ritorna il numero di alert inviati."""
    from bot import _admin_chat_ids, get_subscribers

    if not alerts:
        return 0

    chat_ids = set(_admin_chat_ids())
    # Gli iscritti premium ricevono gli alert RLM
    try:
        from tracker import get_subscribers as gs
        for cid in gs():
            chat_ids.add(cid)
    except Exception:
        pass

    sent = 0
    for alert in alerts:
        text = format_rlm_alert(alert)
        for chat_id in sorted(chat_ids):
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="Markdown")
                sent += 1
            except Exception as e:
                logger.warning("Alert RLM fallito per %s: %s", chat_id, e)

    return sent


async def rlm_alert_job(context) -> None:
    """Job periodico: controlla RLM ogni 5 minuti sulle partite in corso.

    Integrato nel job queue del bot (ogni 5 minuti dalle 14:00 alle 23:50 ITA).
    """
    from tracker import is_notified, mark_notified

    alerts = check_all_alerts()
    if not alerts:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    sent = 0
    for alert in alerts:
        # Cooldown: non reinviare alert per lo stesso match entro 60 min
        cooldown_key = f"RLM_{alert['match_id']}_{alert['alert_type']}"
        if is_notified(cooldown_key, today):
            continue

        text = format_rlm_alert(alert)
        from bot import _admin_chat_ids, get_subscribers
        chat_ids = set(_admin_chat_ids())
        try:
            from tracker import get_subscribers as gs
            for cid in gs():
                chat_ids.add(cid)
        except Exception:
            pass

        for chat_id in sorted(chat_ids):
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="Markdown")
                sent += 1
            except Exception as e:
                logger.debug("Alert RLM skip %s: %s", chat_id, e)

        mark_notified(cooldown_key, today)

    if sent:
        logger.info("Alert RLM: %d alert inviati (%d segnali)", sent, len(alerts))


# --- CLI ---

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Alert RLM real-time")
    ap.add_argument("--check", action="store_true",
                    help="Solo check, nessun invio")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    alerts = check_all_alerts()
    if args.json:
        print(json.dumps(alerts, indent=2, ensure_ascii=False))
    elif not alerts:
        print("✅ Nessun alert RLM/steam attivo.")
    else:
        print(f"📊 {len(alerts)} alert RLM/steam trovati:\n")
        for a in alerts:
            print(format_rlm_alert(a))
            print()

    return 0 if not alerts else 1


if __name__ == "__main__":
    sys.exit(main())
