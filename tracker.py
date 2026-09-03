"""Tracker SQLite per segnali, calendario e analisi"""
import logging
import math
import sqlite3
import os
from datetime import datetime
from pathlib import Path

from config import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "quotaverace.db"

# Ordine di preferenza dei verdetti nel dedup (prima = meglio): il verdetto
# definitivo batte quello provvisorio. Le righe APERTE (esito_finale NULL)
# sono trattate come 'lost' provvisorio: qualsiasi riga gia' chiusa la batte.
PREFERRED_OUTCOME_ORDER = {"won": 0, "push": 1, "lost": 2}

class Signal:
    def __init__(self, id, chat_id, evento, esito, quota, probabilita, ev, timestamp, esito_finale, profit):
        self.id = id; self.chat_id = chat_id; self.evento = evento; self.esito = esito
        self.quota = quota; self.probabilita = probabilita; self.ev = ev
        self.timestamp = timestamp; self.esito_finale = esito_finale; self.profit = profit

def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER, evento TEXT, esito TEXT, quota REAL,
        probabilita REAL, ev REAL, timestamp TEXT, esito_finale TEXT, profit REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY)''')
    # Migrazione free/premium: colonne tier e scadenza abbonamento.
    sub_cols = [r[1] for r in c.execute("PRAGMA table_info(subscribers)")]
    if "tier" not in sub_cols:
        c.execute("ALTER TABLE subscribers ADD COLUMN tier TEXT DEFAULT 'free'")
    if "premium_until" not in sub_cols:
        c.execute("ALTER TABLE subscribers ADD COLUMN premium_until TEXT")
    c.execute('''CREATE TABLE IF NOT EXISTS matches (
        id TEXT PRIMARY KEY, league TEXT, home_team TEXT, away_team TEXT,
        commence_time TEXT, status TEXT, last_updated TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS match_analysis (
        id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT,
        lam_h REAL, lam_a REAL, prob_1 REAL, prob_X REAL, prob_2 REAL,
        prob_over REAL, best_ev REAL, best_esito TEXT, best_quota REAL,
        best_bookmaker TEXT, status TEXT, timestamp TEXT,
        market_prob REAL, market_edge REAL,
        FOREIGN KEY (match_id) REFERENCES matches(id))''')
    # Migrazione per DB esistenti: aggiunge le colonne mercato se mancano.
    cols = [r[1] for r in c.execute("PRAGMA table_info(match_analysis)")]
    if "market_prob" not in cols:
        c.execute("ALTER TABLE match_analysis ADD COLUMN market_prob REAL")
    if "market_edge" not in cols:
        c.execute("ALTER TABLE match_analysis ADD COLUMN market_edge REAL")
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        match_id TEXT, date TEXT, PRIMARY KEY (match_id, date))''')
    c.execute('''CREATE TABLE IF NOT EXISTS clv_history (
        match_id TEXT, esito TEXT,
        signal_quota REAL, closing_quota REAL, updated_at TEXT,
        pinnacle_quota REAL,
        PRIMARY KEY (match_id, esito))''')
    clv_cols = [r[1] for r in c.execute("PRAGMA table_info(clv_history)")]
    if "pinnacle_quota" not in clv_cols:
        c.execute("ALTER TABLE clv_history ADD COLUMN pinnacle_quota REAL")
    c.execute('''CREATE TABLE IF NOT EXISTS cassa (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data TEXT, partita TEXT, esito TEXT, quota REAL,
        importo REAL, ev REAL, timestamp TEXT)''')
    # Migrazione: colonne di saldo (risultato reale + profitto realizzato).
    cassa_cols = [r[1] for r in c.execute("PRAGMA table_info(cassa)")]
    if "esito_finale" not in cassa_cols:
        c.execute("ALTER TABLE cassa ADD COLUMN esito_finale TEXT")
    if "profit" not in cassa_cols:
        c.execute("ALTER TABLE cassa ADD COLUMN profit REAL")
    if "settled_at" not in cassa_cols:
        c.execute("ALTER TABLE cassa ADD COLUMN settled_at TEXT")
    # Ledger previsioni: TUTTI i segnali proposti dal motore, con mercato,
    # saldati a fine partita (esito_finale) per calibrare il modello.
    c.execute('''CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT, mercato TEXT, esito TEXT,
        quota REAL, prob REAL, ev REAL,
        market_prob REAL, market_edge REAL,
        status TEXT, esito_finale TEXT, profit REAL,
        created_at TEXT, settled_at TEXT,
        UNIQUE(match_id, mercato, esito))''')
    # Migrazione idempotente per DB nati prima di settled_at (usato da
    # settle_predictions, settle_bets e drift_monitor): stessa convenzione
    # delle colonne di saldo su cassa.
    _pred_cols = [r[1] for r in c.execute("PRAGMA table_info(predictions)")]
    if "settled_at" not in _pred_cols:
        c.execute("ALTER TABLE predictions ADD COLUMN settled_at TEXT")
    # Puntate automatiche su Betfair (auto_bet.py): ordini reali o dry-run,
    # saldati a fine partita come le previsioni.
    c.execute('''CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT, mercato TEXT, esito TEXT,
        market_id TEXT, selection_id INTEGER,
        price REAL, stake REAL, mode TEXT, status TEXT, bet_id TEXT,
        esito_finale TEXT, profit REAL,
        created_at TEXT, settled_at TEXT,
        UNIQUE(match_id, esito))''')
    _bet_cols = [r[1] for r in c.execute("PRAGMA table_info(bets)")]
    if "settled_at" not in _bet_cols:
        c.execute("ALTER TABLE bets ADD COLUMN settled_at TEXT")
    _ensure_unique_constraints(c)
    conn.commit()
    return conn


def _dedupe_normalized_esito(c) -> int:
    """Dedup dei ledger per chiave NORMALIZZATA (match_id, mercato, esito).

    I segnali 1X2 usano il nome squadra (es. "Inter") mentre il salvataggio
    delle puntate usa la chiave compatta ("1"), e l'OU arriva come "Over 2.5"
    o "over" a seconda del percorso: senza normalizzazione il dedup non li
    riconosce come lo stesso segnale. Ricerca per gruppo NORMALIZZATO e
    conserva la riga migliore (PREFERRED_OUTCOME_ORDER).

    Ritorna il numero di righe eliminate. Chiamata da _ensure_unique_constraints
    (solo dove serve: la normalizzazione puo' generare conflitti NUOVI).
    """
    removed = 0
    try:
        groups = c.execute(
            '''SELECT match_id, mercato, LOWER(TRIM(esito)), COUNT(*) n
               FROM predictions
               WHERE mercato IN ('1X2', 'OU')
               GROUP BY match_id, mercato, LOWER(TRIM(esito))
               HAVING COUNT(*) > 1''').fetchall()
    except sqlite3.OperationalError:
        return 0
    for mid, mkt, es, n in groups:
        rows = c.execute(
            '''SELECT id, esito, esito_finale, profit FROM predictions
               WHERE match_id=? AND mercato=? AND LOWER(TRIM(esito))=?
               ORDER BY id''', (mid, mkt, es)).fetchall()
        if len(rows) <= 1:
            continue
        def _rank(row):
            _id, esito, outcome, profit = row
            if outcome is not None:
                return (0, PREFERRED_OUTCOME_ORDER.get(outcome, 3), -_id)
            return (1, PREFERRED_OUTCOME_ORDER.get("lost"), -_id)
        best_id = min(rows, key=_rank)[0]
        for r_id, _, _, _ in rows:
            if r_id != best_id:
                c.execute("DELETE FROM predictions WHERE id=?", (r_id,))
                removed += 1
    return removed


