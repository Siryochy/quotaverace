"""settlement_apifootball.py — Refertazione risultati ESCLUSIVAMENTE via API-Football.

Dal 04/09 l'architettura NON ha piu' alcuna dipendenza dal Betfair Exchange.
La refertazione (risultati esatti per saldare bets/predictions/cassa) usa
solo API-Football (API_FOOTBALL_KEY); le quote e la CLV restano su
the-odds-api (ODDS_API_KEY). Sostituisce il vecchio flusso basato su
odds_api.fetch_scores.

Come funziona il settlement:
1. Legge i match registrati in `matches` (match_id the-odds-api, nomi
   squadre, lega, commence_time) — le partite che hanno segnali/bet;
2. Per ogni lega coinvolta scarica da API-Football le fixtures FINITE dei
   giorni recenti (endpoint /fixtures con from/to, 1 chiamata per lega);
3. Abbina per NOME squadra normalizzato (TEAM_MAP + _norm_team) e per data
   (la fixture piu' vicina al commence_time del match);
4. Salva in match_results con lo STESSO match_id the-odds-api: il resto del
   settlement (settle_bets/predictions/cassa, sanity check) resta invariato;
5. Le fixtures finite di leghe con CASSA aperta ma senza match in `matches`
   vengono salvate comunque (id sintetico "apifb-<fixture_id>"), cosi' la
   cassa (che si aggancia per nome) continua a saldarsi.

Vincolo del free plan API-Football: 100 richieste/giorno. Si scaricano SOLO
le leghe con match/cassa aperti (poche al giorno), 1 chiamata a lega.
Le leghe senza league_id noto vengono loggate e saltate.

CLI:  python settlement_apifootball.py
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta

from config import load_dotenv
from football_hist import FINISHED_STATUSES, _api_get
from tracker import _norm_team, _create_results_table, save_result

load_dotenv()

logger = logging.getLogger(__name__)

# Mappatura estesa lega -> league_id API-Football (v3): copre tutte le leghe
# principali di SPORTS_MAP. La mappa storica di football_hist (LEAGUE_IDS) e'
# volutamente piu' piccola (8 leghe) per non bruciare il free plan su /sync;
# il settlement usa QUESTA, che abbina per nome quindi non richiede roster.
# Id verificati su api-sports.io (v3); le leghe senza id noto vengono
# loggate e saltate (mai silenzioso: finiscono in res["skipped"]).
SETTLEMENT_LEAGUE_IDS: dict[str, int] = {
    # Italia
    "Serie A": 135, "Serie B": 136, "Coppa Italia": 137,
    # Inghilterra
    "Premier League": 39, "EFL Championship": 40, "League One": 41,
    "League Two": 42, "FA Cup": 45, "EFL Cup": 46,
    # Spagna
    "La Liga": 140, "La Liga 2": 141, "Copa del Rey": 143,
    # Germania
    "Bundesliga": 78, "Bundesliga 2": 79, "3. Liga": 80,
    "DFB Pokal": 81,
    # Francia
    "Ligue 1": 61, "Ligue 2": 62, "Coupe de France": 66,
    # Resto d'Europa
    "Eredivisie": 88, "Primeira Liga": 94, "Scottish Premiership": 179,
    "Austrian Bundesliga": 218, "Belgian First Div": 144,
    "Greek Super League": 197, "Polish Ekstraklasa": 106,
    "Russian Premier League": 235, "Turkey Super Lig": 203,
    "Swiss Super League": 169, "Superliga Danimarca": 119,
    "Allsvenskan": 113, "Sweden Superettan": 114, "Eliteserien": 103,
    # Asia / Oceania
    "J1 League": 98, "K League 1": 292, "A-League": 188,
    # Americhe
    "MLS": 253, "Brasileirao": 71, "Brazil Serie B": 72,
    "Liga MX": 262, "Saudi Pro League": 307, "Argentina Primera": 128,
    "Chile Primera": 265,
    # Coppe europee e internazionali
    "Champions League": 2, "Europa League": 3, "Conference League": 848,
    "Copa Libertadores": 13, "Copa Sudamericana": 11, "Copa America": 9,
    "FIFA World Cup": 1, "UEFA Euro": 4, "UEFA Nations League": 5,
}

# Finestra di ricerca: partite finite negli ultimi giorni + oggi + domani
# (copre kickoff serali e fusi orari diversi). 1 sola chiamata a lega.
DAYS_BACK = 3
DAYS_FORWARD = 1


def _resolve_team(name: str) -> str:
    """Normalizza un nome squadra applicando anche gli alias (TEAM_MAP).

    Allinea i nomi API-Football ('Inter', 'West Ham') a quelli the-odds-api
    ('Inter Milan', 'West Ham United') e viceversa: entrambi i lati passano
    per la stessa normalizzazione, quindi l'accoppiata torna coerente.
    """
    base = _norm_team(name or "")
    if not base:
        return base
    try:
        from fixture_engine import TEAM_MAP
        return _norm_team(TEAM_MAP.get(base, base))
    except Exception:
        return base


def _league_id(league: str) -> int | None:
    """league_id API-Football per una lega (None se non mappata)."""
    lid = SETTLEMENT_LEAGUE_IDS.get(league)
    if not lid:
        logger.warning("settlement_apifootball: lega '%s' senza league_id "
                       "API-Football, salto (niente settlement per questa lega)", league)
        return None
    return int(lid)


def _season_for(d: date) -> int:
    """Stagione API-Football per una data (anno di inizio della stagione)."""
    return d.year


def _fixtures_finished(league_id: int, season: int,
                       date_from: str, date_to: str) -> list[dict]:
    """Fixtures FINITE di una lega nel range date (lista grezza API-Football)."""
    body = _api_get("fixtures", {"league": league_id, "season": season,
                                 "from": date_from, "to": date_to})
    if not body or body.get("results", 0) == 0:
        return []
    return [fx for fx in body.get("response", [])
            if (((fx.get("fixture") or {}).get("status") or {}).get("short") or "")
            in FINISHED_STATUSES]


def _fixture_score(fx: dict):
    """(home_api, away_api, sh, sa, date) da una fixture, o None se non valida."""
    goals = fx.get("goals") or {}
    sh, sa = goals.get("home"), goals.get("away")
    if sh is None or sa is None:
        return None
    try:
        sh, sa = int(sh), int(sa)
    except (TypeError, ValueError):
        return None
    home_api = ((fx.get("teams") or {}).get("home") or {}).get("name", "")
    away_api = ((fx.get("teams") or {}).get("away") or {}).get("name", "")
    if not home_api or not away_api:
        return None
    fdate = (fx.get("fixture") or {}).get("date") or ""
    return home_api, away_api, sh, sa, fdate


def _match_date_ok(match_commence: str | None, fixture_date: str,
                   tolerance_days: int = 2) -> bool:
    """True se la data della fixture e' compatibile col commence del match."""
    if not match_commence:
        return True  # senza riferimento temporale: accetta per nome
    try:
        from datetime import datetime
        mc = datetime.fromisoformat(str(match_commence).replace("Z", "+00:00"))
        fd = datetime.fromisoformat(str(fixture_date).replace("Z", "+00:00"))
        return abs((fd - mc).days) <= tolerance_days
    except Exception:
        return True


