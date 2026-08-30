import os
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from odds_api import fetch_odds, SPORTS_MAP
from leagues_data import ALL_LEAGUES
from poisson_engine import expected_goals, prob_1x2, prob_over_under
from value_filter import (compute_ev, kelly_fraction, kelly_euro, is_sane,
                           combined_quota, combined_probability, multipla_stake)
from tracker import save_match, get_today_matches, save_analysis, get_analysis_for_match, clear_old_matches

logger = logging.getLogger(__name__)

DERIV_BIAS = 0.01  # bonus EV ai mercati derivati (Over/Under): soft book meno efficienti

TEAM_MAP = {
    "inter milan": "Inter", "ac milan": "Milan", "man united": "Manchester United",
    "man utd": "Manchester United", "man city": "Manchester City",
    "tottenham hotspur": "Tottenham", "tottenham": "Tottenham",
    "athletic club": "Athletic Bilbao", "atletico madrid": "Atletico Madrid",
    "atletico": "Atletico Madrid", "paris saint-germain": "Paris Saint-Germain",
    "psg": "Paris Saint-Germain", "borussia dortmund": "Borussia Dortmund",
    "borussia mgladbach": "Borussia Mgladbach", "rb leipzig": "RB Leipzig",
    "bayern munich": "Bayern Munich", "bayer leverkusen": "Bayer Leverkusen",
    "eintracht frankfurt": "Eintracht Frankfurt", "west ham united": "West Ham",
    "crystal palace": "Crystal Palace", "brighton and hove albion": "Brighton",
    "brighton": "Brighton", "aston villa": "Aston Villa",
    "newcastle united": "Newcastle", "leicester city": "Leicester",
    "nottingham forest": "Nottm Forest", "nottm forest": "Nottm Forest",
    "ipswich town": "Ipswich", "southampton": "Southampton",
    "real madrid": "Real Madrid", "barcelona": "Barcelona",
    "sevilla": "Sevilla", "valencia": "Valencia", "getafe": "Getafe",
    "osasuna": "Osasuna", "rayo vallecano": "Rayo Vallecano",
    "mallorca": "Mallorca", "las palmas": "Las Palmas", "alaves": "Alaves",
    "girona": "Girona", "leganes": "Leganes", "espanyol": "Espanyol",
    "valladolid": "Valladolid", "celta vigo": "Celta Vigo", "villarreal": "Villarreal",
    "real sociedad": "Real Sociedad", "lille": "Lille", "marseille": "Marseille",
    "monaco": "Monaco", "lyon": "Lyon", "lens": "Lens", "rennes": "Rennes",
    "nice": "Nice", "strasbourg": "Strasbourg", "nantes": "Nantes",
    "reims": "Reims", "montpellier": "Montpellier", "brest": "Brest",
    "toulouse": "Toulouse", "le havre": "Le Havre", "auxerre": "Auxerre",
    "angers": "Angers", "saint-etienne": "Saint-Etienne", "st. pauli": "St. Pauli",
    "holstein kiel": "Holstein Kiel", "heidenheim": "Heidenheim",
    "bochum": "Bochum", "union berlin": "Union Berlin",
    "werder bremen": "Werder Bremen", "mainz": "Mainz", "freiburg": "Freiburg",
    "wolfsburg": "Wolfsburg", "stuttgart": "Stuttgart", "hoffenheim": "Hoffenheim",
    "augsburg": "Augsburg", "bologna": "Bologna", "torino": "Torino",
    "monza": "Monza", "genoa": "Genoa", "verona": "Verona", "lecce": "Lecce",
    "udinese": "Udinese", "empoli": "Empoli", "cagliari": "Cagliari",
    "sassuolo": "Sassuolo", "frosinone": "Frosinone", "salernitana": "Salernitana",
    "roma": "Roma", "lazio": "Lazio", "fiorentina": "Fiorentina",
    "atalanta": "Atalanta", "napoli": "Napoli", "juventus": "Juventus",
    "inter": "Inter", "milan": "Milan", "liverpool": "Liverpool",
    "arsenal": "Arsenal", "chelsea": "Chelsea", "everton": "Everton",
    "fulham": "Fulham", "brentford": "Brentford", "wolves": "Wolves",
    "leicester": "Leicester", "bournemouth": "Bournemouth",
    "west ham": "West Ham", "newcastle": "Newcastle",
    "manchester city": "Manchester City", "manchester united": "Manchester United",
    "crystal palace": "Crystal Palace", "brighton": "Brighton",
    "aston villa": "Aston Villa", "tottenham": "Tottenham",
    "ipswich": "Ipswich", "nottm forest": "Nottm Forest",
}

def _match_team(api_name: str, league_name: str) -> Optional[str]:
    league_teams = ALL_LEAGUES.get(league_name, {})
    alower = api_name.lower().strip()
    if alower in TEAM_MAP:
        mapped = TEAM_MAP[alower]
        if mapped in league_teams:
            return mapped
    for team in league_teams:
        tlower = team.lower()
        if tlower == alower or tlower in alower or alower in tlower:
            return team
    return None