def _create_ledger_table(c, table: str) -> None:
    """CREATE TABLE (IF NOT EXISTS) per i ledger con i vincoli UNIQUE.

    Unico punto di definizione dello schema: usato da _get_conn, dal
    recupero delle migrazioni interrotte e dalla migrazione stessa.
    """
    if table == "predictions":
        c.execute('''CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT, mercato TEXT, esito TEXT,
            quota REAL, prob REAL, ev REAL,
            market_prob REAL, market_edge REAL,
            status TEXT, esito_finale TEXT, profit REAL,
            created_at TEXT, settled_at TEXT,
            UNIQUE(match_id, mercato, esito))''')
    elif table == "bets":
        c.execute('''CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT, mercato TEXT, esito TEXT,
            market_id TEXT, selection_id INTEGER,
            price REAL, stake REAL, mode TEXT, status TEXT, bet_id TEXT,
            esito_finale TEXT, profit REAL,
            created_at TEXT, settled_at TEXT,
            UNIQUE(match_id, esito))''')


def _ensure_unique_constraints(c) -> None:
    """Garantisce UNIQUE(match_id, mercato, esito) su predictions e
    UNIQUE(match_id, esito) su bets ANCHE sui DB creati prima che i vincoli
    esistessero nel codice (i CREATE TABLE IF NOT EXISTS non migrano le
    tabelle esistenti): su quei DB un re-run del job poteva duplicare le
    righe e sporcare il dataset ML (audit: hash 36aa024f...).

    Idempotente ed economico: due PRAGMA index_list a ogni connessione.
    Se il vincolo manca: dedup NORMALIZZATO -> ricrea la tabella copiando
    le righe deduplicate. I backup (_old) di migrazioni interrotte vengono
    recuperati PRIMA di qualunque check, a ogni avvio.
    """
    migrations = [
        ("predictions", "uq_predictions_sig", "match_id, mercato, esito"),
        ("bets", "uq_bets_sig", "match_id, esito"),
    ]
    for table, idx_name, cols in migrations:
        # 0) Recupero di una migrazione precedente interrotta: SEMPRE prima
        # di qualunque continue, perche' puo' esserci da recuperare anche
        # quando la tabella nuova esiste gia' (o non esiste proprio).
        #   (a) tabella nuova presente: copia le righe mancanti da _old
        #   (b) tabella nuova ASSENTE (crash tra rename e create): ricreala
        try:
            leftover = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (f"{table}_old",)).fetchone()
        except sqlite3.OperationalError:
            leftover = None
        if leftover:
            try:
                cur_cols = [r[1] for r in c.execute(
                    f"PRAGMA table_info({table})").fetchall()]
                bk_cols = [r[1] for r in c.execute(
                    f"PRAGMA table_info({table}_old)").fetchall()]
                if not cur_cols and bk_cols:
                    _create_ledger_table(c, table)
                    cur_cols = [r[1] for r in c.execute(
                        f"PRAGMA table_info({table})").fetchall()]
                if bk_cols and bk_cols == cur_cols:
                    bkl = ", ".join(bk_cols)
                    c.execute(f"INSERT OR IGNORE INTO {table} ({bkl}) "
                              f"SELECT {bkl} FROM {table}_old")
                    c.execute(f"DROP TABLE {table}_old")
                    logger.warning("tracker: recuperate righe da %s_old "
                                   "(migrazione precedente interrotta)", table)
            except sqlite3.OperationalError as e:
                logger.warning("tracker: recupero %s_old rimandato: %s", table, e)

        # 1) Il vincolo esiste gia'? (nome nostro o autoindex dello schema)
        try:
            idx = [r[1] for r in c.execute(f"PRAGMA index_list({table})").fetchall()]
        except sqlite3.OperationalError:
            continue
        if idx_name in idx:
            continue
        if any(i and i.startswith("sqlite_autoindex") for i in idx):
            continue

        # 2) Vincolo davvero assente: migrazione (mai perdere dati)
        try:
            table_cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            if not table_cols:
                continue
            col_list = ", ".join(table_cols)
            if table == "predictions":
                removed = _dedupe_normalized_esito(c)
                if removed:
                    logger.warning("predictions: eliminate %d righe duplicate "
                                   "(chiave normalizzata)", removed)
            c.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            _create_ledger_table(c, table)
            c.execute(f"INSERT OR IGNORE INTO {table} ({col_list}) "
                      f"SELECT {col_list} FROM {table}_old")
            c.execute(f"DROP TABLE {table}_old")
            logger.info("tracker: applicato UNIQUE(%s) su %s (migrazione schema)",
                        cols, table)
        except sqlite3.OperationalError as e:
            # Tipicamente DB lockato da un'altra connessione. MAI buttare il
            # backup: le righe restano in _old e vengono recuperate al prossimo
            # avvio (blocco 0 in cima a questo loop).
            logger.warning("tracker: migrazione UNIQUE su %s rimandata: %s "
                           "(backup dati in %s_old, recupero al prossimo avvio)",
                           table, e, table)


