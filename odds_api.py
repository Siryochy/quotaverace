import json, os, time, logging, requests
from datetime import datetime, timezone
from pathlib import Path
from config import DATA_DIR, load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR
ODDS_TTL = 86400          # cache 24h = 1 chiamata/giorno per lega
MIN_REMAINING = 20        # stop sotto 20 crediti
# Cache punteggi non attendibile per il settlement oltre queste ore dal
# kickoff se la partita e' ancora completed=False (artefatto di una cache
# scritta mentre la partita era in corso: un match di calcio finisce entro
# ~2h). BUG 11/09: la costante era USATA in _cache_is_stale_for_settlement
# ma MAI DEFINITA -> NameError inghiottito dall'except -> la cache stantia
# veniva considerata valida e il settlement restava bloccato (bet aperte).
STALE_INPLAY_HOURS = 3

# Soglie proattive per rotazione intelligente
CREDIT_LOW = 50           # sotto 50: disattiva leghe non-core (intervallo >7gg)
CREDIT_CRITICAL = 30      # sotto 30: solo top 6 leghe core
CREDIT_EMERGENCY = 15     # sotto 15: solo Serie A, PL, La Liga

CORE_LEAGUES_HIGH = {"soccer_italy_serie_a", "soccer_england_pl", "soccer_spain_la_liga",
                      "soccer_germany_bundesliga", "soccer_france_ligue_one",
                      "soccer_efl_champ"}
CORE_LEAGUES_EMERGENCY = {"soccer_italy_serie_a", "soccer_england_pl", "soccer_spain_la_liga"}

def get_remaining() -> int:
    """Crediti residui secondo la lettura PIU' RECENTE tra le cache toa_*.json.

    Ogni risposta dell'API riporta lo stesso contatore autoritativo
    (`x-requests-remaining`), quindi vale l'ULTIMA lettura per data — non il
    MINIMO tra le cache. Con il minimo un file vecchio inchioda il valore per
    settimane (12/09: la chiave nuova riportava 452 crediti ma le cache quote
    scritte con la chiave vecchia tenevano il contatore a 58, cosi' la
    rotazione veniva throttled come se i crediti fossero quasi finiti).

    Si usa `remaining_ts` (istante della LETTURA del credito) quando presente,
    altrimenti `ts`: in `fetch_scores` il `ts` della cache puo' essere
    preservato da un giro precedente, mentre `remaining_ts` e' sempre il
    momento della chiamata che ha prodotto quel valore.
    """
    best = None            # (timestamp lettura credito, remaining)
    fallback = []
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("toa_*.json"):
            try:
                d = json.loads(f.read_text())
                if d.get("remaining") is None:
                    continue
                rem = int(d["remaining"])
                ts = d.get("remaining_ts", d.get("ts"))
                if isinstance(ts, (int, float)):
                    if best is None or ts > best[0]:
                        best = (ts, rem)
                else:
                    fallback.append(rem)
            except Exception:
                continue
    if best is not None:
        return best[1]
    return min(fallback) if fallback else None

def should_query_sport(sport_key: str) -> bool:
    """Decide se una sport key deve essere interrogata in base ai crediti.

    Logica proattiva (09/09):
    - remaining >= 50: tutto attivo (default)
    - remaining < 50: solo leghe core (intervallo <= 7gg)
    - remaining < 30: solo top 6 leghe core
    - remaining < 15: solo Serie A, PL, La Liga

    Previene il rischio di esaurire i crediti a fine mese
    senza preavviso.
    """
    rem = get_remaining()
    if rem is None:
        return True  # no cache data: assume ok
    if rem >= CREDIT_LOW:
        return True
    # Sotto soglia: verifica la lega
    for lg, key in SPORTS_MAP.items():
        if key == sport_key:
            interval = SPORTS_INTERVAL_DAYS.get(lg, 7)
            if rem >= CREDIT_CRITICAL:
                # Solo leghe con intervallo <= 7gg (core)
                return interval <= 7
            elif rem >= CREDIT_EMERGENCY:
                # Solo top 6 leghe core
                return lg in CORE_LEAGUES_HIGH
            else:
                # Emergenza: solo top 3
                return lg in CORE_LEAGUES_EMERGENCY
    return True  # sport non mappato: allow (probabilmente tennis subet)

