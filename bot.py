import json
import logging
import math
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from poisson_engine import expected_goals, prob_1x2, prob_over_under, prob_btts
from leagues_data import ALL_LEAGUES
from tracker import init_db, log_signal, get_signals, get_performance_summary
from odds_ingest import load_odds
from value_filter import compute_ev, kelly_fraction, kelly_euro, filter_value_bets
from surebet_scanner import scan_surebets

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
        ("1", p1, f"Vittoria {home}", 2.0),
        ("X", px, "Pareggio", 3.2),
        ("2", p2, f"Vittoria {away}", 2.0),
        ("Over 2.5", p_over, "Over 2.5 Gol", quota_over),
        ("Under 2.5", p_under, "Under 2.5 Gol", 1.85),
        ("BTTS", p_btts, "Gol Gol (BTTS)", 1.90),
    ]

    best = max(candidates, key=lambda x: compute_ev(x[1], x[3]))
    best_code, best_prob, best_label, best_quota = best

    ev = compute_ev(best_prob, best_quota)
    ev_percent = ev * 100.0
    kelly = kelly_fraction(best_prob, best_quota)
    stake_euro = kelly_euro(bankroll, best_prob, best_quota)

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
        f"   Over 2.5: {p_over*100:.1f}% | Under 2.5: {p_under*100:.1f}%\n"
        f"   BTTS: {p_btts*100:.1f}%\n\n"
        f"🎯 *SEGNALE RACCOMANDATO*\n"
        f"   Esito: {best_label}\n"
        f"   Bookmaker: {bookmaker}\n"
        f"   Quota: {best_quota:.2f}\n"
        f"   Probabilità: {best_prob*100:.1f}%\n"
        f"   EV: {ev_percent:+.2f}%\n\n"
        f"💰 *Kelly Criterion*\n"
        f"   Bankroll: €{bankroll:.2f}\n"
        f"   Frazione Kelly: {kelly*100:.1f}%\n"
        f"   *Stake suggerito: €{stake_euro:.2f}*\n\n"
        f"{valore_label}\n"
        f"{raccomandazione}\n\n"
        f"📅 *Data:* oggi"
    )
    return msg + DISCLAIMER

def format_value_bets(odds_data, bankroll=100.0):
    value_signals = filter_value_bets(odds_data, ev_threshold=0.05)
    if not value_signals:
        return "📊 *Value Bet del giorno*\n\nNessun segnale con EV > 5% trovato oggi.\nProva con `/segnale <casa> <trasferta>` per analizzare una partita specifica." + DISCLAIMER

    msg = "📊 *VALUE BET – Segnali con EV > 5%*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for sig in value_signals[:5]:
        ev_pct = sig["ev"] * 100
        kelly = sig.get("kelly", 0)
        stake = kelly_euro(bankroll, sig.get("probabilita", 0), sig["quota_decimale"])
        msg += f"🏟 {sig['evento']}\n🎯 {sig['esito']} @ {sig['quota_decimale']:.2f} ({sig['bookmaker']})\n📈 EV: +{ev_pct:.2f}% | Kelly: {kelly*100:.1f}% | Stake: €{stake:.2f}\n\n"
    msg += f"💰 Bankroll impostata: €{bankroll:.2f}"
    return msg + DISCLAIMER

def format_surebets(odds_data):
    sures = scan_surebets(odds_data)
    if not sures:
        return "🔍 *Surebet Scanner*\n\nNessun arbitraggio trovato nei dati attuali.\nLe surebet sono rare e richiedono quote da bookmaker diversi in tempo reale." + DISCLAIMER
    msg = "🔍 *SUREBET TROVATE*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for s in sures:
        msg += f"🏟 {s['evento']}\n📊 Tipo: {s['tipo']} | Margine: {s['margin']:.4f}\n💰 Profitto garantito: {s['profit_pct']:.2f}%\n🪙 Distribuzione stake: {json.dumps(s['stakes'], ensure_ascii=False)}\n\n"
    return msg + DISCLAIMER

async def cmd_test_segnale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    bankroll = get_bankroll(chat_id)
    text = format_segnale_pronto("Inter", "Napoli", 1.85, 1.12, 2.10, "Bet365", bankroll)
    await update.message.reply_text(text, parse_mode="Markdown")
    try:
        log_signal(chat_id=chat_id, evento="Inter vs Napoli", esito="Over 2.5 Gol", quota=2.10, probabilita=0.55, ev=0.155)
    except Exception as e:
        logger.warning(f"Log segnale fallito: {e}")