# --- Cassa (registro scommesse inserite dal sito) ---
def save_cassa_entry(partita, esito, quota, importo, ev=0.0, data=None,
                     esito_finale=None, profit=None, settled_at=None):
    """Registra una scommessa nella cassa."""
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO cassa (data, partita, esito, quota, importo, ev, timestamp,
                                    esito_finale, profit, settled_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?)''',
              (data or datetime.now().strftime("%Y-%m-%d"), partita, esito,
               float(quota), float(importo), float(ev), datetime.now().isoformat(),
               esito_finale, profit, settled_at))
    conn.commit(); conn.close()


def get_cassa():
    """Tutte le scommesse della cassa, dalla piu' recente (compreso il saldo)."""
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT id, data, partita, esito, quota, importo, ev, timestamp,"
              " esito_finale, profit, settled_at FROM cassa ORDER BY id DESC")
    rows = c.fetchall(); conn.close()
    return [
        {"id": r[0], "data": r[1], "partita": r[2], "esito": r[3],
         "quota": r[4], "importo": r[5], "ev": r[6], "timestamp": r[7],
         "esito_finale": r[8], "profit": r[9], "settled_at": r[10]}
        for r in rows
    ]


def _norm_team(name):
    """Normalizza un nome squadra per il match: minuscole, no accenti, no 'fc/cf'."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    return " ".join(w for w in s.split() if w not in ("fc", "cf"))


def _esito_won(esito, sh, sa):
    """True/False se l'esito e' vincente col risultato (sh, sa); None se non riconosciuto."""
    el = str(esito or "").lower().strip()
    if "over" in el:
        return (sh + sa) >= 3
    if "under" in el:
        return (sh + sa) <= 2
    if "btts" in el or "gol gol" in el:
        return sh > 0 and sa > 0
    if el == "1" or "casa" in el:
        return sh > sa
    if el == "2" or "trasferta" in el:
        return sa > sh
    if el == "x" or "pareggio" in el:
        return sh == sa
    return None


# Prefissi club comuni ignorati nel match per nome della cassa
# (_norm_team elimina solo fc/cf: 'CA Osasuna' non agganciava 'Osasuna'
# → la cassa finiva saldata su una partita VECCHIA della stessa coppia).
_CLUB_PREFIXES = ("ca ", "ac ", "as ", "cd ", "fc ", "cf ", "de ",
                   "ss ", "sc ", "us ", "ud ", "sd ", "at ", "sv ")


def _loose_team(name):
    """Normalizzazione TOLERANTE per il match cassa: _norm_team + rimozione
    dei prefissi club comuni (es. 'CA Osasuna' → 'osasuna')."""
    t = _norm_team(name)
    for _ in range(3):
        changed = False
        for p in _CLUB_PREFIXES:
            if t.startswith(p):
                t = t[len(p):].strip()
                changed = True
        if not changed:
            break
    return t or _norm_team(name)


def _goals_sane(sh, sa, result=None) -> bool:
    """True se (sh, sa) è un punteggio finale plausibile (int non negativi).

    Se `result` è dato ('1'/'X'/'2'), verifica anche che sia coerente coi
    gol: una riga con result='1' ma sh < sa è un dato CORROTTO su cui il
    settlement non deve chiudere nulla.
    """
    try:
        sh_i, sa_i = int(sh), int(sa)
    except (TypeError, ValueError):
        return False
    if sh_i < 0 or sa_i < 0:
        return False
    if result is not None:
        expected = "1" if sh_i > sa_i else ("2" if sh_i < sa_i else "X")
        if str(result).strip().upper() != expected:
            return False
    return True


def _esito_possible(mercato, esito, sh, sa, home=None, away=None):
    """Tripwire anti-contraddizione: l'esito PUÒ essere vinto coi gol (sh, sa)?

    Ritorna True/False, oppure None per i mercati non verificabili qui
    (Asian Handicap, esiti sconosciuti). Il settlement deve BLOCCARE la
    chiusura di una scommessa quando il verdetto calcolato è 'won' ma i gol
    rendono l'esito impossibile (es. esito '2' con vittoria casa): è il
    segnale di dati corrotti o di una regressione nel calcolo dell'esito.
    """
    m = str(mercato or "").upper()
    el = str(esito or "").lower().strip()
    if m == "AH":
        return None
    home_n = _norm_team(home) if home else ""
    away_n = _norm_team(away) if away else ""
    el_n = _norm_team(el)
    if m in ("1X2",) or el in ("1", "2", "x", "draw", "pareggio") or \
            (home and el_n == home_n) or (away and el_n == away_n):
        if el == "1" or (home and el_n == home_n):
            return sh > sa
        if el == "2" or (away and el_n == away_n):
            return sa > sh
        if el in ("x", "draw", "pareggio"):
            return sh == sa
        return None
    if "over" in el:
        return (sh + sa) >= 3
    if "under" in el:
        return (sh + sa) <= 2
    if "btts" in el or "gol gol" in el:
        return sh > 0 and sa > 0
    return None


def settle_cassa():
    """Salda le scommesse aperte della cassa con i risultati reali (match_results).

    Idempotente: tocca solo le righe con esito_finale NULL. Il match avviene
    per coppia di squadre normalizzate; le scommesse senza risultato ancora
    disponibile restano in 'in gioco'. Ritorna il numero saldate in questa
    chiamata.

    SANITY CHECK: se i gol registrati sono corrotti (punteggio negativo/
    non numerico) o l'esito calcolato è in contraddizione EVIDENTE coi gol
    (es. esito '2' vinto con vittoria casa), la riga NON viene chiusa: resta
    in gioco e viene loggata, per non pagare un verdetto su dati falsi.
    """
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        rows = c.execute("SELECT id, partita, esito, quota, importo FROM cassa "
                         "WHERE esito_finale IS NULL").fetchall()
        results = c.execute("SELECT home_team, away_team, score_home, score_away, "
                            "settled_at FROM match_results").fetchall()
    finally:
        conn.close()
    norm_map = {}
    for home, away, sh, sa, settled_at in results:
        # Doppia chiave (stretta + loose): 'CA Osasuna' e 'Osasuna' devono
        # agganciare lo stesso match. Se la stessa coppia ha più partite
        # (es. stagioni diverse), si salda sulla PIÙ RECENTE: la cassa si
        # riferisce al match corrente, non a una riga storica (bug 2025).
        for key in ((_norm_team(home), _norm_team(away)),
                    (_loose_team(home), _loose_team(away))):
            if key not in norm_map or (settled_at or "") > (norm_map[key][2] or ""):
                norm_map[key] = (sh, sa, settled_at or "")

    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    settled = blocked = 0
    for cid, partita, esito, quota, importo in rows:
        # "Serie A – Roma vs Empoli" -> ultimo " vs " separa casa/trasferta
        clean = partita.split(" – ")[-1].strip() if " – " in partita else partita
        if " vs " not in clean:
            continue
        parts = clean.split(" vs ")
        home, away = _loose_team(parts[0].strip()), _loose_team(parts[1].strip())
        match = norm_map.get((home, away))
        if match is None:
            continue
        sh, sa = match[0], match[1]
        if not _goals_sane(sh, sa):
            logger.warning("settle_cassa: gol non validi (%r-%r) su %s: "
                           "settlement BLOCCATO (cassa #%d)", sh, sa, partita, cid)
            blocked += 1
            continue
        won = _esito_won(esito, sh, sa)
        if won is None:
            continue
        if won and _esito_possible(None, esito, sh, sa) is False:
            logger.warning("settle_cassa: esito '%s' VINTO in contraddizione coi "
                           "gol %s-%s (%s): settlement BLOCCATO (cassa #%d)",
                           esito, sh, sa, partita, cid)
            blocked += 1
            continue
        profit = round((quota - 1) * importo, 2) if won else round(-importo, 2)
        c.execute("UPDATE cassa SET esito_finale=?, profit=?, settled_at=? WHERE id=?",
                  ("won" if won else "lost", profit, now, cid))
        settled += 1
    conn.commit(); conn.close()
    if blocked:
        logger.warning("settle_cassa: %d righe BLOCCATE dal sanity check", blocked)
    return settled


def cassa_totals(entries=None):
    """Totali cassa: speso, potenziale delle in gioco, P/L realizzato."""
    entries = entries if entries is not None else get_cassa()
    speso = sum(e["importo"] or 0 for e in entries)
    vincita = sum((e["importo"] or 0) * (e["quota"] or 0) for e in entries)
    closed = [e for e in entries if e.get("esito_finale")]
    in_gioco = [e for e in entries if not e.get("esito_finale")]
    vinti = sum(1 for e in closed if e["esito_finale"] == "won")
    persi = sum(1 for e in closed if e["esito_finale"] == "lost")
    profit_real = sum(e.get("profit") or 0 for e in closed)
    speso_chiuso = sum(e["importo"] or 0 for e in closed)
    roi = (profit_real / speso_chiuso * 100) if speso_chiuso else 0.0
    return {"n": len(entries), "totale_speso": round(speso, 2),
            "vincita_potenziale": round(vincita, 2),
            "profit_potenziale": round(vincita - speso, 2),
            "chiusi": len(closed), "in_gioco": len(in_gioco),
            "vinti": vinti, "persi": persi,
            "speso_chiuso": round(speso_chiuso, 2),
            "profit_realizzato": round(profit_real, 2),
            "roi": round(roi, 2)}


# --- Ledger previsioni (segnale proposto -> esito reale -> calibrazione) ---
# Ogni segnale che il motore propone (qualunque mercato: 1X2, Over/Under,
# BTTS, Asian Handicap) viene registrato qui con il suo mercato, cosi' a
# fine partita si puo' verificare torto/ragione per mercato e correggere
# il modello dove sbaglia.

def save_prediction(match_id, mercato, esito, quota, prob, ev,
                    market_prob=None, market_edge=None, status="value"):
    """Registra (o aggiorna, se ancora non chiusa) una previsione del motore.

    Idempotente per (match_id, mercato, esito): a ogni nuova analisi la
    previsione non ancora saldata viene aggiornata con i prezzi correnti;
    quella gia' saldata non viene toccata (per non falsare il record).
    """
    now = datetime.now().isoformat()
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO predictions (match_id, mercato, esito, quota, prob, ev,
                                          market_prob, market_edge, status, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(match_id, mercato, esito) DO UPDATE SET
                   quota=excluded.quota, prob=excluded.prob, ev=excluded.ev,
                   market_prob=excluded.market_prob, market_edge=excluded.market_edge,
                   status=excluded.status, created_at=excluded.created_at
                 WHERE esito_finale IS NULL''',
              (match_id, mercato, esito, float(quota), float(prob), float(ev),
               market_prob, market_edge, status, now))
    conn.commit(); conn.close()


