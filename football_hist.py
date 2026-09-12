"""
Raccolta risultati storici da API-Football (api-sports.io, v3).

Popola la tabella match_results con le partite reali gia' giocate: alimenta i
rating dinamici (rating_engine) e il backtest dei segnali. Uso previsto:
    python football_hist.py sync --seasons 2
Il free plan di API-Football concede 100 richieste/giorno su tutti gli endpoint.
"""

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from config import load_dotenv
from leagues_data import ALL_LEAGUES
from rating_engine import compute_ratings

# garantisce che .env sia caricato anche quando questo modulo e' eseguito
# direttamente come CLI (python football_hist.py sync)
load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://v3.football.api-sports.io"

# mappa lega locale -> league id API-Football (v3).
#
# COPERTURA ESTESA (11/09/2026): prima c'erano solo 8 leghe, mentre SX Bet
# scansiona decine di competizioni. Le leghe qui sotto sono quelle che (a)
# SX ha davvero in pancia e (b) hanno gia' un roster in `leagues_data`
# (senza roster `_match_db_name` non allinea le squadre e la sync salva 0
# righe). Senza questa estensione il modello restava CIECO su ~2/3 delle
# partite scansionate (profilo neutro di lega).
LEAGUE_IDS: Dict[str, int] = {
    # Top campionati (storico profondo)
    "Serie A": 135,
    "Premier League": 39,
    "La Liga": 140,
    "Bundesliga": 78,
    "Ligue 1": 61,
    "Eredivisie": 88,
    "MLS": 253,
    "Brasileirao": 71,
    # Coppe europee
    "Champions League": 2,
    "Europa League": 3,
    "Conference League": 848,
    # Cadetterie / campionati che SX scansiona di frequente
    "EFL Championship": 40,
    "League One": 41,
    "Serie B": 136,
    "Primeira Liga": 94,
    "Liga MX": 262,
    "J1 League": 98,
    "K League 1": 292,
    "Saudi Pro League": 307,
    "Swiss Super League": 207,
    "Superliga Danimarca": 119,
    "Allsvenskan": 113,
    "Eliteserien": 103,
    "A-League": 188,
    "Veikkausliiga": 244,
    "Argentina Primera": 128,
    "Chile Primera": 265,
    "Colombia Primera": 239,
    "Egyptian Premier League": 233,
    "Copa Libertadores": 13,
    "Copa Sudamericana": 11,
    # Leghe aggiunte l'11/09/2026 insieme ai loro roster (erano i buchi di
    # copertura: SX le scansiona, `ALL_LEAGUES` non le aveva -> 0 righe).
    "Austrian Bundesliga": 218,
    "Russian Premier League": 235,
    "Turkey Super Lig": 203,
    "Belgian First Div": 144,
    "Scottish Premiership": 179,
    "Greek Super League": 197,
    "Polish Ekstraklasa": 106,
    "3. Liga": 80,
    "Sweden Superettan": 114,
    "Brazil Serie B": 72,
    "League One": 41,
}