def fetch_and_analyze_today():
    if not os.getenv("API_FOOTBALL_KEY"):
        logger.warning("API_FOOTBALL_KEY mancante, skip calendario")
        return 0, 0
    clear_old_matches()
    today = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    tomorrow = (datetime.utcnow() + timedelta(hours=28)).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_matches = 0
    value_count = 0
    rejected_count = 0
    for league, sport_key in SPORTS_MAP.items():
        try:
            raw = fetch_odds(sport=sport_key, commence_time_from=today, commence_time_to=tomorrow)
            for match in raw:
                mid = match.get("id", "")
                home_api = match.get("home_team", "")
                away_api = match.get("away_team", "")
                home_db = _match_team(home_api, league)
                away_db = _match_team(away_api, league)
                if not home_db or not away_db:
                    continue
                save_match(mid, league, home_db, away_db, match.get("commence_time", ""))
                total_matches += 1
                res = _analyze_match(mid, match, home_db, away_db, league)
                if res == "strong_value" or res == "value":
                    value_count += 1
                elif res == "rejected":
                    rejected_count += 1
        except Exception as e:
            logger.warning(f"Errore calendario {league}: {e}")
    logger.info(f"Calendario: {total_matches} partite | {value_count} value | {rejected_count} filtrate")
    return total_matches, value_count

def _analyze_match(match_id, match, home_db, away_db, league):
    try:
        lam_h, lam_a = expected_goals(home_db, away_db)
    except Exception:
        return "error"
    p1, px, p2 = prob_1x2(lam_h, lam_a)
    p_over, _ = prob_over_under(lam_h, lam_a)
    best = None
    for bm in match.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            bias = DERIV_BIAS if mkt["key"] == "totals" else 0.0
            if mkt["key"] == "h2h":
                for out in mkt.get("outcomes", []):
                    name, price = out["name"], out["price"]
                    prob = p1 if name == match["home_team"] else (p2 if name == match["away_team"] else px)
                    ev = compute_ev(prob, price)
                    if best is None or (ev + bias) > best["score"]:
                        best = {"score": ev + bias, "ev": ev, "esito": name,
                                "quota": price, "bookmaker": bm["title"], "prob": prob}
            elif mkt["key"] == "totals":
                for out in mkt.get("outcomes", []):
                    if "over" in out["name"].lower() and out.get("point") == 2.5:
                        price = out["price"]
                        ev = compute_ev(p_over, price)
                        if best is None or (ev + bias) > best["score"]:
                            best = {"score": ev + bias, "ev": ev, "esito": "Over 2.5",
                                    "quota": price, "bookmaker": bm["title"], "prob": p_over}
    if not best:
        return "no_odds"
    
    sane, reason = is_sane(best["prob"], best["quota"], best["ev"])
    if not sane:
        status = "rejected"
        # Log solo a livello debug per non sporcare la dashboard
        logger.debug(f"FILTRATO: {home_db} vs {away_db} — {reason}")
    elif best["ev"] > 0.08:
        status = "strong_value"
    elif best["ev"] > 0.03:
        status = "value"
    else:
        status = "no_value"
    
    save_analysis(match_id, lam_h, lam_a, p1, px, p2, p_over, best["ev"], best["esito"], best["quota"], best["bookmaker"], status)
    return status

def get_calendar_formatted() -> str:
    rows = get_today_matches()
    if not rows:
        return "📅 *CALENDARIO DEL GIORNO*\n\nNessuna partita trovata oggi.\nAssicurati che API_FOOTBALL_KEY sia configurata."
    msg = "📅 *CALENDARIO DEL GIORNO*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    current_league = ""
    for row in rows:
        mid, league, home, away, commence, status, _ = row
        if league != current_league:
            msg += f"🏆 *{league}*\n"
            current_league = league
        time_str = commence[11:16] if len(commence) > 16 else "--:--"
        ana = get_analysis_for_match(mid)
        if ana:
            _, _, lam_h, lam_a, p1, px, p2, p_over, best_ev, best_esito, best_quota, best_bookmaker, status, _ = ana
            ev_txt = f"+{best_ev*100:.1f}%" if best_ev > 0 else f"{best_ev*100:.1f}%"
            if status == "strong_value":
                line = f"🔥 {time_str} {home} vs {away} → {best_esito} @ {best_quota:.2f} (EV {ev_txt})"
            elif status == "value":
                line = f"🟡 {time_str} {home} vs {away} → {best_esito} @ {best_quota:.2f} (EV {ev_txt})"
            elif status == "rejected":
                line = f"❌ {time_str} {home} vs {away} → FILTRATO"
            else:
                line = f"⚪ {time_str} {home} vs {away}"
        else:
            line = f"⚪ {time_str} {home} vs {away} (analisi in corso)"
        msg += line + "\n"
    msg += "\n💡 Usa `/analisi` per aggiornare quote e analisi."
    return msg