def get_predictions(mercato=None, status=None, closed=None, limit=500):
    conn = _get_conn(); c = conn.cursor()
    q = "SELECT match_id, mercato, esito, quota, prob, ev, market_prob, market_edge, " \
        "status, esito_finale, profit, created_at, settled_at FROM predictions"
    conds, args = [], []
    if mercato:
        conds.append("mercato=?"); args.append(mercato)
    if status:
        conds.append("status=?"); args.append(status)
    if closed is True:
        conds.append("esito_finale IS NOT NULL")
    elif closed is False:
        conds.append("esito_finale IS NULL")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall()
    conn.close()
    return [
        {"match_id": r[0], "mercato": r[1], "esito": r[2], "quota": r[3],
         "prob": r[4], "ev": r[5], "market_prob": r[6], "market_edge": r[7],
         "status": r[8], "esito_finale": r[9], "profit": r[10],
         "created_at": r[11], "settled_at": r[12]}
        for r in rows
    ]


def _ah_halves(line: float):
    """Linea AH quarter -> (due mezze linee, share 0.5); altrimenti (linea, 1.0)."""
    if abs(line * 2) % 1 != 0:  # .25 / .75 -> due mezze puntate
        low = math.floor(line * 2) / 2
        return (low, low + 0.5), 0.5
    return (line,), 1.0


def ah_pnl_units(esito: str, quota: float, adv: int) -> float:
    """P/L (in unita' da 1) di una scommessa AH flat, con split-bet per le quarter.

    esito nel formato "Home -0.75" / "Away +0.25": side + linea del lato.
    Linee intere/mezze = puntata piena; quarter (.25/.75) = due mezze.
    """
    parts = str(esito).split()
    if len(parts) < 2:
        return None
    side_raw = parts[0].lower()
    if side_raw.startswith("home"):
        side = "home"
    elif side_raw.startswith("away") or side_raw.startswith("guest"):
        side = "away"
    else:
        return None
    try:
        line = float(parts[1].replace("+", ""))
    except ValueError:
        return None
    halves, share = _ah_halves(line)
    total = 0.0
    for hline in halves:
        net = (adv + hline) if side == "home" else (-adv + hline)
        if net > 0:
            total += share * (quota - 1)
        elif net < 0:
            total -= share
    return total


def _prediction_outcome(mercato, esito, quota, sh, sa, home, away):
    """Risolve l'esito di una previsione. Ritorna (outcome, profit) o (None, None).

    outcome: 'won' | 'lost' | 'push'. Per l'Asian Handicap il profitto usa
    la logica split-bet (quarter lines = due mezze puntate).
    """
    m = str(mercato or "").upper()
    el = str(esito or "").lower().strip()
    if m == "AH" or any(t in el.split()[:1] for t in ("home", "away")):
        pnl = ah_pnl_units(esito, quota, sh - sa)
        if pnl is None:
            return None, None
        if pnl > 0:
            return "won", round(pnl, 4)
        if pnl == 0:
            return "push", 0.0
        return "lost", round(pnl, 4)
    if "over" in el:
        won = (sh + sa) >= 3
    elif "under" in el:
        won = (sh + sa) <= 2
    elif "btts" in el or "gol gol" in el:
        won = (sh > 0 and sa > 0)
    elif el in ("draw", "pareggio", "x"):
        won = sh == sa
    elif el == "1":
        won = sh > sa
    elif el == "2":
        won = sa > sh
    else:
        # esito = nome squadra (es. "Osasuna")
        hn = _norm_team(home); an = _norm_team(away); en = _norm_team(el)
        if en == hn:
            won = sh > sa
        elif en == an:
            won = sa > sh
        else:
            return None, None
    return ("won" if won else "lost"), round((quota - 1) if won else -1.0, 4)


