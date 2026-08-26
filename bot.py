"""
QuotaVerace Bot – Modulo Telegram per segnali Poisson pronti per la scommessa

Comandi:
    /test_segnale    – segnale demo
    /segnale <casa> <trasferta> – analisi Poisson + quote reali
    /value           – tutti i value bet (EV > 5%) del giorno
    /storico_personale – segnali inviati + performance
    /help            – elenco comandi
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import List, Tuple

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from poisson_engine import expected_goals, SERIE_A_2023_24
from tracker import init_db, log_signal, get_signals, get_performance_summary

try:
    from config import TOKEN
except ImportError:
    TOKEN = os.getenv("QUOTAVERACE_BOT_TOKEN", "")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DISCLAIMER = (
    "\n\n────────────────────\n"
    "🎲 *Gioca responsabilmente*\n"
    "Le scommesse sono un gioco d'azzardo. Non puntare più di quanto puoi "
    "permetterti di perdere. Se hai bisogno di aiuto, visita il portale ADM: "
    "[www.adm.gov.it](https://www.adm.gov.it)"
)

ODDS_FILE = Path(__file__).parent / "data" / "odds_sample.json"


def _poisson_pmf(k: int, lam: float) -> float:
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def prob_1x2(lam_h: float, lam_a: float, max_goals: int = 10) -> Tuple[float, float, float]:
    p1, px, p2 = 0.0, 0.0, 0.0
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = _poisson_pmf(i, lam_h) * _poisson_pmf(j, lam_a)
            if i > j:
                p1 += p
            elif i == j:
                px += p
            else:
                p2 += p
    return round(p1, 4), round(px, 4), round(p2, 4)


def prob_over_under(lam_h: float, lam_a: float, threshold: float = 2.5, max_goals: int = 10) -> Tuple[float, float]:
    p_under = 0.0
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            if i + j < threshold:
                p_under += _poisson_pmf(i, lam_h) * _poisson_pmf(j, lam_a)
    p_over = 1.0 - p_under
    return round(p_over, 4), round(p_under, 4)


def compute_ev(prob: float, odds: float) -> float:
    return prob * odds - 1.0


def load_odds_from_file() -> List[dict]:
    if not ODDS_FILE.exists():
        return []
    try:
        with open(ODDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def find_best_odds(evento: str, esito: str, odds_data: List[dict]) -> Tuple[float, str]:
    matches = [
        (o["quota_decimale"], o["bookmaker"])
        for o in odds_data
        if o.get("evento", "").lower() == evento.lower()
        and o.get("esito", "").lower() == esito.lower()
        and o.get("quota_decimale", 0) > 1.0
    ]
    if not matches:
        return 0.0, ""
    return max(matches, key=lambda x: x[0])


def format_segnale_pronto(
    home: str,
    away: str,
    lam_h: float,
    lam_a: float,
    quota_over: float = 2.10,
    bookmaker: str = "Generico",
) -> str:
    p1, px, p2 = prob_1x2(lam_h, lam_a)
    p_over, p_under = prob_over_under(lam_h, lam_a)

    candidates = [
        ("1", p1, f"Vittoria {home}"),
        ("X", px, "Pareggio"),
        ("2", p2, f"Vittoria {away}"),
        ("Over 2.5", p_over, "Over 2.5 Gol"),
        ("Under 2.5", p_under, "Under 2.5 Gol"),
    ]

    best = max(candidates, key=lambda x: compute_ev(x[1], quota_over if x[0] == "Over 2.5" else 2.0))
    best_code, best_prob, best_label = best
    best_quota = quota_over if best_code == "Over 2.5" else 2.0

    ev = compute_ev(best_prob, best_quota)
    ev_percent = ev * 100.0

    if ev > 0.10:
        valore_label = "🟢 *FORTE VALORE*"
        raccomandazione = "✅ Raccomandato per la scommessa"
    elif ev > 0.03:
        valore_label = "🟡 *Valore positivo*"
        raccomandazione = "⚠️ Valore marginale, valutare con cautela"
    elif ev > 0:
        valore_label = "🟠 *Valore debole*"
        raccomandazione = "ℹ️ EV positivo ma rischio elevato — stake minimo"
    else:
        valore_label = "🔴 *Valore negativo*"
        raccomandazione = "❌ NON raccomandato"

    msg = (
        f"📊 *SEGNALE PRONTO – {home} vs {away}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚽ *Expected Goals:*\n"
        f"   {home}: {lam_h:.2f}\n"
        f"   {away}: {lam_a:.2f}\n\n"
        f"📈 *Probabilità modello:*\n"
        f"   1: {p1*100:.1f}% | X: {px*100:.1f}% | 2: {p2*100:.1f}%\n"
        f"   Over 2.5: {p_over*100:.1f}% | Under 2.5: {p_under*100:.1f}%\n\n"
        f"🎯 *SEGNALE RACCOMANDATO*\n"
        f"   Esito: {best_label}\n"
        f"   Bookmaker: {bookmaker}\n"
        f"   Quota: {best_quota:.2f}\n"
        f"   Probabilità: {best_prob*100:.1f}%\n"
        f"   EV: {ev_percent:+.2f}%\n\n"
        f"{valore_label}\n"
        f"{raccomandazione}\n\n"
        f"🪙 *Stake suggerito:* 1 unità\n"
        f"📅 *Data:* oggi"
    )

    return msg + DISCLAIMER


def format_value_bets(odds_data: List[dict]) -> str:
    value_signals = [
        o for o in odds_data
        if o.get("quota_decimale", 0) > 1.0 and o.get("ev", 0) > 0.05
    ]

    if not value_signals:
        return (
            "📊 *Value Bet del giorno*\n\n"
            "Nessun segnale con EV > 5% trovato oggi.\n"
            "Prova con `/segnale <casa> <trasferta>` per analizzare una partita specifica."
            + DISCLAIMER
        )

    msg = "📊 *VALUE BET – Segnali con EV > 5%*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for sig in value_signals[:5]:
        ev_pct = sig["ev"] * 100
        msg += (
            f"🏟 {sig['evento']}\n"
            f"🎯 {sig['esito']} @ {sig['quota_decimale']:.2f} ({sig['bookmaker']})\n"
            f"📈 EV: +{ev_pct:.2f}%\n\n"
        )
    msg += "🪙 Stake suggerito: 1 unità per segnale"
    return msg + DISCLAIMER


async def cmd_test_segnale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_segnale_pronto(
        home="Juventus",
        away="Inter",
        lam_h=1.61,
        lam_a=0.98,
        quota_over=2.10,
        bookmaker="Bet365",
    )
    await update.message.reply_text(text, parse_mode="Markdown")
    
    try:
        log_signal(
            chat_id=update.effective_chat.id,
            evento="Juventus vs Inter",
            esito="Under 2.5 Gol",
            quota=2.00,
            probabilita=0.521,
            ev=-0.0418,
        )
    except Exception as e:
        logger.warning(f"Log segnale fallito: {e}")


async def cmd_segnale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Errore:* specifica casa e trasferta.\n"
            "Esempio: `/segnale Roma Empoli`\n"
            "Esempio: `/segnale Inter Milan AC Milan`",
            parse_mode="Markdown",
        )
        return

    raw = " ".join(args)
    if '"' in raw:
        parts = [p.strip().strip('"') for p in raw.split('"') if p.strip()]
        home, away = (parts[0], parts[1]) if len(parts) >= 2 else (parts[0], parts[0])
    else:
        words = raw.split()
        home, away = raw, raw
        for i in range(1, len(words)):
            h = " ".join(words[:i])
            a = " ".join(words[i:])
            if h in SERIE_A_2023_24 and a in SERIE_A_2023_24:
                home, away = h, a
                break

    if home not in SERIE_A_2023_24 or away not in SERIE_A_2023_24:
        await update.message.reply_text(
            f"❌ Squadra non trovata.\nHai scritto: *{home}* vs *{away}*\n"
            f"Usa `/help` per vedere le squadre disponibili.",
            parse_mode="Markdown",
        )
        return

    try:
        lam_h, lam_a = expected_goals(home, away)
        odds_data = load_odds_from_file()
        event_name = f"Serie A – {home} vs {away}"
        quota, bookmaker = find_best_odds(event_name, "Over 2.5", odds_data)
        if quota == 0.0:
            quota, bookmaker = 2.10, "Modello (quota stimata)"

        text = format_segnale_pronto(home, away, lam_h, lam_a, quota, bookmaker)
        await update.message.reply_text(text, parse_mode="Markdown")
        
        p1, px, p2 = prob_1x2(lam_h, lam_a)
        p_over, p_under = prob_over_under(lam_h, lam_a)
        candidates = [
            ("1", p1, f"Vittoria {home}"),
            ("X", px, "Pareggio"),
            ("2", p2, f"Vittoria {away}"),
            ("Over 2.5", p_over, "Over 2.5 Gol"),
            ("Under 2.5", p_under, "Under 2.5 Gol"),
        ]
        best = max(candidates, key=lambda x: compute_ev(x[1], quota if x[0] == "Over 2.5" else 2.0))
        best_code, best_prob, best_label = best
        best_quota = quota if best_code == "Over 2.5" else 2.0
        
        try:
            log_signal(
                chat_id=update.effective_chat.id,
                evento=f"{home} vs {away}",
                esito=best_label,
                quota=best_quota,
                probabilita=best_prob,
                ev=compute_ev(best_prob, best_quota),
            )
        except Exception as e:
            logger.warning(f"Log segnale fallito: {e}")
            
    except Exception as e:
        logger.error(f"Errore calcolo segnale: {e}")
        await update.message.reply_text(
            "❌ Errore nel calcolo del segnale. Riprova più tardi.",
            parse_mode="Markdown",
        )


async def cmd_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    odds_data = load_odds_from_file()
    text = format_value_bets(odds_data)
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_storico_personale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    signals = get_signals(chat_id=chat_id, limit=20)

    if not signals:
        await update.message.reply_text(
            "📭 Non hai ancora ricevuto segnali.\n"
            "Usa `/segnale <casa> <trasferta>` per iniziare.",
            parse_mode="Markdown",
        )
        return

    msg = "📊 *I tuoi ultimi segnali*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for s in signals[:10]:
        status = "✅" if s.esito_finale == "won" else "❌" if s.esito_finale == "lost" else "⏳"
        profit = f" ({s.profit:+.2f}u)" if s.profit != 0 else ""
        msg += (
            f"{status} {s.evento}\n"
            f"   {s.esito} @ {s.quota:.2f} | EV {s.ev*100:+.1f}%{profit}\n\n"
        )

    summary = get_performance_summary(days=30)
    if summary["closed"] > 0:
        msg += (
            f"📈 *Riepilogo 30 giorni*\n"
            f"   Segnali: {summary['closed']} | "
            f"Vinti: {summary['won']} | Persi: {summary['lost']}\n"
            f"   Profitto: {summary['net_profit']:+.2f}u | "
            f"ROI: {summary['roi']:.1f}%"
        )

    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    squadre = "\n".join(f"• {s}" for s in sorted(SERIE_A_2023_24.keys()))
    text = (
        "📋 *Comandi QuotaVerace*\n\n"
        "`/test_segnale` – segnale demo pronto per la scommessa\n"
        "`/segnale <casa> <trasferta>` – analisi Poisson + quote reali\n"
        "   Esempio: `/segnale Roma Empoli`\n"
        "   Esempio: `/segnale Inter Milan AC Milan`\n"
        "`/value` – tutti i value bet (EV > 5%) del giorno\n"
        "`/storico_personale` – i tuoi segnali e performance\n\n"
        "🏟 *Squadre disponibili (Serie A 2023/24):*\n"
        f"{squadre}\n\n"
        "📖 Ogni segnale include:\n"
        "• Expected goals (modello Poisson)\n"
        "• Probabilità 1X2 e Over/Under\n"
        "• Quota reale da bookmaker (se disponibile)\n"
        "• EV% calcolato\n"
        "• Raccomandazione stake"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


def main() -> None:
    if not TOKEN:
        raise ValueError(
            "Token non configurato. "
            "Imposta QUOTAVERACE_BOT_TOKEN in config.py o come variabile d'ambiente."
        )

    init_db()

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("test_segnale", cmd_test_segnale))
    application.add_handler(CommandHandler("segnale", cmd_segnale))
    application.add_handler(CommandHandler("value", cmd_value))
    application.add_handler(CommandHandler("storico_personale", cmd_storico_personale))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("start", cmd_help))

    logger.info("QuotaVerace Bot avviato. Premi Ctrl+C per terminare.")
    application.run_polling()


if __name__ == "__main__":
    main()
