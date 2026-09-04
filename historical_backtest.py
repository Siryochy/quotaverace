"""historical_backtest.py — Backtest storico walk-forward (senza look-ahead).

Valida lo stack di produzione corrente su 4 stagioni di calcio europeo
(football-data.co.uk: E0/D1/I1/SP1/F1/N1/P1/B1/T1/G1, 2022-23 → 2025-26,
~12.900 partite) con le quote OPENING (entry) e CLOSING Pinnacle (riferimento):

  1. RATING: repliche time-decay di rating_engine, calcolate SOLO dalle
     partite precedenti (walk-forward, mai dati futuri).
  2. MODELLO: Poisson/Dixon-Coles (poisson_engine) → prob 1X2 + OU2.5.
  3. MERCATO: devig power delle quote opening (market_calib.devig).
  4. BLEND: blend_probability dinamico per lega (market_calib).
  5. ENSEMBLE: XGBoost (o Logistic) calibrato con PAVA (ml_ensemble +
     probability_calibration), retrain periodico walk-forward sulle sole
     partite gia' chiuse.
  6. FILTRO VALUE: is_sane (EV 3-15%, quota 1.50-5.00, edge >= +3pp,
     strong_value >= +5pp).
  7. ANTI-OVERCONFIDENCE (04/09): shrink verso il mercato dopo il blend,
     cap sull'edge e sotto-peso dei pareggi (vedi costanti SHRINK_TO_MARKET /
     MAX_EDGE / DRAW_PENALTY).
  8. STAKING: Kelly 1/4 (kelly_euro, cap 3% bankroll), 1 bet per partita
     (miglior EV tra i candidati value/strong_value — niente correlazioni
     intra-partita).
  9. CLV: entry = opening odds dell'esito giocato, closing = Pinnacle
     closing -> clv_raw + clv_vig_free (con l'intero mercato closing).

Metriche: ROI, Max Drawdown (curva bankroll), hit rate, P/L, CLV medio
raw/vig-free, per lega, per stagione e per mercato. Confronto esplicito
"con ensemble" vs "solo blend" per isolare il contributo del ML.

CLI:
  venv/bin/python historical_backtest.py                 # full
  venv/bin/python historical_backtest.py --limit 500     # test rapido
  venv/bin/python historical_backtest.py --json          # solo JSON
  venv/bin/python historical_backtest.py --no-download   # usa CSV in cache
  venv/bin/python historical_backtest.py --shrink 0.3 --edge-cap 0.08 \
      --draw-penalty 0.7     # override anti-overconfidence

Flag anti-overconfidence (default: shrink=0.50, edge-cap=0.10,
draw-penalty=0.80): usali per misurare l'impatto di ogni correzione.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# garantisce che .env sia caricato (per ODDS_API_KEY, anche se qui non serve)
try:
    from config import load_dotenv
    load_dotenv()
except Exception:
    pass

DATA_DIR = Path(__file__).parent / "data" / "backtest_hist"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# --- Config ---
# Stagioni su cui si PUNTA (backtest) e stagione di WARM-UP dei rating
# (2021-22: processata nei rating ma senza bet, come in produzione dove lo
# storico 2022-24 alimenta i rating prima della prima puntata).
SEASONS = ["2223", "2324", "2425", "2526"]
WARMUP_SEASONS = ["2122"]
BET_SEASONS = set(SEASONS)
LEAGUE_CODES = {
    "E0": "Premier League", "D1": "Bundesliga", "I1": "Serie A",
    "SP1": "La Liga", "F1": "Ligue 1", "N1": "Eredivisie",
    "P1": "Liga Portugal", "B1": "Belgian Pro League",
    "T1": "Turkey Super Lig", "G1": "Greek Super League",
}
BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"

BANKROLL0 = 1000.0        # euro
KELLY_FRACTION = 0.25     # 1/4 Kelly (come value_filter)
STAKE_CAP = 0.03          # cap 3% bankroll (MAX_STAKE_PCT)
MIN_STAKE = 0.0           # nessun floor: l'edge va misurato su TUTTE le pick

# --- Anti-overconfidence (dal primo run: il modello sovrastimava 7-18pp ---
# --- a ogni bucket di probabilita' e il CLV era negativo ovunque) --------
# 1) SHRINK: dopo il blend modello+mercato, la deviazione dal mercato viene
#    ridotta (0.50 = meta' strada verso il mercato). Il mercato e' la stima
#    piu' calibrata: meno fiducia nel modello = meno falsi segnali.
SHRINK_TO_MARKET = 0.50
# 2) CAP sull'edge: l'edge finale sul mercato non supera MAI +10pp
#    (gli edge enormi sono quasi sempre errore del modello: winner's curse).
MAX_EDGE = 0.10
# 3) DRAW PENALTY: i pareggi sono sistematicamente sovrastimati dal modello
#    Poisson/DC -> sotto-peso la probabilita' X prima di normalizzare.
DRAW_PENALTY = 0.80
# 4) MAX ODDS: i longshot 4.0-5.0 perdono -24% (winner's curse sui
#    longshot); il pocket 3.0-4.0 e' l'unico positivo. Coerente con
#    LONG_SHOT_ODDS=3.5 di produzione.
MAX_ODDS = 5.0

RETRAIN_EVERY = 1000      # retrain ensemble ogni N partite chiuse
MAX_TRAIN_ROWS = 8000     # tetto righe di training per il retrain
MIN_MATCHES = 6           # soglia rating (coerente con rating_engine)
HALF_LIFE_DAYS = 100.0
PRIOR_MATCHES = 6.0
GLOBAL_H, GLOBAL_A = 1.52, 1.28   # gol medi (rating_engine)

# mappa esito football-data -> nostro
FTR_MAP = {"H": "1", "D": "X", "A": "2"}


# ---------------------------------------------------------------------------
# Dati
# ---------------------------------------------------------------------------

def _download_csv(season: str, code: str) -> Optional[Path]:
    """Scarica il CSV della stagione/lega in cache. Ritorna il path."""
    dest = DATA_DIR / f"{season}_{code}.csv"
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    url = BASE_URL.format(season=season, code=code)
    try:
        r = requests.get(url, timeout=60)
        if r.status_code != 200 or not r.text.strip():
            print(f"  ⚠️  download fallito {url} ({r.status_code})")
            return None
        dest.write_text(r.text, encoding="utf-8")
        return dest
    except Exception as e:
        print(f"  ⚠️  errore download {url}: {e}")
        return None


def _f(v) -> Optional[float]:
    """Converte una cella CSV in float (None se vuota/invalida)."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_matches(no_download: bool = False) -> List[Dict]:
    """Carica tutte le partite dei CSV (cache o download). Ritorna lista
    ordinata per data crescente. Le stagioni WARMUP sono caricate ma
    marcate bet=False (solo per i rating)."""
    matches: List[Dict] = []
    for season in WARMUP_SEASONS + SEASONS:
        for code, league in LEAGUE_CODES.items():
            if no_download:
                p = DATA_DIR / f"{season}_{code}.csv"
                if not p.exists():
                    print(f"  ⚠️  cache mancante {p.name} (usa --no-download solo "
                          f"dopo un primo download)")
                    continue
            else:
                p = _download_csv(season, code)
                if p is None:
                    continue
            with p.open(newline="", encoding="utf-8-sig") as fh:
                rd = csv.DictReader(fh)
                for row in rd:
                    ftr = (row.get("FTR") or "").strip().upper()
                    if ftr not in FTR_MAP:
                        continue  # partita non giocata / rinviata
                    try:
                        d = datetime.strptime((row.get("Date") or "").strip(),
                                              "%d/%m/%Y")
                    except ValueError:
                        continue
                    sh, sa = _f(row.get("FTHG")), _f(row.get("FTAG"))
                    if sh is None or sa is None:
                        continue
                    # NOTE football-data: le colonne SENZA C (B365H, PSH...)
                    # sono le quote PRE-CLOSING (early), quelle CON C
                    # (B365CH, PSCH...) sono le CLOSING.
                    # ENTRY = B365 pre-closing (soft, come il segnale
                    # the-odds-api di produzione), CLOSING = Pinnacle
                    # (sharp, riferimento CLV) -> CLV soft-vs-sharp.
                    op_h = _f(row.get("B365H")) or _f(row.get("PSH"))
                    op_d = _f(row.get("B365D")) or _f(row.get("PSD"))
                    op_a = _f(row.get("B365A")) or _f(row.get("PSA"))
                    # CLOSING stessa fonte (B365) per il CLV pulito
                    # (stesso bookmaker, senza distorsione soft-vs-sharp).
                    # Pinnacle closing resta come riferimento secondario.
                    cl_h = _f(row.get("B365CH")) or _f(row.get("PSCH"))
                    cl_d = _f(row.get("B365CD")) or _f(row.get("PSCD"))
                    cl_a = _f(row.get("B365CA")) or _f(row.get("PSCA"))
                    # OU2.5: entry = B365 pre-closing (soft), closing =
                    # Pinnacle (sharp). Fallback Max/Avg.
                    ou_ov_e = (_f(row.get("B365>2.5"))
                               or _f(row.get("Max>2.5"))
                               or _f(row.get("P>2.5")))
                    ou_un_e = (_f(row.get("B365<2.5"))
                               or _f(row.get("Max<2.5"))
                               or _f(row.get("P<2.5")))
                    # closing stessa fonte (B365C) per il CLV pulito
                    ou_ov_c = (_f(row.get("B365C>2.5"))
                               or _f(row.get("MaxC>2.5"))
                               or _f(row.get("PC>2.5")))
                    ou_un_c = (_f(row.get("B365C<2.5"))
                               or _f(row.get("MaxC<2.5"))
                               or _f(row.get("PC<2.5")))
                    matches.append({
                        "date": d,
                        "season": season,
                        "bet": season in BET_SEASONS,
                        "code": code,
                        "league": league,
                        "home": (row.get("HomeTeam") or "").strip(),
                        "away": (row.get("AwayTeam") or "").strip(),
                        "sh": int(sh), "sa": int(sa),
                        "ftr": FTR_MAP[ftr],
                        "op": [op_h, op_d, op_a],
                        "cl": [cl_h, cl_d, cl_a],
                        "ou_entry": [ou_ov_e, ou_un_e],
                        "ou_closing": [ou_ov_c, ou_un_c],
                    })
    matches.sort(key=lambda m: m["date"])
    return matches