# Attesa su nome (e paese, se indicato) della lega restituita dall'API:
# frammenti minuscoli, confrontati in contenimento reciproco. Serve a NON
# importare la lega sbagliata se un id e' sbagliato: la sync salta la lega e
# logga il mismatch (nessun dato inquinato). Le voci senza paese non lo
# verificano; una verifica troppo stretta fa solo perdere quella lega (che
# resta vuota come oggi), mai dati sbagliati.
LEAGUE_API: Dict[str, Tuple[str, str]] = {
    "Serie A": ("serie a", "italy"),
    "Premier League": ("premier league", "england"),
    # ⚠️ Nome/paese verificati DAL VIVO il 12/09/2026: un'attesa sbagliata fa
    # SCARTARE la lega da sync_history (_league_response_ok) senza importare
    # nulla. "laliga" non e' contenuto in "La Liga" -> La Liga veniva saltata.
    "La Liga": ("la liga", "spain"),
    "Bundesliga": ("bundesliga", "germany"),
    "Ligue 1": ("ligue 1", "france"),
    "Eredivisie": ("eredivisie", "netherlands"),
    "MLS": ("major league soccer", "usa"),
    "Brasileirao": ("serie a", "brazil"),
    "Champions League": ("champions league", "world"),
    "Europa League": ("europa league", "world"),
    "Conference League": ("conference league", "world"),
    "EFL Championship": ("championship", "england"),
    "League One": ("league one", "england"),
    "Serie B": ("serie b", "italy"),
    "Primeira Liga": ("primeira liga", "portugal"),
    "Liga MX": ("liga mx", "mexico"),
    "J1 League": ("j1 league", "japan"),
    "K League 1": ("k league 1", "south-korea"),
    "Saudi Pro League": ("pro league", "saudi-arabia"),
    "Swiss Super League": ("super league", "switzerland"),
    "Superliga Danimarca": ("superliga", "denmark"),
    "Allsvenskan": ("allsvenskan", "sweden"),
    "Eliteserien": ("eliteserien", "norway"),
    "A-League": ("a-league", "australia"),
    "Veikkausliiga": ("veikkausliiga", "finland"),
    # API: "Liga Profesional Argentina" (il nome non contiene "primera"):
    # si valida il solo PAESE (un id sbagliato punta a un altro paese).
    "Argentina Primera": ("", "argentina"),
    "Chile Primera": ("primera", "chile"),
    "Colombia Primera": ("primera", "colombia"),
    "Egyptian Premier League": ("premier", "egypt"),
    "Copa Libertadores": ("libertadores", "world"),
    "Copa Sudamericana": ("sudamericana", "world"),
    # Nome API incerto -> si valida il PAESE (stringa vuota = nessun check sul
    # nome): basta a intercettare un id sbagliato.
    "Austrian Bundesliga": ("bundesliga", "austria"),
    "Russian Premier League": ("premier league", "russia"),
    # API: "Süper Lig" (la dieresi rompe il confronto per contenimento con
    # "super lig"): si valida il solo PAESE.
    "Turkey Super Lig": ("", "turkey"),
    "Belgian First Div": ("", "belgium"),
    "Scottish Premiership": ("premiership", "scotland"),
    "Greek Super League": ("super league", "greece"),
    "Polish Ekstraklasa": ("ekstraklasa", "poland"),
    "3. Liga": ("3. liga", "germany"),
    "Sweden Superettan": ("superettan", "sweden"),
    "Brazil Serie B": ("serie b", "brazil"),
    "League One": ("league one", "england"),
}

# Marker di sincronizzazione (una riga per lega+stagione completata): evita
# di ri-scaricare ogni giorno decine di leghe (il free plan ha 100 richieste
# al giorno). `FORCE_HISTORY_SYNC=1` ignora i marker.
MIN_ROWS_SYNCED = 1
STATE_TABLE = "sync_state"

# Memo in-process della prima stagione accessibile col piano attuale: la
# prima lega prova l'anno corrente (che il free plan rifiuta) e le successive
# partono direttamente da li'. Azzerabile con `reset_sync_state()`.
_SYNC_STATE: Dict[str, Optional[int]] = {"first_year": None}

# status API-Football trattati come "partita finita"
FINISHED_STATUSES = {"FT", "AET", "PEN", "FF"}

# quanti anni di stagioni sono recuperabili col free plan (indicativo)
DEFAULT_SEASONS = 2


def _env(name: str) -> str:
    exact = os.getenv(name)
    if exact is not None:
        return exact.strip()
    for k, v in os.environ.items():
        if k.strip() == name:
            return v.strip()
    return ""


MAX_RETRIES = 3        # tentativi per errore transitorio (rate-limit/rete)
RETRY_BACKOFF = 3      # secondi tra i tentativi (x2 a ogni retry)
# Retry consecutivi consentiti dal ciclo per-stagione di sync_history PRIMA di
# passare all'anno precedente. Il retry con backoff vive gia' dentro _api_get
# (MAX_RETRIES tentativi HTTP); senza questa guardia un errore persistente
# (es. body None dopo i 3 tentativi HTTP, o errori JSON non "plan") faceva
# girare il ramo "retry" all'infinito bloccando l'intero job
# (loop "Retry stesso anno per Serie A 2024", osservato in produzione il
# 08/09/2026: ~50 righe/sec di log per oltre 25 minuti).
MAX_YEAR_RETRIES = 3


# Rate limit del piano free: 10 richieste al MINUTO (oltre a 100/giorno).
# Un burst (es. `--verify-ids` su 41 leghe) prende 429 a raffica e brucia
# richieste nei retry: le chiamate vanno DISTANZIATE. Il ritmo e'
# configurabile in secondi (env `API_FOOTBALL_MIN_INTERVAL`, 0 = nessun
# ritardo) e vale per OGNI chiamata API-Football del processo, incluso il
# fallback di settlement in `sx_signals`.
MIN_INTERVAL_SECONDS = 6.5   # ~9 richieste/minuto: sotto il limite di 10
_last_call_ts = [0.0]        # timestamp monotono dell'ultima chiamata