def settle_predictions():
    """Salda le previsioni aperte coi risultati reali (idempotente).

    Ritorna (saldate, push): numero di previsioni chiuse in questa chiamata
    e quante di queste si sono concluse in push (es. handicap pari).
    """
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        open_rows = c.execute("SELECT id, match_id, mercato, esito, quota FROM predictions "
                              "WHERE esito_finale IS NULL").fetchall()
        results = c.execute("SELECT match_id, home_team, away_team, score_home, score_away "
                            "FROM match_results").fetchall()
    finally:
        conn.close()
    res_map = {r[0]: r[1:] for r in results}

    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    settled = pushes = blocked = 0
    for pid, match_id, mercato, esito, quota in open_rows:
        r = res_map.get(match_id)
        if not r:
            continue
        home, away, sh, sa = r
        if not _goals_sane(sh, sa):
            logger.warning("settle_predictions: gol non validi (%r-%r) su "
                           "match %s: settlement BLOCCATO (pred #%d)",
                           sh, sa, match_id, pid)
            blocked += 1
            continue
        outcome, profit = _prediction_outcome(mercato, esito, quota, sh, sa, home, away)
        if outcome is None:
            continue
        if outcome == "won" and _esito_possible(mercato, esito, sh, sa,
                                                home, away) is False:
            logger.warning("settle_predictions: esito '%s' VINTO in "
                           "contraddizione coi gol %s-%s (%s vs %s): "
                           "settlement BLOCCATO (pred #%d)",
                           esito, sh, sa, home, away, pid)
            blocked += 1
            continue
        if outcome == "push":
            pushes += 1
        c.execute("UPDATE predictions SET esito_finale=?, profit=?, settled_at=? WHERE id=?",
                  (outcome, profit, now, pid))
        settled += 1
    conn.commit(); conn.close()
    if blocked:
        logger.warning("settle_predictions: %d righe BLOCCATE dal sanity check", blocked)
    return settled, pushes


def predictions_summary(mercato=None, settled_since=None):
    """Riepilogo previsioni CHIUSE per mercato: hit, ROI, gap EV, edge mercato.

    E' la telemetria di calibrazione: mostra per ogni mercato se il modello
    batte davvero la closing line (ROI realizzato vs EV atteso).
    Con `settled_since` (ISO, es. "2026-09-01") filtra solo le previsioni
    saldate a partire da quella data (report giornaliero).
    """
    rows = get_predictions(mercato=mercato, closed=True, limit=100000)
    if settled_since:
        rows = [r for r in rows if (r.get("settled_at") or "") >= settled_since]
    by_mkt: dict = {}
    for r in rows:
        key = r["mercato"] or "?"
        b = by_mkt.setdefault(key, {"n": 0, "won": 0, "lost": 0, "push": 0,
                                    "pnl": 0.0, "ev_sum": 0.0, "prob_sum": 0.0,
                                    "edge_sum": 0.0, "edge_n": 0})
        b["n"] += 1
        out = r["esito_finale"]
        if out == "won":
            b["won"] += 1
        elif out == "lost":
            b["lost"] += 1
        else:
            b["push"] += 1
        b["pnl"] += r["profit"] or 0.0
        b["ev_sum"] += (r["ev"] or 0.0)
        b["prob_sum"] += (r["prob"] or 0.0)
        if r.get("market_edge") is not None:
            b["edge_sum"] += r["market_edge"]
            b["edge_n"] += 1
    out = {}
    for key, b in by_mkt.items():
        closed = b["n"] - b["push"]
        roi = (b["pnl"] / b["n"] * 100) if b["n"] else 0.0
        out[key] = {
            "n": b["n"], "won": b["won"], "lost": b["lost"], "push": b["push"],
            "hit_rate": ((b["won"] / closed * 100) if closed else 0.0),
            "roi": round(roi, 2),
            "avg_ev": round(((b["ev_sum"] / b["n"]) * 100), 2) if b["n"] else 0.0,
            "gap": round(((b["pnl"] - b["ev_sum"]) / b["n"] * 100), 2) if b["n"] else 0.0,
            "avg_prob": round((b["prob_sum"] / b["n"]), 4) if b["n"] else 0.0,
            "avg_market_edge": round((b["edge_sum"] / b["edge_n"] * 100), 2) if b["edge_n"] else None,
        }
    return out