# ---------------------------------------------------------------------------
# Rating walk-forward (replica di rating_engine, in memoria e causale)
# ---------------------------------------------------------------------------

class WalkForwardRatings:
    """Rating time-decay calcolati SOLO dalle partite passate.

    Per ogni squadra accumula (data, gol fatti, gol subiti, casa/trasferta)
    e calcola attack/defense alla data della partita corrente con il decay
    esponenziale (halflife 100gg) e lo shrink sul conteggio reale n
    (stessa formula di rating_engine, MA il 'now' e' la data della partita,
    non la data odierna -> niente look-ahead). Include anche le medie gol
    PER LEGA causali (solo partite passate) per calibrare lam_h/lam_a.
    """

    def __init__(self):
        self._hist: Dict[str, List[Tuple]] = {}   # team -> [(date, gf, ga, side)]
        self._league_goals: Dict[str, List[Tuple]] = {}  # lega -> [(date, gh, ga)]

    def add(self, match: Dict) -> None:
        d, sh, sa = match["date"], match["sh"], match["sa"]
        for team, gf, ga, side in ((match["home"], sh, sa, "h"),
                                   (match["away"], sa, sh, "a")):
            self._hist.setdefault(team, []).append((d, gf, ga, side))
        self._league_goals.setdefault(match["league"], []).append((d, sh, sa))

    def league_avgs(self, league: str, at: datetime, prior_n: float = 100.0,
                    prior_h: float = 1.50, prior_a: float = 1.30) -> Tuple[float, float]:
        """Medie gol casa/trasferta della lega, causali (solo partite con
        data < at), con shrink verso il prior globale per piccoli campioni.

        Le medie di lega vere variano molto (Serie A ~2.54 gol/partita,
        Bundesliga ~3.19): usare le medie globali ovunque distorce il
        modello (prob pareggio e over/under sbagliate per lega).
        """
        rows = [r for r in self._league_goals.get(league, []) if r[0] < at]
        n = len(rows)
        if n == 0:
            return prior_h, prior_a
        sh = sum(r[1] for r in rows)
        sa = sum(r[2] for r in rows)
        obs_h = sh / n
        obs_a = sa / n
        w = n / (n + prior_n)
        return (prior_h * (1 - w) + obs_h * w,
                prior_a * (1 - w) + obs_a * w)

    def _rate(self, pairs: List[Tuple], avg: float, at: datetime) -> Optional[float]:
        """Coefficiente attacco/difesa con shrink sul conteggio reale."""
        if not pairs:
            return None
        wsum = 0.0
        obs_num = 0.0
        n = 0
        for d, g, _, _ in pairs:
            days = max(0.0, (at - d).total_seconds() / 86400.0)
            w = math.exp(-math.log(2) * days / HALF_LIFE_DAYS)
            wsum += w
            obs_num += w * g
            n += 1
        if wsum <= 0 or avg <= 0:
            return None
        obs = obs_num / wsum
        coeff = obs / avg
        return (coeff * n + 1.0 * PRIOR_MATCHES) / (n + PRIOR_MATCHES)

    def rating(self, team: str, at: datetime) -> Optional[Dict]:
        """Ritorna {attack_home, defense_home, attack_away, defense_away}
        o None se la squadra ha meno di MIN_MATCHES partite (come produzione)."""
        hist = self._hist.get(team)
        if not hist or len(hist) < MIN_MATCHES:
            return None
        home = [x for x in hist if x[3] == "h"]
        away = [x for x in hist if x[3] == "a"]
        atk_h = self._rate(home, GLOBAL_H, at)
        def_h = self._rate(home, GLOBAL_H, at)
        atk_a = self._rate(away, GLOBAL_A, at)
        def_a = self._rate(away, GLOBAL_A, at)
        if atk_h is None or def_h is None or atk_a is None or def_a is None:
            return None
        return {"attack_home": atk_h, "defense_home": def_h,
                "attack_away": atk_a, "defense_away": def_a}


