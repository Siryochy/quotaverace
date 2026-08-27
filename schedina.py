import os
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from poisson_engine import expected_goals, prob_1x2, prob_over_under
from leagues_data import ALL_LEAGUES
from value_filter import compute_ev, kelly_fraction, kelly_euro

try:
    from odds_api import fetch_odds, SPORTS_MAP
    LIVE_ODDS = True
except Exception:
    LIVE_ODDS = False

logger = logging.getLogger(__name__)

TEAM_MAP = {
    "inter milan": "Inter", "ac milan": "Milan",
    "man united": "Manchester United", "man utd": "Manchester United",
    "man city": "Manchester City", "tottenham hotspur": "Tottenham",
    "tottenham": "Tottenham", "athletic club": "Athletic Bilbao",
    "atletico madrid": "Atletico Madrid", "atletico": "Atletico Madrid",
    "real betis": "Real Betis", "paris saint-germain": "Paris Saint-Germain",
    "psg": "Paris Saint-Germain", "borussia dortmund": "Borussia Dortmund",
    "borussia mgladbach": "Borussia Mgladbach", "rb leipzig": "RB Leipzig",
    "bayern munich": "Bayern Munich", "bayer leverkusen": "Bayer Leverkusen",
    "eintracht frankfurt": "Eintracht Frankfurt", "wolves": "Wolves",
    "west ham united": "West Ham", "west ham": "West Ham",
    "crystal palace": "Crystal Palace", "brighton and hove albion": "Brighton",
    "brighton": "Brighton", "aston villa": "Aston Villa",
    "newcastle united": "Newcastle", "newcastle": "Newcastle",
    "leicester city": "Leicester", "leicester": "Leicester",
    "southampton": "Southampton", "brentford": "Brentford",
    "fulham": "Fulham", "everton": "Everton",
    "nottm forest": "Nottm Forest", "nottingham forest": "Nottm Forest",
    "ipswich town": "Ipswich", "ipswich": "Ipswich",
    "bournemouth": "Bournemouth", "liverpool": "Liverpool",
    "arsenal": "Arsenal", "chelsea": "Chelsea",
    "real madrid": "Real Madrid", "barcelona": "Barcelona",
    "sevilla": "Sevilla", "valencia": "Valencia",
    "getafe": "Getafe", "osasuna": "Osasuna",
    "rayo vallecano": "Rayo Vallecano", "mallorca": "Mallorca",
    "las palmas": "Las Palmas", "alaves": "Alaves",
    "girona": "Girona", "leganes": "Leganes",
    "espanyol": "Espanyol", "valladolid": "Valladolid",
    "celta vigo": "Celta Vigo", "villarreal": "Villarreal",
    "real sociedad": "Real Sociedad", "lille": "Lille",
    "marseille": "Marseille", "monaco": "Monaco", "lyon": "Lyon",
    "lens": "Lens", "rennes": "Rennes", "nice": "Nice",
    "strasbourg": "Strasbourg", "nantes": "Nantes", "reims": "Reims",
    "montpellier": "Montpellier", "brest": "Brest", "toulouse": "Toulouse",
    "le havre": "Le Havre", "auxerre": "Auxerre", "angers": "Angers",
    "saint-etienne": "Saint-Etienne", "st. pauli": "St. Pauli",
    "holstein kiel": "Holstein Kiel", "heidenheim": "Heidenheim",
    "bochum": "Bochum", "union berlin": "Union Berlin",
    "werder bremen": "Werder Bremen", "mainz": "Mainz",
    "freiburg": "Freiburg", "wolfsburg": "Wolfsburg",
    "stuttgart": "Stuttgart", "hoffenheim": "Hoffenheim",
    "augsburg": "Augsburg", "bologna": "Bologna", "torino": "Torino",
    "monza": "Monza", "genoa": "Genoa", "verona": "Verona",
    "lecce": "Lecce", "udinese": "Udinese", "empoli": "Empoli",
    "cagliari": "Cagliari", "sassuolo": "Sassuolo",
    "frosinone": "Frosinone", "salernitana": "Salernitana",
    "roma": "Roma", "lazio": "Lazio", "fiorentina": "Fiorentina",
    "atalanta": "Atalanta", "napoli": "Napoli", "juventus": "Juventus",
    "inter": "Inter", "milan": "Milan",
}

def _match_team(api_name: str, league_name: str) -> Optional[str]:
    league_teams = ALL_LEAGUES.get(league_name, {})
    api_lower = api_name.lower().strip()
    if api_lower in TEAM_MAP:
        mapped = TEAM_MAP[api_lower]
        if mapped in league_teams:
            return mapped
    for team in league_teams:
        tlower = team.lower()
        if tlower == api_lower or tlower in api_lower or api_lower in tlower:
            return team
    return None