# --- Puntate automatiche (auto_bet.py) ---
def save_bet(match_id, mercato, esito, market_id=None, selection_id=None,
             price=0.0, stake=0.0, mode="dry-run", status=None, bet_id=None):
    """Registra (o aggiorna, se non chiusa) una puntata automatica.

    UNIQUE(match_id, esito): una sola puntata per esito anche se il job
    viene rilanciato; la puntata gia' saldata non viene toccata.
    """
    now = datetime.now().isoformat()
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO bets (match_id, mercato, esito, market_id, selection_id,
                                    price, stake, mode, status, bet_id, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(match_id, esito) DO UPDATE SET
                   market_id=excluded.market_id, selection_id=excluded.selection_id,
                   price=excluded.price, stake=excluded.stake, mode=excluded.mode,
                   status=excluded.status, bet_id=excluded.bet_id
                 WHERE esito_finale IS NULL''',
              (match_id, mercato, esito, market_id, selection_id,
               float(price), float(stake), mode, status, bet_id, now))
    conn.commit(); conn.close()


def bet_exists_open(match_id, esito):
    """True se esiste gia' una puntata aperta per (match_id, esito)."""
    conn = _get_conn(); c = conn.cursor()
    r = c.execute("SELECT 1 FROM bets WHERE match_id=? AND esito=? "
                  "AND esito_finale IS NULL", (match_id, esito)).fetchone()
    conn.close()
    return r is not None


def get_bets(day=None, closed=None, limit=200):
    conn = _get_conn(); c = conn.cursor()
    q = "SELECT match_id, mercato, esito, market_id, selection_id, price, stake, " \
        "mode, status, bet_id, esito_finale, profit, created_at, settled_at FROM bets"
    conds, args = [], []
    if day:
        conds.append("created_at LIKE ?"); args.append(f"{day}%")
    if closed is True:
        conds.append("esito_finale IS NOT NULL")
    elif closed is False:
        conds.append("esito_finale IS NULL")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall()
    conn.close()
    return [
        {"match_id": r[0], "mercato": r[1], "esito": r[2], "market_id": r[3],
         "selection_id": r[4], "price": r[5], "stake": r[6], "mode": r[7],
         "status": r[8], "bet_id": r[9], "esito_finale": r[10], "profit": r[11],
         "created_at": r[12], "settled_at": r[13]}
        for r in rows
    ]


def settle_bets(return_details: bool = False):
    """Salda le puntate automatiche aperte coi risultati reali (idempotente).

    Ritorna (saldate, push); con `return_details=True` anche la lista dei
    verdetti appena emessi: {match_id, league, home, away, mercato, esito,
    price, stake, mode, outcome, profit} — serve alle notifiche Telegram.
    """
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        open_rows = c.execute("SELECT id, match_id, mercato, esito, price, stake, mode "
                              "FROM bets WHERE esito_finale IS NULL").fetchall()
        results = c.execute("SELECT match_id, home_team, away_team, score_home, score_away "
                            "FROM match_results").fetchall()
        leagues = {row[0]: row[1] for row in
                   c.execute("SELECT id, league FROM matches").fetchall()}
    finally:
        conn.close()
    res_map = {r[0]: r[1:] for r in results}

    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    settled = pushes = blocked = 0
    details = []
    for bid, match_id, mercato, esito, price, stake, mode in open_rows:
        r = res_map.get(match_id)
        if not r:
            continue
        home, away, sh, sa = r
        if not _goals_sane(sh, sa):
            logger.warning("settle_bets: gol non validi (%r-%r) su match %s: "
                           "settlement BLOCCATO (bet #%d)", sh, sa, match_id, bid)
            blocked += 1
            continue
        outcome, _ = _prediction_outcome(mercato, esito, price, sh, sa, home, away)
        if outcome is None:
            continue
        if outcome == "won" and _esito_possible(mercato, esito, sh, sa,
                                                home, away) is False:
            logger.warning("settle_bets: esito '%s' VINTO in contraddizione "
                           "coi gol %s-%s (%s vs %s): settlement BLOCCATO "
                           "(bet #%d)", esito, sh, sa, home, away, bid)
            blocked += 1
            continue
        if outcome == "push":
            profit = 0.0
            pushes += 1
        elif outcome == "won":
            profit = round(stake * (price - 1), 2)
        else:
            profit = round(-stake, 2)
        c.execute("UPDATE bets SET esito_finale=?, profit=?, settled_at=? WHERE id=?",
                  (outcome, profit, now, bid))
        settled += 1
        details.append({
            "match_id": match_id,
            "league": leagues.get(match_id, ""),
            "home": home, "away": away,
            "mercato": mercato, "esito": esito,
            "price": price, "stake": stake, "mode": mode,
            "outcome": outcome, "profit": profit,
        })
    conn.commit(); conn.close()
    if blocked:
        logger.warning("settle_bets: %d righe BLOCCATE dal sanity check", blocked)

    if return_details:
        return settled, pushes, details
    return settled, pushes


def settlement_sanity_check() -> list:
    """Righe GIÀ saldate (bets/predictions) il cui verdetto contraddice i
    gol correnti di match_results.

    Tripwire post-fix (bug 02/09): se match_results viene corretto dopo che
    la bet era stata chiusa (es. il watchdog risalva il punteggio vero con
    match_scores_by_name), il verdetto salvato può restare SPECCHIATO
    (esito '2' marcato won mentre i gol dicono vittoria casa). Questa
    funzione le trova ricomputando l'esito atteso dai gol correnti.

    Ritorna una lista di dict: {table, id, match_id, mercato, esito,
    stored, expected, home, away, sh, sa}. Vuota se tutto coerente.
    """
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        results = {r[0]: r[1:] for r in c.execute(
            "SELECT match_id, home_team, away_team, score_home, score_away "
            "FROM match_results").fetchall()}
        rows = []
        for bid, match_id, mercato, esito, price, stored in c.execute(
                "SELECT id, match_id, mercato, esito, price, esito_finale "
                "FROM bets WHERE esito_finale IS NOT NULL").fetchall():
            rows.append(("bets", bid, match_id, mercato, esito, price, stored))
        for pid, match_id, mercato, esito, quota, stored in c.execute(
                "SELECT id, match_id, mercato, esito, quota, esito_finale "
                "FROM predictions WHERE esito_finale IS NOT NULL").fetchall():
            rows.append(("predictions", pid, match_id, mercato, esito, quota, stored))
    finally:
        conn.close()

    out = []
    for table, rid, match_id, mercato, esito, price, stored in rows:
        r = results.get(match_id)
        if not r:
            continue
        home, away, sh, sa = r
        if not _goals_sane(sh, sa):
            continue
        expected, _ = _prediction_outcome(mercato, esito, price, sh, sa,
                                          home, away)
        if expected is None or expected == stored:
            continue
        out.append({"table": table, "id": rid, "match_id": match_id,
                    "mercato": mercato, "esito": esito,
                    "stored": stored, "expected": expected,
                    "home": home, "away": away, "sh": sh, "sa": sa})
    return out


def heal_settled_contradictions(contradictions: list) -> int:
    """Riapre le righe in contraddizione e le ri-salda coi gol correnti.

    Il verdetto specchiato (bug 02/09) viene azzerato (esito_finale/profit/
    settled_at NULL) e il settlement ricomputa l'esito dai gol VERI di
    match_results: la bet passa da 'won' a 'lost' senza intervento manuale.

    Ritorna il numero di righe corrette (0 se la lista è vuota).
    """
    if not contradictions:
        return 0
    conn = _get_conn(); c = conn.cursor()
    for item in contradictions:
        c.execute(f"UPDATE {item['table']} SET esito_finale=NULL, profit=NULL, "
                  f"settled_at=NULL WHERE id=?", (item["id"],))
    conn.commit(); conn.close()
    nb, _ = settle_bets()
    npr, _ = settle_predictions()
    logger.warning("heal_settled_contradictions: riaperte e ri-sal date "
                   "%d righe (%d bets, %d pred)",
                   len(contradictions), nb, npr)
    return len(contradictions)


def bets_period(since: str):
    """Puntate automatiche piazzate da `since` (ISO): stake, chiuse e P/L."""
    rows = get_bets(limit=100000)
    rows = [r for r in rows if (r.get("created_at") or "") >= since]
    closed = [r for r in rows if r.get("esito_finale")]
    return {
        "piazzate": len(rows),
        "stake_totale": round(sum(r["stake"] or 0 for r in rows), 2),
        "chiusi": len(closed),
        "vinti": sum(1 for r in closed if r["esito_finale"] == "won"),
        "persi": sum(1 for r in closed if r["esito_finale"] == "lost"),
        "push": sum(1 for r in closed if r["esito_finale"] == "push"),
        "profit": round(sum(r["profit"] or 0 for r in closed), 2),
    }


def day_completed(day: str | None = None) -> bool:
    """True se tutte le partite del giorno (gia' iniziate) hanno il risultato.

    Le partite non ancora iniziate NON bloccano la giornata (in tarda serata
    nessuna partita inizia piu', quindi il check converge). Serve per il
    riepilogo "a fine ultima partita".
    """
    from datetime import timezone
    day = day or datetime.now().strftime("%Y-%m-%d")
    conn = _get_conn(); c = conn.cursor()
    rows = c.execute("SELECT id, commence_time FROM matches WHERE commence_time LIKE ?",
                     (f"{day}%",)).fetchall()
    conn.close()
    if not rows:
        return True  # nessuna partita -> giornata "completata" (niente da aspettare)
    now_utc = datetime.now(timezone.utc)
    result_conn = _get_conn()
    _create_results_table(result_conn)
    result_c = result_conn.cursor()
    try:
        for mid, commence in rows:
            try:
                start = datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
            except Exception:
                continue  # data malformata: non blocca
            if start > now_utc:
                continue  # non ancora iniziata
            r = result_c.execute("SELECT 1 FROM match_results WHERE match_id=?", (mid,)).fetchone()
            if r is None:
                return False
    finally:
        result_conn.close()
    return True


def cassa_period(since: str):
    """Scommesse della cassa SALDATE a partire da `since` (ISO): P/L del periodo.

    Serve ai report giornalieri (sera = oggi, mattina = ieri): quanto ho
    davvero vinto/perso con le puntate in quel periodo.
    """
    closed = [e for e in get_cassa()
              if e.get("esito_finale") and (e.get("settled_at") or "") >= since]
    vinti = sum(1 for e in closed if e["esito_finale"] == "won")
    persi = sum(1 for e in closed if e["esito_finale"] == "lost")
    profit = sum(e.get("profit") or 0 for e in closed)
    speso = sum(e["importo"] or 0 for e in closed)
    return {"chiusi": len(closed), "vinti": vinti, "persi": persi,
            "speso": round(speso, 2), "profit": round(profit, 2),
            "roi": round((profit / speso * 100), 2) if speso else 0.0}


def clear_cassa():
    """Svuota la cassa (backup server)."""
    conn = _get_conn(); c = conn.cursor()
    c.execute("DELETE FROM cassa")
    conn.commit(); conn.close()

def init_db():
    conn = _get_conn(); c = conn.cursor()
    # Pulizia idempotente: una sola analisi per match (i doppioni di
    # match_analysis duplicavano i pick nella schedina via JOIN).
    c.execute("DELETE FROM match_analysis WHERE id NOT IN "
              "(SELECT MAX(id) FROM match_analysis GROUP BY match_id)")
    conn.commit(); conn.close()

def log_signal(chat_id, evento, esito, quota, probabilita, ev):
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO signals VALUES (NULL,?,?,?,?,?,?,?,?,?)''',
              (chat_id, evento, esito, quota, probabilita, ev, datetime.now().isoformat(), None, 0.0))
    conn.commit(); conn.close()