def expected_goals_bt(home_rating: Optional[Dict], away_rating: Optional[Dict],
                      avg_h: float = 1.50, avg_a: float = 1.30) -> Tuple[float, float]:
    """lam_h/lam_a come poisson_engine.expected_goals: se mancano i rating
    di una squadra, profilo neutro (coefficienti 1.0)."""
    home_data = home_rating or {"attack_home": 1.0, "defense_home": 1.0,
                                "attack_away": 1.0, "defense_away": 1.0}
    away_data = away_rating or {"attack_home": 1.0, "defense_home": 1.0,
                                "attack_away": 1.0, "defense_away": 1.0}
    lam_h = avg_h * home_data["attack_home"] * away_data["defense_away"]
    lam_a = avg_a * away_data["attack_away"] * home_data["defense_home"]
    return lam_h, lam_a



# ---------------------------------------------------------------------------
# Modello
# ---------------------------------------------------------------------------

def model_probs(lam_h: float, lam_a: float) -> Dict:
    """Probabilita' Poisson/Dixon-Coles: 1X2 + over/under 2.5.

    Applica il DRAW_PENALTY ai pareggi (sovrastimati dal modello) e
    rinormalizza il mercato 1X2.
    """
    from poisson_engine import prob_1x2, prob_over_under
    p1, px, p2 = prob_1x2(lam_h, lam_a)
    px *= DRAW_PENALTY
    tot = p1 + px + p2
    if tot > 0:
        p1, px, p2 = p1 / tot, px / tot, p2 / tot
    p_over, _ = prob_over_under(lam_h, lam_a, 2.5)
    return {"prob_1": p1, "prob_X": px, "prob_2": p2, "prob_over": p_over}