def get_value_picks_for_schedina() -> List[Dict]:
    conn = None
    try:
        from tracker import _get_conn
        conn = _get_conn()
        c = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute('''SELECT m.league, m.home_team, m.away_team, a.best_esito, a.best_quota, a.best_bookmaker, a.best_ev, a.lam_h, a.lam_a
                     FROM matches m JOIN match_analysis a ON m.id = a.match_id
                     WHERE m.commence_time LIKE ? AND a.status IN ('value','strong_value')
                     ORDER BY a.best_ev DESC LIMIT 7''', (f"{today}%",))
        rows = c.fetchall()
        picks = []
        for r in rows:
            picks.append({
                "league": r[0], "home": r[1], "away": r[2], "esito": r[3],
                "quota": r[4], "bookmaker": r[5], "ev": r[6], "lam_h": r[7], "lam_a": r[8],
                "evento": f"{r[0]} – {r[1]} vs {r[2]}"
            })
        return picks
    except Exception as e:
        logger.warning(f"Errore get_value_picks: {e}")
        return []
    finally:
        if conn: conn.close()

def format_schedina(picks: List[Dict], bankroll: float = 100.0) -> str:
    if not picks:
        return "📋 *SCHEDINA DEL GIORNO*\n\nNessuna partita con valore positivo trovata oggi.\nRiprova più tardi con `/analisi`."
    msg = "📋 *SCHEDINA DEL GIORNO*\n"
    msg += f"🗓 {datetime.now().strftime('%d/%m/%Y')}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += "🎯 *MIGLIORI SINGOLE DEL GIORNO*\n"
    msg += "⚠️ Gioca SEMPRE le singole. La multipla distrugge il valore.\n\n"
    total_stake = 0.0
    for i, p in enumerate(picks, 1):
        prob = p["ev"] + (1/p["quota"])
        pro = get_pro_stake(bankroll, prob, p["quota"])
        stake = pro["stake"]
        total_stake += stake
        msg += (
            f"*{i}. {p['evento']}*\n"
            f"   🎯 {p['esito']} @ {p['quota']:.2f} ({p['bookmaker']})\n"
            f"   📈 EV: +{p['ev']*100:.1f}% | Stake: €{stake:.2f} ({pro['stake_pct_of_bankroll']:.1f}% bankroll)\n"
            f"   🛡 Filtri: Kelly 1/4 | Cap 3% | EV 3-15% | Odds 1.50-5.00\n\n"
        )
    msg += f"💵 *Investimento totale:* €{total_stake:.2f} ({(total_stake/bankroll*100):.1f}% bankroll)\n"
    msg += f"💰 *Bankroll di riferimento:* €{bankroll:.2f}\n\n"
    # --- Multipla prolungata (massimo 7 esiti) ---
    msg += "\n" + build_multipla_block(picks, bankroll)
    return msg


def build_multipla(picks: List[Dict], max_legs: int = 7) -> Optional[Dict]:
    """Costruisce una multipla dai migliori esiti con quota e prob combinate.

    La prob combinata usa il denominatore corretto (prob = ev + 1/quota),
    stimata dall'EV di ciascun esito. Ritorna None se ci sono meno di 2 esiti.
    """
    if len(picks) < 2:
        return None
    legs = picks[:max_legs]
    odds = [p["quota"] for p in legs]
    probs = [p["ev"] + (1.0 / p["quota"]) for p in legs]
    total_quota = combined_quota(odds)
    total_prob = combined_probability(probs)
    ev = compute_ev(total_prob, total_quota)
    return {
        "legs": legs,
        "quota": total_quota,
        "prob": total_prob,
        "ev": ev,
        "esiti": " + ".join(p["esito"] for p in legs),
    }


def build_multipla_block(picks: List[Dict], bankroll: float = 100.0) -> str:
    """Formatta la sezione multipla prolungata con risk management automatico."""
    mp = build_multipla(picks)
    if not mp:
        return ""
    stake = multipla_stake(bankroll, mp["prob"], mp["quota"])
    ev_txt = f"+{mp['ev']*100:.1f}%" if mp['ev'] > 0 else f"{mp['ev']*100:.1f}%"
    if mp["ev"] >= 0.05:
        verdict = "🟢 MULTIPLA ACCETTABILE (EV buono)"
    elif mp["ev"] >= 0:
        verdict = "🟡 MULTIPLA MARGINALE (EV ~0)"
    else:
        verdict = "🔴 MULTIPLA NEGATIVA — sconsigliata"
    block = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔗 *MULTIPLA PROLUNGATA* (massimo 7 esiti)\n"
        "⚠️ La multipla aumenta il rischio: vince solo se passano TUTTI gli esiti.\n\n"
    )
    for i, p in enumerate(mp["legs"], 1):
        block += f"{i}. {p['esito']} @ {p['quota']:.2f}\n"
    block += (
        "\n"
        f"💯 Quota combinata: @{mp['quota']:.2f}\n"
        f"📈 Probabilità congiunta: {mp['prob']*100:.1f}% | EV: {ev_txt}\n"
        f"💰 Stake suggerito (1/8 Kelly, cap 1%): *€{stake:.2f}*\n\n"
        f"🛡 {verdict}\n"
        f"✅ Giocare solo se la combinazione resta sotto l'1% del bankroll."
    )
    return block