def get_signals(chat_id=None, limit=50):
    conn = _get_conn(); c = conn.cursor()
    if chat_id:
        c.execute("SELECT * FROM signals WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))
    else:
        c.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall(); conn.close()
    return [Signal(*r) for r in rows]

def get_performance_summary(days=30):
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*), SUM(CASE WHEN esito_finale='won' THEN 1 ELSE 0 END), SUM(CASE WHEN esito_finale='lost' THEN 1 ELSE 0 END), SUM(profit) FROM signals WHERE esito_finale IS NOT NULL")
    row = c.fetchone(); conn.close()
    total, won, lost, profit = row if row else (0,0,0,0.0)
    roi = (profit/total*100) if total>0 else 0.0
    return {"closed":total or 0, "won":won or 0, "lost":lost or 0, "net_profit":profit or 0.0, "roi":roi}

def add_subscriber(chat_id, tier="free"):
    """Iscrive una chat. tier: 'free' o 'premium'.

    Se la chat esiste gia' la riga non viene sovrascritta (INSERT OR IGNORE):
    il tier esistente resta invariato; usare set_tier() per cambiarlo.
    """
    conn = _get_conn(); c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO subscribers (chat_id, tier, premium_until) VALUES (?,?,NULL)',
              (chat_id, tier))
    conn.commit(); conn.close()

def remove_subscriber(chat_id):
    conn = _get_conn(); c = conn.cursor()
    c.execute('DELETE FROM subscribers WHERE chat_id=?', (chat_id,))
    conn.commit(); conn.close()

def set_tier(chat_id, tier, premium_until=None):
    """Aggiorna tier e scadenza premium di un iscritto.

    tier 'premium' senza premium_until = abbonamento senza scadenza.
    """
    conn = _get_conn(); c = conn.cursor()
    c.execute('UPDATE subscribers SET tier=?, premium_until=? WHERE chat_id=?',
              (tier, premium_until, chat_id))
    conn.commit(); conn.close()

def get_subscription(chat_id):
    """Ritorna (tier, premium_until) di una chat, o None se non iscritta.

    Un abbonamento premium scaduto viene degradato a 'free' (senza cancellare
    la riga: e' comunque un iscritto).
    """
    conn = _get_conn(); c = conn.cursor()
    c.execute('SELECT tier, premium_until FROM subscribers WHERE chat_id=?', (chat_id,))
    row = c.fetchone(); conn.close()
    if not row:
        return None
    tier, premium_until = row
    if tier == "premium" and premium_until:
        try:
            if datetime.fromisoformat(premium_until) < datetime.now():
                tier = "free"
        except ValueError:
            tier = "free"
    return tier, premium_until

def is_premium(chat_id):
    """True se la chat e' iscritta con abbonamento premium valido."""
    sub = get_subscription(chat_id)
    return bool(sub) and sub[0] == "premium"

def get_subscribers(tier=None):
    """Chat_id degli iscritti, opzionalmente filtrati per tier.

    Con tier='premium' include solo abbonamenti ancora validi (scadenza
    futura o assente). Con tier='free' include anche i premium scaduti.
    """
    conn = _get_conn(); c = conn.cursor()
    if tier is None:
        c.execute('SELECT chat_id FROM subscribers')
    elif tier == "premium":
        c.execute("SELECT chat_id FROM subscribers WHERE tier='premium' AND "
                  "(premium_until IS NULL OR premium_until >= ?)",
                  (datetime.now().isoformat(),))
    else:
        c.execute("SELECT chat_id FROM subscribers WHERE tier=? OR tier IS NULL", (tier,))
    rows = c.fetchall(); conn.close()
    return [r[0] for r in rows]

# --- Calendario ---
def save_match(match_id, league, home, away, commence, status="scheduled"):
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?)''',
              (match_id, league, home, away, commence, status, datetime.now().isoformat()))
    conn.commit(); conn.close()

def get_today_matches():
    conn = _get_conn(); c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("SELECT * FROM matches WHERE commence_time LIKE ? ORDER BY commence_time", (f"{today}%",))
    rows = c.fetchall(); conn.close()
    return rows

def save_analysis(match_id, lam_h, lam_a, p1, px, p2, p_over, best_ev, best_esito, best_quota, best_bookmaker, status,
                  market_prob=None, market_edge=None):
    """Salva l'analisi di un match, incluso il confronto col mercato.

    Idempotente per match_id: ogni nuova analisi SOSTITUISCE la precedente
    (prima con piu' righe per match la schedina mostrava doppioni nel JOIN
    matches x match_analysis).

    market_prob: probabilita' implicita del mercato (devig) per l'esito scelto.
    market_edge: model_prob - market_prob (quanto il modello batte il mercato).
    """
    conn = _get_conn(); c = conn.cursor()
    c.execute("DELETE FROM match_analysis WHERE match_id=?", (match_id,))
    c.execute('''INSERT INTO match_analysis (match_id,lam_h,lam_a,prob_1,prob_X,prob_2,prob_over,best_ev,best_esito,best_quota,best_bookmaker,status,timestamp,market_prob,market_edge)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (match_id, lam_h, lam_a, p1, px, p2, p_over, best_ev, best_esito, best_quota, best_bookmaker, status,
               datetime.now().isoformat(), market_prob, market_edge))
    conn.commit(); conn.close()