def _cache_is_stale_for_settlement(payload: list) -> bool:
    """True se la cache punteggi non e' attendibile per il settlement.

    Serve a _scores_from_cache (via fetch_scores): una partita iniziata da
    oltre STALE_INPLAY_HOURS ma con completed=False e' un artefatto di una
    cache troppo vecchia, non un dato reale (una partita di calcio finisce
    entro ~2h dal kickoff; oltre quelle ore 'completed=False' significa
    'il risultato non era ancora disponibile quando la cache e' stata scritta').
    """
    if not payload:
        return False
    now = time.time()
    for m in payload:
        if m.get("completed"):
            continue
        scores = m.get("scores") or []
        if len(scores) >= 2:
            continue
        try:
            commence = (m.get("commence_time") or "").replace("Z", "+00:00")
            start = datetime.fromisoformat(commence)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            kickoff_age_h = (now - start.timestamp()) / 3600.0
            if kickoff_age_h > STALE_INPLAY_HOURS:
                return True  # partita finita da ore ma cache dice 'in corso'
        except Exception:
            continue
    return False

def match_scores_by_name(m):
    """Punteggi (home, away) di un match the-odds-api associati per NOME.

    L'array `scores` NON ha ordine garantito: ogni elemento ha `name`
    (e opzionale `key`) da confrontare con home_team/away_team. NON si puo'
    assumere che scores[0] sia la squadra di casa: il 02/09 FC Machida
    Zelvia vs Kawasaki Frontale e' stato saldato con i punteggi invertiti
    (bet sul 2 segnata vinta per una vittoria casalinga).

    Returns:
        (score_home, score_away) oppure None se i punteggi non sono
        associabili con certezza (dati parziali o nomi non corrispondenti).
    """
    scores = m.get("scores") or []
    home = str(m.get("home_team") or "").strip().lower()
    away = str(m.get("away_team") or "").strip().lower()
    if not home or not away:
        return None
    sh = sa = None
    for s in scores:
        name = str(s.get("name") or "").strip().lower()
        key = str(s.get("key") or "").strip().lower()
        try:
            val = int(s.get("score"))
        except (TypeError, ValueError):
            continue
        if name == home or (key and key == home):
            sh = val
        elif name == away or (key and key == away):
            sa = val
    if sh is None or sa is None:
        return None
    return sh, sa


# TUTTE le competizioni di calcio coperte da the-odds-api (chiavi ufficiali
# verificate su the-odds-api.com/sports-apis). Le squadre senza roster in
# leagues_data usano il profilo di lega di default (expected_goals).
SPORTS_MAP = {
    # Campionati top + serie B
    "Serie A": "soccer_italy_serie_a", "Serie B": "soccer_italy_serie_b",
    "Premier League": "soccer_epl", "EFL Championship": "soccer_efl_champ",
    "League One": "soccer_england_league1", "League Two": "soccer_england_league2",
    "La Liga": "soccer_spain_la_liga", "La Liga 2": "soccer_spain_segunda_division",
    "Bundesliga": "soccer_germany_bundesliga", "Bundesliga 2": "soccer_germany_bundesliga2",
    "3. Liga": "soccer_germany_liga3", "Frauen-Bundesliga": "soccer_germany_bundesliga_women",
    "Ligue 1": "soccer_france_ligue_one", "Ligue 2": "soccer_france_ligue_two",
    "Eredivisie": "soccer_netherlands_eredivisie", "Primeira Liga": "soccer_portugal_primeira_liga",
    "Scottish Premiership": "soccer_spl", "Austrian Bundesliga": "soccer_austria_bundesliga",
    "Belgian First Div": "soccer_belgium_first_div", "Greek Super League": "soccer_greece_super_league",
    "Polish Ekstraklasa": "soccer_poland_ekstraklasa", "Russian Premier League": "soccer_russia_premier_league",
    "Turkey Super Lig": "soccer_turkey_super_league", "Swiss Super League": "soccer_switzerland_superleague",
    "Superliga Danimarca": "soccer_denmark_superliga", "Allsvenskan": "soccer_sweden_allsvenskan",
    "Sweden Superettan": "soccer_sweden_superettan", "Eliteserien": "soccer_norway_eliteserien",
    "Veikkausliiga": "soccer_finland_veikkausliiga", "League of Ireland": "soccer_league_of_ireland",
    "China Super League": "soccer_china_superleague", "J1 League": "soccer_japan_j_league",
    "K League 1": "soccer_korea_kleague1", "A-League": "soccer_australia_aleague",
    # Americhe
    "MLS": "soccer_usa_mls", "Brasileirao": "soccer_brazil_campeonato",
    "Brazil Serie B": "soccer_brazil_serie_b", "Liga MX": "soccer_mexico_ligamx",
    "Saudi Pro League": "soccer_saudi_arabia_pro_league",
    "Argentina Primera": "soccer_argentina_primera_division",
    "Chile Primera": "soccer_chile_campeonato",
    # Coppe europee
    "Champions League": "soccer_uefa_champs_league",
    "Europa League": "soccer_uefa_europa_league",
    "Conference League": "soccer_uefa_europa_conference_league",
    "UCL Qualification": "soccer_uefa_champs_league_qualification",
    "UEFA Women's Champions League": "soccer_uefa_champs_league_women",
    "Coppa Italia": "soccer_italy_coppa_italia",
    "Copa del Rey": "soccer_spain_copa_del_rey",
    "Coupe de France": "soccer_france_coupe_de_france",
    "DFB Pokal": "soccer_germany_dfb_pokal",
    "FA Cup": "soccer_fa_cup", "EFL Cup": "soccer_england_efl_cup",
    # Coppe internazionali + nazionali
    "Copa Libertadores": "soccer_conmebol_copa_libertadores",
    "Copa Sudamericana": "soccer_conmebol_copa_sudamericana",
    "Copa America": "soccer_conmebol_copa_america",
    "CONCACAF Gold Cup": "soccer_concacaf_gold_cup",
    "CONCACAF Leagues Cup": "soccer_concacaf_leagues_cup",
    "Africa Cup of Nations": "soccer_africa_cup_of_nations",
    "FIFA Club World Cup": "soccer_fifa_club_world_cup",
    "FIFA World Cup": "soccer_fifa_world_cup",
    "FIFA World Cup Qualifiers Europe": "soccer_fifa_world_cup_qualifiers_europe",
    "FIFA World Cup Qualifiers S.America": "soccer_fifa_world_cup_qualifiers_south_america",
    "FIFA Women's World Cup": "soccer_fifa_world_cup_womens",
    "UEFA Euro": "soccer_uefa_european_championship",
    "UEFA Euro Qualifiers": "soccer_uefa_euro_qualification",
    "UEFA Nations League": "soccer_uefa_nations_league",
}

