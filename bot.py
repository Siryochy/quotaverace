import json
import logging
import os
import shutil
import sqlite3
from datetime import time, datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import DATA_DIR
from poisson_engine import expected_goals, prob_1x2, prob_over_under, prob_btts
from leagues_data import ALL_LEAGUES
from tracker import (init_db, log_signal, get_signals, get_performance_summary,
                     add_subscriber, remove_subscriber, get_subscribers, set_tier,
                     get_subscription, is_premium, is_notified, mark_notified)
from odds_ingest import load_odds
from value_filter import compute_ev, kelly_fraction, kelly_euro, filter_value_bets, is_sane, get_pro_stake
from surebet_scanner import scan_surebets
from backtest import run_backtest
from football_hist import run_sync
from fixture_engine import (fetch_and_analyze_today, get_calendar_formatted,
                            get_value_picks_for_schedina, format_schedina, build_multipla_block)
from auto_bet import run_today_bets

try:
    from odds_api import get_live_odds
    LIVE_ODDS_AVAILABLE = True
except Exception:
    LIVE_ODDS_AVAILABLE = False

from config import TOKEN, BANKROLL_DEFAULT

# --- AI Commander (opsional: butuh GOOGLE_API_KEY di .env) ---
try:
    from ai_commander import AICommander
    _AI_OK = True
except Exception as _e:  # modul ada tapi dependensi/env hilang
    _AI_OK = False
    _AI_ERR = _e

import asyncio
import concurrent.futures
_ai_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_scan_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_ai_commander = None  # dibuat lazy saat pertama dipakai


