import json
import logging
import os
from datetime import time

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from poisson_engine import expected_goals, prob_1x2, prob_over_under, prob_btts
from leagues_data import ALL_LEAGUES
from tracker import init_db, log_signal, get_signals, get_performance_summary, add_subscriber, remove_subscriber, get_subscribers, is_notified, mark_notified
from odds_ingest import load_odds
from value_filter import compute_ev, kelly_fraction, kelly_euro, filter_value_bets
from surebet_scanner import scan_surebets
from calendar import fetch_and_analyze_today, get_calendar_formatted, get_value_picks_for_schedina, format_schedina

try:
    from odds_api import get_live_odds
    LIVE_ODDS_AVAILABLE = True
except Exception:
    LIVE_ODDS_AVAILABLE = False

from config import TOKEN, BANKROLL_DEFAULT

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

chat_bankrolls: dict[int, float] = {}

def get_bankroll(chat_id: int) -> float:
    return chat_bankrolls.get(chat_id, BANKROLL_DEFAULT)

def set_bankroll(chat_id: int, amount: float) -> None:
    chat_bankrolls[chat_id] = max(10.0, amount)

def get_odds_data():
    if os.getenv("ODDS_API_KEY") and LIVE_ODDS_AVAILABLE:
        try:
            odds = get_live_odds()
            logger.info(f"Quote reali caricate: {len(odds)} quote")
            return odds
        except Exception as e:
            logger.warning(f"Quote reali non disponibili: {e}")
    return load_odds()

def _all_teams():
    teams = set()
    for lt in ALL_LEAGUES.values():
        teams.update(lt.keys())
    return teams

def format_segnale_pronto(home, away, lam_h, lam_a, quota_over=2.10, bookmaker="Generico", bankroll=100.0):
    p1, px, p2 = prob_1x2(lam_h, lam_a)
    p_over, p_under = prob_over_under(lam_h, lam_a)
    p_btts = prob_btts(lam_h, lam_a)
    candidates = [
        ("1", p1, f"Vittoria {home}", 2.0), ("X", px, "Pareggio", 3.2),
        ("2", p2, f"Vittoria {away}", 2.0), ("Over 2.5", p_over, "Over 2.5 Gol", quota_over),
        ("Under 2.5", p_under, "Under 2.5 Gol", 1.85), ("BTTS", p_btts, "Gol Gol (BTTS)", 1.90),
    ]
    best = max(candidates, key=lambda x: compute_ev(x[1], x[3]))
    _, best_prob, best_label, best_quota = best
    ev = compute_ev(best_prob, best_quota)
    ev_percent = ev * 100.0
    kelly = kelly_fraction(best_prob, best_quota)
    stake_euro = kelly_euro(bankroll, best_prob, best_quota)
    if ev > 0.10: valore_label, raccomandazione = "🟢 *FORTE VALORE*", "✅ Raccomandato"
    elif ev > 0.03: valore_label, raccomandazione = "🟡 *Valore positivo*", "⚠️ Marginale"
    elif ev > 0: valore_label, raccomandazione = "🟠 *Valore debole*", "ℹ️ Rischio elevato"
    else: valore_label, raccomandazione = "🔴 *Valore negativo*", "❌ NON raccomandato"
    msg = (
        f"📊 *SEGNALE PRONTO – {home} vs {away}*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚽ *Expected Goals:*\n   {home}: {lam_h:.2f}\n   {away}: {lam_a:.2f}\n\n"
        f"📈 *Probabilità:*\n   1: {p1*100:.1f}% | X: {px*100:.1f}% | 2: {p2*100:.1f}%\n"
        f"   Over 2.5: {p_over*100:.1f}% | Under 2.5: {p_under*100:.1f}%\n   BTTS: {p_btts*100:.1f}%\n\n"
        f"🎯 *SEGNALE:* {best_label}\n   Bookmaker: {bookmaker} | Quota: {best_quota:.2f}\n"
        f"   EV: {ev_percent:+.2f}%\n\n💰 *Kelly:*\n   Bankroll: €{bankroll:.2f}\n"
        f"   Frazione: {kelly*100:.1f}% | *Stake: €{stake_euro:.2f}*\n\n"
        f"{valore_label}\n{raccomandazione}\n\n📅 *Data:* oggi"
    )
    return msg + DISCLAIMER