def final_prob(model_prob: float, market_prob: Optional[float],
               league: str, odds: float) -> float:
    """Probabilita' finale anti-overconfidence, in 3 passi:

    1. BLEND dinamico modello+mercato (blend_probability, peso per lega);
    2. SHRINK verso il mercato: la deviazione residua dal mercato viene
       ridotta del fattore SHRINK_TO_MARKET;
    3. CAP sull'edge: mai piu' di MAX_EDGE sopra la probabilita' di mercato
       (gli edge estremi sono il sintomo del winner's curse).
    """
    from market_calib import blend_probability
    pb = blend_probability(model_prob, market_prob, league=league, odds=odds)
    if market_prob is None:
        return pb
    # shrink: muovi meta' strada verso il mercato
    pb = market_prob + SHRINK_TO_MARKET * (pb - market_prob)
    # cap: mai piu' di MAX_EDGE sopra il mercato
    pb = min(pb, market_prob + MAX_EDGE)
    return max(0.01, min(0.99, pb))


def market_probs(odds: List[Optional[float]], n_outcomes: int = 3) -> Optional[List[float]]:
    """Devig power delle quote (1X2 o OU). None se quote mancanti."""
    from market_calib import devig
    valid = [o for o in odds[:n_outcomes] if o and o > 1.0]
    if len(valid) < 2:
        return None
    return devig(valid, method="power")


def build_candidates(match: Dict, probs: Dict, mkt: List[float],
                     mkt_ou: Optional[List[float]]) -> List[Dict]:
    """Candidati (esito, quota entry, market_prob) per 1X2 e OU2.5."""
    cands = []
    labels = [("1", probs["prob_1"], 0, mkt[0] if mkt else None),
              ("X", probs["prob_X"], 1, mkt[1] if mkt else None),
              ("2", probs["prob_2"], 2, mkt[2] if mkt else None)]
    for esito, p_model, idx, mp in labels:
        quota = match["op"][idx]
        if not quota or quota <= 1.0 or mp is None:
            continue
        cands.append({"mercato": "MATCH_ODDS", "esito": esito, "quota": quota,
                      "model_prob": p_model, "market_prob": mp})
    ov_e, un_e = match["ou_entry"][0], match["ou_entry"][1]
    if mkt_ou and ov_e and ov_e > 1.0 and un_e and un_e > 1.0:
        p_over = probs["prob_over"]
        cands.append({"mercato": "OVER_UNDER_25", "esito": "over", "quota": ov_e,
                      "model_prob": p_over, "market_prob": mkt_ou[0]})
        cands.append({"mercato": "OVER_UNDER_25", "esito": "under", "quota": un_e,
                      "model_prob": 1.0 - p_over, "market_prob": mkt_ou[1]})
    return cands


# ---------------------------------------------------------------------------
# Ensemble walk-forward
# ---------------------------------------------------------------------------

class EnsembleBT:
    """Involucro dell'ensemble di produzione (XGBoost/LR + PAVA) con
    retrain periodico walk-forward sulle sole partite chiuse.

    Il retrain scatta ogni RETRAIN_EVERY PARTITE ANALIZZATE (contatore
    dedicato, non l'indice del dataset: col warm-up in testa l'indice
    partirebbe da ~3364 e il confronto con il numero di righe farebbe
    ritrainare a ogni partita).
    """

    def __init__(self):
        self.ens = None
        self.trained = False
        self.rows: List[Dict] = []
        self._n_matches = 0          # partite analizzate dall'ultimo retrain
        self._last_train_rows = 0    # righe usate nell'ultimo retrain

    def add_closed(self, row: Dict) -> None:
        self.rows.append(row)

    def maybe_retrain(self) -> None:
        self._n_matches += 1
        if self._n_matches < RETRAIN_EVERY:
            return
        self.retrain()

    def retrain(self) -> None:
        self._n_matches = 0
        from ml_ensemble import EnsemblePredictor
        rows = self.rows[-MAX_TRAIN_ROWS:] if len(self.rows) > MAX_TRAIN_ROWS \
            else self.rows
        if len(rows) < 30 or len(rows) <= self._last_train_rows:
            return
        try:
            ens = EnsemblePredictor()
            metrics = ens.train(rows)
            if metrics.get("status") == "trained":
                self.ens = ens
                self.trained = True
                self._last_train_rows = len(rows)
        except Exception as e:
            print(f"  ⚠️  retrain ensemble fallito: {e}")

    def predict(self, row: Dict) -> Optional[float]:
        """Probabilita' ensemble (XGB/LR calibrato PAVA + blend Poisson).
        None se ensemble non addestrato."""
        if not self.trained or self.ens is None:
            return None
        try:
            res = self.ens.predict(row)
            return res.get("ensemble_prob")
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Simulazione
# ---------------------------------------------------------------------------

def _kelly_stake(bankroll: float, prob: float, odds: float) -> float:
    """Kelly 1/4 con cap 3% del bankroll (come value_filter.kelly_euro)."""
    from value_filter import kelly_euro, KELLY_FRACTION
    return kelly_euro(bankroll, prob, odds, fraction=KELLY_FRACTION)


def _esito_ok(match: Dict, cand: Dict) -> bool:
    if cand["mercato"] == "MATCH_ODDS":
        return match["ftr"] == cand["esito"]
    return (match["sh"] + match["sa"] > 2.5) == (cand["esito"] == "over")