async def cmd_segnale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❌ *Errore:* specifica casa e trasferta.\nEsempio: `/segnale Roma Milan`", parse_mode="Markdown")
        return

    raw = " ".join(args)
    words = raw.split()
    home, away = raw, raw
    for i in range(1, len(words)):
        h = " ".join(words[:i])
        a = " ".join(words[i:])
        if h in _all_teams() and a in _all_teams():
            home, away = h, a
            break

    if home not in _all_teams() or away not in _all_teams():
        await update.message.reply_text(f"❌ Squadra non trovata.\nHai scritto: *{home}* vs *{away}*\nUsa `/campionati` per vedere le squadre disponibili.", parse_mode="Markdown")
        return

    try:
        lam_h, lam_a = expected_goals(home, away)
        odds_data = get_odds_data()
        event_name = None
        for league in ALL_LEAGUES:
            if home in ALL_LEAGUES[league]:
                event_name = f"{league} – {home} vs {away}"
                break

        best_over = None
        if event_name:
            try:
                best_over = max((o for o in odds_data if event_name.lower() in o.get("evento", "").lower() and "over" in o.get("esito", "").lower()), key=lambda x: x.get("quota_decimale", 0), default=None)
            except:
                pass

        if best_over:
            quota, bookmaker = best_over["quota_decimale"], best_over["bookmaker"]
        else:
            quota, bookmaker = 2.10, "Modello (quota stimata)"

        chat_id = update.effective_chat.id
        bankroll = get_bankroll(chat_id)
        text = format_segnale_pronto(home, away, lam_h, lam_a, quota, bookmaker, bankroll)
        await update.message.reply_text(text, parse_mode="Markdown")

        p1, px, p2 = prob_1x2(lam_h, lam_a)
        p_over, p_under = prob_over_under(lam_h, lam_a)
        p_btts = prob_btts(lam_h, lam_a)
        candidates = [("1", p1, f"Vittoria {home}", 2.0), ("X", px, "Pareggio", 3.2), ("2", p2, f"Vittoria {away}", 2.0), ("Over 2.5", p_over, "Over 2.5 Gol", quota), ("Under 2.5", p_under, "Under 2.5 Gol", 1.85), ("BTTS", p_btts, "Gol Gol (BTTS)", 1.90)]
        best = max(candidates, key=lambda x: compute_ev(x[1], x[3]))
        best_code, best_prob, best_label, best_quota = best
        try:
            log_signal(chat_id=chat_id, evento=f"{home} vs {away}", esito=best_label, quota=best_quota, probabilita=best_prob, ev=compute_ev(best_prob, best_quota))
        except Exception as e:
            logger.warning(f"Log segnale fallito: {e}")
    except Exception as e:
        logger.error(f"Errore calcolo segnale: {e}")
        await update.message.reply_text("❌ Errore nel calcolo del segnale. Riprova più tardi.", parse_mode="Markdown")

async def cmd_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    odds_data = get_odds_data()
    chat_id = update.effective_chat.id
    bankroll = get_bankroll(chat_id)
    text = format_value_bets(odds_data, bankroll)
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_surebet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    odds_data = get_odds_data()
    text = format_surebets(odds_data)
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_storico_personale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    signals = get_signals(chat_id=chat_id, limit=20)
    if not signals:
        await update.message.reply_text("📭 Non hai ancora ricevuto segnali.\nUsa `/segnale <casa> <trasferta>` per iniziare.", parse_mode="Markdown")
        return

    msg = "📊 *I tuoi ultimi segnali*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for s in signals[:10]:
        status = "✅" if s.esito_finale == "won" else "❌" if s.esito_finale == "lost" else "⏳"
        profit = f" ({s.profit:+.2f}u)" if s.profit != 0 else ""
        msg += f"{status} {s.evento}\n   {s.esito} @ {s.quota:.2f} | EV {s.ev*100:+.1f}%{profit}\n\n"

    summary = get_performance_summary(days=30)
    if summary["closed"] > 0:
        msg += f"📈 *Riepilogo 30 giorni*\n   Segnali: {summary['closed']} | Vinti: {summary['won']} | Persi: {summary['lost']}\n   Profitto: {summary['net_profit']:+.2f}u | ROI: {summary['roi']:.1f}%"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_setbankroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        await update.message.reply_text(f"💰 Bankroll attuale: €{get_bankroll(chat_id):.2f}\nUsa `/setbankroll 500` per impostare un nuovo bankroll.", parse_mode="Markdown")
        return
    try:
        amount = float(args[0].replace(",", "."))
        set_bankroll(chat_id, amount)
        await update.message.reply_text(f"✅ Bankroll impostato a €{amount:.2f}", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Inserisci un numero valido. Esempio: `/setbankroll 250`", parse_mode="Markdown")

async def cmd_campionati(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = "🏆 *Campionati disponibili*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for name, teams in ALL_LEAGUES.items():
        msg += f"• *{name}*: {len(teams)} squadre\n"
    msg += "\nEsempio: `/segnale Manchester City Arsenal` (Premier League)"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    squadre = "\n".join(f"• {s}" for s in sorted(_all_teams()))
    text = (
        "📋 *Comandi QuotaVerace*\n\n"
        "`/test_segnale` – segnale demo Inter-Napoli\n"
        "`/segnale <casa> <trasferta>` – analisi Poisson + quote reali\n"
        "   Esempio: `/segnale Roma Milan`\n"
        "`/value` – value bet con EV > 5% e stake Kelly\n"
        "`/surebet` – scanner arbitraggi\n"
        "`/storico_personale` – i tuoi segnali e performance\n"
        "`/setbankroll <€>` – imposta bankroll per Kelly Criterion\n"
        "`/campionati` – elenco campionati e squadre\n\n"
        "🏟 *Squadre disponibili (5 campionati):*\n"
        f"{squadre}\n\n"
        "📖 Ogni segnale include:\n"
        "• Expected goals (modello Poisson aggiornato)\n"
        "• Probabilità 1X2, Over/Under, BTTS\n"
        "• Quota reale da bookmaker (se disponibile)\n"
        "• EV% calcolato\n"
        "• *Kelly Criterion* per stake ottimale in €"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

def main() -> None:
    if not TOKEN:
        raise ValueError("Token non configurato. Imposta QUOTAVERACE_BOT_TOKEN.")

    init_db()

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("test_segnale", cmd_test_segnale))
    application.add_handler(CommandHandler("segnale", cmd_segnale))
    application.add_handler(CommandHandler("value", cmd_value))
    application.add_handler(CommandHandler("surebet", cmd_surebet))
    application.add_handler(CommandHandler("storico_personale", cmd_storico_personale))
    application.add_handler(CommandHandler("setbankroll", cmd_setbankroll))
    application.add_handler(CommandHandler("campionati", cmd_campionati))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("start", cmd_help))

    logger.info("QuotaVerace Bot avviato.")
    application.run_polling()

if __name__ == "__main__":
    main()