def api_min_interval() -> float:
    """Secondi minimi fra due chiamate API-Football (env, default 6.5)."""
    raw = _env("API_FOOTBALL_MIN_INTERVAL")
    if not raw:
        return MIN_INTERVAL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return MIN_INTERVAL_SECONDS


def _throttle() -> None:
    """Attende il tempo necessario per non superare il limite per minuto."""
    interval = api_min_interval()
    if interval <= 0:
        return
    wait = interval - (time.monotonic() - _last_call_ts[0])
    if wait > 0:
        time.sleep(wait)
    _last_call_ts[0] = time.monotonic()


def reset_throttle() -> None:
    """Azzera il timer del throttle (usato dai test)."""
    _last_call_ts[0] = 0.0


def _api_get(path: str, params: Dict) -> Optional[dict]:
    """GET con header x-apisports-key. Ritorna il body json (o None).

    Gli errori transitori (429 rate-limit, 503, eccezioni di rete) vengono
    ritentati con backoff esponenziale, cosi' una corsa lunga /sync non perde
    intere leghe per un blocco temporaneo del free plan.
    """
    key = _env("API_FOOTBALL_KEY")
    if not key:
        logger.warning("API_FOOTBALL_KEY mancante")
        return None
    for attempt in range(MAX_RETRIES):
        try:
            _throttle()  # ogni tentativo e' una richiesta: va distanziato
            r = requests.get(f"{BASE_URL}/{path}", headers={"x-apisports-key": key},
                             params=params, timeout=30)
            if r.status_code in (401, 403):
                logger.warning(f"API-Football bloccata (codice {r.status_code}), key non valida o non attiva")
                return None
            if r.status_code in (429, 503):
                wait = RETRY_BACKOFF * (2 ** attempt)
                logger.info(f"Rate-limit API-Football ({r.status_code}), retry {attempt+1}/{MAX_RETRIES} tra {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"Errore rete API-Football {path}: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
            return None
        except Exception as e:
            logger.warning(f"Errore API-Football {path}: {e}")
            return None
    logger.warning(f"API-Football {path}: rate-limit persistente dopo {MAX_RETRIES} tentativi")
    return None


def _match_db_name(api_name: str, league: str) -> Optional[str]:
    """Allinea il nome squadra API-Football ai nomi nel database locale.

    Prima cerca nella lega indicata; se non trova (es. squadra promossa o
    retrocessa che nel DB vive in un'altra lega), fa un fallback globale su
    tutte le leghe con priorita' al match esatto.
    """
    a = api_name.strip()
    al = a.lower()
    if not al:
        return None

    def _search(teams):
        # prima il match esatto, poi il contenimento bilaterale
        for team in teams:
            if team.lower() == al:
                return team
        for team in teams:
            tl = team.lower()
            if tl in al or al in tl:
                return team
        return None

    found = _search(ALL_LEAGUES.get(league, {}))
    if found:
        return found
    # fallback globale: esatto su tutte le leghe, poi contenimento
    for lname, teams in ALL_LEAGUES.items():
        for team in teams:
            if team.lower() == al:
                return team
    for lname, teams in ALL_LEAGUES.items():
        for team in teams:
            tl = team.lower()
            if tl in al or al in tl:
                return team
    return None


def fetch_fixtures(league_id: int, season: int) -> List[dict]:
    """Recupera le fixtures di una lega/season. Ritorna la lista fixture.

    Nota: su /fixtures il parametro 'page' NON esiste (l'API risponde
    'The Page field do not exist.'); l'endpoint restituisce tutte le partite
    della stagione in una sola risposta.
    """
    body = _api_get("fixtures", {"league": league_id, "season": season})
    if not body or body.get("results", 0) == 0:
        return []
    return body.get("response", [])


def _save_results_batch(rows: List[Tuple], conn=None) -> None:
    """Inserisce in blocco nella tabella match_results (una transazione)."""
    if not rows:
        return
    own_conn = conn is None
    if own_conn:
        from tracker import DB_PATH
        conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS match_results (
        match_id TEXT PRIMARY KEY, league TEXT, home_team TEXT, away_team TEXT,
        score_home INTEGER, score_away INTEGER, result TEXT, settled_at TEXT)''')
    # risultato 1/X/2 per risolvere il vincitore in /risultati
    def _res(sh, sa):
        return "1" if sh > sa else ("2" if sh < sa else "X")
    c.executemany(
        '''INSERT OR REPLACE INTO match_results VALUES (?,?,?,?,?,?,?,?)''',
        [(mid, lg, h, a, sh, sa, _res(sh, sa), ts) for mid, lg, h, a, sh, sa, ts in rows])
    conn.commit()
    if own_conn:
        conn.close()


def _parse_fixture(fx: dict, league: str) -> Optional[Tuple]:
    """
    Estrae (match_id, home, away, sh, sa, date) da una fixture finita.
    Ritorna None se non e' una vittoria regolare con punteggio valido.
    """
    fixture = fx.get("fixture") or {}
    match_id = fixture.get("id")
    if match_id is None:
        return None
    goals = fx.get("goals") or {}
    sh, sa = goals.get("home"), goals.get("away")
    if sh is None or sa is None:
        return None
    try:
        sh = int(sh); sa = int(sa)
    except (TypeError, ValueError):
        return None
    home_api = ((fx.get("teams") or {}).get("home") or {}).get("name", "")
    away_api = ((fx.get("teams") or {}).get("away") or {}).get("name", "")
    home = _match_db_name(home_api, league)
    away = _match_db_name(away_api, league)
    if not home or not away or home == away:
        return None
    date = fixture.get("date") or ""
    return match_id, home, away, sh, sa, date


def _season_status(fx_body: Optional[dict]) -> str:
    """Classifica l'esito della chiamata fixtures per una stagione.

    Ritorna uno di:
      "ok"        -> stagione disponibile e con dati
      "skip"      -> stagione non esposta dal free plan (errore 'plan')
      "retry"     -> errore transitorio (rate-limit / rete): da ritentare
    """
    if fx_body is None:
        return "retry"
    errs = fx_body.get("errors")
    if not errs:
        return "ok"
    joined = " ".join(str(v).lower() for v in errs.values() if v)
    if "plan" in joined or "free" in joined:
        return "skip"
    return "retry"


def _fragment_ok(expected: str, got: str) -> bool:
    """Confronto per contenimento reciproco (minuscolo, spazi normalizzati)."""
    e = " ".join(str(expected or "").lower().split())
    g = " ".join(str(got or "").lower().split())
    if not e:
        return True
    return e in g or g in e


def _league_response_ok(fixtures: List[dict], league: str) -> bool:
    """La risposta API e' davvero della lega attesa?

    Il body di /fixtures porta l'oggetto `league` (nome + paese) in ogni
    partita: si valida la PRIMA. Se il campo non c'e' (stub/API vecchia) NON
    si blocca: si valida solo cio' che l'API espone. Con un id sbagliato
    questo check evita di importare un'altra competizione sotto il nome
    sbagliato (l'errore piu' pericoloso per i rating).
    """
    exp = LEAGUE_API.get(league)
    if not exp:
        return True
    lg = (fixtures[0].get("league") or {}) if fixtures else {}
    if not lg:
        return True            # API/stub senza metadati: nessuna verifica
    name = lg.get("name") or ""
    country = lg.get("country") or ""
    if not _fragment_ok(exp[0], name):
        return False
    if exp[1] and country and not _fragment_ok(exp[1], country):
        return False
    return True


def _state_conn():
    from tracker import DB_PATH as _TRACKER_DB
    conn = sqlite3.connect(str(_TRACKER_DB))
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
        league TEXT, season INTEGER, rows INTEGER, synced_at TEXT,
        PRIMARY KEY (league, season))''')
    return conn