def run_backtest(matches: List[Dict], ensemble: bool = True) -> Dict:
    """Esegue il backtest walk-forward. Ritorna il report completo."""
    from market_calib import clv_raw, clv_vig_free
    from value_filter import is_sane

    ratings = WalkForwardRatings()
    ens = EnsembleBT()
    bets: List[Dict] = []
    bankroll = BANKROLL0
    peak = BANKROLL0
    max_dd = 0.0
    dd_peak = BANKROLL0

    n_matches = len(matches)
    for i, match in enumerate(matches):
        if i % 2000 == 0 and i > 0:
            print(f"  ... {i}/{n_matches} partite, {len(bets)} bet, "
                  f"bankroll €{bankroll:.0f}")

        # Stagioni di warm-up (es. 2021-22): solo rating, niente bet.
        # Scaldano i rating e le medie di lega prima della prima puntata,
        # come in produzione dove lo storico alimenta i rating.
        if not match.get("bet", True):
            ratings.add(match)
            continue

        avg_h, avg_a = ratings.league_avgs(match["league"], match["date"])
        rh = ratings.rating(match["home"], match["date"])
        ra = ratings.rating(match["away"], match["date"])
        lam_h, lam_a = expected_goals_bt(rh, ra, avg_h=avg_h, avg_a=avg_a)
        probs = model_probs(lam_h, lam_a)

        mkt = market_probs(match["op"], 3)
        mkt_ou = market_probs(match["ou_entry"], 2)

        cands = build_candidates(match, probs, mkt, mkt_ou)

        best = None
        best_prob = None
        for cand in cands:
            if not cand["quota"] or cand["quota"] <= 1.0:
                continue
            if cand["quota"] > MAX_ODDS:
                continue
            # prob finale: blend + shrink verso il mercato + cap edge
            pb = final_prob(cand["model_prob"], cand["market_prob"],
                            match["league"], cand["quota"])
            # ensemble (se disponibile)
            row = {
                "prob_1": probs["prob_1"], "prob_X": probs["prob_X"],
                "prob_2": probs["prob_2"], "prob_over": probs["prob_over"],
                "lam_h": lam_h, "lam_a": lam_a,
                "quota": cand["quota"], "prob": pb,
                "ev": pb * cand["quota"] - 1.0,
                "market_prob": cand["market_prob"],
                "market_edge": pb - cand["market_prob"],
            }
            if ensemble:
                p_ens = ens.predict(row)
                if p_ens is not None:
                    # anche l'ensemble passa dallo shrink/cap (era ancora
                    # sovraconfidente: bucket 0.6-0.7 -> hit 52% vs 65% atteso)
                    p_fin = min(p_ens, cand["market_prob"] + MAX_EDGE)
                    p_fin = cand["market_prob"] + SHRINK_TO_MARKET * (p_fin - cand["market_prob"])
                    p_fin = max(0.01, min(0.99, p_fin))
                    row["prob"] = p_fin
                    row["ev"] = p_fin * cand["quota"] - 1.0
                    row["market_edge"] = p_fin - cand["market_prob"]
            prob = row["prob"]
            ev = row["ev"]
            sane, _ = is_sane(prob, cand["quota"], ev,
                              market_prob=cand["market_prob"])
            if not sane:
                continue
            status = ("strong_value" if (ev > 0.08 and row["market_edge"] >= 0.05)
                      else "value")
            if best is None or ev > best["ev"]:
                best = {**cand, "prob": prob, "ev": ev, "status": status,
                        "market_edge": row["market_edge"]}
                best_prob = prob

        # --- 1 bet per partita: il miglior candidato value/strong_value ---
        # Bankrupt: bankroll sotto il minimo -> stop (come in produzione)
        if best is not None and bankroll >= 2.0:
            stake = _kelly_stake(bankroll, best["prob"], best["quota"])
            if stake > MIN_STAKE and stake <= bankroll:
                won = _esito_ok(match, best)
                profit = stake * (best["quota"] - 1.0) if won else -stake
                bankroll += profit
                peak = max(peak, bankroll)
                dd = (peak - bankroll) / peak * 100.0 if peak > 0 else 0.0
                max_dd = max(max_dd, dd)

                # CLV: entry = opening (o soft per OU), closing = Pinnacle
                clv_r = None
                clv_vf = None
                if best["mercato"] == "MATCH_ODDS":
                    idx = {"1": 0, "X": 1, "2": 2}[best["esito"]]
                    closing = match["cl"][idx]
                    all_cl = [o for o in match["cl"] if o and o > 1.0]
                    if closing and closing > 1.0:
                        clv_r = clv_raw(best["quota"], closing)
                        clv_vf = clv_vig_free(best["quota"], closing,
                                              all_closing_odds=all_cl)
                else:
                    idx = 0 if best["esito"] == "over" else 1
                    closing = match["ou_closing"][idx]
                    if closing and closing > 1.0:
                        clv_r = clv_raw(best["quota"], closing)
                        clv_vf = clv_vig_free(best["quota"], closing,
                                              all_closing_odds=None)

                bets.append({
                    "date": match["date"].isoformat(),
                    "season": match["season"],
                    "league": match["league"],
                    "home": match["home"], "away": match["away"],
                    "mercato": best["mercato"], "esito": best["esito"],
                    "quota": best["quota"], "prob": best["prob"],
                    "ev": best["ev"], "status": best["status"],
                    "market_edge": best["market_edge"],
                    "stake": round(stake, 2), "profit": round(profit, 2),
                    "won": won, "clv_raw": clv_r, "clv_vig_free": clv_vf,
                })

        # --- training dell'ensemble: riga per ogni esito con label reale ---
        for cand in cands:
            if not cand["quota"] or cand["quota"] <= 1.0:
                continue
            if cand["quota"] > MAX_ODDS:
                continue
            pb = final_prob(cand["model_prob"], cand["market_prob"],
                            match["league"], cand["quota"])
            row = {
                "prob_1": probs["prob_1"], "prob_X": probs["prob_X"],
                "prob_2": probs["prob_2"], "prob_over": probs["prob_over"],
                "lam_h": lam_h, "lam_a": lam_a,
                "quota": cand["quota"], "prob": pb,
                "ev": pb * cand["quota"] - 1.0,
                "market_prob": cand["market_prob"],
                "market_edge": pb - cand["market_prob"],
                "label_ml": 1 if _esito_ok(match, cand) else 0,
            }
            ens.add_closed(row)
        ens.maybe_retrain()

        ratings.add(match)

    report = _build_report(bets, max_dd, BANKROLL0)
    report["_bets"] = bets
    return report


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _clv_stats(bets: List[Dict]) -> Dict:
    raw = [b["clv_raw"] for b in bets if b["clv_raw"] is not None]
    vf = [b["clv_vig_free"] for b in bets if b["clv_vig_free"] is not None]
    def _avg(x):
        return (sum(x) / len(x) * 100.0) if x else None
    def _med(x):
        if not x:
            return None
        s = sorted(x)
        return s[len(s) // 2] * 100.0
    return {
        "n_raw": len(raw), "n_vf": len(vf),
        "clv_raw_avg": round(_avg(raw), 2) if raw else None,
        "clv_raw_med": round(_med(raw), 2) if raw else None,
        "clv_vf_avg": round(_avg(vf), 2) if vf else None,
        "clv_vf_med": round(_med(vf), 2) if vf else None,
        "pct_positive_raw": round(sum(1 for x in raw if x > 0) / len(raw) * 100, 1)
        if raw else None,
        "pct_positive_vf": round(sum(1 for x in vf if x > 0) / len(vf) * 100, 1)
        if vf else None,
    }


def _diagnostics(bets: List[Dict]) -> Dict:
    """Diagnosi di calibrazione del run (stessa struttura del primo report):
    hit rate per bucket di probabilita' del modello, ROI flat se le bet
    fossero pagate a quota CLOSING (controllo "la selezione batte il
    mercato?") e CLV vig-free medio per bucket di edge.
    """
    # bucket di calibrazione
    buckets = {}
    for b in bets:
        p = b["prob"]
        if p < 0.2:
            key = "0.0-0.2"
        elif p < 0.3:
            key = "0.2-0.3"
        elif p < 0.4:
            key = "0.3-0.4"
        elif p < 0.5:
            key = "0.4-0.5"
        elif p < 0.6:
            key = "0.5-0.6"
        elif p < 0.7:
            key = "0.6-0.7"
        else:
            key = "0.7-1.0"
        g = buckets.setdefault(key, {"n": 0, "won": 0})
        g["n"] += 1
        g["won"] += 1 if b["won"] else 0
    expected = {"0.0-0.2": 10, "0.2-0.3": 25, "0.3-0.4": 35, "0.4-0.5": 45,
                "0.5-0.6": 55, "0.6-0.7": 65, "0.7-1.0": 85}
    calib = [{"bucket": k, "n": g["n"],
              "hit_pct": round(g["won"] / g["n"] * 100.0, 1),
              "expected_pct": expected.get(k)}
             for k, g in sorted(buckets.items()) if g["n"] > 0]

    # ROI flat se pagate a quota CLOSING (ricostruita da clv_raw)
    closing_roi = None
    c_vals = []
    for b in bets:
        if b.get("clv_raw") is None or b.get("stake", 0) <= 0:
            continue
        closing = b["quota"] / (1.0 + b["clv_raw"])
        if closing <= 1.0:
            continue
        profit = b["stake"] * (closing - 1.0) if b["won"] else -b["stake"]
        c_vals.append(profit / b["stake"] * 100.0)
    if c_vals:
        closing_roi = round(sum(c_vals) / len(c_vals), 2)

    # CLV vig-free medio per bucket di edge
    edge_buckets = {}
    for b in bets:
        if b.get("clv_vig_free") is None:
            continue
        e = b["market_edge"]
        if e < 0.05:
            key = "0.03-0.05"
        elif e < 0.08:
            key = "0.05-0.08"
        else:
            key = "0.08+"
        g = edge_buckets.setdefault(key, [])
        g.append(b["clv_vig_free"] * 100.0)
    clv_edge = [{"edge_bucket": k, "n": len(v),
                 "clv_vf_avg": round(sum(v) / len(v), 2)}
                for k, v in sorted(edge_buckets.items())]

    return {
        "calibrazione_modello": calib,
        "roi_flat_a_quota_closing_pct": closing_roi,
        "clv_vf_per_bucket_edge": clv_edge,
    }


def _build_report(bets: List[Dict], max_dd: float, bankroll0: float) -> Dict:
    n = len(bets)
    if n == 0:
        return {"status": "no_bets", "n": 0}
    staked = sum(b["stake"] for b in bets)
    pnl = sum(b["profit"] for b in bets)
    won = sum(1 for b in bets if b["won"])
    roi = pnl / staked * 100.0 if staked > 0 else 0.0
    # ROI FLAT: media del ritorno per singola bet (edge puro, non distorto
    # dal compounding Kelly che riduce le stake dopo le perdite)
    valid = [b for b in bets if b["stake"] > 0]
    roi_flat = sum((b["profit"] / b["stake"] * 100.0) for b in valid) / \
        len(valid) if valid else 0.0
    avg_odds = sum(b["quota"] for b in bets) / n if n else 0.0

    def _group(key):
        out = {}
        for b in bets:
            k = b[key]
            g = out.setdefault(k, {"n": 0, "staked": 0.0, "pnl": 0.0, "won": 0})
            g["n"] += 1
            g["staked"] += b["stake"]
            g["pnl"] += b["profit"]
            g["won"] += 1 if b["won"] else 0
        return {k: {**v, "roi": (v["pnl"] / v["staked"] * 100.0
                                 if v["staked"] else 0.0),
                    "hit": (v["won"] / v["n"] * 100.0 if v["n"] else 0.0)}
                for k, v in out.items()}

    return {
        "status": "ok",
        "n_bets": n,
        "staked": round(staked, 2),
        "pnl": round(pnl, 2),
        "roi": round(roi, 2),
        "roi_flat": round(roi_flat, 2),
        "avg_odds": round(avg_odds, 3),
        "hit_rate": round(won / n * 100.0, 1),
        "max_drawdown_pct": round(max_dd, 2),
        "bankroll_final": round(bankroll0 + pnl, 2),
        "avg_stake": round(staked / n, 2),
        "clv": _clv_stats(bets),
        "by_league": _group("league"),
        "by_season": _group("season"),
        "by_market": _group("mercato"),
        "by_status": _group("status"),
        "by_esito": _group("esito"),
        "diagnosi": _diagnostics(bets),
    }


def format_report(res: Dict, n_matches: int = 0,
                  ensemble_on: bool = True) -> str:
    if res.get("status") != "ok":
        return (f"📊 *BACKTEST STORICO*\n"
                f"⚠️ {res.get('status', 'errore')} — nessuna bet selezionata "
                f"({n_matches} partite analizzate)")

    def pct(v, sign=False):
        if v is None:
            return "—"
        return f"{v:+.1f}%" if sign else f"{v:.1f}%"

    clv = res["clv"]
    lines = [
        "📊 *BACKTEST STORICO — 4 STAGIONI (2022-2026)*",
        f"Partite analizzate: {n_matches} | Bet: {res['n_bets']} "
        f"(1 per partita, miglior EV value/strong)",
        f"Stake totale: €{res['staked']:.0f} | Stake medio: €{res['avg_stake']:.2f}",
        "━" * 34,
        f"💹 *ROI:* {pct(res['roi'], sign=True)}  (P/L €{res['pnl']:+.2f})",
        f"💹 *ROI flat (edge puro per bet):* {pct(res['roi_flat'], sign=True)}",
        f"🎯 Hit rate: {res['hit_rate']:.1f}%  ({res['n_bets']} bet, "
        f"quota media {res['avg_odds']:.2f})",
        f"📉 *MAX DRAWDOWN:* {res['max_drawdown_pct']:.1f}%",
        f"💰 Bankroll finale: €{res['bankroll_final']:.2f} "
        f"(da €1000)",
        "━" * 34,
        f"🎯 *CLV (entry vs Pinnacle closing)*",
        f"   RAW:    media {pct(clv['clv_raw_avg'], sign=True)} | "
        f"mediana {pct(clv['clv_raw_med'], sign=True)} | "
        f"positivi {pct(clv['pct_positive_raw'])} (n={clv['n_raw']})",
        f"   VIG-FREE: media {pct(clv['clv_vf_avg'], sign=True)} | "
        f"mediana {pct(clv['clv_vf_med'], sign=True)} | "
        f"positivi {pct(clv['pct_positive_vf'])} (n={clv['n_vf']})",
        "━" * 34,
        "📈 *Per stagione*",
    ]
    for k in ["2223", "2324", "2425", "2526"]:
        g = res["by_season"].get(k)
        if g:
            lines.append(f"   {k}: {g['n']} bet | ROI {pct(g['roi'], sign=True)} "
                         f"| hit {g['hit']:.0f}%")
    lines.append("📈 *Per lega (top ROI)*")
    top = sorted(res["by_league"].items(),
                 key=lambda kv: kv[1]["roi"], reverse=True)[:8]
    for league, g in top:
        lines.append(f"   {league}: {g['n']} bet | ROI {pct(g['roi'], sign=True)} "
                     f"| hit {g['hit']:.0f}%")
    lines.append("📈 *Per mercato*")
    for k, g in res["by_market"].items():
        lines.append(f"   {k}: {g['n']} bet | ROI {pct(g['roi'], sign=True)} "
                     f"| hit {g['hit']:.0f}%")
    lines.append("📈 *Per esito (MATCH_ODDS)*")
    mo = res["by_esito"]
    for k, lbl in [("1", "1 (Casa)"), ("X", "X (Pareggio)"),
                   ("2", "2 (Trasferta)")]:
        g = mo.get(k)
        if g:
            lines.append(f"   {lbl}: {g['n']} bet | ROI {pct(g['roi'], sign=True)} "
                         f"| hit {g['hit']:.0f}%")
    lines.append("📈 *Per status*")
    for k in ["value", "strong_value"]:
        g = res["by_status"].get(k)
        if g:
            lines.append(f"   {k}: {g['n']} bet | ROI {pct(g['roi'], sign=True)}")
    lines.append("━" * 34)
    dg = res.get("diagnosi") or {}
    lines.append("🔬 *DIAGNOSI (controlli di calibrazione)*")
    c_roi = dg.get("roi_flat_a_quota_closing_pct")
    if c_roi is not None:
        lines.append(f"   Controllo: ROI flat se pagate a quota CLOSING: "
                     f"{pct(c_roi, sign=True)}"
                     f"  (entry {pct(res['roi_flat'], sign=True)})"
                     f"  -> selezione batte il mercato? "
                     + ("✅ sì" if c_roi > 0 else "❌ no"))
    for row in (dg.get("calibrazione_modello") or [])[:7]:
        exp = row.get("expected_pct")
        gap = (row["hit_pct"] - exp) if exp is not None else None
        lines.append(f"   prob {row['bucket']}: n={row['n']:>4} "
                     f"hit {row['hit_pct']:.0f}%"
                     + (f" (atteso ~{exp}%, gap {gap:+.0f}pp)" if exp is not None else ""))
    for row in (dg.get("clv_vf_per_bucket_edge") or []):
        lines.append(f"   edge {row['edge_bucket']}: n={row['n']:>4} "
                     f"CLV vf {pct(row['clv_vf_avg'], sign=True)}")
    lines.append("━" * 34)
    lines.append("📌 Metodo: walk-forward senza look-ahead (rating, ensemble e "
                 "mercato solo da partite/quote passate). Entry = opening odds, "
                 "CLV vs closing. Ensemble: "
                 + ("XGBoost+PAVA" if ensemble_on else "disattivato (solo blend)")
                 + f". Anti-overconfidence: shrink={SHRINK_TO_MARKET}, "
                 + f"edge-cap={MAX_EDGE:.2f}, draw-penalty={DRAW_PENALTY}. "
                 + "Kelly 1/4 con cap 3%.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backtest storico walk-forward")
    ap.add_argument("--limit", type=int, default=0,
                    help="Analizza solo le prime N partite (test rapido)")
    ap.add_argument("--no-download", action="store_true",
                    help="Usa i CSV già in cache")
    ap.add_argument("--no-ensemble", action="store_true",
                    help="Disattiva l'ensemble ML (solo Poisson+blend)")
    ap.add_argument("--shrink", type=float, default=None,
                    help="Shrink verso il mercato dopo il blend (default 0.50)")
    ap.add_argument("--edge-cap", type=float, default=None,
                    help="Cap sull'edge in pp (default 0.10 = 10pp)")
    ap.add_argument("--draw-penalty", type=float, default=None,
                    help="Sotto-peso dei pareggi (default 0.80)")
    ap.add_argument("--max-odds", type=float, default=None,
                    help="Quota massima accettata (default 5.0)")
    ap.add_argument("--json", action="store_true",
                    help="Stampa solo il JSON del report")
    ap.add_argument("--save", action="store_true",
                    help="Salva report JSON e CSV delle bet in data/")
    args = ap.parse_args(argv)

    global SHRINK_TO_MARKET, MAX_EDGE, DRAW_PENALTY, MAX_ODDS
    if args.shrink is not None:
        SHRINK_TO_MARKET = args.shrink
    if args.edge_cap is not None:
        MAX_EDGE = args.edge_cap
    if args.draw_penalty is not None:
        DRAW_PENALTY = args.draw_penalty
    if args.max_odds is not None:
        MAX_ODDS = args.max_odds
    print(f"⚙️  Anti-overconfidence: shrink={SHRINK_TO_MARKET}, "
          f"edge-cap={MAX_EDGE:.2f}, draw-penalty={DRAW_PENALTY}, "
          f"max-odds={MAX_ODDS}")

    print("⬇️  Caricamento dati football-data.co.uk (4 stagioni × 10 leghe)...")
    matches = load_matches(no_download=args.no_download)
    print(f"✅ {len(matches)} partite caricate "
          f"({matches[0]['date'].date()} → {matches[-1]['date'].date()})")
    if args.limit:
        matches = matches[:args.limit]
        print(f"   (limit: {args.limit} partite)")

    print("🧮 Backtest walk-forward (rating + Poisson/DC + devig + blend"
          + (" + ensemble XGB/PAVA" if not args.no_ensemble else "")
          + " + Kelly 1/4 + CLV)...")
    res = run_backtest(matches, ensemble=not args.no_ensemble)
    res["n_matches"] = len(matches)
    res["ensemble_on"] = not args.no_ensemble

    if args.save:
        out = DATA_DIR.parent / "historical_backtest_report.json"
        report = {k: v for k, v in res.items() if k != "_bets"}
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"💾 Report salvato: {out}")
        bets = res.get("_bets", [])
        if bets:
            csvp = DATA_DIR.parent / "historical_backtest_bets.csv"
            with open(csvp, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(bets[0].keys()))
                w.writeheader()
                for b in bets:
                    w.writerow(b)
            print(f"💾 {len(bets)} bet salvate: {csvp}")
    else:
        print(format_report(res, len(matches),
                            ensemble_on=not args.no_ensemble))
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())