def get_analysis_for_match(match_id):
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM match_analysis WHERE match_id=? ORDER BY id DESC LIMIT 1", (match_id,))
    row = c.fetchone(); conn.close()
    return row

def is_notified(match_id, date_str):
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT 1 FROM notifications WHERE match_id=? AND date=?", (match_id, date_str))
    row = c.fetchone(); conn.close()
    return row is not None

def mark_notified(match_id, date_str):
    conn = _get_conn(); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO notifications VALUES (?,?)", (match_id, date_str))
    conn.commit(); conn.close()

def save_clv(match_id, esito, quota, signal_started=False, pinnacle_quota=None):
    """Registra un campione CLV per una coppia match+esito.

    - Prima analisi del match (signal_started=True): la quota corrente diventa la
      quota del segnale, e viene usata anche come primo campione di chiusura.
    - Analisi successive (signal_started=False): aggiorna la quota di chiusura,
      che converge verso il prezzo di mercato finale (CLV).
    - pinnacle_quota (opz.): prezzo Pinnacle per lo stesso esito, la closing
      line piu' sharp. Se assente, si usa solo la chiusura del miglior bookmaker.
    """
    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("SELECT signal_quota, closing_quota, pinnacle_quota FROM clv_history "
              "WHERE match_id=? AND esito=?", (match_id, esito))
    row = c.fetchone()
    if row is None or signal_started:
        pin = pinnacle_quota if pinnacle_quota and pinnacle_quota > 0 else None
        c.execute("INSERT OR REPLACE INTO clv_history VALUES (?,?,?,?,?,?)",
                  (match_id, esito, quota, quota, now, pin))
    else:
        if pinnacle_quota and pinnacle_quota > 0:
            c.execute("UPDATE clv_history SET closing_quota=?, updated_at=?, pinnacle_quota=? "
                      "WHERE match_id=? AND esito=?",
                      (quota, now, pinnacle_quota, match_id, esito))
        else:
            c.execute("UPDATE clv_history SET closing_quota=?, updated_at=? "
                      "WHERE match_id=? AND esito=?",
                      (quota, now, match_id, esito))
    conn.commit(); conn.close()

def clear_old_matches():
    conn = _get_conn(); c = conn.cursor()
    c.execute("DELETE FROM matches WHERE status='finished' OR commence_time < date('now','-2 days')")
    conn.commit(); conn.close()

# --- Tracking risultati ---
def _create_results_table(conn):
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS match_results (
        match_id TEXT PRIMARY KEY, league TEXT, home_team TEXT, away_team TEXT,
        score_home INTEGER, score_away INTEGER, result TEXT, settled_at TEXT)''')

def save_result(match_id, league, home, away, sh, sa, settled_at):
    conn = _get_conn(); _create_results_table(conn); c = conn.cursor()
    res = "1" if sh > sa else ("2" if sh < sa else "X")
    c.execute('''INSERT OR REPLACE INTO match_results VALUES (?,?,?,?,?,?,?,?)''',
              (match_id, league, home, away, sh, sa, res, settled_at))
    conn.commit(); conn.close()

def get_leagues_with_signals(days=3):
    conn = _get_conn(); c = conn.cursor()
    c.execute('''SELECT DISTINCT m.league FROM matches m
                 JOIN match_analysis a ON a.match_id = m.id
                 WHERE a.status IN ('value','strong_value')
                   AND m.commence_time >= date('now', ?)''', (f"-{days} days",))
    rows = c.fetchall(); conn.close()
    return [r[0] for r in rows]

def get_results_stats():
    conn = _get_conn(); _create_results_table(conn); c = conn.cursor()
    c.execute('''SELECT r.home_team, r.away_team, r.score_home, r.score_away,
                        a.best_esito, a.best_quota, a.best_ev, a.status, a.match_id
                 FROM match_results r
                 JOIN match_analysis a ON a.match_id = r.match_id''')
    rows = c.fetchall(); conn.close()
    # Mappa CLV per (match_id, esito): raw + vig-free.
    clv_map = {}
    clv_vf_map = {}  # vig-free CLV
    try:
        from market_calib import clv_vig_free, clv_raw, devig
        conn2 = _get_conn(); c2 = conn2.cursor()
        c2.execute("SELECT match_id, esito, signal_quota, closing_quota, "
                   "pinnacle_quota FROM clv_history")
        for mid, esito, sig, clos, pin in c2.fetchall():
            el = (esito or "").lower().strip()
            if clos and clos > 0 and sig and sig > 0:
                clv_map[(mid, el)] = (sig / clos) - 1.0
                # CLV vig-free: usa Pinnacle come closing line se disponibile
                # (Pinnacle ha vig minimo, quasi fair).
                # Altrimenti deviga la closing quota stimando il mercato.
                fair_close = pin if (pin and pin > 0) else clos
                vf = clv_vig_free(sig, fair_close)
                if vf is not None:
                    clv_vf_map[(mid, el)] = vf
        conn2.close()
    except Exception:
        pass
    bets = []
    clvs = []
    clvs_vf = []
    for home, away, sh, sa, esito, quota, ev, status, mid in rows:
        if status not in ("value", "strong_value") or not esito or not quota or quota <= 1.0:
            continue
        el = esito.lower().strip()
        if "over" in el:
            won = (sh + sa) >= 3
        elif "under" in el:
            won = (sh + sa) <= 2
        elif el == (home or "").lower().strip():
            won = sh > sa
        elif el == (away or "").lower().strip():
            won = sa > sh
        else:
            won = sh == sa
        bets.append({"quota": quota, "won": won, "ev": ev or 0.0})
        clv = clv_map.get((mid, el))
        if clv is not None:
            clvs.append(clv)
        clvf = clv_vf_map.get((mid, el))
        if clvf is not None:
            clvs_vf.append(clvf)
    total = len(bets)
    won_n = sum(1 for b in bets if b["won"])
    net = sum((b["quota"] - 1) if b["won"] else -1 for b in bets)
    return {
        "total": total, "won": won_n, "lost": total - won_n,
        "net": net, "roi": (net / total * 100) if total else 0.0,
        "hit_rate": (won_n / total * 100) if total else 0.0,
        "avg_ev": (sum(b["ev"] for b in bets) / total) if total else 0.0,
        "clv_tracked": len(clvs),
        "avg_clv": (sum(clvs) / len(clvs)) if clvs else 0.0,
        "avg_clv_vf": (sum(clvs_vf) / len(clvs_vf)) if clvs_vf else 0.0,
        "clv_vf_tracked": len(clvs_vf),
    }
