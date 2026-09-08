"""tennis_sandbox.py — Paper trading Tennis (Moneyline 2 vie) su SX Bet.

Modulo PARALLELO e SOLO-SIMULAZIONE: legge i mercati tennis Moneyline
(market type 52 = "12", chi vince senza pareggio) dall'API PUBBLICA di
SX Bet, stima le probabilita' con un ELO SUPERFICIE-SPECIFICO e
TIME-DECAY (surface hard=cemento/clay=terra/grass=erba rilevata dal
nome del torneo; seme iniziale dalle probabilita' implicite del mercato,
poi aggiornato SOLO dai ghost bet/observation saldati; i risultati
invecchiano: peso pieno sugli ultimi ~30 giorni, dimezzati a ~60,
dimenticati oltre l'anno), calcola il Valore Atteso (+EV) sulle quote a
2 vie e registra TUTTI i segnali +EV su un ledger dedicato (SQLite in
data/tennis_sandbox/) per misurare opportunita' giornaliere, ROI
teorico e tasso di vincita prima di qualsiasi integrazione con denaro
reale.

Vincoli architetturali (pattern surebet_engine):
- indipendente da tracker/bot: ledger, ratings e loop PROPRI; nessun
  import da tracker.py/bot.py (test dedicato lo verifica);
- ZERO ordini reali: nessuna credenziale, nessuna firma EIP-712, solo
  endpoint pubblici di lettura (markets/active, orderbook-v3/snapshot,
  markets/find). Fail-closed totale: in caso di errore non registra nulla;
- ZERO crediti the-odds-api: le letture SX Bet pubbliche sono gratuite
  (a differenza del surebet engine, NON tocca il budget del value bot);
- mercato a 2 vie: una sola riga per (marketHash, selezione) sul ledger
  (UNIQUE), aggiornata in place al cambio prezzo finche' non si salda.

Settlement: per ogni ghost bet aperto interroga /markets/find; quando il
mercato risulta saldato (campo `outcome`: 1 = vince outcomeOne, 2 =
vince outcomeTwo, 0 = void) chiude la riga con profit e aggiorna l'ELO.

CLI:
    venv/bin/python tennis_sandbox.py --scan          # 1 scansione +EV
    venv/bin/python tennis_sandbox.py --settle        # salda ghost bet
    venv/bin/python tennis_sandbox.py --loop 1800     # loop scan+settle
    venv/bin/python tennis_sandbox.py --report        # report aggregato
    venv/bin/python tennis_sandbox.py --report --json # report JSON
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from config import DATA_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (env, con default)
# ---------------------------------------------------------------------------
SX_API_BASE = os.getenv("SX_API_BASE", "https://api.sx.bet").rstrip("/")

# Tennis su SX Bet: sportId 6, market type 52 = Moneyline "12" (chi vince,
# senza pareggio — esattamente il mercato a 2 vie richiesto).
TENNIS_SPORT_ID = int(os.getenv("TENNIS_SPORT_ID", "6"))
TENNIS_MARKET_TYPE = int(os.getenv("TENNIS_MARKET_TYPE", "52"))

TENNIS_DATA_DIR = Path(os.getenv("TENNIS_DATA_DIR",
                                 str(DATA_DIR / "tennis_sandbox")))
LEDGER_DB = TENNIS_DATA_DIR / "ledger.db"
RATINGS_FILE = TENNIS_DATA_DIR / "ratings.json"
HEARTBEAT_FILE = TENNIS_DATA_DIR / "heartbeat.json"
REPORT_FILE = TENNIS_DATA_DIR / "report.json"

# Soglia EV minima per considerare un segnale (default +3pp, come il bot
# value 1X2). Env: TENNIS_EV_MIN.
EV_MIN = float(os.getenv("TENNIS_EV_MIN", "0.03"))

# Bankroll VIRTUALE di paper trading (mai soldi veri) e staking Kelly
# frazionato: lo stake di ogni ghost bet e' calcolato dal Kelly pieno
# (max crescita logaritmica) ridotto del fattore TENNIS_KELLY_FRACTION e
# mai oltre TENNIS_MAX_STAKE_PCT del bankroll virtuale.
PAPER_BANKROLL = float(os.getenv("TENNIS_PAPER_BANKROLL", "1000"))
KELLY_FRACTION = float(os.getenv("TENNIS_KELLY_FRACTION", "0.25"))
MAX_STAKE_PCT = float(os.getenv("TENNIS_MAX_STAKE_PCT", "0.05"))

# Anti-garbage quote (come SUREBET_MAX_ODDS): i prezzi fuori range sono
# mercati illiquidi/sporchi, non opportunita'.
MIN_ODDS = float(os.getenv("TENNIS_MIN_ODDS", "1.10"))
MAX_ODDS = float(os.getenv("TENNIS_MAX_ODDS", "30"))

# Anti-EV-spurio: coerenza del mercato. Su un exchange i due best back
# devono avere somma degli inversi ~1 (nessun margine). Se e' ben sotto 1
# (book sporchi/illiquidi, es. favorito @1.13 e sfavorito @50: somma
# 0.885+0.02=0.905) il devig "inventa" probabilita' piu' alte del reale e
# l'EV risulta positivo per costruzione, NON per edge del modello.
# Range accettato: 0.98-1.08. Con EV_MIN=3% l'EV spurio massimo passabile
# e' 1/0.98-1 = 2.04% < 3% → un segnale richiede SEMPRE un vero edge del
# modello sul mercato (che emerge quando l'ELO impara dai settlement).
MIN_INV_SUM = float(os.getenv("TENNIS_MIN_INV_SUM", "0.98"))
MAX_INV_SUM = float(os.getenv("TENNIS_MAX_INV_SUM", "1.08"))

# ELO superficie-specifico + time-decay.
#
# - Surface-Specific ELO: ogni giocatore ha un rating OVERALL (tutte le
#   superfici) e un rating per superficie (hard=cemento/clay=terra/grass=
#   erba). Un match su superficie nota aggiorna overall E superficie; un
#   match senza superficie riconosciuta aggiorna solo l'overall.
# - Time-decay (30/60 giorni): i risultati invecchiano al momento dell'uso.
#   La rating efficace regredisce verso il neutro 1500 man mano che l'ultimo
#   match osservato invecchia: peso PIENO entro ELO_RECENT_DAYS (default
#   30 giorni), poi il peso dimezza ogni ELO_HALF_LIFE_DAYS (default 30
#   giorni: ~50% a 60 giorni), zero oltre ELO_WINDOW_DAYS (default 365).
# - Probabilita' su una superficie: blended rating efficace
#   overall*(1-w) + superficie*w con w = min(1, n_superficie/
#   SURFACE_MIN_MATCHES): la superficie conta davvero dopo i primi match
#   saldati su quella superficie, prima l'overall fa da smoothing.
ELO_K = float(os.getenv("TENNIS_ELO_K", "32"))
ELO_RECENT_DAYS = float(os.getenv("TENNIS_ELO_RECENT_DAYS", "30"))
ELO_HALF_LIFE_DAYS = float(os.getenv("TENNIS_ELO_HALF_LIFE_DAYS", "30"))
ELO_WINDOW_DAYS = float(os.getenv("TENNIS_ELO_WINDOW_DAYS", "365"))
SURFACE_MIN_MATCHES = float(os.getenv("TENNIS_SURFACE_MIN_MATCHES", "3"))

# Rete
REQUEST_TIMEOUT = float(os.getenv("TENNIS_REQUEST_TIMEOUT", "15"))

# Scala probabilita' SX Bet (percentageOdds * 1e20, docs V3)
SX_PROB_SCALE = 10 ** 20


# ---------------------------------------------------------------------------
# Client SX Bet (solo letture PUBBLICHE: nessuna chiave, nessun ordine)
# ---------------------------------------------------------------------------
def pct_scaled_to_decimal(pct: object) -> Optional[float]:
    """percentageOdds (1e20) -> quota decimale. Difensivo: garbage -> None."""
    try:
        p = float(str(pct)) / SX_PROB_SCALE
    except (TypeError, ValueError):
        return None
    if not 0.0 < p < 1.0:
        return None
    return round(1.0 / p, 4)


class SxTennisClient:
    """Client di lettura pubblico dell'API SX Bet per i mercati tennis.

    Fail-closed: ogni errore viene loggato e ritorna None/[] — il chiamante
    decide se considerarlo bloccante (nel sandbox non lo e' mai).
    """

    def __init__(self, api_base: Optional[str] = None,
                 timeout: Optional[float] = None) -> None:
        self.api_base = (api_base or SX_API_BASE).rstrip("/")
        self.timeout = timeout or REQUEST_TIMEOUT

    def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        resp = requests.get(f"{self.api_base}/{path}", params=params,
                            timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def active_markets(self, max_markets: int = 500) -> List[Dict]:
        """Mercati Moneyline tennis attivi (sportId 6, type 52), con
        paginazione `nextKey` (cap a max_markets). Fail-closed: [] su errore.
        """
        out: List[Dict] = []
        next_key: Optional[str] = None
        while len(out) < max_markets:
            params: Dict = {
                "sportIds": str(TENNIS_SPORT_ID),
                "type": str(TENNIS_MARKET_TYPE),
                "pageSize": 100,
            }
            if next_key:
                params["paginationKey"] = next_key
            try:
                data = self._get("markets/active", params=params)
            except Exception as e:
                logger.warning("sx tennis: markets/active fallita: %s", e)
                break
            d = data.get("data") if isinstance(data, dict) else {}
            markets = (d or {}).get("markets") or []
            for m in markets:
                if len(out) >= max_markets:
                    break
                out.append(m)
            next_key = (d or {}).get("nextKey")
            if not next_key or not markets:
                break
        return out

    def orderbook(self, market_hash: str) -> Optional[Dict]:
        """Snapshot del book taker: {outcomeOne: levels, outcomeTwo: levels}.

        Il livello [0] e' il migliore per chi vuole scommettere quell'esito.
        Fail-closed: None su errore / payload inatteso.
        """
        try:
            data = self._get("orderbook-v3/snapshot", params={
                "marketHash": market_hash, "showTakerPerspective": "true"})
        except Exception as e:
            logger.warning("sx tennis: orderbook %s fallito: %s",
                           market_hash, e)
            return None
        d = data.get("data") if isinstance(data, dict) else {}
        if not isinstance(d, dict):
            return None
        return {"outcomeOne": (d or {}).get("outcomeOne") or [],
                "outcomeTwo": (d or {}).get("outcomeTwo") or []}

    def market(self, market_hash: str) -> Optional[Dict]:
        """Dettaglio mercato via /markets/find (stato + esito saldato).

        Su un mercato saldato la risposta include `outcome` (1|2|0),
        `reportedDate`, teamOneScore/teamTwoScore. Fail-closed: None.
        """
        try:
            data = self._get("markets/find", params={
                "marketHashes": market_hash})
        except Exception as e:
            logger.warning("sx tennis: markets/find %s fallito: %s",
                           market_hash, e)
            return None
        arr = data.get("data") if isinstance(data, dict) else []
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            return arr[0]
        return None


# ---------------------------------------------------------------------------
# Rilevamento superficie (l'API V3 di SX Bet NON espone la superficie)
# ---------------------------------------------------------------------------
SURFACES = ("hard", "clay", "grass")
# Etichetta italiana (report/Telegram)
SURFACE_IT = {"hard": "cemento", "clay": "terra battuta",
              "grass": "erba"}

# Nomi superficie canonici + alias accettati (input/output normalizzati)
_SURFACE_ALIASES = {
    "hard": "hard", "hardcourt": "hard", "hard court": "hard",
    "cemento": "hard", "pista dura": "hard",
    "clay": "clay", "clay court": "clay", "terra": "clay",
    "terra battuta": "clay", "terre": "clay",
    "terre battue": "clay", "rosso": "clay",
    "grass": "grass", "grass court": "grass", "erba": "grass",
    "rasen": "grass",
}

# Tornei (o parole) che identificano la superficie, in forma NORMALIZZATA
# (lowercase, senza accenti). La lista NON e' esaustiva per design: se un
# torneo non e' riconosciuto la superficie resta sconosciuta e l'ELO
# aggiorna solo l'overall, senza mai contaminare le superfici note.
# Estendere qui per coprire altri eventi (Challenger/ITF/WTA).
_SURFACE_KEYWORDS = {
    "clay": [
        "clay", "clay court", "terra battuta", "terre battue",
        "terre", "rosso",
        "roland garros", "roland-garros", "french open",
        "monte carlo", "montecarlo",
        "mutua madrid", "madrid open", "madrid",
        "internazionali d italia", "italian open", "rome masters",
        "roma",
        "barcelona open", "conde de godo", "barcellona",
        "bogota", "buenos aires", "cordoba", "rio open",
        "rio de janeiro", "santiago", "sao paulo", "brasil open",
        "montevideo", "south american",
        "estoril", "casablanca", "marrakech", "hassan ii",
        "munich", "geneva open", "geneve", "lyon open", "lyon",
        "gstaad", "umag", "kitzbuhel", "kitzbuehel", "hamburg",
        "bastad", "bastaad", "bucharest", "belgrade", "bordeaux",
        "charleston", "houston", "porsche", "bavarian open",
        "oranjeboom", "santos",
    ],
    "grass": [
        "grass", "grass court", "erba", "rasen", "rasenturnier",
        "wimbledon", "the championships",
        "queens club", "queen s club", "london grass",
        "halle", "terra wortmann", "gerry weber", "boss open",
        "stuttgart",
        "rosmalen", "s hertogenbosch", "den bosch", "libema open",
        "eastbourne", "newport", "mallorca championships",
        "bad homburg", "nottingham", "illy classic",
    ],
    "hard": [
        "hard", "hard court", "cemento", "pista dura", "indoor",
        "us open", "australian open", "melbourne",
        "indian wells", "bnp paribas open",
        "miami open", "miami",
        "cincinnati", "western southern",
        "rogers cup", "canada masters", "canadian open", "toronto",
        "montreal",
        "shanghai masters", "shanghai", "china open", "beijing",
        "paris masters", "paris bercy", "bercy", "rolex paris",
        "tokyo", "japan open", "dubai", "doha", "qatar open",
        "abu dhabi", "acapulco", "mexico open", "los cabos",
        "dallas open", "delray beach", "washington", "dc open",
        "atlanta", "winston salem", "winston-salem",
        "rotterdam", "abn amro", "marseille", "open 13",
        "montpellier", "vienna", "erste bank open", "basel",
        "basilea", "stockholm", "antwerp", "tel aviv", "sofia",
        "metz", "moselle open", "astana", "almaty", "zhuhai",
        "chengdu", "guanzhou",
        "atp finals", "nitto atp finals", "next gen",
        "united cup", "atp cup", "laver cup",
    ],
}


def _normalize_text(text: Optional[str]) -> str:
    """Lowercase senza accenti, solo alfanumerici separati da spazio."""
    if not text:
        return ""
    t = unicodedata.normalize("NFD", str(text))
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    return " ".join("".join(ch if ch.isalnum() else " "
                              for ch in t.lower()).split())


def detect_surface(*texts: Optional[str]) -> Optional[str]:
    """Rileva la superficie (hard|clay|grass) dai campi testuali di un
    mercato tennis SX (leagueLabel/group1/group2/eventName).

    Matching pesato su parole chiave normalizzate: i nomi di torneo
    (multi-parola) valgono 2, le parole generiche 1. Ritorna None se
    nessun torneo/superficie e' riconosciuto o se il testo e' AMBIGUO
    (parita' di punteggio tra superfici): mai indovinare — una superficie
    sbagliata contaminerebbe i rating di quella superficie.
    """
    blob = " ".join(_normalize_text(t) for t in texts if t).strip()
    if not blob:
        return None
    padded = f" {blob} "
    scores = {s: 0 for s in SURFACES}
    for surface, kws in _SURFACE_KEYWORDS.items():
        for kw in kws:
            norm_kw = _normalize_text(kw).strip()
            if not norm_kw:
                continue
            if f" {norm_kw} " in padded:
                scores[surface] += 2 if len(norm_kw.split()) > 1 else 1
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, score = ranked[0]
    if score <= 0:
        return None
    if len(ranked) > 1 and ranked[1][1] == score:
        return None  # ambiguo
    return best


def normalize_surface(surface: Optional[str]) -> Optional[str]:
    """Riporta un nome superficie ai valori canonici (hard|clay|grass)."""
    if not surface:
        return None
    norm = _normalize_text(surface).strip()
    return _SURFACE_ALIASES.get(norm)


def market_surface(market: Dict) -> Optional[str]:
    """Superficie di un mercato tennis SX Bet (None se non rilevabile)."""
    return detect_surface(
        market.get("eventName"), market.get("name"),
        market.get("leagueLabel"), market.get("group1"),
        market.get("group2"))


# ---------------------------------------------------------------------------
# ELO superficie-specifico con time-decay (baseline predittiva tennis)
# ---------------------------------------------------------------------------
@dataclass
class SurfaceRating:
    rating: float
    n: int
    last_ts: float


@dataclass
class PlayerRating:
    rating: float
    n: int
    last_ts: float
    surfaces: Dict[str, SurfaceRating] = field(default_factory=dict)


class TennisElo:
    """ELO a 2 giocatori SUPERFICIE-SPECIFICO con time-decay.

    - prob(r_a, r_b) = 1 / (1 + 10^((r_b - r_a)/400)), formula invariata;
    - rating OVERALL (tutte le superfici) + rating per superficie
      (chiavi SURFACES: hard/clay/grass). Un match su superficie nota
      aggiorna sia l'overall sia quella superficie; un match senza
      superficie riconosciuta aggiorna SOLO l'overall;
    - seeding: i giocatori senza rating (nessun match saldato) vengono
      seminati dalle probabilita' implicite del mercato (devig semplice),
      il miglior prior disponibile. Il seeding NON invecchia (n=0 =>
      nessun time-decay);
    - time-decay: i risultati invecchiano al momento dell'USO. La rating
      efficace `_decayed` regredisce verso il neutro 1500 in base all'eta'
      dell'ultimo match osservato: peso pieno entro ELO_RECENT_DAYS
      (default 30g), poi dimezzamento ogni ELO_HALF_LIFE_DAYS (default
      30g: ~50% a 60 giorni), zero oltre ELO_WINDOW_DAYS (365g). Cosi' i
      risultati recenti pesano piu' di quelli vecchi sia nelle
      probabilita' usate per i +EV sia negli update;
    - probabilita' di un match sulla superficie S: blended rating
      effettiva = overall_eff * (1-w) + surface_S_eff * w, con
      w = min(1, n_S / SURFACE_MIN_MATCHES): la superficie conta davvero
      dopo i primi match saldati su quella superficie, prima l'overall fa
      da smoothing (i rating di superficie nascono dall'overall, quindi
      senza storico superficie le due stime coincidono).

    Persistenza: ratings.json (player -> {rating, n, last_ts, surfaces:
    {surface: {rating, n, last_ts}}}). Caricamento RETROCOMPATIBILE col
    vecchio formato {rating, n, last_ts}: quei rating diventano l'overall
    e le superfici partono vuote (nessuna migrazione distruttiva).
    """

    def __init__(self, ratings_file: Optional[Path] = None,
                 k: Optional[float] = None) -> None:
        self.ratings_file = Path(ratings_file or RATINGS_FILE)
        self.k = k if k is not None else ELO_K
        self.players: Dict[str, PlayerRating] = {}
        self.load()

    # -- matematica -----------------------------------------------------
    @staticmethod
    def prob(r_a: float, r_b: float) -> float:
        return 1.0 / (1.0 + 10 ** ((r_b - r_a) / 400.0))

    @staticmethod
    def implied_rating(p: float) -> float:
        """Rating che, contro un avversario a 1500, produce la prob. p."""
        if not 0.0 < p < 1.0:
            return 1500.0
        return 1500.0 + 400.0 * math.log10(p / (1.0 - p))

    # -- time-decay ------------------------------------------------------
    @staticmethod
    def _decayed(rating: float, n: int, last_ts: float,
                 now: float) -> float:
        """Rating efficace al tempo `now` (time-decay dei risultati).

        - rating seminati dal mercato (n=0) o match con eta' <= RECENT_DAYS
          giorni (ultimi ~30 giorni): peso pieno, nessun decadimento;
        - poi il peso dimezza ogni HALF_LIFE_DAYS giorni (~50% a ~60
          giorni dall'ultimo match);
        - oltre WINDOW_DAYS (365) il risultato e' dimenticato: la rating
          torna al neutro 1500.
        """
        if n <= 0 or not last_ts:
            return rating
        age_days = max(0.0, (now - last_ts) / 86400.0)
        if age_days <= ELO_RECENT_DAYS:
            return rating
        if age_days >= ELO_WINDOW_DAYS:
            return 1500.0
        w = 0.5 ** ((age_days - ELO_RECENT_DAYS) / ELO_HALF_LIFE_DAYS)
        return 1500.0 + (rating - 1500.0) * w

    # -- seeding ---------------------------------------------------------
    def ensure(self, player: str, market_prob: float) -> float:
        """Ritorna il rating del giocatore; se assente lo semina dalla
        probabilita' implicita del mercato (devig sul prezzo best back)."""
        name = (player or "").strip()
        if not name:
            return 1500.0
        pr = self.players.get(name)
        if pr is not None:
            return pr.rating
        self.players[name] = PlayerRating(
            rating=self.implied_rating(market_prob), n=0, last_ts=0.0)
        return self.players[name].rating

    def ensure_pair(self, player_a: str, p_a: float,
                    player_b: str, p_b: float) -> None:
        """Seeding COERENTE di una coppia (rating OVERALL): assegna rating
        simmetrici attorno a 1500 tali che prob(r_a, r_b) == p_a.

        NB: seminare i due giocatori separatamente con `ensure` (implied_
        rating contro 1500) NON funziona: prob(r_a, r_b) distorcerebbe la
        probabilita' (es. p_a=0.773 -> 0.920). Qui invece:
          d = 400*log10(p_a/(1-p_a)); r_a = 1500 + d/2; r_b = 1500 - d/2
        cosi' prob(r_a, r_b) = 1/(1+10^(-d/400)) = p_a.
        I giocatori gia' noti (con storico) mantengono il loro rating.
        Le superfici NON vengono seminate qui: nascono lazy dal primo
        update su quella superficie (vedi `update`).
        """
        a = (player_a or "").strip()
        b = (player_b or "").strip()
        if not a or not b or not (0.0 < p_a < 1.0):
            return
        known_a = self.players.get(a) is not None
        known_b = self.players.get(b) is not None
        if known_a and known_b:
            return
        d = 400.0 * math.log10(p_a / (1.0 - p_a))
        if not known_a:
            self.players[a] = PlayerRating(
                rating=round(1500.0 + d / 2.0, 1), n=0, last_ts=0.0)
        if not known_b:
            self.players[b] = PlayerRating(
                rating=round(1500.0 - d / 2.0, 1), n=0, last_ts=0.0)

    # -- learning --------------------------------------------------------
    def update(self, winner: str, loser: str,
               surface: Optional[str] = None,
               ts: Optional[float] = None) -> None:
        """Aggiorna l'ELO col risultato reale (settlement).

        Aggiorna SEMPRE l'overall; se la superficie del match e' nota
        (canonica o alias) aggiorna anche il rating di quella superficie
        (creandolo all'occorrenza, seminato dalla rating overall
        efficace). L'update usa le rating EFFICACI (time-decay applicato
        prima: i risultati vecchi pesano meno) e il K pieno.
        """
        now = ts if ts is not None else time.time()
        pr_w = self.players.get(winner)
        pr_l = self.players.get(loser)
        if pr_w is None or pr_l is None:
            logger.warning("tennis elo: update senza rating per %s/%s",
                           winner, loser)
            return
        self._apply_update(pr_w, pr_l, now)
        surf = normalize_surface(surface)
        if surf is not None:
            sw = pr_w.surfaces.get(surf)
            if sw is None:
                sw = SurfaceRating(rating=self._effective_pr(pr_w, now),
                                   n=0, last_ts=now)
                pr_w.surfaces[surf] = sw
            sl = pr_l.surfaces.get(surf)
            if sl is None:
                sl = SurfaceRating(rating=self._effective_pr(pr_l, now),
                                   n=0, last_ts=now)
                pr_l.surfaces[surf] = sl
            self._apply_update(sw, sl, now)

    def _apply_update(self, rw: "RatingObj", rl: "RatingObj",
                      now: float) -> None:
        """Un passo ELO standard con K pieno su una coppia di rating
        (overall o superficie). Prima ogni rating viene fatta invecchiare
        (time-decay), poi il risultato appena osservato muove a K pieno:
        e' cosi' che i risultati recenti pesano piu' di quelli vecchi.
        """
        eff_w = self._decayed(rw.rating, rw.n, rw.last_ts, now)
        eff_l = self._decayed(rl.rating, rl.n, rl.last_ts, now)
        expected = self.prob(eff_w, eff_l)
        rw.rating = round(eff_w + self.k * (1.0 - expected), 1)
        rl.rating = round(eff_l - self.k * expected, 1)
        rw.n += 1
        rl.n += 1
        rw.last_ts = now
        rl.last_ts = now

    # -- probabilita' ----------------------------------------------------
    @staticmethod
    def _effective_pr(pr: "RatingObj", now: float) -> float:
        return TennisElo._decayed(pr.rating, pr.n, pr.last_ts, now)

    def overall_effective(self, player: str,
                          now: Optional[float] = None) -> Optional[float]:
        """Rating overall efficace al tempo `now` (None se giocatore
        ignoto). Applica il time-decay: un giocatore inattivo da piu' di
        ~60 giorni vede il suo rating regredire verso il neutro."""
        pr = self.players.get(player)
        if pr is None:
            return None
        return self._decayed(pr.rating, pr.n, pr.last_ts,
                             now if now is not None else time.time())

    def blended_rating(self, player: str,
                       surface: Optional[str] = None,
                       now: Optional[float] = None) -> Optional[float]:
        """Rating efficace usata per la probabilita' di un match su
        `surface`. Senza storico su quella superficie ritorna l'overall
        efficace; con storico combina overall e superficie con peso che
        sale col numero di match saldati su quella superficie
        (SURFACE_MIN_MATCHES = fiducia piena).
        """
        now = now if now is not None else time.time()
        overall = self.overall_effective(player, now)
        if overall is None:
            return None
        surf = normalize_surface(surface)
        pr = self.players[player]
        sr = pr.surfaces.get(surf) if surf is not None else None
        if sr is None or sr.n <= 0:
            return overall
        w = min(1.0, sr.n / SURFACE_MIN_MATCHES)
        surf_r = self._decayed(sr.rating, sr.n, sr.last_ts, now)
        return (1.0 - w) * overall + w * surf_r

    def match_prob(self, player_a: str, player_b: str,
                   surface: Optional[str] = None) -> float:
        """Probabilita' di vittoria di player_a su `surface` (surface None
        = overall, usato quando la superficie del torneo e' sconosciuta)."""
        r_a = self.blended_rating(player_a, surface)
        r_b = self.blended_rating(player_b, surface)
        if r_a is None or r_b is None:
            return 0.5
        return self.prob(r_a, r_b)

    # -- persistenza -----------------------------------------------------
    def save(self) -> None:
        try:
            self.ratings_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {name: asdict(pr)
                       for name, pr in self.players.items()}
            self.ratings_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning("tennis elo: salvataggio ratings fallito: %s", e)

    def load(self) -> None:
        """Carica ratings.json. Retrocompatibile: i file nel vecchio
        formato {player: {rating, n, last_ts}} (senza `surfaces`)
        vengono caricati come overall con superfici vuote.
        """
        try:
            if not self.ratings_file.exists():
                return
            payload = json.loads(self.ratings_file.read_text(encoding="utf-8"))
            for name, data in payload.items():
                surfaces: Dict[str, SurfaceRating] = {}
                for s, sd in (data.get("surfaces") or {}).items():
                    try:
                        surfaces[str(s)] = SurfaceRating(
                            rating=float(sd.get("rating", 1500)),
                            n=int(sd.get("n", 0)),
                            last_ts=float(sd.get("last_ts", 0.0)))
                    except (TypeError, ValueError):
                        continue
                self.players[name] = PlayerRating(
                    rating=float(data.get("rating", 1500)),
                    n=int(data.get("n", 0)),
                    last_ts=float(data.get("last_ts", 0.0)),
                    surfaces=surfaces)
        except Exception as e:
            logger.warning("tennis elo: caricamento ratings fallito: %s", e)


# ---------------------------------------------------------------------------
# Ledger SQLite dedicato (data/tennis_sandbox/ledger.db)
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    market_hash TEXT NOT NULL,
    event_id TEXT NOT NULL,
    league TEXT NOT NULL DEFAULT '',
    surface TEXT NOT NULL DEFAULT '',
    player_a TEXT NOT NULL,
    player_b TEXT NOT NULL,
    selection TEXT NOT NULL,
    price REAL NOT NULL,
    model_prob REAL NOT NULL,
    ev REAL NOT NULL,
    stake REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    settled_at TEXT,
    profit REAL,
    UNIQUE(market_hash, selection)
);

-- Osservazioni: OGNI match scansionato con book valido, saldato col
-- risultato reale per far imparare l'ELO anche senza segnali +EV
-- (altrimenti deadlock: nessun ghost bet -> nessun settlement -> l'ELO
-- non divergerebbe mai dal mercato).
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    market_hash TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    league TEXT NOT NULL DEFAULT '',
    surface TEXT NOT NULL DEFAULT '',
    player_a TEXT NOT NULL,
    player_b TEXT NOT NULL,
    price_a REAL NOT NULL,
    price_b REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    settled_at TEXT,
    winner TEXT
);
"""


def _migrate_ledger(conn: sqlite3.Connection) -> None:
    """Migrazioni idempotenti per DB nati prima dell'era superficie.

    Aggiunge la colonna `surface` a signals/observations se manca (i
    DB esistenti su Railway continuano a funzionare senza reset).
    """
    for table, column, decl in (
            ("signals", "surface", "TEXT NOT NULL DEFAULT ''"),
            ("observations", "surface", "TEXT NOT NULL DEFAULT ''")):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            logger.info("ledger tennis: colonna %s.%s aggiunta",
                        table, column)
    conn.commit()


def _open_ledger(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_ledger(conn)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Motore sandbox
# ---------------------------------------------------------------------------
def kelly_stake(prob: float, odds: float, bankroll: float,
                fraction: Optional[float] = None,
                max_pct: Optional[float] = None) -> float:
    """Stake Kelly frazionario (ghost bet): f = (p*o - 1)/(o - 1).

    Ritorna 0 se l'EV non e' positivo (mai stake negativi). Cap al
    max_pct del bankroll virtuale. Rounding a 2 decimali.
    """
    if not (0.0 < prob < 1.0) or odds <= 1.0 or bankroll <= 0:
        return 0.0
    ev = prob * odds - 1.0
    if ev <= 0:
        return 0.0
    full = (prob * odds - 1.0) / (odds - 1.0)
    stake = bankroll * full * (fraction if fraction is not None
                               else KELLY_FRACTION)
    cap = bankroll * (max_pct if max_pct is not None else MAX_STAKE_PCT)
    return round(min(max(0.0, stake), cap), 2)


def devig(p_a: float, p_b: float) -> float:
    """Probabilita' implicite normalizzate (somma a 1)."""
    tot = p_a + p_b
    if tot <= 0:
        return 0.5
    return p_a / tot


class TennisSandbox:
    """Scansione +EV sul tennis SX Bet, ghost bet e settlement paper.

    Mai ordini reali: il client usato e' di SOLA lettura (o un fake nei
    test). Tutte le scritture vanno sul ledger dedicato e su ratings.json.
    """

    def __init__(self, client: Optional[SxTennisClient] = None,
                 data_dir: Optional[Path] = None,
                 ev_min: Optional[float] = None,
                 bankroll: Optional[float] = None,
                 elo: Optional[TennisElo] = None) -> None:
        self.client = client if client is not None else SxTennisClient()
        self.data_dir = Path(data_dir or TENNIS_DATA_DIR)
        self.ledger_path = self.data_dir / "ledger.db"
        self.ev_min = ev_min if ev_min is not None else EV_MIN
        self.bankroll = bankroll if bankroll is not None else PAPER_BANKROLL
        self.elo = elo if elo is not None else TennisElo(
            ratings_file=self.data_dir / "ratings.json")
        self._conn = None

    # -- ledger ----------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _open_ledger(self.ledger_path)
        return self._conn

    def _insert_observation(self, market: Dict, price_a: float,
                            price_b: float,
                            surface: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        self.conn.execute(
            """INSERT OR IGNORE INTO observations
               (ts, day, market_hash, event_id, league, surface,
                player_a, player_b, price_a, price_b)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now.isoformat(), day, market.get("marketHash", ""),
             market.get("sportXeventId", ""),
             market.get("leagueLabel") or market.get("group1") or "",
             surface or "",
             (market.get("teamOneName") or market.get("outcomeOneName")
              or "").strip(),
             (market.get("teamTwoName") or market.get("outcomeTwoName")
              or "").strip(),
             price_a, price_b))
        self.conn.commit()

    def _insert_or_update_signal(self, market: Dict, selection: str,
                                 price: float, model_prob: float,
                                 ev: float, stake: float,
                                 surface: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        player_a = (market.get("teamOneName")
                    or market.get("outcomeOneName") or "").strip()
        player_b = (market.get("teamTwoName")
                    or market.get("outcomeTwoName") or "").strip()
        market_hash = market.get("marketHash", "")
        event_id = market.get("sportXeventId", "")
        league = market.get("leagueLabel") or market.get("group1") or ""
        self.conn.execute(
            """INSERT INTO signals
               (ts, day, market_hash, event_id, league, surface,
                player_a, player_b, selection, price, model_prob, ev, stake)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(market_hash, selection) DO UPDATE SET
                 ts=excluded.ts, day=excluded.day, league=excluded.league,
                 surface=excluded.surface,
                 price=excluded.price, model_prob=excluded.model_prob,
                 ev=excluded.ev, stake=excluded.stake
               WHERE signals.status = 'open'""",
            (now.isoformat(), day, market_hash, event_id, league,
             surface or "", player_a, player_b, selection, price,
             model_prob, ev, stake))
        self.conn.commit()

    # -- scansione -------------------------------------------------------
    def scan(self, max_markets: int = 500) -> Dict:
        """Una scansione: mercati attivi -> book -> ELO -> EV -> ghost bet.

        Ritorna un riepilogo {markets, with_book, signals, errors}.
        Fail-closed: nessuna eccezione verso il chiamante.
        """
        result = {"markets": 0, "with_book": 0, "signals": 0, "errors": 0}
        markets = self.client.active_markets(max_markets=max_markets)
        result["markets"] = len(markets)
        for m in markets:
            market_hash = m.get("marketHash", "")
            if not market_hash:
                continue
            try:
                book = self.client.orderbook(market_hash)
                if book is None:
                    continue
                o1 = book.get("outcomeOne") or []
                o2 = book.get("outcomeTwo") or []
                if not o1 or not o2:
                    continue
                price_a = pct_scaled_to_decimal(
                    (o1[0] if isinstance(o1[0], dict) else {}).get(
                        "percentageOdds"))
                price_b = pct_scaled_to_decimal(
                    (o2[0] if isinstance(o2[0], dict) else {}).get(
                        "percentageOdds"))
                if price_a is None or price_b is None:
                    continue
                result["with_book"] += 1
                if not (MIN_ODDS <= price_a <= MAX_ODDS
                        and MIN_ODDS <= price_b <= MAX_ODDS):
                    continue
                # Coerenza del mercato: senza questa guardia i book sporchi
                # generano +EV finti (vedi commento MIN_INV_SUM).
                inv_sum = 1.0 / price_a + 1.0 / price_b
                if not (MIN_INV_SUM <= inv_sum <= MAX_INV_SUM):
                    continue
                # Seeding ELO dalle probabilita' implicite del mercato
                # (devig semplice) per i giocatori senza storico.
                impl_a = devig(1.0 / price_a, 1.0 / price_b)
                player_a = (m.get("teamOneName")
                            or m.get("outcomeOneName") or "").strip()
                player_b = (m.get("teamTwoName")
                            or m.get("outcomeTwoName") or "").strip()
                if not player_a or not player_b:
                    continue
                self.elo.ensure_pair(player_a, impl_a, player_b, 1.0 - impl_a)
                # Superficie del torneo (None se non riconoscibile): la
                # probabilita' usa il rating superficie-specifico se c'e'
                # storico su quella superficie, altrimenti l'overall.
                surface = market_surface(m)
                # Osservazione: ogni match con book valido finisce nel
                # ledger per alimentare l'apprendimento ELO (vedi _SCHEMA),
                # con la superficie per gli update superficie-specifici.
                self._insert_observation(m, price_a, price_b, surface)
                prob_a = self.elo.match_prob(player_a, player_b,
                                             surface=surface)
                prob_b = 1.0 - prob_a
                ev_a = prob_a * price_a - 1.0
                ev_b = prob_b * price_b - 1.0
                for sel, prob, price, ev in (
                        (player_a, prob_a, price_a, ev_a),
                        (player_b, prob_b, price_b, ev_b)):
                    if ev < self.ev_min:
                        continue
                    stake = kelly_stake(prob, price, self.bankroll)
                    self._insert_or_update_signal(
                        m, sel, price, prob, ev, stake, surface)
                    result["signals"] += 1
                    su = f" (su {SURFACE_IT.get(surface, '')})" if surface \
                        else ""
                    logger.info(
                        "tennis sandbox +EV: %s vs %s — %s @%.2f "
                        "(p=%.3f, EV %+.2f%%, stake paper %.2f)%s",
                        player_a, player_b, sel, price, prob, ev * 100,
                        stake, su)
            except Exception as e:
                result["errors"] += 1
                logger.warning("tennis sandbox: market %s fallito: %s",
                               market_hash, e)
        try:
            self.elo.save()
        except Exception as e:
            logger.warning("tennis sandbox: elo save: %s", e)
        self._write_heartbeat()
        return result

    # -- settlement ------------------------------------------------------
    def settle(self) -> List[Dict]:
        """Salda i ghost bet open il cui mercato risulta chiuso, e prima
        ancora salda le OSSERVAZIONI (tutti i match scansionati) col
        risultato reale per far imparare l'ELO.

        outcome 1 = vince outcomeOne (player_a), 2 = vince outcomeTwo
        (player_b), 0 = void (stake restituito). Aggiorna l'ELO col
        vincitore reale e scrive profit/settled_at sul ledger.
        """
        settled: List[Dict] = []
        # 1) Osservazioni -> apprendimento ELO (indipendente dai segnali)
        obs = self.conn.execute(
            "SELECT * FROM observations WHERE status = 'open'").fetchall()
        for row in obs:
            market_hash = row["market_hash"]
            info = self.client.market(market_hash)
            if info is None:
                continue
            outcome = info.get("outcome")
            if outcome is None:
                continue  # mercato non ancora saldato
            try:
                outcome = int(outcome)
            except (TypeError, ValueError):
                continue
            player_a = row["player_a"]
            player_b = row["player_b"]
            if outcome == 1:
                winner, loser = player_a, player_b
            elif outcome == 2:
                winner, loser = player_b, player_a
            else:  # void / no contest
                winner = loser = None
            if winner is not None and loser is not None:
                self.elo.ensure_pair(player_a, 0.5, player_b, 0.5)
                # Apprendimento ELO col risultato reale: aggiorna l'overall
                # e, se la superficie del match e' nota, anche il rating
                # superficie-specifico (time-decay incluso).
                self.elo.update(winner, loser,
                                surface=row["surface"] or None)
            now = datetime.now(timezone.utc).isoformat()
            self.conn.execute(
                "UPDATE observations SET status='settled', winner=?, "
                "settled_at=? WHERE id=?",
                (winner, now, row["id"]))
            self.conn.commit()
            logger.info("tennis sandbox obs: %s vs %s saldata (vince %s)",
                        player_a, player_b, winner or "VOID")
        # 2) Segnali +EV -> profit del ghost bet
        rows = self.conn.execute(
            "SELECT * FROM signals WHERE status = 'open'").fetchall()
        for row in rows:
            market_hash = row["market_hash"]
            info = self.client.market(market_hash)
            if info is None:
                continue
            outcome = info.get("outcome")
            if outcome is None:
                continue  # mercato non ancora saldato (status ACTIVE/INACTIVE)
            try:
                outcome = int(outcome)
            except (TypeError, ValueError):
                continue
            sel = row["selection"]
            stake = row["stake"] or 0.0
            player_a = row["player_a"]
            player_b = row["player_b"]
            if outcome == 1:
                winner, loser = player_a, player_b
            elif outcome == 2:
                winner, loser = player_b, player_a
            else:  # void / no contest
                status, profit = "void", 0.0
                winner = loser = None
            if outcome in (1, 2):
                won = (sel == winner)
                status = "won" if won else "lost"
                profit = round(stake * (row["price"] - 1.0), 2) if won \
                    else -stake
                # NB: l'ELO si aggiorna SOLO dal ramo observations (sopra),
                # non qui: il match viene processato una sola volta.
            now = datetime.now(timezone.utc).isoformat()
            self.conn.execute(
                "UPDATE signals SET status=?, profit=?, settled_at=? "
                "WHERE id=?",
                (status, profit, now, row["id"]))
            self.conn.commit()
            settled.append({
                "market_hash": market_hash,
                "event": f"{player_a} vs {player_b}",
                "selection": sel, "status": status, "profit": profit,
                "stake": stake,
            })
            logger.info("tennis sandbox settle: %s — %s (%s) P/L %+.2f",
                        f"{player_a} vs {player_b}", sel, status, profit)
        try:
            self.elo.save()
        except Exception as e:
            logger.warning("tennis sandbox: elo save: %s", e)
        return settled

    # -- reportistica ----------------------------------------------------
    def report(self, days: Optional[int] = None) -> Dict:
        """Aggregati del ledger: opportunita'/giorno, ROI teorico, win rate.

        days=None -> tutto il ledger. Ritorna un dict con il riepilogo
        complessivo, la serie giornaliera (ultimi `days` giorni o tutti) e
        il confronto EV medio vs ROI realizzato.
        """
        conn = self.conn
        obs_total = conn.execute(
            "SELECT COUNT(*) n, "
            "       COALESCE(SUM(CASE WHEN status='settled' THEN 1 "
            "ELSE 0 END),0) settled "
            "FROM observations").fetchone()
        total = conn.execute(
            "SELECT COUNT(*) n, "
            "       COALESCE(SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),0) open, "
            "       COALESCE(SUM(CASE WHEN status IN ('won','lost','void') "
            "THEN 1 ELSE 0 END),0) closed, "
            "       COALESCE(SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),0) won, "
            "       COALESCE(SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),0) lost, "
            "       COALESCE(SUM(CASE WHEN status='void' THEN 1 ELSE 0 END),0) void, "
            "       COALESCE(SUM(stake),0) stake_total, "
            "       COALESCE(SUM(CASE WHEN status IN ('won','lost','void') "
            "THEN stake ELSE 0 END),0) stake_closed, "
            "       COALESCE(SUM(profit),0) profit_total, "
            "       COALESCE(AVG(ev),0) avg_ev "
            "FROM signals").fetchone()
        daily_rows = conn.execute(
            "SELECT day, COUNT(*) n, "
            "       COALESCE(SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),0) open, "
            "       COALESCE(SUM(CASE WHEN status IN ('won','lost','void') "
            "THEN 1 ELSE 0 END),0) closed, "
            "       COALESCE(SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),0) won, "
            "       COALESCE(SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),0) lost, "
            "       COALESCE(SUM(CASE WHEN status='void' THEN 1 ELSE 0 END),0) void, "
            "       COALESCE(SUM(stake),0) stake_total, "
            "       COALESCE(SUM(profit),0) profit_total "
            "FROM signals GROUP BY day ORDER BY day DESC").fetchall()
        if days:
            daily = [dict(r) for r in daily_rows[:days]]
        else:
            daily = [dict(r) for r in daily_rows]
        # Riepilogo PER SUPERFICIE (ELO superficie-specifico): quante
        # osservazioni apprese e quanti segnali chiusi su ogni superficie.
        obs_surf = conn.execute(
            "SELECT COALESCE(surface,'') surface, COUNT(*) n, "
            "       COALESCE(SUM(CASE WHEN status='settled' THEN 1 "
            "ELSE 0 END),0) settled "
            "FROM observations GROUP BY surface ORDER BY surface").fetchall()
        sig_surf = conn.execute(
            "SELECT COALESCE(surface,'') surface, COUNT(*) n, "
            "       COALESCE(SUM(CASE WHEN status IN ('won','lost','void') "
            "THEN 1 ELSE 0 END),0) closed, "
            "       COALESCE(SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),0) won, "
            "       COALESCE(SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),0) lost "
            "FROM signals GROUP BY surface ORDER BY surface").fetchall()
        sig_by_surf = {r["surface"]: r for r in sig_surf}
        surfaces = []
        for r in obs_surf:
            s = r["surface"] or ""
            sig = sig_by_surf.get(s)
            surfaces.append({
                "surface": s,
                "label": SURFACE_IT.get(s, "sconosciuta") if s
                else "sconosciuta",
                "observations": int(r["n"] or 0),
                "observations_settled": int(r["settled"] or 0),
                "signals": int((sig["n"] if sig else 0) or 0),
                "closed": int((sig["closed"] if sig else 0) or 0),
                "won": int((sig["won"] if sig else 0) or 0),
                "lost": int((sig["lost"] if sig else 0) or 0),
            })
        n_closed = int(total["closed"] or 0)
        stake_closed = float(total["stake_closed"] or 0.0)
        roi = (float(total["profit_total"] or 0.0) / stake_closed * 100.0) \
            if stake_closed > 0 else None
        win_rate = (int(total["won"] or 0) / n_closed * 100.0) \
            if n_closed > 0 else None
        settled = int(total["won"] or 0) + int(total["lost"] or 0)
        rep = {
            "observations": int(obs_total["n"] or 0),
            "observations_settled": int(obs_total["settled"] or 0),
            "total_signals": int(total["n"] or 0),
            "open": int(total["open"] or 0),
            "closed": n_closed,
            "won": int(total["won"] or 0),
            "lost": int(total["lost"] or 0),
            "void": int(total["void"] or 0),
            "settled_non_void": settled,
            "win_rate_pct": round(win_rate, 2) if win_rate is not None
            else None,
            "stake_total": round(float(total["stake_total"] or 0.0), 2),
            "stake_closed": round(stake_closed, 2),
            "profit_total": round(float(total["profit_total"] or 0.0), 2),
            "roi_pct": round(roi, 2) if roi is not None else None,
            "avg_ev_pct": round(float(total["avg_ev"] or 0.0) * 100.0, 2),
            "daily": daily,
            "surfaces": surfaces,
        }
        try:
            REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            REPORT_FILE.write_text(json.dumps(rep, ensure_ascii=False,
                                              indent=1), encoding="utf-8")
        except Exception as e:
            logger.warning("tennis sandbox: report file fallito: %s", e)
        return rep

    # -- heartbeat -------------------------------------------------------
    def _write_heartbeat(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_FILE.write_text(json.dumps({
                "ts": time.time(),
                "sport_id": TENNIS_SPORT_ID,
                "market_type": TENNIS_MARKET_TYPE,
                "pid": os.getpid(),
            }), encoding="utf-8")
        except Exception as e:
            logger.warning("tennis sandbox: heartbeat fallito: %s", e)

    # -- chiusura --------------------------------------------------------
    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


# ---------------------------------------------------------------------------
# Formattazione report (Telegram/CLI)
# ---------------------------------------------------------------------------
def format_report(rep: Dict) -> str:
    """Testo leggibile del report sandbox tennis (per Telegram/CLI)."""
    lines = [
        "🎾 *SANDBOX TENNIS (paper trading)*",
        f"Match osservati: {rep['observations']} "
        f"(saldati {rep['observations_settled']} per l'apprendimento ELO)",
        f"Segnali +EV totali: *{rep['total_signals']}* "
        f"(aperti: {rep['open']}, chiusi: {rep['closed']})",
        f"Vinti {rep['won']} / persi {rep['lost']} / void {rep['void']}",
    ]
    if rep["win_rate_pct"] is not None:
        lines.append(f"Win rate (non-void): *{rep['win_rate_pct']:.1f}%*")
    if rep["roi_pct"] is not None:
        lines.append(f"ROI teorico: *{rep['roi_pct']:+.2f}%* "
                     f"(P/L {rep['profit_total']:+.2f} su "
                     f"{rep['stake_closed']:.2f} paper)")
    else:
        lines.append("ROI teorico: in attesa del primo settlement")
    lines.append(f"EV medio segnali: {rep['avg_ev_pct']:+.2f}%")
    if rep.get("surfaces"):
        lines.append("")
        lines.append("🏟️ *Per superficie*")
        for s in rep["surfaces"]:
            lines.append(
                f"  • {s['label']}: {s['observations_settled']} match "
                f"appresi, {s['closed']} segnali chiusi "
                f"(✅{s['won']}/❌{s['lost']})")
    if rep["daily"]:
        lines.append("")
        lines.append("📅 *Per giornata*")
        for d in rep["daily"][:7]:
            roi_d = (d["profit_total"] / d["stake_total"] * 100.0) \
                if d["stake_total"] else 0.0
            lines.append(
                f"  {d['day']}: {d['n']} segnali "
                f"(✅{d['won']}/❌{d['lost']}/⚪{d['void']}) "
                f"ROI {roi_d:+.1f}%")
    return "\n".join(lines)


def run_scan_settle(max_markets: int = 500) -> Dict:
    """Scan + settle in un colpo (per il job bot e per --loop)."""
    sb = TennisSandbox()
    try:
        scan_res = sb.scan(max_markets=max_markets)
        settled = sb.settle()
        scan_res["settled"] = settled
        return scan_res
    finally:
        sb.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Tennis sandbox — paper trading Moneyline 2 vie su SX Bet "
                    "(nessun ordine reale, ledger dedicato)")
    ap.add_argument("--scan", action="store_true",
                    help="Una scansione +EV (registra i segnali sul ledger)")
    ap.add_argument("--settle", action="store_true",
                    help="Salda i ghost bet aperti il cui mercato e' chiuso")
    ap.add_argument("--loop", type=int, default=0,
                    help="Loop continuo ogni N secondi (scan+settle; "
                         "0 = scan singolo)")
    ap.add_argument("--report", action="store_true",
                    help="Report aggregato del ledger")
    ap.add_argument("--json", action="store_true",
                    help="Output JSON (per --report o --scan)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.report:
        sb = TennisSandbox()
        try:
            rep = sb.report()
        finally:
            sb.close()
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print(format_report(rep))
        return 0

    def _one_pass() -> None:
        sb = TennisSandbox()
        try:
            res = sb.scan()
            settled = sb.settle()
            if args.json:
                print(json.dumps({**res, "settled": settled},
                                 ensure_ascii=False))
                return
            logger.info("tennis sandbox: %d mercati, %d book, %d segnali, "
                        "%d saldati, %d errori",
                        res["markets"], res["with_book"], res["signals"],
                        len(settled), res["errors"])
        finally:
            sb.close()

    logger.info("tennis sandbox: run avviato (sportId=%d, type=%d, EV>=%+.0f%%)",
                TENNIS_SPORT_ID, TENNIS_MARKET_TYPE, EV_MIN * 100)
    _one_pass()
    if args.loop:
        logger.info("tennis sandbox: loop ogni %ds (CTRL+C per fermare)",
                    args.loop)
        try:
            while True:
                time.sleep(args.loop)
                _one_pass()
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())