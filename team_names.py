"""team_names.py — risoluzione dei nomi squadra (bookmaker -> nome del DB).

PROBLEMA (misurato l'11/09/2026 sul container). I nomi che arrivano dai
bookmaker NON coincidono con quelli salvati nel nostro DB (the-odds-api /
API-Football): la verifica in sola lettura sul volume Railway ha mostrato

    SX Bet                        DB (team_ratings)
    --------------------------    --------------------------
    Tottenham Hotspur             Tottenham            (0.69)
    AFC Bournemouth               Bournemouth          (0.85)
    Ipswich Town                  Ipswich              (0.74)
    Wrexham                       Wrexham AFC          (0.78)
    Willem II Tilburg             Willem II            (0.69)
    AS Monaco FC                  Monaco               (0.67)
    Olympique Marseille           Marseille            (0.64)
    KV Mechelen / RSC Anderlecht  Mechelen / Anderlecht

Conseguenza: `rating_engine.get_rating(nome_sx)` non trova la riga, quindi
`poisson_engine.expected_goals` ricade sul PROFILO NEUTRO di lega e il
modello diventa "cieco" proprio sui favoriti netti (edge finto -24pp).

SOLUZIONE: tre stadi DETERMINISTICI, nessun indovinello. Si risolve il nome
contro l'insieme dei nomi NOTI (per default `team_ratings`, l'unico che
conta per il modello):

  1. match ESATTO (case-insensitive);
  2. alias espliciti (`TEAM_ALIASES`) per i casi che la normalizzazione non
     copre (es. "Nottingham Forest" -> "Nottm Forest");
  3. chiave NORMALIZZATA: minuscole, senza accenti/punteggiatura, senza
     token societari (FC/AFC/AC/AS/KV/RSC...), token ORDINATI;
  4. contenimento di token (sottoinsieme) con guardia di AMBIGUITA': se due
     candidati sono ugualmente buoni si ritorna None — meglio nessun rating
     (profilo neutro) che il rating della squadra sbagliata.

Il modulo e' PURO e senza dipendenze dal modello: legge solo `team_ratings`.

Due punti d'ingresso:

  * `resolve_team(nome)` — risolve un nome ESTERNO contro il roster del DB
    (usato dal modello: `rating_engine`/`poisson_engine`);
  * `same_team(a, b)` — confronto SIMMETRICO tra due nomi di provider
    diversi, senza roster (usato dal settlement SX, dove 'Flamengo-RJ' e
    'CR Flamengo', 'Vila Nova GO' e 'Vila Nova', "Newell's Old Boys" e
    'Newells Old Boys' devono coincidere).
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Dict, Iterable, List, Optional, Set

from config import DATA_DIR

DB_PATH = DATA_DIR / "quotaverace.db"

# Sotto questa quota di similarita' (jaccard sui token) il contenimento non
# basta: non e' un nome "sporco", e' un'altra squadra.
MIN_TOKEN_SCORE = 0.34
# Se il secondo candidato e' a meno di questo scarto dal primo la scelta e'
# ambigua: si rifiuta (fail-closed, si resta sul profilo neutro).
AMBIGUITY_GAP = 0.05

# Token che NON identificano la squadra: forme societarie e prefissi diffusi
# in Europa (FC, AFC, KV, RSC, ...). Rimossi SOLO in normalizzazione, cosi'
# "AFC Bournemouth" e "Bournemouth" coincidono e "KV Mechelen" -> "Mechelen".
NOISE_TOKENS = {
    "fc", "cf", "afc", "sc", "ac", "as", "ss", "ssc", "us", "ud", "cd", "ca",
    "rc", "rcd", "sd", "sv", "sk", "fk", "if", "bk", "ik", "kf", "nk", "hnk",
    "gd", "kv", "kvc", "rsc", "bsc", "tsg", "vfl", "vfb", "fsv", "bvb",
    "cfr", "cs", "cp", "se", "club", "calcio", "srl", "spa",
}

# Alias espliciti per i casi dove la normalizzazione NON arriva (sigle e
# nomi "giornalistici" che non contengono il nome del DB). Chiave = forma
# normalizzata in ingresso, valore = nome canonico nel DB.
TEAM_ALIASES: Dict[str, str] = {
    "nottingham forest": "Nottm Forest",
    "nottm forest": "Nottm Forest",
    "man utd": "Manchester United",
    "man united": "Manchester United",
    "manchester utd": "Manchester United",
    "man city": "Manchester City",
    "inter milan": "Inter",
    "fc internazionale": "Inter",
    "internazionale": "Inter",
    "ac milan": "Milan",
    "as milan": "Milan",
    "spurs": "Tottenham",
    "psg": "Paris Saint-Germain",
    "paris st germain": "Paris Saint-Germain",
    "atletico": "Atletico Madrid",
    "atletico de madrid": "Atletico Madrid",
    "athletic club": "Athletic Bilbao",
    "athletic": "Athletic Bilbao",
    "borussia dortmund": "Borussia Dortmund",
    "bvb dortmund": "Borussia Dortmund",
    "wolves": "Wolverhampton",
    "olympique lyonnais": "Lyon",
    "olympique lyon": "Lyon",
    "sporting lisbona": "Sporting CP",
    "sporting lisbon": "Sporting CP",
    "bayern": "Bayern Munich",
    "bayern monaco": "Bayern Munich",
    "wrexham": "Wrexham AFC",
}

_INDEX_CACHE: Optional[Dict[str, object]] = None


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))


def normalize(name: str) -> str:
    """Chiave di confronto: minuscole, senza accenti/punteggiatura, token
    societari rimossi, token ordinati alfabeticamente."""
    if not name:
        return ""
    s = _strip_accents(str(name)).lower()
    # L'apostrofo e' PARTE del nome, non un separatore: "Newell's Old Boys" e
    # 'Newells Old Boys' sono la stessa squadra. Va eliminato PRIMA della
    # sostituzione della punteggiatura, altrimenti resterebbe un token 's'
    # ('newell s old boys') che non aggancia piu' nulla (settlement SX).
    s = re.sub(r"['\u2019`\u00b4]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    # I token puramente numerici sono anni di fondazione, non identificativi
    # ('1. FC Koln' -> 'koln', 'Hannover 96' -> 'hannover', 'Mainz 05').
    toks = [t for t in s.split()
            if t and t not in NOISE_TOKENS and not t.isdigit()]
    return " ".join(sorted(toks))


def _alias_key(name: str) -> str:
    """Chiave per `TEAM_ALIASES`: normalizzata ma SENZA riordinare i token
    (le chiavi della tabella sono in ordine naturale: 'nottingham forest')."""
    if not name:
        return ""
    s = _strip_accents(str(name)).lower()
    s = re.sub(r"['\u2019`\u00b4]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    toks = [t for t in s.split() if t and t not in NOISE_TOKENS]
    return " ".join(toks)


# Codici di stato/regione (2 lettere) che i provider accodano al nome della
# squadra: 'Flamengo-RJ', 'Velez Sarsfield BA', 'Vila Nova GO'. Non
# identificano la squadra: nel confronto tra fonti DIVERSE vanno ignorati.
REGION_CODES = {
    "ac", "al", "am", "ap", "ba", "ce", "df", "es", "go", "ma", "mg",
    "ms", "mt", "pa", "pb", "pe", "pi", "pr", "rj", "rn", "ro", "rr",
    "rs", "sc", "se", "sp", "to",
}

# Particelle di raccordo dei nomi latini ('Estudiantes de La Plata', 'Vasco
# da Gama'): rumore nel confronto, mai l'identita' della squadra.
JOIN_PARTICLES = {"de", "del", "do", "da", "dos", "das", "la", "las", "los"}


def _core(name: str) -> str:
    """Chiave di confronto TRA PROVIDER: `normalize` senza codici di
    stato/regione e senza particelle di raccordo.

    Serve al settlement, dove si confrontano due nomi che arrivano da fonti
    diverse (SX Bet vs the-odds-api/API-Football) e non esiste un roster di
    riferimento: 'Flamengo-RJ' e 'CR Flamengo' devono coincidere.
    """
    key = normalize(name)
    toks = [t for t in key.split()
            if t not in REGION_CODES and t not in JOIN_PARTICLES]
    return " ".join(toks) or key


def same_team(a: str, b: str) -> bool:
    """True se due nomi designano la stessa squadra (confronto SIMMETRICO).

    Diverso da `resolve_team`: li' un nome viene risolto contro il roster del
    DB, qui si confrontano due nomi di provider DIVERSI (SX Bet vs
    the-odds-api) senza un elenco di verita'. Stadi, tutti deterministici:

      1. nome grezzo uguale (case-insensitive);
      2. chiave `normalize` uguale (accenti, punteggiatura, apostrofi, token
         societari: 'Club Cienciano' == 'Cienciano');
      3. chiave `_core` uguale (anche codici di stato e particelle:
         'Flamengo-RJ' == 'CR Flamengo', 'Vila Nova GO' == 'Vila Nova');
      4. contenimento dei token (`MIN_TOKEN_SCORE`): 'cr flamengo' contiene
         'flamengo'.

    Il contenimento (e in generale il confronto tollerante) puo' essere
    AMBIGUO: 'Manchester' sta dentro sia 'Manchester United' sia
    'Manchester City'. Per questo da solo non basta a chiudere una bet —
    il chiamante deve verificare che la partita sia UNICA tra i candidati
    (`sx_signals._results_from_the_odds_api`). Mai fuzzy: un falso positivo
    chiuderebbe una bet col risultato di un'altra partita.
    """
    if not a or not b:
        return False
    ra, rb = str(a).strip(), str(b).strip()
    if not ra or not rb:
        return False
    if ra.lower() == rb.lower():
        return True
    ka, kb = normalize(ra), normalize(rb)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    ca, cb = _core(ra), _core(rb)
    if ca == cb:
        return True
    ta, tb = set(ca.split()), set(cb.split())
    if not ta or not tb:
        return False
    if not (ta <= tb or tb <= ta):
        return False
    inter = len(ta & tb)
    return inter / float(len(ta | tb)) >= MIN_TOKEN_SCORE


def invalidate() -> None:
    """Svuota la cache dell'indice (chiamare dopo un ricalcolo dei rating)."""
    global _INDEX_CACHE
    _INDEX_CACHE = None