def _get_ai_commander():
    global _ai_commander
    if _ai_commander is None:
        _ai_commander = AICommander()
    return _ai_commander


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ai <pertanyaan bebas> — Comandante AI memilih tool mesin sendiri."""
    if not _AI_OK:
        await update.message.reply_text(
            "🤖 AI Commander tidak aktif (butuh GOOGLE_API_KEY di .env).",
            parse_mode="Markdown")
        return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text(
            "🤖 *AI Commander*\nTanya bebas, saya pilih tool mesinnya sendiri.\n"
            "Contoh: `/ai analisa Inter vs Napoli` atau `/ai schedina hari ini`",
            parse_mode="Markdown")
        return
    note = await update.message.reply_text("🤖 Comandante sedang menganalisa...")
    loop = asyncio.get_running_loop()
    try:
        answer = await loop.run_in_executor(
            _ai_executor, lambda: _get_ai_commander().run(prompt))
    except Exception as e:
        logger.exception("AI commander gagal")
        answer = f"🤖 AI Commander gagal: {type(e).__name__}: {e}"
    # Markdown Telegram è severo: se il testo contiene caratteri non validi
    # (es. underscore in nomi squadra) mandiamo il testo grezzo.
    try:
        await note.edit_text(answer, parse_mode="Markdown")
    except Exception:
        try:
            await note.edit_text(answer)
        except Exception:
            await update.message.reply_text(answer)

import secure_logging
secure_logging.setup()  # maschera segreti nei log + httpx a WARNING (no token negli URL)

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

# File di fallback delle quote reali (schema odds_ingest).
ODDS_FALLBACK_FILE = DATA_DIR / "odds_sample.json"


def get_odds_data():
    if os.getenv("ODDS_API_KEY") and LIVE_ODDS_AVAILABLE:
        try:
            odds = get_live_odds()
            logger.info(f"Quote reali caricate: {len(odds)} quote")
            return odds
        except Exception as e:
            logger.warning(f"Quote reali non disponibili: {e}")
    try:
        return load_odds(str(ODDS_FALLBACK_FILE))
    except Exception as e:
        logger.warning(f"Fallback quote non disponibile: {e}")
        return []


def get_odds_freshness_note() -> str | None:
    """Nota di freschezza delle quote per i segnali manuali.

    None se le quote vengono dal feed live (ODDS_API_KEY). Altrimenti
    l'eta' del file di fallback: se vecchio, chi punta deve verificare
    il prezzo attuale sul bookmaker (le quote stale = edge finto).
    """
    if os.getenv("ODDS_API_KEY") and LIVE_ODDS_AVAILABLE:
        return None
    try:
        age_sec = datetime.now().timestamp() - ODDS_FALLBACK_FILE.stat().st_mtime
        age_min = int(age_sec / 60)
    except Exception:
        return "Quote di mercato non disponibili: segnale basato solo sul modello."
    if age_min < 60:
        return None
    return (f"Quote di mercato da cache ({age_min} min fa): verifica il prezzo "
            f"attuale sul bookmaker prima di puntare.")

def _all_teams():
    teams = set()
    for lt in ALL_LEAGUES.values():
        teams.update(lt.keys())
    return teams

# --- Sticker premium animato (gratis: nessun Telegram Premium/Fragment richiesto) ---
# Telegram non permette custom emoji nel testo senza usernames su Fragment o
# Premium sull'account proprietario. Workaround: il bot invia uno sticker
# animato (set pubblico configurabile via PREMIUM_STICKER_SET) prima dei
# messaggi premium. Mai bloccante: se fallisce si manda solo il testo.
PREMIUM_STICKER_SET = os.getenv("PREMIUM_STICKER_SET", "Diamond")
_PREMIUM_STICKER_EMOJIS = ("💎", "🔔", "⚡", "🔥", "🏆", "💰", "✅")
_premium_sticker_file_id: str | None = None


async def get_premium_sticker_file_id(bot) -> str | None:
    """File_id di uno sticker del set configurato, preferendo animato ed emoji pertinente.

    Il file_id e' stabile per bot: viene recuperato una sola volta e messo in
    cache in memoria.
    """
    global _premium_sticker_file_id
    if _premium_sticker_file_id:
        return _premium_sticker_file_id
    try:
        sticker_set = await bot.get_sticker_set(PREMIUM_STICKER_SET)
        stickers = list(sticker_set.stickers)
        if not stickers:
            return None
        def _score(s):
            return (bool(getattr(s, "is_animated", False)),
                    str(getattr(s, "emoji", "")) in _PREMIUM_STICKER_EMOJIS)
        best = max(stickers, key=_score)
        _premium_sticker_file_id = best.file_id
        logger.info("Sticker premium pronto: %s/%s (animato=%s)",
                    PREMIUM_STICKER_SET, best.emoji,
                    getattr(best, "is_animated", False))
        return _premium_sticker_file_id
    except Exception as e:
        logger.warning("Sticker set '%s' non disponibile: %s", PREMIUM_STICKER_SET, e)
        return None


async def send_premium_sticker(bot, chat_id) -> None:
    """Invia lo sticker animato prima di un messaggio premium (mai bloccante)."""
    try:
        file_id = await get_premium_sticker_file_id(bot)
        if file_id:
            await bot.send_sticker(chat_id=chat_id, sticker=file_id)
    except Exception as e:
        logger.warning(f"Sticker premium non inviato a {chat_id}: {e}")

def format_segnale_pronto(home, away, lam_h, lam_a, quota_over=2.10, bookmaker="Generico", bankroll=100.0,
                          extra_note=None):
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
    pro = get_pro_stake(bankroll, best_prob, best_quota)
    stake_euro = pro["stake"]
    sane, reason = is_sane(best_prob, best_quota, ev)
    if not sane:
        valore_label = f"🔴 *FILTRATO — {reason}*"
        raccomandazione = "❌ Segnale scartato dai filtri di sanità Pro"
    elif ev > 0.10:
        valore_label = "🟢 *FORTE VALORE*"
        raccomandazione = "✅ Raccomandato"
    elif ev > 0.03:
        valore_label = "🟡 *Valore positivo*"
        raccomandazione = "⚠️ Marginale, valutare con cautela"
    elif ev > 0:
        valore_label = "🟠 *Valore debole*"
        raccomandazione = "ℹ️ Rischio elevato — stake minimo"
    else:
        valore_label = "🔴 *Valore negativo*"
        raccomandazione = "❌ NON raccomandato"
    msg = (
        f"📊 *SEGNALE PRONTO – {home} vs {away}*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚽ *Expected Goals:*\n   {home}: {lam_h:.2f}\n   {away}: {lam_a:.2f}\n\n"
        f"📈 *Probabilità:*\n   1: {p1*100:.1f}% | X: {px*100:.1f}% | 2: {p2*100:.1f}%\n"
        f"   Over 2.5: {p_over*100:.1f}% | Under 2.5: {p_under*100:.1f}%\n   BTTS: {p_btts*100:.1f}%\n\n"
        f"🎯 *SEGNALE:* {best_label}\n   Bookmaker: {bookmaker} | Quota: {best_quota:.2f}\n"
        f"   EV: {ev_percent:+.2f}%\n\n"
        f"💰 *Kelly Pro (1/4 + cap 3%):*\n"
        f"   Bankroll: €{bankroll:.2f}\n"
        f"   Kelly grezzo: {pro['kelly_pct']:.1f}% | Cap: €{pro['stake_cap']:.2f} (3%)\n"
        f"   *Stake finale: €{stake_euro:.2f}* ({pro['stake_pct_of_bankroll']:.1f}% bankroll)\n\n"
        f"🛡 *Filtri applicati:*\n"
        f"   EV: 3%-15% | Odds: 1.50-5.00 | Kelly: 1/4 | Cap: 3%\n\n"
        f"{valore_label}\n{raccomandazione}\n\n📅 *Data:* oggi"
    )
    if extra_note:
        msg += f"\n\n⚠️ {extra_note}"
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
        await update.message.reply_text(f"❌ Squadra non trovata: {home}.\nUsa `/campionati` per la lista.", parse_mode="Markdown")
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
                best_over = max((o for o in odds_data if event_name.lower() in o.get("evento","").lower()
                                 and "over" in o.get("esito","").lower()), key=lambda x: x.get("quota_decimale",0), default=None)
            except: pass
        quota, bookmaker = (best_over["quota_decimale"], best_over["bookmaker"]) if best_over else (2.10, "Modello")
        notes = []
        if bookmaker == "Modello":
            notes.append("Quota di MODELLO, non verificata su un bookmaker reale: "
                         "controlla il miglior prezzo disponibile prima di puntare.")
        fresh = get_odds_freshness_note()
        if fresh:
            notes.append(fresh)
        text = format_segnale_pronto(home, away, lam_h, lam_a, quota, bookmaker,
                                     get_bankroll(update.effective_chat.id),
                                     extra_note=" ".join(notes) if notes else None)
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
        await update.message.reply_text(f"✅ Bankroll: €{amount:.2f}\n\n🛡 Sistema Pro attivo:\n• Kelly 1/4 | Cap 3%\n• EV 3%-15% | Odds 1.50-5.00", parse_mode="Markdown")
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
    total, _, _ = fetch_and_analyze_today()
    text = get_calendar_formatted()
    await update.message.reply_text(f"✅ Analizzate {total} partite.\n\n{text}", parse_mode="Markdown")

async def cmd_schedina(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    picks = get_value_picks_for_schedina()
    text = format_schedina(picks, get_bankroll(chat_id))
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_multipla(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    picks = get_value_picks_for_schedina()
    block = build_multipla_block(picks, get_bankroll(chat_id))
    if not block:
        await update.message.reply_text("🎲 *MULTIPLA PROLUNGATA*\n\nServono almeno 2 esiti con valore positivo.\nRiprova dopo `/analisi`.", parse_mode="Markdown")
        return
    prefix = "🎲 *MULTIPLA PROLUNGATA*\n🗓 " + datetime.now().strftime('%d/%m/%Y') + "\n"
    await update.message.reply_text(prefix + block, parse_mode="Markdown")

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    add_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "🔔 *Iscrizione attivata!* (piano: FREE)\n\nRiceverai:\n"
        "• Schedina mattutina alle 8:00\n"
        "• Notifiche value bet (EV 3%-15%, Odds 1.50-5.00)\n"
        "• Aggiornamenti pomeriggio e sera\n\n"
        "🛡 *Filtri Pro attivi:*\n"
        "• Kelly 1/4 | Cap puntata 3%\n"
        "• EV min +3% | EV max +15%\n"
        "• Odds 1.50-5.00\n\n"
        "💎 *Premium* (segnali istantanei, strong value, surebet): "
        "`/premium` per info.", parse_mode="Markdown")

async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("🔕 Disiscrizione completata.", parse_mode="Markdown")

async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Attiva il piano premium GRATUITAMENTE (offerta lancio).

    Nessun pagamento richiesto: chiunque puo' sbloccare premium con
    /premium. L'infrastruttura dei tier resta pronta per monetizzare
    in futuro — basta impostare PREMIUM_FREE=0 per chiudere il gate
    e richiedere il pagamento (Telegram Stars / Stripe).
    """
    chat_id = update.effective_chat.id
    add_subscriber(chat_id)
    days = 90
    if context.args:
        try:
            days = max(1, int(context.args[0]))
        except ValueError:
            pass
    from datetime import timedelta
    until = (datetime.now() + timedelta(days=days)).isoformat()
    premium_free = os.getenv("PREMIUM_FREE", "1").lower() not in ("0", "false", "no")
    if premium_free:
        set_tier(chat_id, "premium", until)
        await send_premium_sticker(context.bot, chat_id)
        await update.message.reply_text(
            "💎 *Premium attivo — GRATIS!*\n\n"
            f"Scadenza: {datetime.strptime(until[:10], '%Y-%m-%d').strftime('%d/%m/%Y')} "
            "(rinnovabile sempre gratis con `/premium`)\n\n"
            "Hai sbloccato:\n"
            "• Segnali value IMMEDIATI (no ritardo di 3 ore)\n"
            "• Alert surebet in tempo reale\n"
            "• Badge 💎 sui segnali\n\n"
            "`/mytier` per lo stato. `/unsubscribe` per disiscriverti.",
            parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "💎 *Piano Premium*\n\n"
            "Riceverai (rispetto al piano free):\n"
            "• Segnali value IMMEDIATI (il piano free li riceve in ritardo)\n"
            "• Alert surebet in tempo reale\n\n"
            "Per attivarlo contatta l'amministratore.", parse_mode="Markdown")

async def cmd_mytier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sub = get_subscription(update.effective_chat.id)
    if not sub:
        await update.message.reply_text(
            "Non risulti iscritto. `/subscribe` per attivare le notifiche.",
            parse_mode="Markdown")
        return
    tier, until = sub
    if tier == "premium":
        expiry = (datetime.fromisoformat(until).strftime("%d/%m/%Y")
                  if until else "senza scadenza")
        await update.message.reply_text(
            f"💎 Piano: PREMIUM (scadenza: {expiry})", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "🆓 Piano: FREE\n\n"
            "Il piano premium aggiunge: segnali immediati, strong value, "
            "surebet. `/premium` per info.", parse_mode="Markdown")