# Finestra di ricerca delle partite: 7 giorni. Con la rotazione dei crediti
# una lega puo' essere interrogata 1 volta a settimana: la finestra ampia
# garantisce che NESSUNA partita sfugga (una chiamata copre l'intera
# settimana di calendario).
QUERY_WINDOW_DAYS = 7

# Rotazione interrogazioni (giorni) — PROFILO SETTEMBRE 2026 SOTTO-BUDGET.
# Audit crediti 05/09: 239/500 gia' consumati al giorno 5 (settlement + surebet
# NON erano stati conteggiati nel ~407/mese originale) → restano ~260 crediti
# per ~25 giorni (~10,3/giorno). Ripartizione del resto del mese:
#   settlement mirato ~2-3/giorno (fetch_scores solo leghe con righe aperte)
#   surebet MLB ~4/giorno (cron 15' + TTL 6h, 1 sport in stagione: NBA e'
#     off-season a settembre, riattivare a ottobre col reset crediti)
#   calendario value ~3,5/giorno = SOLO i top campionati (3gg) + coppe
#     europee/mercati maggiori (7gg); tutto il resto a 30gg = DORMIENTE.
# Costo mensile di questo profilo ~158/mese (test test_budget_mensile ok);
# nel resto di settembre le leghe a 30gg erano gia' state interrogate il 1°
# (cache fresca fino a ottobre) → costo residuo reale ~85-95 crediti.
# ⚠️ RIPRISTINARE il profilo completo il 1° ottobre (stagione NBA + reset
# crediti): commit precedente / git log per la tabella a 66 leghe.
# Con la finestra a 7 giorni, anche le leghe interrogate 1 volta a settimana
# non perdono partite: vedono tutto il calendario della settimana.
SPORTS_INTERVAL_DAYS = {
    # ogni 3 giorni: i top campionati (freschezza quote vicino al calcio
    # d'inizio, meglio per il CLV + copertura schedina/auto-bet)
    "Serie A": 3, "Premier League": 3, "La Liga": 3, "Bundesliga": 3,
    "Ligue 1": 3, "Eredivisie": 3, "EFL Championship": 3, "Serie B": 3,
    # ogni 7 giorni: coppe europee + mercati maggiori extra-Europa
    "Champions League": 7, "Europa League": 7,
    "MLS": 7, "Brasileirao": 7, "Liga MX": 7, "Saudi Pro League": 7,
    # ogni 30 giorni (dormienti a settembre, riattivare a ottobre): coppe
    # nazionali, campionati secondari, resto del mondo e nazionali
    "Conference League": 30, "Coppa Italia": 30, "Copa del Rey": 30,
    "Coupe de France": 30, "DFB Pokal": 30, "FA Cup": 30, "EFL Cup": 30,
    "Swiss Super League": 30, "Primeira Liga": 30, "Allsvenskan": 30,
    "Eliteserien": 30, "Superliga Danimarca": 30, "Veikkausliiga": 30,
    "J1 League": 30, "K League 1": 30, "A-League": 30,
    "Argentina Primera": 30, "Chile Primera": 30, "Copa Libertadores": 30,
    "Copa Sudamericana": 30, "Ligue 2": 30, "Bundesliga 2": 30,
    "La Liga 2": 30, "League One": 30, "League Two": 30,
    "Scottish Premiership": 30, "Austrian Bundesliga": 30,
    "Belgian First Div": 30, "Greek Super League": 30,
    "Polish Ekstraklasa": 30, "Turkey Super Lig": 30,
    "Russian Premier League": 30, "3. Liga": 30, "Brazil Serie B": 30,
    "Sweden Superettan": 30, "China Super League": 30,
    "League of Ireland": 30, "Frauen-Bundesliga": 30,
    "FIFA Club World Cup": 30, "UCL Qualification": 30,
    "UEFA Women's Champions League": 30, "UEFA Nations League": 30,
    "Copa America": 30, "CONCACAF Leagues Cup": 30,
    "FIFA World Cup": 30, "FIFA World Cup Qualifiers Europe": 30,
    "FIFA World Cup Qualifiers S.America": 30, "FIFA Women's World Cup": 30,
    "UEFA Euro": 30, "UEFA Euro Qualifiers": 30,
    "CONCACAF Gold Cup": 30, "Africa Cup of Nations": 30,
}