def known_teams(refresh: bool = False) -> Set[str]:
    """Nomi squadra presenti in `team_ratings` (cache di processo).

    Fail-safe: su errore ritorna l'insieme vuoto, cosi' il chiamante resta
    sul comportamento di prima (profilo neutro) invece di rompersi.
    """
    global _INDEX_CACHE
    if _INDEX_CACHE is None or refresh:
        try:
            conn = sqlite3.connect(str(DB_PATH))
            rows = conn.execute("SELECT team FROM team_ratings").fetchall()
            conn.close()
            names = [r[0] for r in rows if r and r[0]]
        except Exception:
            names = []
        by_norm: Dict[str, List[str]] = {}
        for n in names:
            by_norm.setdefault(normalize(n), []).append(n)
        _INDEX_CACHE = {
            "names": set(names),
            "lower": {n.lower(): n for n in names},
            "by_norm": by_norm,
        }
    return _INDEX_CACHE["names"]  # type: ignore[return-value]


def _pool_index(known: Iterable[str]) -> Dict[str, object]:
    """Indice temporaneo per un pool esplicito (usato dai test)."""
    names = [n for n in known if n]
    by_norm: Dict[str, List[str]] = {}
    for n in names:
        by_norm.setdefault(normalize(n), []).append(n)
    return {"names": set(names),
            "lower": {n.lower(): n for n in names},
            "by_norm": by_norm}