async def cmd_checknow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Controllo quote reali con filtri Pro...", parse_mode="Markdown")
    await notify_job(context)
    await update.message.reply_text("✅ Completato.", parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📋 *QuotaVerace Pro — Comandi*\n\n"
        "`/calendario` – partite del giorno con analisi\n"
        "`/analisi` – aggiorna calendario e analisi\n"
        "`/schedina` – schedina con filtri Pro\n"
        "`/multipla` – multipla prolungata con risk management\n"
        "`/segnale <casa> <trasferta>` – analisi specifica\n"
        "`/value` – value bet filtrate\n"
        "`/surebet` – scanner arbitraggi\n"
        "`/setbankroll <€>` – imposta bankroll\n"
        "`/subscribe` – attiva notifiche Pro\n"
        "`/risultati` – statistiche reali dei segnali\n"
        "`/backtest` – calibrazione EV atteso vs ROI realizzato\n"
        "`/sync` – sincronizza risultati storici (API-Football)\n"
        "`/quota` – crediti API rimanenti\n"
        "`/campionati` – elenco squadre\n"
        "`/ai <pertanyaan>` – Comandante AI (Gemini)\n\n"
        "🛡 *Filtri Pro attivi:*\n"
        "• EV: +3% to +15%\n"
        "• Odds: 1.50 to 5.00\n"
        "• Kelly: 1/4 | Cap: 3% bankroll"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


def enrich_odds_with_probs(odds_data):
    from poisson_engine import expected_goals, prob_1x2, prob_over_under
    teams = _all_teams()

    def match_team(t):
        if t in teams: return t
        t_low = t.lower().replace(" fc", "").replace("cf ", "").strip()
        for tm in teams:
            tm_low = tm.lower()
            if t_low == tm_low or t_low in tm_low or tm_low in t_low:
                return tm
        return None

    enriched = []
    if hasattr(odds_data, "to_dict"):
        odds_data = odds_data.to_dict(orient="records")
    for odd in odds_data:
        evento = odd.get("evento", "")
        if " vs " not in evento: continue
        parts = evento.split(" vs ")
        home = parts[0].split(" – ")[-1].strip()
        away = parts[1].strip()
        hm, am = match_team(home), match_team(away)
        if not (hm and am and hm != am): continue
        try:
            lh, la = expected_goals(hm, am)
            p1, px, p2 = prob_1x2(lh, la)
            po, pu = prob_over_under(lh, la)
            esito = str(odd.get("esito", "")).lower()
            prob = 0.0
            if esito == "1": prob = p1
            elif esito == "x": prob = px
            elif esito == "2": prob = p2
            elif "over" in esito: prob = po
            elif "under" in esito: prob = pu
            if prob > 0:
                new_odd = odd.copy()
                new_odd["probabilita"] = prob
                new_odd["evento"] = f"{hm} vs {am}"
                enriched.append(new_odd)
        except Exception:
            pass
    return enriched

def format_value_bets(odds_data, bankroll=100.0):
    enriched = enrich_odds_with_probs(odds_data)
    value_signals = filter_value_bets(enriched, ev_threshold=0.03)
    if not value_signals:
        return "📊 *Value Bet Pro*\n\nNessun segnale che supera i filtri (EV 3%-15%, Odds 1.50-5.00)." + DISCLAIMER
    msg = "📊 *VALUE BET PRO — Filtri attivi*\n"
    msg += "🛡 EV: 3%-15% | Odds: 1.50-5.00 | Kelly 1/4 | Cap 3%\n"
    msg += "🎯 Bonus: confronto col mercato (devig power)\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for sig in value_signals[:5]:
        ev_pct = sig["ev"] * 100
        prob = sig.get("probabilita", 0)
        quota = sig.get("quota_decimale", 1.0)
        pro = get_pro_stake(bankroll, prob, quota)
        mkt_txt = ""
        if sig.get("market_edge") is not None:
            mkt_txt = f" | 🎯 mercato {sig['market_edge']*100:+.1f}pp"
        msg += (
            f"🏟 {sig['evento']}\n"
            f"🎯 {sig['esito']} @ {sig['quota_decimale']:.2f} ({sig['bookmaker']})\n"
            f"📈 EV: +{ev_pct:.2f}%{mkt_txt} | Stake: €{pro['stake']:.2f} ({pro['stake_pct_of_bankroll']:.1f}%)\n\n"
        )
    msg += f"💰 Bankroll: €{bankroll:.2f}"
    return msg + DISCLAIMER

def format_surebets(odds_data):
    sures = scan_surebets(odds_data)
    if not sures: return "🔍 *Surebet*\n\nNessun arbitraggio trovato." + DISCLAIMER
    msg = "🔍 *SUREBET*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for s in sures: msg += f"🏟 {s['evento']}\n💰 Profitto: {s['profit_pct']:.2f}%\n\n"
    return msg + DISCLAIMER




def _update_results():
    """Aggiorna risultati e rating dalle API. Ritorna (updated, stats, bet_settlements).

    Ordine corretto:
      1. Scarica risultati dall'API e salva in match_results
      2. Salda cassa / previsioni / puntate auto (ora i risultati esistono)
      3. Aggiorna rating
    """
    from odds_api import SPORTS_MAP, fetch_scores, match_scores_by_name
    from tracker import (save_result, get_results_stats, get_leagues_with_open_rows,
                          settle_cassa, settle_predictions, settle_bets)
    from rating_engine import compute_ratings
    # --- STEP 1: scarica risultati PRIMA di saldare ---
    # Refertazione ESCLUSIVAMENTE via the-odds-api (fetch_scores): la stessa
    # chiave delle quote restituisce anche i risultati FINITI delle partite
    # correnti. API-Football resta SOLO per lo storico ratings (football_hist,
    # stagioni 2022-2024 coperte dal piano free).
    # Refertazione MIRATA (risparmio crediti piano free): si interrogano SOLO
    # le leghe con scommesse attive (o chiuse da <48h) su partite già iniziate
    # — zero righe aperte = zero chiamate fetch_scores per quella lega.
    leagues = get_leagues_with_open_rows()
    updated = 0
    for lg in leagues:
        sport = SPORTS_MAP.get(lg)
        if not sport:
            continue
        for m in fetch_scores(sport, days_from=2):
            if not m.get("id"):
                continue
            parsed = match_scores_by_name(m)
            if parsed is None:
                continue
            sh, sa = parsed
            save_result(m["id"], lg, m.get("home_team", ""), m.get("away_team", ""),
                        sh, sa, m.get("last_update", ""))
            updated += 1
    if updated:
        logger.info("Risultati scaricati: %d partite aggiornate.", updated)
    # --- STEP 2: salda cassa, previsioni e puntate AUTO ---
    try:
        settled = settle_cassa()
        if settled:
            logger.info("Cassa: saldate %d scommesse coi risultati reali.", settled)
    except Exception as e:
        logger.warning("settle_cassa fallita: %s", e)
    try:
        settled, pushes = settle_predictions()
        if settled:
            logger.info("Previsioni: saldate %d (di cui %d push).", settled, pushes)
    except Exception as e:
        logger.warning("settle_predictions fallita: %s", e)
    bet_settlements = []
    try:
        settled, pushes, details = settle_bets(return_details=True)
        bet_settlements = details
        if settled:
            logger.info("Puntate auto: saldate %d (di cui %d push).", settled, pushes)
    except Exception as e:
        logger.warning("settle_bets fallita: %s", e)
    # --- STEP 2.5: SANITY CHECK sui verdetti già emessi ---
    # Tripwire del bug 02/09 (punteggi invertiti): se match_results è stato
    # corretto dopo la chiusura (es. watchdog con match_scores_by_name), una
    # bet può restare 'won' con l'esito SPECCHIATO. Qui si ricomputa l'esito
    # dai gol correnti e si ri-saldano automaticamente le righe in
    # contraddizione, con alert su Telegram (blocca la chiusura sbagliata).
    sanity_alerts = []
    try:
        from tracker import settlement_sanity_check, heal_settled_contradictions
        contrad = settlement_sanity_check()
        if contrad:
            healed = heal_settled_contradictions(contrad)
            lines = []
            for c in contrad[:8]:
                lines.append(
                    f"   ⚠️ {c['table']} #{c['id']} {c['esito']}: "
                    f"era '{c['stored']}' → atteso '{c['expected']}' "
                    f"({c['home']} {c['sh']}-{c['sa']} {c['away']})")
            if len(contrad) > 8:
                lines.append(f"   … e altre {len(contrad) - 8} righe.")
            alert = ("🔔 *SANITY CHECK SETTLEMENT*\n"
                     f"{healed} verdetto/i in contraddizione coi gol "
                     "registrati: RI-SALDATI automaticamente\n\n" +
                     "\n".join(lines))
            sanity_alerts.append(alert)
            logger.warning("SANITY CHECK: %d righe ri-sal date "
                           "automaticamente", healed)
    except Exception as e:
        logger.warning("settlement sanity check fallita: %s", e)
    # --- STEP 3: aggiorna rating ---
    compute_ratings()
    return updated, get_results_stats(), bet_settlements, sanity_alerts


def _admin_chat_ids() -> list:
    """Chat ID che ricevono SEMPRE i report (proprietario), anche senza /subscribe.

    Variabile ADMIN_CHAT_ID, separata da virgole se piu' di uno.
    """
    ids = []
    for part in os.getenv("ADMIN_CHAT_ID", "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


def _missing_env_keys() -> list:
    """Chiavi mancanti: i job corrispondenti saltano in silenzio."""
    missing = []
    for key, what in (("API_FOOTBALL_KEY", "storico ratings 2022-2024 (football_hist)"),
                      ("ODDS_API_KEY", "quote live + CLV + settlement risultati"),
                      ("QUOTAVERACE_BOT_TOKEN", "bot Telegram")):
        if not os.getenv(key):
            missing.append(f"{key} ({what})")
    return missing


def format_daily_report(since: str, label: str) -> str:
    """Riepilogo di un periodo (ISO `since`): previsioni chiuse per mercato,
    cassa saldata, CLV medio e job saltati per chiavi mancanti."""
    from tracker import predictions_summary, cassa_period, _get_conn

    by_mkt = predictions_summary(settled_since=since)
    ct = cassa_period(since)

    total_n = sum(b["n"] for b in by_mkt.values())
    total_pnl = sum((b["roi"] / 100.0) * b["n"] for b in by_mkt.values())
    total_ev = sum((b["avg_ev"] / 100.0) * b["n"] for b in by_mkt.values())

    lines = [f"📅 *RIEPILOGO — {label}*", "━━━━━━━━━━━━━━━━━━━━━━\n"]
    if total_n:
        lines.append(f"🎯 *Previsioni chiuse:* {total_n}")
        for mkt in ("1X2", "OU", "BTTS", "AH"):
            b = by_mkt.get(mkt)
            if not b or not b["n"]:
                continue
            outcome = f"✅ {b['won']}/❌ {b['lost']}"
            if b["push"]:
                outcome += f"/⚪ {b['push']}"
            lines.append(
                f"   {mkt}: {b['n']} ({outcome}) ROI {b['roi']:+.1f}% "
                f"vs EV {b['avg_ev']:+.1f}%"
            )
        closed = max(total_n - sum(b["push"] for b in by_mkt.values()), 1)
        hit = (sum(b["won"] for b in by_mkt.values()) / closed) * 100
        roi_tot = (total_pnl / total_n * 100) if total_n else 0.0
        lines.append(
            f"   *Totale: P/L {total_pnl:+.1f}u | ROI {roi_tot:+.1f}% "
            f"| hit {hit:.0f}%* (EV medio atteso {total_ev/total_n*100:+.1f}%)"
        )
    else:
        lines.append("🎯 *Nessuna previsione chiusa nel periodo.*")
        # Diagnostica: ci sono bet aperte ma senza risultati?
        try:
            from tracker import _get_conn
            conn = _get_conn(); c = conn.cursor()
            open_bets = c.execute(
                "SELECT COUNT(*) FROM bets WHERE esito_finale IS NULL"
            ).fetchone()[0]
            open_preds = c.execute(
                "SELECT COUNT(*) FROM predictions WHERE esito_finale IS NULL"
            ).fetchone()[0]
            conn.close()
            if open_bets or open_preds:
                if not os.getenv("ODDS_API_KEY"):
                    lines.append(
                        "   ⚠️ *Possibile causa:* `ODDS_API_KEY` non configurata "
                        "→ i risultati non vengono scaricati e le bet "
                        "restano aperte. Impostala su Railway.")
                else:
                    lines.append(
                        f"   ℹ️ {open_bets} bet + {open_preds} previsioni "
                        "ancora aperte (risultati in attesa)")
        except Exception:
            pass

    if ct["chiusi"]:
        lines.append(
            f"💰 *Cassa saldata:* {ct['chiusi']} (✅ {ct['vinti']}/❌ {ct['persi']}) "
            f"| speso €{ct['speso']:.2f} | P/L €{ct['profit']:+.2f} | "
            f"ROI {ct['roi']:+.1f}%"
        )
    else:
        lines.append("💰 *Cassa:* nessuna puntata saldata nel periodo.")

    try:
        from market_calib import clv_vig_free, clv_raw
        conn = _get_conn(); c = conn.cursor()
        rows = c.execute("SELECT signal_quota, closing_quota, pinnacle_quota "
                         "FROM clv_history WHERE updated_at >= ?", (since,)).fetchall()
        conn.close()
        clvs_raw = []
        clvs_vf = []
        for s, clos, pin in rows:
            if not (clos and clos > 0 and s and s > 0):
                continue
            clvs_raw.append((s / clos) - 1.0)
            # Vig-free: usa Pinnacle come proxy fair (vig ~1-2%)
            fair = pin if (pin and pin > 0) else clos
            vf = clv_vig_free(s, fair)
            if vf is not None:
                clvs_vf.append(vf)
        if clvs_raw:
            lines.append(f"📈 *CLV medio:* {sum(clvs_raw)/len(clvs_raw)*100:+.2f}% (n {len(clvs_raw)})")
        if clvs_vf:
            lines.append(f"🎯 *CLV vig-free:* {sum(clvs_vf)/len(clvs_vf)*100:+.2f}% (n {len(clvs_vf)})")
        # CLV vs Pinnacle: la closing line piu' sharp del mercato.
        pin_clvs = [(s / pin) - 1.0 for s, _, pin in rows
                    if pin and pin > 0]
        if pin_clvs:
            lines.append(f"🏆 *CLV vs Pinnacle:* {sum(pin_clvs)/len(pin_clvs)*100:+.2f}% "
                         f"(n {len(pin_clvs)})")
    except Exception:
        pass

    # RLM / Steam / Crollo quota: movimenti di linea sui segnali attivi,
    # classificati dai VERI rilevatori (line_movement + rlm_alert) tramite
    # l'aggregatore condiviso con la webapp (/api/market_signals).
    try:
        from market_signals import collect_market_signals, format_market_signals_report
        lines.extend(format_market_signals_report(collect_market_signals()))
    except Exception as e:
        logger.warning("segnali mercato nel report falliti: %s", e)

    try:
        from tracker import bets_period
        bp = bets_period(since)
        if bp["piazzate"]:
            line = (f"🎯 *Puntate automatiche:* {bp['piazzate']} "
                    f"(€{bp['stake_totale']:.2f})")
            if bp["chiusi"]:
                outcome = f"✅ {bp['vinti']}/❌ {bp['persi']}"
                if bp["push"]:
                    outcome += f"/⚪ {bp['push']}"
                line += (f" | chiuse {bp['chiusi']} ({outcome}) "
                         f"P/L €{bp['profit']:+.2f}")
            lines.append(line)
    except Exception:
        pass

    # Audit qualita' dataset ML: un dataset sporco viene IMPARATO dal
    # modello come verita'. Controlla solo le previsioni/puntate chiuse
    # nel periodo e segnala i problemi (per tipo + primi esempi).
    try:
        from ml_dataset import build_training_rows
        from ml_audit import audit_training_rows, summarize
        period_rows = [r for r in build_training_rows()
                       if (r.get("settled_at") or "") >= since]
        problems = audit_training_rows(period_rows)
        if problems:
            by_type = summarize(problems)
            dettaglio = ", ".join(f"{t}: {n}" for t, n in by_type.items())
            lines.append(
                f"🔎 *Audit dataset ML:* ⚠️ {len(problems)} problemi "
                f"({dettaglio})"
            )
            for p in problems[:5]:
                # caratteri sicuri per il Markdown di Telegram
                msg = (p.get("msg") or "").replace("[", "(").replace("]", ")")
                lines.append(f"   • {p['tipo']} [{p.get('match_id','?')}] "
                             f"{p.get('mercato','?')} {p.get('esito','?')}: {msg}")
            if len(problems) > 5:
                lines.append(f"   … e altri {len(problems) - 5} problemi "
                             "(vedi `venv/bin/python ml_audit.py`)")
    except Exception as e:
        logger.warning("audit ML nel report fallito: %s", e)

    # Streak + bankroll/drawdown: il polso del periodo (stessi numeri del
    # backtest). Streak dalle previsioni chiuse, bankroll dalla cassa reale.
    try:
        from performance_report import _calc_streaks, _bankroll_stats
        conn = _get_conn(); c = conn.cursor()
        end = datetime.now().strftime("%Y-%m-%d")
        streaks = _calc_streaks(conn, since, end)
        br = _bankroll_stats(conn)
        conn.close()
        parts = []
        if streaks["current_streak"]:
            if streaks["current_type"] == "won":
                parts.append(f"🔥 {streaks['current_streak']} vittorie di fila")
            else:
                parts.append(f"📉 {streaks['current_streak']} perse di fila")
            parts.append(f"max {streaks['max_win_streak']}V/{streaks['max_loss_streak']}P")
        if br.get("current") is not None:
            parts.append(f"bankroll €{br['current']:.2f} "
                         f"(peak €{br['peak']:.2f}, dd {br['drawdown_pct']:.1f}%)")
        if parts:
            lines.append("📊 *Stato:* " + " | ".join(parts))
    except Exception as e:
        logger.warning("streak/bankroll nel report falliti: %s", e)

    # Concept drift del modello: Brier/LogLoss rolling vs baseline sulle
    # previsioni chiuse. Se la calibrazione sta degradando, il report
    # segnala il retraining dell'ensemble (monitoraggio periodico).
    try:
        from drift_monitor import check_drift, format_drift_report
        lines.extend(format_drift_report(check_drift()))
    except Exception as e:
        logger.warning("drift monitor nel report fallito: %s", e)

    missing = _missing_env_keys()
    if missing:
        lines.append("\n⚠️ *Job saltati per chiavi mancanti:*\n   " + "\n   ".join(missing))
    else:
        lines.append("\n✅ Tutti i job attivi (chiavi presenti).")

    # Partite trovate ma non analizzate per squadre fuori roster: gap di
    # copertura reso visibile (mai piu' silenzioso). Solo quelle delle
    # ultime 24h, cosi' il riepilogo del mattino segnala il giorno prima.
    try:
        from datetime import datetime as _dt
        from fixture_engine import get_skipped_matches
        def _recent(s):
            try:
                return (_dt.utcnow() - _dt.fromisoformat(s["ts"])).total_seconds() < 86400
            except Exception:
                return False
        skipped = [s for s in get_skipped_matches() if _recent(s)]
    except Exception:
        skipped = []
    if skipped:
        rows = "\n   ".join(
            f"• {s.get('home','?')} vs {s.get('away','?')} "
            f"[{s.get('league','?')}] — squadre non coperte: "
            f"{', '.join(s.get('non_coperte', []) or ['?'])}"
            for s in skipped[:5]
        )
        extra = f" (+{len(skipped)-5} altre)" if len(skipped) > 5 else ""
        lines.append(f"\n⚠️ *Partite non coperte (fuori roster):*\n   {rows}{extra}")
    return "\n".join(lines)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mostra il Chat ID: utile per impostare ADMIN_CHAT_ID nel .env."""
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"🆔 Il tuo Chat ID: `{chat_id}`\n\n"
        "Impostalo nel file `.env` (o su Railway) come:\n"
        f"`ADMIN_CHAT_ID={chat_id}`\n\n"
        "Così il bot ti invia SEMPRE i report mattutino e serale "
        "su Telegram, anche senza /subscribe.",
        parse_mode="Markdown")


async def cmd_riepilogo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Riepilogo del periodo: oggi (default), 'ieri' o una data YYYY-MM-DD."""
    from datetime import timedelta
    arg = " ".join(context.args or []).strip().lower()
    if arg in ("ieri", "yesterday"):
        since = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        label = "IERI"
    elif arg:
        since = arg
        label = since
    else:
        since = datetime.now().strftime("%Y-%m-%d")
        label = "OGGI"
    text = format_daily_report(since, label)
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_quota(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from odds_api import get_quota
    q = get_quota()
    if q is None:
        text = ("🔋 *QUOTA the-odds-api*\n\n"
                "Nessuna scansione in cache.\n"
                "Fai `/analisi` per aggiornare i crediti.")
    else:
        remaining, n = q
        pct = remaining / 500 * 100
        text = (
            "🔋 *QUOTA the-odds-api*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⚡ Crediti residui: **{remaining} / 500** ({pct:.0f}%)\n"
            f"📊 Campionati in cache: {n}/8\n\n"
            "📅 Reset: 1° del mese\n"
            "💡 Aggiornato con l'ultimo `/analisi` (costo zero)."
        )
    await update.message.reply_text(text + DISCLAIMER, parse_mode="Markdown")

async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = run_backtest()
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_backtest_mc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backtest_mc [sims] — Backtest walk-forward ML+Kelly con Monte Carlo.

    ROI atteso, Max Drawdown (base/mediana/p95) e probabilita' di riduzione,
    simulando lo stesso stack di produzione (ensemble + adaptive staking).
    """
    sims = 1000
    if context.args:
        try:
            sims = max(100, min(10000, int(context.args[0])))
        except ValueError:
            pass
    await update.message.reply_text(
        f"🔄 Backtest walk-forward + Monte Carlo ({sims} simulazioni)...\n"
        "Può richiedere qualche decina di secondi.")
    loop = asyncio.get_running_loop()
    try:
        from backtest_mc import run_backtest_mc, format_backtest_report
        res = await loop.run_in_executor(
            _scan_executor, run_backtest_mc, 30, sims, 100.0)
        text = format_backtest_report(res)
    except Exception as e:
        logger.error("cmd_backtest_mc: %s", e)
        text = f"❌ Errore backtest MC: {e}"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backup — snapshot manuale del DB + dataset ML (solo admin)."""
    admin_ids = _admin_chat_ids()
    if admin_ids and update.effective_chat.id not in admin_ids:
        await update.message.reply_text(
            "⛔ Comando riservato agli admin.")
        return
    await update.message.reply_text("💾 Backup in corso...")
    loop = asyncio.get_running_loop()
    try:
        from backup_manager import run_backup, format_backup_report
        s = await loop.run_in_executor(_scan_executor, run_backup)
        text = format_backup_report(s)
    except Exception as e:
        logger.error("cmd_backup: %s", e)
        text = f"❌ Errore backup: {e}"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔄 Sincronizzazione risultati storici... (può richiedere qualche minuto per via del rate limit)", parse_mode="Markdown")
    text = run_sync()
    await update.message.reply_text(text, parse_mode="Markdown")

def format_bet_verdicts(settlements: list) -> str:
    """Formatta i verdetti delle puntate appena saldate (fine partita)."""
    if not settlements:
        return ""
    mode_txt = "DRY-RUN" if settlements[0].get("mode") == "dry-run" else "LIVE"
    lines = []
    for s in settlements:
        if s["outcome"] == "won":
            icon = "✅"
            verdict = "VINTA"
        elif s["outcome"] == "push":
            icon = "⚪"
            verdict = "PUSH"
        else:
            icon = "❌"
            verdict = "PERSA"
        profit = s["profit"] or 0.0
        pl = f"+€{profit:.2f}" if profit >= 0 else f"-€{abs(profit):.2f}"
        lines.append(
            f"{icon} *{verdict}* — {s.get('home', '?')} vs {s.get('away', '?')}"
            f" ({s.get('league', '')})\n"
            f"   🎯 {s['mercato']} {s['esito']} @ {s['price']:.2f} | "
            f"stake €{s['stake']:.2f} | *P/L {pl}*"
        )
    return ("🔔 *ESITO PUNTATE AUTOMATICHE* "
            f"({mode_txt})\n\n" + "\n\n".join(lines))


async def _send_bet_settlements(context, settlements: list):
    text = format_bet_verdicts(settlements)
    if text:
        await _send_report_to_recipients(context, text)


async def cmd_risultati(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        from tracker import get_results_stats
        updated, stats, settlements, sanity = _update_results()
        if settlements:
            await _send_bet_settlements(context, settlements)
        for alert in sanity:
            await _send_report_to_recipients(context, alert)
        if stats["total"] == 0:
            text = "📊 *RISULTATI TRACKING*\n\nNessuna scommessa chiusa ancora.\nI risultati si aggiornano da soli quando le partite finiscono."
        else:
            text = (
                "📊 *RISULTATI TRACKING*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🎯 Scommesse chiuse: {stats['total']}\n"
                f"✅ Vinte: {stats['won']} | ❌ Perse: {stats['lost']}\n"
                f"📈 Hit rate: {stats['hit_rate']:.1f}%\n"
                f"💰 P/L (unità da 1): {stats['net']:+.2f}\n"
                f"📊 ROI: {stats['roi']:+.2f}%\n"
                f"⚖ EV medio segnali: {stats['avg_ev']*100:+.2f}%\n"
            )
            clv = stats.get("avg_clv", 0.0)
            clv_tracked = stats.get("clv_tracked", 0)
            if clv_tracked:
                clv_txt = f"+{clv*100:.2f}%" if clv >= 0 else f"{clv*100:.2f}%"
                clv_line = (
                    f"\n🎯 *Closing Line Value* (su {clv_tracked} segnali): {clv_txt}\n"
                    f"   > 0 significa che battiamo la chiusura del mercato 👉 edge reale"
                )
                text += clv_line
        if updated:
            text += f"\n\n🔄 Aggiornate {updated} partite dai risultati."
        try:
            from tracker import cassa_totals
            ct = cassa_totals()
            if ct["chiusi"]:
                text += (
                    "\n\n💰 *CASSA REALE* (le tue puntate)\n"
                    f"   Chiuse: {ct['chiusi']} (✅ {ct['vinti']} / ❌ {ct['persi']}) "
                    f"| in gioco: {ct['in_gioco']}\n"
                    f"   Speso: €{ct['totale_speso']:.2f} | "
                    f"P/L: €{ct['profit_realizzato']:+.2f} | ROI: {ct['roi']:+.2f}%"
                )
        except Exception:
            pass
        # Telemetria di calibrazione: torto/ragione per MERCATO. E' qui che
        # si vede se il modello batte davvero la closing line, per mercato.
        try:
            from tracker import predictions_summary
            by_mkt = predictions_summary()
            lines = []
            for mkt in ("1X2", "OU", "BTTS", "AH"):
                b = by_mkt.get(mkt)
                if not b or not b["n"]:
                    continue
                outcome = f"✅ {b['won']}/❌ {b['lost']}"
                if b["push"]:
                    outcome += f"/⚪ {b['push']}"
                lines.append(
                    f"   {mkt}: {b['n']} prev ({outcome}) "
                    f"ROI {b['roi']:+.1f}% vs EV {b['avg_ev']:+.1f}%"
                )
            if lines:
                text += "\n\n📊 *CALIBRAZIONE PER MERCATO*\n" + "\n".join(lines)
        except Exception:
            pass
        await update.message.reply_text(text + DISCLAIMER, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Errore risultati: {e}")
        await update.message.reply_text("❌ Errore nel recupero risultati.", parse_mode="Markdown")

# Ritardo dei segnali per il piano free (ore): il premium li riceve subito.
FREE_DELAY_HOURS = 3

async def notify_job(context: ContextTypes.DEFAULT_TYPE, delayed: bool = False):
    """Notifica value bet: immediata per premium, ritardata per free.

    delayed=True e' usato dal job free (3 ore dopo): ripete il controllo ma
    salta i segnali gia' spediti ai premium (mark_notified e' globale).
    """
    if not os.getenv("ODDS_API_KEY") or not LIVE_ODDS_AVAILABLE: return
    try:
        odds = get_live_odds()
        if not odds: return
        value_signals = filter_value_bets(enrich_odds_with_probs(odds), ev_threshold=0.03)
        if not value_signals: return
        if delayed:
            # Piano free: solo segnali non ancora notificati (i premium li
            # hanno gia' ricevuti al giro immediato, con mark_notified).
            subscribers = [cid for cid in get_subscribers(tier="free")
                           if not is_premium(cid)]
        else:
            subscribers = [cid for cid in get_subscribers()
                           if is_premium(cid)]
        if not subscribers: return
        today = __import__('datetime').datetime.now().strftime("%Y-%m-%d")
        if not delayed:
            # Sticker animato una volta per chat, prima dei messaggi premium.
            for chat_id in subscribers:
                await send_premium_sticker(context.bot, chat_id)
        for sig in value_signals[:3]:
            if is_notified(sig.get("match_id","unknown"), today): continue
            ev_pct = sig["ev"] * 100
            prob = sig.get("probabilita", 0)
            quota = sig.get("quota_decimale", 1.0)
            pro = get_pro_stake(100.0, prob, quota)
            tag = "💎 PREMIUM" if not delayed else "🔔 VALUE BET"
            msg = (
                f"🔔 *NOTIFICA {tag}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🏟 {sig['evento']}\n"
                f"🎯 {sig['esito']} @ {sig['quota_decimale']:.2f} ({sig['bookmaker']})\n"
                f"📈 EV: +{ev_pct:.2f}% | Stake ref: €{pro['stake']:.2f} ({pro['stake_pct_of_bankroll']:.1f}%)\n\n"
                f"🛡 Filtri: EV 3%-15% | Odds 1.50-5.00 | Kelly 1/4 | Cap 3%\n\n"
                f"💡 `/segnale` per analisi dettagliata"
            )
            for chat_id in subscribers:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    # Traccia il segnale ricevuto: alimenta /storico_personale,
                    # il backtest e il tracking risultati/CLV.
                    log_signal(chat_id, sig["evento"], sig["esito"],
                               sig["quota_decimale"], prob, sig["ev"])
                except Exception:
                    pass
            mark_notified(sig.get("match_id","unknown"), today)
    except Exception as e: logger.error(f"Errore notify: {e}")

async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Job mattutino: calendario + schedina Pro")
    fetch_and_analyze_today()
    picks = get_value_picks_for_schedina()
    if not picks: return
    text = format_schedina(picks, 100.0)
    await _send_report_to_recipients(context, text)

async def afternoon_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Job pomeridiano: ricontrollo Pro")
    fetch_and_analyze_today()
    try:
        _, _, settlements, sanity = _update_results()
        if settlements:
            await _send_bet_settlements(context, settlements)
        for alert in sanity:
            await _send_report_to_recipients(context, alert)
    except Exception as e:
        logger.error(f"Errore update risultati job: {e}")
    await notify_job(context)          # immediato per i premium
    await notify_job(context, delayed=True)  # ritardo 3h per il piano free

async def free_delayed_job(context: ContextTypes.DEFAULT_TYPE):
    """Job con ritardo di 3 ore per il piano free."""
    await notify_job(context, delayed=True)

async def evening_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Job serale: ricontrollo Pro")
    fetch_and_analyze_today()
    try:
        _, _, settlements, sanity = _update_results()
        if settlements:
            await _send_bet_settlements(context, settlements)
        for alert in sanity:
            await _send_report_to_recipients(context, alert)
    except Exception as e:
        logger.error(f"Errore update risultati job: {e}")
    await notify_job(context)

async def results_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Job risultati serali (23:30 ITA)")
    try:
        updated, stats, settlements, sanity = _update_results()
        logger.info(f"Risultati aggiornati: {updated} partite")
        if settlements:
            await _send_bet_settlements(context, settlements)
        for alert in sanity:
            await _send_report_to_recipients(context, alert)
    except Exception as e:
        logger.error(f"Errore results_job: {e}")


async def settlement_watchdog_job(context: ContextTypes.DEFAULT_TYPE):
    """Self-healing pendenze: ogni 4h (dopo la finestra dei match job)
    scarica i risultati e salda cassa/previsioni/puntate rimaste aperte.

    Copre i buchi della copertura job (es. redeploy alle 23:13 che salta il
    results_job delle 21:30, come il 01/09): al prossimo tick le bet delle
    16:40 vengono saldate automaticamente, senza intervento manuale.
    Frequenza 4h (era 2h): il referto non serve istantaneo per il ROI e
    ogni chiamata in meno aiuta il budget crediti del piano free.
    """
    try:
        updated, stats, settlements, sanity = _update_results()
    except Exception as e:
        logger.error(f"Errore settlement_watchdog_job: {e}")
        return
    open_bets = open_preds = -1
    try:
        from tracker import _get_conn
        conn = _get_conn(); c = conn.cursor()
        open_bets = c.execute(
            "SELECT COUNT(*) FROM bets WHERE esito_finale IS NULL").fetchone()[0]
        open_preds = c.execute(
            "SELECT COUNT(*) FROM predictions WHERE esito_finale IS NULL").fetchone()[0]
        conn.close()
    except Exception:
        pass
    if updated or settlements or sanity:
        logger.info("settlement_watchdog: %d risultati, %d bet saldate, "
                    "pendenze: %d bet / %d previsioni, %d sanity check",
                    updated, len(settlements), open_bets, open_preds,
                    len(sanity))
    if settlements:
        await _send_bet_settlements(context, settlements)
    for alert in sanity:
        await _send_report_to_recipients(context, alert)

async def _send_report_to_recipients(context, text: str):
    """Invia il messaggio agli iscritti + sempre ai chat ADMIN_CHAT_ID."""
    chat_ids = set(get_subscribers())
    chat_ids.update(_admin_chat_ids())
    for chat_id in sorted(chat_ids):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text,
                                           parse_mode="Markdown")
        except Exception:
            pass


async def end_of_day_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Riepilogo quando FINISCE L'ULTIMA PARTITA della giornata.

    Controlla ogni 15 minuti (dalle 21:00): quando tutte le partite del
    giorno iniziate hanno il risultato, invia il riepilogo una volta sola.
    Fallback notturno (23:50 UTC): se qualche partita non si chiude
    (rinvio, dati lenti), invia comunque per non perdere la giornata.
    """
    from tracker import day_completed, is_notified, mark_notified
    from datetime import timezone as _tz, timedelta as _td
    # Ora italiana (UTC+2 estive)
    now_it = datetime.now(_tz.utc) + _td(hours=2)
    today = now_it.strftime("%Y-%m-%d")
    if is_notified("EOD", today):
        return
    forced = now_it.hour >= 23 and now_it.minute >= 50
    if not forced and not day_completed(today):
        return
    text = format_daily_report(today, "OGGI — FINE GIORNATA")
    await _send_report_to_recipients(context, text)
    mark_notified("EOD", today)
    logger.info("Riepilogo di fine giornata inviato (ultima partita chiusa).")


async def report_morning_job(context: ContextTypes.DEFAULT_TYPE):
    """Riepilogo del mattino (08:05 ITA): cosa è successo ieri."""
    from datetime import timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    text = format_daily_report(yesterday, "IERI")
    await _send_report_to_recipients(context, text)
    logger.info("Riepilogo di ieri inviato agli iscritti.")


async def auto_bet_job(context: ContextTypes.DEFAULT_TYPE):
    """08:50: piazza le puntate del giorno (SIM-only dal 04/09).

    Nessun conto Exchange: puntate simulate con la quota del segnale
    (paper trading), registrate in `bets` e saldate a fine partita come
    sempre — alimentano ledger, CLV e dataset ML.
    """
    loop = asyncio.get_running_loop()
    try:
        placed = await loop.run_in_executor(_scan_executor, run_today_bets,
                                            None, True)
    except Exception as e:
        logger.error("auto_bet_job: %s", e)
        return
    if not placed:
        return
    mode = placed[0]["mode"]
    mode_label = {"live": "LIVE", "sim": "SIMULAZIONE",
                  "dry-run": "DRY-RUN"}.get(mode, mode)
    total = sum(p["stake"] for p in placed)
    rows = "\n".join(
        f"• {p['home']} vs {p['away']} — {p['esito_key']} @ {p['price']:.2f} "
        f"(€{p['stake']:.2f})" for p in placed)
    text = (f"🎯 *PUNTATE AUTOMATICHE ({mode_label})*\n"
            f"{len(placed)} puntate, €{total:.2f} di stake\n\n{rows}\n\n"
            f"📌 {'ORDINI REALI' if mode == 'live' else 'Simulazione: nessun ordine reale inviato.'}")
    await _send_report_to_recipients(context, text)
    logger.info("auto_bet_job: %d puntate (%s), €%.2f", len(placed), mode, total)


async def history_sync_job(context: ContextTypes.DEFAULT_TYPE):
    """Sincronizzazione risultati storici (API-Football) + ricalcolo rating.

    Gira quotidianamente per mantenere aggiornati i dati che alimentano
    rating dinamici e backtest. Se API_FOOTBALL_KEY non e' configurata non
    fa nulla. Usa il default di 1 stagione per restare entro il rate-limit
    del free plan (100 richieste/giorno)."""
    if not os.getenv("API_FOOTBALL_KEY"):
        logger.info("history_sync_job: API_FOOTBALL_KEY assente, salto")
        return
    try:
        res = run_sync(seasons=1)
        logger.info(f"history_sync_job: {res}")
    except Exception as e:
        logger.error(f"Errore history_sync_job: {e}")

def _db_path():
    """Percorso del DB SQLite (vive in DATA_DIR)."""
    return DATA_DIR / "quotaverace.db"


async def backup_data_job(context: ContextTypes.DEFAULT_TYPE):
    """Backup giornaliero dei dati persistenti (delega a backup_manager).

    Snapshot in data/backups/<timestamp>/: DB (consistente via backup API),
    dataset ML fresco (csv+json), cache e log. Tiene gli ultimi BACKUP_KEEP
    snapshot (env, default 7).
    """
    from backup_manager import run_backup
    try:
        await asyncio.get_running_loop().run_in_executor(
            _scan_executor, run_backup)
    except Exception as e:
        logger.error(f"backup_data_job: {e}")


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
    application.add_handler(CommandHandler("multipla", cmd_multipla))
    application.add_handler(CommandHandler("subscribe", cmd_subscribe))
    application.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    application.add_handler(CommandHandler("premium", cmd_premium))
    application.add_handler(CommandHandler("mytier", cmd_mytier))
    application.add_handler(CommandHandler("checknow", cmd_checknow))
    application.add_handler(CommandHandler("risultati", cmd_risultati))
    application.add_handler(CommandHandler("backtest", cmd_backtest))
    application.add_handler(CommandHandler("backtest_mc", cmd_backtest_mc))
    application.add_handler(CommandHandler("backup", cmd_backup))
    application.add_handler(CommandHandler("sync", cmd_sync))
    application.add_handler(CommandHandler("quota", cmd_quota))
    application.add_handler(CommandHandler("riepilogo", cmd_riepilogo))
    application.add_handler(CommandHandler("myid", cmd_myid))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("start", cmd_help))
    if _AI_OK:
        application.add_handler(CommandHandler("ai", cmd_ai))
        logger.info("AI Commander aktif: /ai <pertanyaan>")
    else:
        logger.warning("AI Commander non-aktif: %s", _AI_ERR)
    job_queue = application.job_queue
    if job_queue:
        # I job usano UTC (timezone del container Railway).
        # Per avere gli orari italiani (UTC+2 estive / UTC+1 invernali),
        # soutraiamo 2h (estive) o 1h (invernali). Usa -2 per semplicita'
        # (cambia a -1 a fine ottobre se necessario).
        IT_OFFSET = 2  # UTC+2 (ora legale estiva italiana)
        job_queue.run_daily(morning_job, time=time(hour=6 - IT_OFFSET, minute=0))
        job_queue.run_daily(afternoon_job, time=time(hour=14 - IT_OFFSET, minute=0))
        job_queue.run_daily(evening_job, time=time(hour=20 - IT_OFFSET, minute=0))
        job_queue.run_daily(results_job, time=time(hour=21, minute=30 - IT_OFFSET))
        # Self-healing pendenze: ogni 4h scarica risultati e salda bet/
        # previsioni/cassa rimaste aperte (copre redeploy che saltano i job
        # serali, cache stantie, API lente). Silenzioso se non c'e' nulla.
        # Frequenza 4h (era 2h): risparmio crediti, il referto non serve
        # istantaneo (i risultati serali li coprono i job 21:30/EOD).
        job_queue.run_repeating(settlement_watchdog_job, interval=14400,
                                first=1200)  # primo giro dopo 20 min
        # Riepilogo a fine ultima partita: check ogni 15' dalle 21:00 ITA
        # (fallback notturno 23:50 ITA se la giornata non si chiude da sola).
        job_queue.run_repeating(end_of_day_report_job, interval=900,
                                first=time(hour=21 - IT_OFFSET, minute=0))
        job_queue.run_daily(report_morning_job, time=time(hour=6, minute=5 - IT_OFFSET))
        job_queue.run_daily(history_sync_job, time=time(hour=8, minute=30 - IT_OFFSET))
        job_queue.run_daily(auto_bet_job, time=time(hour=8, minute=50 - IT_OFFSET))
        job_queue.run_daily(backup_data_job, time=time(hour=3, minute=30))
        job_queue.run_once(backup_data_job, when=10)  # snapshot di base all'avvio
        # Piano free: riceve gli stessi segnali con 3 ore di ritardo.
        job_queue.run_daily(free_delayed_job, time=time(hour=17 - IT_OFFSET, minute=0))
        # Alert RLM real-time: ogni 5 minuti dalle 14:00 alle 23:50 ITA
        try:
            from rlm_alert import rlm_alert_job
            job_queue.run_repeating(rlm_alert_job, interval=300,
                                    first=time(hour=14 - IT_OFFSET, minute=0))
        except ImportError:
            logger.warning("rlm_alert non disponibile, alert RLM disabilitato")
        logger.info("Job Pro schedulati (ora italiana): 03:30 backup / 06:05 riepilogo ieri / "
                    "08:30 sync / 08:50 auto-bet (SIM) / 14:00 pomeriggio / "
                    "14:00-23:50 RLM alert (5') / 17:00 free / 20:00 sera / "
                    "21:30 risultati / 21:00-23:50 EOD (ogni 15') / "
                    "watchdog settlement (ogni 4h)")
    else: logger.warning("JobQueue non disponibile")
    logger.info("QuotaVerace Pro avviato.")
    application.run_polling()

if __name__ == "__main__":
    main()