# Cap giornaliero di chiamate odds (piano free 500/mese -> ~16/giorno).
# Le leghe in eccedenza vengono rinviate al giorno dopo (elasticita' degli
# intervalli: niente partite perse, la finestra a 7 giorni copre).
DAILY_QUERY_BUDGET = int(os.getenv("ODDS_DAILY_BUDGET", "12"))


def interval_for_sport(sport_key: str) -> int:
    """Giorni tra un'interrogazione e l'altra per una sport key.
    Default 7 (settimanale): se una lega manca dalla tabella, meglio
    interrogarla poco che tutti i giorni (protezione crediti)."""
    for lg, key in SPORTS_MAP.items():
        if key == sport_key:
            return SPORTS_INTERVAL_DAYS.get(lg, 7)
    return 7


def _rotation_phase(sport_key: str, interval: int) -> int:
    """Fase stabile (0..interval-1) della lega nel ciclo di rotazione.

    Spalma le scadenze delle leghe con lo STESSO intervallo su giorni
    diversi: senza, le leghe core (tutte a 3gg) si sincronizzano e vengono
    interrogate tutte lo stesso giorno -> analisi e segnali nuovi solo 1
    giorno su 3. Il costo mensile NON cambia (ogni lega resta sul suo
    intervallo), ma i giri analisi diventano giornalieri.
    """
    if interval <= 1:
        return 0
    h = 0
    for ch in sport_key:
        h = (h * 31 + ord(ch)) % 1000003
    return h % interval