def _mark_synced(league: str, season: int, rows: int) -> None:
    try:
        conn = _state_conn()
        conn.execute(f"INSERT OR REPLACE INTO {STATE_TABLE} VALUES (?,?,?,?)",
                     (league, int(season), int(rows),
                      datetime.now(timezone.utc).isoformat()))
        conn.commit(); conn.close()
    except Exception as e:  # pragma: no cover - difensivo
        logger.warning(f"sync_state non aggiornabile ({league} {season}): {e}")


def _is_synced(league: str, seasons: int) -> bool:
    """Vero se la lega ha gia' `seasons` stagioni completate sul volume.

    Evita di ri-scaricare ogni giorno decine di leghe (free plan: 100
    richieste/giorno). `FORCE_HISTORY_SYNC=1` ignora i marker.
    """
    if _env("FORCE_HISTORY_SYNC").lower() in ("1", "true", "yes", "on"):
        return False
    try:
        conn = _state_conn()
        n = conn.execute(f"SELECT COUNT(*) FROM {STATE_TABLE} WHERE league=?",
                         (league,)).fetchone()[0]
        conn.close()
        return int(n or 0) >= int(seasons)
    except Exception:
        return False


def reset_sync_state() -> None:
    """Azzera il memo in-process delle stagioni accessibili (usato dai test)."""
    _SYNC_STATE["first_year"] = None