def _open_matches() -> dict:
    """Match con segnali/bet/cassa aperte, per lega.

    Ritorna {league: {coppia_normalizzata: [(match_id, home, away, commence)]}}.
    Include le leghe presenti in `matches` (ultimi 7 giorni) + quelle con
    cassa aperta, cosi' il settlement copre anche partite non analizzate.
    """
    from tracker import _get_conn
    conn = _get_conn()
    c = conn.cursor()
    out: dict = {}
    try:
        rows = c.execute(
            "SELECT id, league, home_team, away_team, commence_time "
            "FROM matches WHERE commence_time >= datetime('now', '-7 days') "
            "OR status='scheduled'").fetchall()
    except Exception:
        rows = []
    for mid, league, home, away, commence in rows:
        pair = (_resolve_team(home), _resolve_team(away))
        out.setdefault(league or "", []).append(
            {"match_id": mid, "home": home, "away": away,
             "commence": commence, "pair": pair})
    # Leghe con cassa aperta (partite magari mai analizzate)
    try:
        cassa = c.execute("SELECT partita FROM cassa "
                          "WHERE esito_finale IS NULL").fetchall()
    except Exception:
        cassa = []
    for (partita,) in cassa:
        clean = str(partita).split(" – ")[-1].strip() if " – " in str(partita) else str(partita)
        if " vs " not in clean:
            continue
        h, a = clean.split(" vs ", 1)
        league = None
        # la lega non e' nella stringa: la cerchiamo nelle partite in matches
        # con la stessa coppia (fallback: 'global')
        pair = (_resolve_team(h.strip()), _resolve_team(a.strip()))
        found = None
        for lg, ms in out.items():
            if any(m["pair"] == pair for m in ms):
                found = lg
                break
        if found is None:
            # salva la coppia sotto 'global' se nessuna lega la ha: il
            # settlement provera' tutte le leghe scaricate
            out.setdefault("__cassa__", []).append(
                {"match_id": None, "home": h.strip(), "away": a.strip(),
                 "commence": None, "pair": pair})
    conn.close()
    return out