def resolve_team(name: str, known: Optional[Iterable[str]] = None
                 ) -> Optional[str]:
    """Nome del DB corrispondente a `name` (None se non risolvibile).

    `known` permette di passare un insieme esplicito (test); di default si
    usa `team_ratings`. Non solleva mai eccezioni.
    """
    if not name or not str(name).strip():
        return None
    raw = str(name).strip()
    try:
        if known is not None:
            idx = _pool_index(list(known))
        else:
            known_teams()
            idx = _INDEX_CACHE or _pool_index([])
        names: Set[str] = idx["names"]          # type: ignore[assignment]
        if not names:
            return None
        # 1) match esatto
        if raw in names:
            return raw
        ci = idx["lower"].get(raw.lower())      # type: ignore[union-attr]
        if ci:
            return ci
        # 2) alias espliciti
        alias = TEAM_ALIASES.get(_alias_key(raw))
        if alias:
            if alias in names:
                return alias
            hit = idx["lower"].get(alias.lower())  # type: ignore[union-attr]
            if hit:
                return hit
        # 3) chiave normalizzata esatta
        key = normalize(raw)
        if not key:
            return None
        hits = idx["by_norm"].get(key) or []    # type: ignore[union-attr]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None          # omonimie nel DB: mai indovinare
        # 4) contenimento di token con guardia di ambiguita'
        nt = set(key.split())
        if not nt:
            return None
        scored: List[tuple] = []
        by_norm = idx["by_norm"]  # type: ignore[assignment]
        for key2, cands in by_norm.items():     # type: ignore[union-attr]
            tt = set(key2.split())
            if not tt or not (tt <= nt or nt <= tt):
                continue
            inter = len(tt & nt)
            if inter == 0:
                continue
            jac = inter / float(len(tt | nt))
            for cand in cands:
                scored.append((jac, cand))
        if not scored:
            return None
        scored.sort(key=lambda x: (-x[0], x[1]))
        best = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best[0] < MIN_TOKEN_SCORE:
            return None
        if best[0] - second < AMBIGUITY_GAP:
            return None
        return best[1]
    except Exception:
        return None


def resolve_pair(home: str, away: str,
                 known: Optional[Iterable[str]] = None) -> tuple:
    """Risolve una coppia (home, away): utile per diagnostica e test."""
    return resolve_team(home, known), resolve_team(away, known)