def verify_league_ids(leagues: Optional[List[str]] = None) -> Dict[str, dict]:
    """Verifica id <-> nome/paese con UNA chiamata per lega (/leagues?id=).

    Da usare dopo aver aggiunto/modificato un id: se `ok` e' False la lega
    verrebbe SALTATA dalla sync (nessun dato sbagliato importato).
    """
    out: Dict[str, dict] = {}
    for league in (leagues or list(LEAGUE_IDS)):
        lid = LEAGUE_IDS.get(league)
        if not lid:
            continue
        body = _api_get("leagues", {"id": lid})
        resp = (body or {}).get("response") or []
        if not resp:
            out[league] = {"id": lid, "ok": False,
                           "error": (body or {}).get("errors") or "nessuna risposta"}
            continue
        got = resp[0]
        name = ((got.get("league") or {}).get("name") or "")
        country = ((got.get("country") or {}).get("name") or "")
        exp = LEAGUE_API.get(league, (league, ""))
        ok = _fragment_ok(exp[0], name) and (
            not exp[1] or _fragment_ok(exp[1], country))
        out[league] = {"id": lid, "ok": ok, "api_name": name,
                       "api_country": country,
                       "expected": {"name": exp[0], "country": exp[1]}}
    return out


def sync_history(seasons: int = DEFAULT_SEASONS, leagues: Optional[List[str]] = None) -> Dict:
    """
    Scarica i risultati storici e li salva in match_results (INSERT OR REPLACE),
    poi ricalcola i rating.

    Il free plan di API-Football espone solo le stagioni dal 2022 al 2024; le
    stagioni piu' recenti vengono saltate (l'API risponde con un errore 'plan')
    e si scende finche' non si raccolgono 'seasons' stagioni accessibili.
    Ritorna un riepilogo per lega.
    """
    if not _env("API_FOOTBALL_KEY"):
        return {"error": "API_FOOTBALL_KEY mancante"}
    target = leagues or list(LEAGUE_IDS.keys())
    current_year = time.localtime().tm_year
    summary = {}
    total = 0
    skipped = 0
    for league in target:
        lid = LEAGUE_IDS.get(league)
        if not lid:
            continue
        # Già sincronizzata sul volume: l'API-Football si chiama UNA volta
        # per lega nella vita, non ogni giorno (free plan: 100 req/giorno).
        if _is_synced(league, seasons):
            logger.info("sync: %s gia' sincronizzata (%d stagioni), salto",
                        league, seasons)
            skipped += 1
            continue
        done = 0
        collected = 0
        # Memo in-process: la prima lega scopre dove comincia la copertura del
        # piano, le successive partono da li' (meno richieste sprecate).
        year = min(current_year, _SYNC_STATE["first_year"] or current_year)
        rows = []
        completed = []    # (stagione, righe) da marcare DOPO il salvataggio
        year_retries = 0  # retry consecutivi sulla stessa (lega, stagione)
        while collected < seasons and year >= 2018:
            body = _api_get("fixtures", {"league": lid, "season": year})
            status = _season_status(body)
            if status == "retry":
                # Errore transitorio (rate-limit/rete): riprova la stessa
                # stagione, ma con LIMITE MASSIMO RIGIDO (MAX_YEAR_RETRIES).
                # Un errore persistente non deve mai bloccare il job: dopo il
                # limite si tratta la stagione come non accessibile e si passa
                # a quella precedente.
                year_retries += 1
                if year_retries > MAX_YEAR_RETRIES:
                    logger.warning(
                        "Stagione %s non disponibile (%s): %d retry "
                        "consecutivi falliti, salto all'anno precedente",
                        year, league, MAX_YEAR_RETRIES)
                    year_retries = 0
                    year -= 1
                    continue
                logger.info(
                    "Retry stesso anno per %s %s (status retry) %d/%d",
                    league, year, year_retries, MAX_YEAR_RETRIES)
                time.sleep(RETRY_BACKOFF)  # gentilezza verso il rate limit
                continue
            year_retries = 0
            if status == "skip":
                logger.info(f"Stagione {year} non accessibile ({league}), salto")
                # Il piano non copre questo anno: memorizzo il limite per le
                # leghe successive (evita 2 richieste sprecate per lega).
                hint = year - 1
                prev = _SYNC_STATE["first_year"]
                if prev is None or hint < prev:
                    _SYNC_STATE["first_year"] = hint
                year -= 1
                continue
            fixtures = (body or {}).get("response", []) or []
            # L'id risponde per un'ALTRA competizione (id sbagliato): si salta
            # la lega senza salvare nulla. Mai importare dati sbagliati.
            if fixtures and not _league_response_ok(fixtures, league):
                lg = (fixtures[0].get("league") or {})
                logger.warning(
                    "sync: id %s NON e' '%s' (API: %s - %s); lega saltata",
                    lid, league, lg.get("country"), lg.get("name"))
                break
            rows_before = len(rows)
            for fx in fixtures:
                status_short = (((fx.get("fixture") or {}).get("status") or {}).get("short")) or ""
                if status_short not in FINISHED_STATUSES:
                    continue
                parsed = _parse_fixture(fx, league)
                if not parsed:
                    continue
                mid, home, away, sh, sa, date = parsed
                rows.append((f"{league}-{mid}", league, home, away, sh, sa, date))
                done += 1
            collected += 1
            completed.append((year, len(rows) - rows_before))
            year -= 1
            time.sleep(1)  # gentilezza verso il limite di rate
        _save_results_batch(rows)
        # Marker scritti SOLO dopo il salvataggio riuscito: se il processo
        # muore a meta' la stagione viene ritentata al giro successivo.
        for y, n in completed:
            _mark_synced(league, y, n)
        # La lega e' stata TENTATA (non saltata): compare sempre nel
        # riepilogo, anche con 0 righe (segnale di problema, non silenzio).
        summary[league] = done
        total += done
    if total:
        try:
            compute_ratings()
        except Exception as e:
            logger.warning(f"Errore ricalcolo rating: {e}")
    if skipped:
        logger.info("sync: %d leghe gia' sincronizzate saltate", skipped)
    return {**summary, "_total": total, "_skipped": skipped}