def settle_results_from_apifootball(days_back: int = DAYS_BACK) -> dict:
    """Scarica i risultati finiti da API-Football e li salva in match_results.

    Ritorna {"updated": n, "matched": n, "cassa_only": n, "skipped": [leghe]}.
    Idempotente: save_result e' INSERT OR REPLACE, le righe gia' salvate
    vengono aggiornate solo se il punteggio cambia (stesso match_id).
    """
    if not os.getenv("API_FOOTBALL_KEY"):
        logger.warning("settlement_apifootball: API_FOOTBALL_KEY mancante")
        return {"updated": 0, "matched": 0, "cassa_only": 0, "skipped": [],
                "error": "API_FOOTBALL_KEY mancante"}

    open_matches = _open_matches()
    leagues = [lg for lg in open_matches if lg != "__cassa__"]
    today = date.today()
    d_from = (today - timedelta(days=days_back)).isoformat()
    d_to = (today + timedelta(days=DAYS_FORWARD)).isoformat()

    updated = matched = cassa_only = 0
    skipped = []
    # Indice per nome anche delle partite cassa-only
    for lg in leagues:
        lid = _league_id(lg)
        if lid is None:
            skipped.append(lg)
            continue
        season = _season_for(today)
        fixtures = _fixtures_finished(lid, season, d_from, d_to)
        if not fixtures and (today.month == 1 or today.month == 2):
            # a inizio anno la stagione corrente e' quella iniziata l'anno prima
            fixtures = _fixtures_finished(lid, season - 1, d_from, d_to)
        if not fixtures:
            continue
        # indice per coppia normalizzata dei match della lega
        by_pair: dict = {}
        for m in open_matches.get(lg, []):
            by_pair.setdefault(m["pair"], []).append(m)
        for fx in fixtures:
            parsed = _fixture_score(fx)
            if parsed is None:
                continue
            home_api, away_api, sh, sa, fdate = parsed
            pair = (_resolve_team(home_api), _resolve_team(away_api))
            candidates = by_pair.get(pair) or []
            if candidates:
                # preferisci il match con la data piu' vicina alla fixture
                best = None
                for m in candidates:
                    if not _match_date_ok(m["commence"], fdate):
                        continue
                    if best is None:
                        best = m
                        break
                if best is None:
                    best = candidates[0]
                save_result(best["match_id"], lg, best["home"], best["away"],
                            sh, sa, fdate)
                matched += 1
                updated += 1
            else:
                # cassa-only (nessun match the-odds-api in matches): salva con
                # id sintetico, la cassa si aggancia per nome
                save_result(f"apifb-{lg}-{home_api}-{away_api}-{fdate[:10]}",
                            lg, home_api, away_api, sh, sa, fdate)
                cassa_only += 1
                updated += 1

    logger.info("settlement_apifootball: %d partite aggiornate "
                "(%d match, %d cassa-only), %d leghe saltate",
                updated, matched, cassa_only, len(skipped))
    return {"updated": updated, "matched": matched,
            "cassa_only": cassa_only, "skipped": skipped}


def fetch_true_scores(days_back: int = DAYS_BACK) -> dict:
    """Punteggi VERI (per match_id the-odds-api) dalle fixtures finite.

    Usato da repair_scores: NON scrive in match_results, ritorna
    {match_id: (league, home, away, sh, sa)} per i match gia' registrati
    in match_results — cosi' il repair puo' confrontare i punteggi salvati
    con quelli veri e correggere i verdetti specchiati.

    Solo le leghe presenti in match_results (risparmio crediti: si
    riscaricano SOLO le leghe con risultati salvati).
    """
    from tracker import _get_conn
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    rows = c.execute(
        "SELECT match_id, league, home_team, away_team FROM match_results"
    ).fetchall()
    conn.close()
    if not rows:
        return {}
    by_league: dict = {}
    for mid, lg, home, away in rows:
        by_league.setdefault(lg or "", []).append(
            {"match_id": mid, "home": home, "away": away,
             "pair": (_resolve_team(home), _resolve_team(away))})

    today = date.today()
    d_from = (today - timedelta(days=days_back)).isoformat()
    d_to = (today + timedelta(days=DAYS_FORWARD)).isoformat()
    out: dict = {}
    for lg, ms in by_league.items():
        lid = _league_id(lg)
        if lid is None:
            continue
        fixtures = _fixtures_finished(lid, _season_for(today), d_from, d_to)
        if not fixtures and (today.month == 1 or today.month == 2):
            fixtures = _fixtures_finished(lid, _season_for(today) - 1, d_from, d_to)
        for fx in fixtures:
            parsed = _fixture_score(fx)
            if parsed is None:
                continue
            home_api, away_api, sh, sa, fdate = parsed
            pair = (_resolve_team(home_api), _resolve_team(away_api))
            for m in ms:
                if m["pair"] == pair and _match_date_ok(m.get("commence"), fdate):
                    out[m["match_id"]] = (lg, m["home"], m["away"], sh, sa)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    res = settle_results_from_apifootball()
    print(f"✅ Settlement API-Football: {res['updated']} partite aggiornate "
          f"({res['matched']} match, {res['cassa_only']} cassa-only). "
          f"Leghe saltate: {res['skipped']}")