def is_sport_due(sport_key: str) -> bool:
    """True se la cache della lega e' scaduta rispetto al suo intervallo
    (quindi oggi va interrogata l'API, costo 1 credito).

    Regola di stagger (10/09): oltre alla scadenza per intervallo, una
    lega core (intervallo <= 7gg) diventa "dovuta" anche sul suo giorno
    di fase (eta' cache >= 1 giorno), cosi' le leghe a 3gg non si
    sincronizzano tutte nello stesso giorno. Le leghe a 30gg restano
    dormienti puri (solo scadenza per intervallo): nessun costo extra.
    """
    cache_file = CACHE_DIR / f"toa_{sport_key}.json"
    if not cache_file.exists():
        return True
    try:
        data = json.loads(cache_file.read_text())
        interval = interval_for_sport(sport_key)
        ttl = interval * 86400
        age = time.time() - data.get("ts", 0)
        if age >= ttl:
            return True
        if interval <= 7 and age >= 86400:
            day = int(time.time() // 86400)
            if day % interval == _rotation_phase(sport_key, interval):
                return True
        return False
    except Exception:
        return True

def _env(name):
    exact = os.getenv(name)
    if exact is not None: return exact.strip()
    for k, v in os.environ.items():
        if k.strip() == name: return v.strip()
    return ""

def _get_odds(sport, frm, to):
    cache_file = CACHE_DIR / f"toa_{sport}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            ttl = interval_for_sport(sport) * 86400
            if time.time() - data.get("ts", 0) < ttl:
                return data.get("payload", []), data.get("remaining", 999)
        except Exception: pass
    key = _env("ODDS_API_KEY")
    if not key: return [], 999
    # Filtro proattivo crediti: non interrogare se sotto soglia
    if not should_query_sport(sport):
        logger.info(f"Crediti bassi: {sport} saltata per risparmio crediti")
        return [], 0
    try:
        # SOLO h2h: the-odds-api addebita markets x regions per chiamata
        # (h2h,totals = 2 crediti). Il mercato totals (Over/Under) e'
        # escluso dalle selezioni dal 06/09: richiederlo e' puro spreco
        # (tripwire test_ou_exclusion.test_odds_request_solo_h2h).
        r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sport}/odds", params={
            "apiKey": key, "regions": "eu", "markets": "h2h",
            "oddsFormat": "decimal", "commenceTimeFrom": frm, "commenceTimeTo": to,
        }, timeout=30)
        remaining = int(r.headers.get("x-requests-remaining", 999))
        if r.status_code in (401, 429):
            logger.warning(f"the-odds-api bloccata (codice {r.status_code})")
            return [], 0
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.warning(f"Errore the-odds-api {sport}: {e}")
        return [], 999
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps({"ts": time.time(), "payload": payload,
                                      "remaining": remaining,
                                      "remaining_ts": time.time()}))
    logger.info(f"the-odds-api {sport}: {len(payload)} match | crediti residui: {remaining}")
    return payload, remaining

def fetch_odds(sport=None, commence_time_from=None, commence_time_to=None, **kwargs):
    if not sport: return []
    payload, remaining = _get_odds(sport, commence_time_from, commence_time_to)
    if remaining < MIN_REMAINING:
        logger.warning(f"Crediti esauriti ({remaining}), nessuna quota scaricata")
        return []
    return payload

# Finestra di refertazione `daysFrom`: l'API the-odds-api copre al MASSIMO
# 3 giorni indietro (422 oltre). Con 2 giorni le partite di due sere prima
# (kickoff a >48h) restavano fuori dal payload e la bet non si saldava mai:
# il costo per chiamata NON cambia, quindi tanto vale usare il massimo.
SCORES_DAYS_FROM = 3