def run_sync(seasons: int = DEFAULT_SEASONS, leagues: Optional[List[str]] = None) -> str:
    """Endpoint CLI/Telegram: esegue la sincronizzazione e produce un riepilogo."""
    if not _env("API_FOOTBALL_KEY"):
        return "❌ *API_FOOTBALL_KEY mancante.* Imposta la variabile per sincronizzare i risultati storici.\n\n`/sync` popola il database con le partite giocate: alimenta i rating dinamici e il backtest."
    result = sync_history(seasons, leagues)
    if isinstance(result, dict) and result.get("error"):
        return f"❌ *Errore:* {result['error']}"
    lines = []
    for k, v in result.items():
        if k.startswith("_"):
            continue
        lines.append(f"• *{k}*: {v} partite salvate")
    total = result.get("_total", 0)
    skipped = result.get("_skipped", 0)
    tail = ""
    if skipped:
        tail = (f"\nℹ️ {skipped} leghe erano gia' sincronizzate "
                "(nessuna richiesta sprecata).")
    return (
        "🔄 *SINCRONIZZAZIONE RISULTATI STORICI*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        + ("\n".join(lines) if lines else "• nessuna lega da sincronizzare")
        + f"\n\n✅ Totale: {total} partite | Rating ricalcolati.\n"
        + tail
        + "Usa `/backtest` e `/risultati` per vedere i dati."
    )


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, default=DEFAULT_SEASONS)
    ap.add_argument("--league", nargs="*", default=None)
    ap.add_argument("--verify-ids", action="store_true",
                    help="verifica id<->nome lega con l'API (nessuna scrittura)")
    ap.add_argument("--reset-markers", action="store_true",
                    help="ignora i marker di sincronizzazione (riscarica)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.reset_markers:
        os.environ["FORCE_HISTORY_SYNC"] = "1"
    if args.verify_ids:
        res = verify_league_ids(args.league)
        bad = 0
        for lg, info in res.items():
            if info.get("ok"):
                print(f"OK   {lg:<26} id={info['id']:<5} "
                      f"API='{info.get('api_name')}' ({info.get('api_country')})")
            else:
                bad += 1
                print(f"BAD  {lg:<26} id={info['id']:<5} "
                      f"API='{info.get('api_name','')}' "
                      f"({info.get('api_country','')}) err={info.get('error','')}")
        print(f"\n{len(res) - bad}/{len(res)} id verificati")
        raise SystemExit(0 if bad == 0 else 1)
    print(run_sync(args.seasons, args.league))