async def cmd_test_segnale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = format_segnale_pronto("Inter", "Napoli", 1.85, 1.12, 2.10, "Bet365", get_bankroll(chat_id))
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_segnale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❌ Errore: specifica casa e trasferta.\nEsempio: `/segnale Roma Milan`", parse_mode="Markdown")
        return
    raw = " ".join(args)
    words = raw.split()
    home, away = raw, raw
    for i in range(1, len(words)):
        h = " ".join(words[:i]); a = " ".join(words[i:])
        if h in _all_teams() and a in _all_teams():
            home, away = h, a; break
    if home not in _all_teams() or away not in _all_teams():
        await update.message.reply_text(f"❌ Squadra non trovata.\nUsa `/campionati` per la lista.", parse_mode="Markdown")
        return
    try:
        lam_h, lam_a = expected_goals(home, away)
        odds_data = get_odds_data()
        event_name = None
        for league in ALL_LEAGUES:
            if home in ALL_LEAGUES[league]: event_name = f"{league} – {home} vs {away}"; break
        best_over = None
        if event_name:
            try:
                best_over = max((o for o in odds_data if event_name.lower() in o.get("evento","").lower() and "over" in o.get("esito","").lower()), key=lambda x: x.get("quota_decimale",0), default=None)
            except: pass
        quota, bookmaker = (best_over["quota_decimale"], best_over["bookmaker"]) if best_over else (2.10, "Modello")
        text = format_segnale_pronto(home, away, lam_h, lam_a, quota, bookmaker, get_bankroll(update.effective_chat.id))
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Errore segnale: {e}")
        await update.message.reply_text("❌ Errore nel calcolo. Riprova.", parse_mode="Markdown")