def fetch_scores(sport=None, days_from=SCORES_DAYS_FROM):
    """Risultati finali (stessa chiave, ~1 credito/call, cache 24h).

    La cache NON viene fidata se contiene partite iniziate da oltre
    STALE_INPLAY_HOURS ancora marcate completed=False (cache scritta mentre
    la partita era in gioco): in quel caso si richiama l'API per avere i
    risultati veri, altrimenti il settlement delle puntate resta bloccato.
    Se la chiamata fallisce (crediti esauriti, rete), si ripiega sulla cache
    comunque: meglio dati vecchi di nessun dato.
    """
    if not sport:
        return []
    cache_file = CACHE_DIR / f"toa_scores_{sport}.json"
    payload = []
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            ts = data.get("ts", 0)
            payload = data.get("payload", [])
            if time.time() - ts < ODDS_TTL:
                if not _cache_is_stale_for_settlement(payload):
                    return payload
                # Cache stantia per il settlement: forza il refresh (fallthrough)
                logger.warning("scores cache stantia (%s): refresh forzato", sport)
        except Exception:
            pass
    key = _env("ODDS_API_KEY")
    if not key:
        # Nessuna chiave: la cache e' tutto quello che abbiamo.
        return payload if cache_file.exists() else []
    try:
        r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sport}/scores",
                         params={"apiKey": key, "daysFrom": days_from}, timeout=30)
        remaining = int(r.headers.get("x-requests-remaining", 999))
        if r.status_code in (401, 429):
            logger.warning(f"Scores bloccati ({r.status_code})")
            return payload if cache_file.exists() else []
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.warning(f"Errore scores {sport}: {e}")
        return payload if cache_file.exists() else []
    CACHE_DIR.mkdir(exist_ok=True)
    # Se il payload contiene SOLO partite completate, salviamo con il
    # timestamp originale della cache precedente (se fresca): cosi' la
    # scrittura non 'ringiovanisce' artificialmente una cache che copre
    # ancora la finestra quote, e il refresh non costa piu' crediti del
    # necessario nelle ore successive.
    save_ts = time.time()
    if isinstance(payload, list) and payload and all(m.get("completed") for m in payload):
        try:
            old = json.loads(cache_file.read_text())
            if time.time() - old.get("ts", 0) < ODDS_TTL:
                save_ts = old["ts"]
        except Exception:
            pass
    # Il consumo crediti va PERSISTITO anche nelle cache dei punteggi:
    # `get_remaining()`/`get_quota()` (guardia proattiva + credit watchdog)
    # leggono le cache `toa_*.json`, quindi senza questo campo il consumo di
    # `fetch_scores` era invisibile (bug 12/09: il contatore restava a 58
    # mentre l'API ne riportava 6 -> nessun throttle, nessun alert, crediti
    # bruciati fino all'esaurimento).
    cache_file.write_text(json.dumps({"ts": save_ts, "payload": payload,
                                      "remaining": remaining,
                                      "remaining_ts": time.time()}))
    logger.info(f"the-odds-api scores {sport}: {len(payload)} | crediti residui: {remaining}")
    return payload

def oddsapi_to_records(payload, sport="calcio"):
    """Converte il payload v4 the-odds-api nel contratto normalizzato di odds_ingest.

    Righe {bookmaker, evento, sport, esito, quota_decimale, timestamp}:
    - h2h: outcome nome squadra -> "1"/"2", "Draw" -> "X";
    - totals: "Over X.5"/"Under X.5" lasciati com' sono;
    - evento = f"{home} vs {away}" (senza campionato: per il merging
      con Betfair l'accoppiata squadre e' la chiave).
    """
    rows = []
    for match in payload:
        home = (match.get("home_team") or "").strip()
        away = (match.get("away_team") or "").strip()
        if not home or not away:
            continue
        commence = match.get("commence_time") or ""
        for bm in match.get("bookmakers", []):
            bookmaker = bm.get("title") or bm.get("key") or "unknown"
            for mkt in bm.get("markets", []):
                key = mkt.get("key")
                for out in mkt.get("outcomes", []):
                    name = (out.get("name") or "").strip()
                    price = out.get("price")
                    if not name or price is None or float(price) <= 1.0:
                        continue
                    if key == "h2h":
                        if name == home:
                            esito = "1"
                        elif name == away:
                            esito = "2"
                        elif name.lower() in ("draw", "pareggio"):
                            esito = "X"
                        else:
                            continue
                    else:
                        continue
                    rows.append({
                        "bookmaker": bookmaker,
                        "evento": f"{home} vs {away}",
                        "sport": sport,
                        "esito": esito,
                        "quota_decimale": float(price),
                        "timestamp": commence,
                    })
    return rows


def get_live_odds():
    """Quote reali oggi per tutte le leghe, come lista di righe normalizzate.

    Usa la cache 24h per lega: dopo il job mattutino 6:00 (fetch_and_analyze
    today) le chiamate successive costano zero crediti. Serve ODDS_API_KEY.
    """
    if not _env("ODDS_API_KEY"):
        return []
    from datetime import datetime, timedelta
    frm = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (datetime.utcnow() + timedelta(hours=28)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for sport_key in SPORTS_MAP.values():
        try:
            payload = fetch_odds(sport=sport_key, commence_time_from=frm,
                                 commence_time_to=to)
        except Exception as e:
            logger.warning(f"get_live_odds {sport_key}: {e}")
            continue
        if payload:
            rows.extend(oddsapi_to_records(payload))
    logger.info(f"get_live_odds: {len(rows)} quote normalizzate")
    return rows

def get_quota():
    """Crediti residui dall'ultimo scan (dalle cache, costo zero)."""
    remaining = []
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("toa_*.json"):
            try:
                d = json.loads(f.read_text())
                if d.get("remaining") is not None:
                    remaining.append(int(d["remaining"]))
            except Exception:
                continue
    if not remaining:
        return None
    return min(remaining), len(remaining)