def build_daily_card() -> Tuple[List[Dict], Optional[str]]:
    if not os.getenv("ODDS_API_KEY") or not LIVE_ODDS:
        return None, "ODDS_API_KEY non configurata. Schedina non disponibile."
    
    picks: List[Dict] = []
    for league, sport_key in SPORTS_MAP.items():
        try:
            raw = fetch_odds(sport=sport_key)
            for match in raw:
                home_api = match.get("home_team", "")
                away_api = match.get("away_team", "")
                home_db = _match_team(home_api, league)
                away_db = _match_team(away_api, league)
                if not home_db or not away_db:
                    continue
                try:
                    lam_h, lam_a = expected_goals(home_db, away_db)
                except Exception:
                    continue
                
                p1, px, p2 = prob_1x2(lam_h, lam_a)
                p_over, _ = prob_over_under(lam_h, lam_a)
                
                best_1 = best_X = best_2 = best_over = None
                for bm in match.get("bookmakers", []):
                    for mkt in bm.get("markets", []):
                        if mkt["key"] == "h2h":
                            for out in mkt.get("outcomes", []):
                                name = out["name"]
                                price = out["price"]
                                if name == home_api:
                                    if best_1 is None or price > best_1["price"]:
                                        best_1 = {"price": price, "bookmaker": bm["title"]}
                                elif name == away_api:
                                    if best_2 is None or price > best_2["price"]:
                                        best_2 = {"price": price, "bookmaker": bm["title"]}
                                else:
                                    if best_X is None or price > best_X["price"]:
                                        best_X = {"price": price, "bookmaker": bm["title"]}
                        elif mkt["key"] == "totals":
                            for out in mkt.get("outcomes", []):
                                if "over" in out["name"].lower() and out.get("point") == 2.5:
                                    price = out["price"]
                                    if best_over is None or price > best_over["price"]:
                                        best_over = {"price": price, "bookmaker": bm["title"]}
                
                candidates = []
                if best_1: candidates.append((p1, f"1 ({home_db})", best_1["price"], best_1["bookmaker"]))
                if best_X: candidates.append((px, "X", best_X["price"], best_X["bookmaker"]))
                if best_2: candidates.append((p2, f"2 ({away_db})", best_2["price"], best_2["bookmaker"]))
                if best_over: candidates.append((p_over, "Over 2.5", best_over["price"], best_over["bookmaker"]))
                
                for prob, label, quota, bookmaker in candidates:
                    ev = compute_ev(prob, quota)
                    if ev > 0.03:
                        picks.append({
                            "evento": f"{league} – {home_db} vs {away_db}",
                            "esito": label,
                            "quota": quota,
                            "bookmaker": bookmaker,
                            "prob": prob,
                            "ev": ev,
                        })
        except Exception as e:
            logger.warning(f"Errore analisi {league}: {e}")
    
    picks = sorted(picks, key=lambda x: x["ev"], reverse=True)
    return picks[:5], None

def format_schedina(picks: List[Dict], bankroll: float = 100.0) -> str:
    if not picks:
        return "📋 *SCHEDINA DEL GIORNO*\n\nNessuna partita con valore positivo trovata oggi.\nRiprova più tardi con `/schedina`."
    
    msg = "📋 *SCHEDINA DEL GIORNO*\n"
    msg += f"🗓 {datetime.now().strftime('%d/%m/%Y')}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += "🎯 *MIGLIORI SINGOLE DEL GIORNO*\n"
    msg += "⚠️ Gioca SEMPRE le singole. La multipla distrugge il valore.\n\n"
    
    total_stake = 0.0
    for i, p in enumerate(picks, 1):
        kelly = kelly_fraction(p["prob"], p["quota"])
        stake = kelly_euro(bankroll, p["prob"], p["quota"])
        total_stake += stake
        msg += (
            f"*{i}. {p['evento']}*\n"
            f"   🎯 {p['esito']} @ {p['quota']:.2f} ({p['bookmaker']})\n"
            f"   📈 Probabilità: {p['prob']*100:.1f}% | EV: +{p['ev']*100:.1f}%\n"
            f"   💰 Stake: €{stake:.2f} (Kelly {kelly*100:.1f}%)\n\n"
        )
    
    msg += f"💵 *Investimento totale:* €{total_stake:.2f}\n"
    msg += f"💰 *Bankroll di riferimento:* €{bankroll:.2f}\n\n"
    
    if len(picks) >= 2:
        multipla_quota = 1.0
        for p in picks[:3]:
            multipla_quota *= p["quota"]
        msg += (
            f"🎲 *MULTIPLA DIVERTIMENTO (opzionale)*\n"
            f"   {' + '.join([p['esito'] for p in picks[:3]])}\n"
            f"   Quota totale: @{multipla_quota:.2f}\n"
            f"   ⚠️ Stake max consigliato: €2 (la multipla è -EV)\n\n"
        )
    
    msg += "📌 *Regola d'oro:* Le singole con Kelly Criterion battono la multipla nel lungo periodo."
    return msg