async def cmd_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_value_bets(get_odds_data(), get_bankroll(update.effective_chat.id))
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_surebet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_surebets(get_odds_data())
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_storico_personale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    signals = get_signals(chat_id=chat_id, limit=20)
    if not signals:
        await update.message.reply_text("📭 Nessun segnale ricevuto.", parse_mode="Markdown"); return
    msg = "📊 *I tuoi ultimi segnali*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for s in signals[:10]:
        status = "✅" if s.esito_finale == "won" else "❌" if s.esito_finale == "lost" else "⏳"
        profit = f" ({s.profit:+.2f}u)" if s.profit != 0 else ""
        msg += f"{status} {s.evento}\n   {s.esito} @ {s.quota:.2f} | EV {s.ev*100:+.1f}%{profit}\n\n"
    summary = get_performance_summary(days=30)
    if summary["closed"] > 0:
        msg += f"📈 30gg: {summary['closed']} segnali | ROI: {summary['roi']:.1f}%"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_setbankroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        await update.message.reply_text(f"💰 Bankroll: €{get_bankroll(chat_id):.2f}\nUsa `/setbankroll 500`", parse_mode="Markdown"); return
    try:
        amount = float(args[0].replace(",","."))
        set_bankroll(chat_id, amount)
        await update.message.reply_text(f"✅ Bankroll: €{amount:.2f}", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Numero non valido.", parse_mode="Markdown")

async def cmd_campionati(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = "🏆 *Campionati disponibili*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for name, teams in ALL_LEAGUES.items(): msg += f"• *{name}*: {len(teams)} squadre\n"
    msg += "\nEsempio: `/segnale Manchester City Arsenal`"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_calendario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = get_calendar_formatted()
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_analisi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔄 Aggiornamento calendario e analisi in corso...", parse_mode="Markdown")
    total, _ = fetch_and_analyze_today()
    text = get_calendar_formatted()
    await update.message.reply_text(f"✅ Analizzate {total} partite.\n\n{text}", parse_mode="Markdown")

async def cmd_schedina(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    picks = get_value_picks_for_schedina()
    text = format_schedina(picks, get_bankroll(chat_id))
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    add_subscriber(update.effective_chat.id)
    await update.message.reply_text("🔔 *Iscrizione attivata!*\nRiceverai:\n• Schedina mattutina alle 8:00\n• Notifiche value bet\n• Aggiornamenti pomeriggio e sera\n\n`/unsubscribe` per disiscriverti.", parse_mode="Markdown")

async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("🔕 Disiscrizione completata.", parse_mode="Markdown")

async def cmd_checknow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Controllo quote reali...", parse_mode="Markdown")
    await notify_job(context)
    await update.message.reply_text("✅ Completato.", parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📋 *Comandi QuotaVerace*\n\n"
        "`/calendario` – partite del giorno con analisi\n"
        "`/analisi` – aggiorna calendario e analisi ora\n"
        "`/schedina` – schedina con le migliori singole\n"
        "`/segnale <casa> <trasferta>` – analisi specifica\n"
        "`/value` – value bet EV > 5%\n"
        "`/surebet` – scanner arbitraggi\n"
        "`/setbankroll <€>` – imposta bankroll\n"
        "`/subscribe` – attiva notifiche automatiche\n"
        "`/campionati` – elenco squadre\n\n"
        "📖 Ogni segnale: Poisson, EV%, Kelly Criterion in €"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

def format_value_bets(odds_data, bankroll=100.0):
    value_signals = filter_value_bets(odds_data, ev_threshold=0.05)
    if not value_signals:
        return "📊 *Value Bet*\n\nNessun segnale EV > 5%." + DISCLAIMER
    msg = "📊 *VALUE BET – EV > 5%*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for sig in value_signals[:5]:
        ev_pct = sig["ev"] * 100
        kelly = sig.get("kelly", 0)
        stake = kelly_euro(bankroll, sig.get("probabilita", 0), sig["quota_decimale"])
        msg += f"🏟 {sig['evento']}\n🎯 {sig['esito']} @ {sig['quota_decimale']:.2f} ({sig['bookmaker']})\n📈 EV: +{ev_pct:.2f}% | Stake: €{stake:.2f}\n\n"
    msg += f"💰 Bankroll: €{bankroll:.2f}"
    return msg + DISCLAIMER

def format_surebets(odds_data):
    sures = scan_surebets(odds_data)
    if not sures: return "🔍 *Surebet*\n\nNessun arbitraggio trovato." + DISCLAIMER
    msg = "🔍 *SUREBET*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for s in sures: msg += f"🏟 {s['evento']}\n💰 Profitto: {s['profit_pct']:.2f}%\n\n"
    return msg + DISCLAIMER

async def notify_job(context: ContextTypes.DEFAULT_TYPE):
    if not os.getenv("ODDS_API_KEY") or not LIVE_ODDS_AVAILABLE: return
    try:
        odds = get_live_odds()
        if not odds: return
        value_signals = filter_value_bets(odds, ev_threshold=0.08)
        if not value_signals: return
        subscribers = get_subscribers()
        if not subscribers: return
        today = __import__('datetime').datetime.now().strftime("%Y-%m-%d")
        for sig in value_signals[:3]:
            if is_notified(sig.get("match_id","unknown"), today): continue
            ev_pct = sig["ev"] * 100
            msg = f"🔔 *NOTIFICA VALUE BET*\n━━━━━━━━━━━━━━━━━━━━━━\n\n🏟 {sig['evento']}\n🎯 {sig['esito']} @ {sig['quota_decimale']:.2f} ({sig['bookmaker']})\n📈 EV: +{ev_pct:.2f}%\n\n💡 `/segnale` per dettagli"
            for chat_id in subscribers:
                try: await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                except: pass
            mark_notified(sig.get("match_id","unknown"), today)
    except Exception as e: logger.error(f"Errore notify: {e}")

async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Job mattutino: calendario + schedina")
    fetch_and_analyze_today()
    picks = get_value_picks_for_schedina()
    if not picks: return
    text = format_schedina(picks, 100.0)
    for chat_id in get_subscribers():
        try: await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except: pass

async def afternoon_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Job pomeridiano: ricontrollo")
    fetch_and_analyze_today()
    await notify_job(context)

async def evening_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Job serale: ricontrollo")
    fetch_and_analyze_today()
    await notify_job(context)

def main() -> None:
    if not TOKEN: raise ValueError("Token non configurato.")
    init_db()
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("test_segnale", cmd_test_segnale))
    application.add_handler(CommandHandler("segnale", cmd_segnale))
    application.add_handler(CommandHandler("value", cmd_value))
    application.add_handler(CommandHandler("surebet", cmd_surebet))
    application.add_handler(CommandHandler("storico_personale", cmd_storico_personale))
    application.add_handler(CommandHandler("setbankroll", cmd_setbankroll))
    application.add_handler(CommandHandler("campionati", cmd_campionati))
    application.add_handler(CommandHandler("calendario", cmd_calendario))
    application.add_handler(CommandHandler("analisi", cmd_analisi))
    application.add_handler(CommandHandler("schedina", cmd_schedina))
    application.add_handler(CommandHandler("subscribe", cmd_subscribe))
    application.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    application.add_handler(CommandHandler("checknow", cmd_checknow))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("start", cmd_help))
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_daily(morning_job, time=time(hour=6, minute=0))
        job_queue.run_daily(afternoon_job, time=time(hour=14, minute=0))
        job_queue.run_daily(evening_job, time=time(hour=20, minute=0))
        logger.info("Job schedulati: 08:00 / 16:00 / 22:00 ITA")
    else: logger.warning("JobQueue non disponibile")
    logger.info("QuotaVerace avviato.")
    application.run_polling()

if __name__ == "__main__":
    main()
