"""pinnacle_oracle.py — Pinnacle come oracolo del mercato (FASE PROBE).

Direttiva del proprietario (25/09/2026): pivot verso un modello TOP-DOWN.
Congelare il modello statistico bottom-up (Poisson) e far dettare la
probabilita' "vera" dal mercato sharp (Pinnacle), usando poi lo SCARTO fra
quella probabilita' e il prezzo disponibile sull'exchange (SX Bet) come
trigger di valore. La decisione diventa puramente matematica: Pinnacle dice
quanto vale un esito, SX dice quanto lo pagano, e si compra solo il ritardo.

⚠️ TRE COSE MISURATE PRIMA DI SCRIVERE QUESTO CODICE (non assunte, verificate
   il 25/09/2026 sulle cache di PRODUZIONE — costo 0 crediti):

1. **PINNACLE E' GIA' NEL PAYLOAD CHE PAGHIAMO.** La fetch delle quote usa
   `regions=eu` SENZA filtro `bookmakers`, quindi il payload contiene gia'
   Pinnacle. Verificato: **9 leghe di calcio su 9** hanno Pinnacle (MLS 15/15
   partite, Liga MX 9/9, League Two 12/12, Bundesliga 2 9/9, League One 7/7,
   Primeira Liga 9/10, Brazil B 1/1, K League 1 1/1, Superettan 1/1).
   → Aggiungere `bookmakers=pinnacle` NON compra dati nuovi: riduce il payload.
   Il costo marginale dell'oracolo e' quindi **ZERO** se si estrae dalla fetch
   che facciamo gia', ed e' questo il percorso che la pipeline deve usare.
2. **IL DEVIG ESISTE GIA'**: `market_calib.devig` con tre metodi
   (`multiplicative`, `power`, `shin`) + `market_implied`. Nessuna copia.
3. **LA CADENZA E' IL VINCOLO, NON L'ESTRAZIONE.** the-odds-api addebita
   `markets x regions` per chiamata (qui 1x1 = 1 credito) e il piano free da'
   500 crediti/mese (~16/giorno). **Un job che interroga l'API in tempo reale
   non e' sostenibile**: 1 lega ogni 5 minuti = 288 chiamate/giorno, ~17 volte
   il budget. Da qui i DUE percorsi espliciti: `--from-cache` (0 crediti,
   quello che gira nella pipeline) e `--live` (1 credito, SOLO diagnostica).

Questo modulo e' la FASE 1 (probe): dimostra che estrazione, de-vig e gate
funzionano. NON scrive sul ledger, NON piazza ordini, NON consulta Poisson:
`bypass` del motore statistico e' una decisione di pipeline (fase 2), non un
effetto collaterale di una funzione di lettura.

⚠️ Il gate EV e' definito una volta sola:
    EV = p_true x (quota - 1) - (1 - p_true)
e le due letture della direttiva COINCIDONO esattamente:
    EV >= ev_min   <=>   quota >= true_odd x (1 + ev_min)
(`true_odd` = 1 / p_true, la "True Odd" di Pinnacle). `required_price` espone
la seconda forma: stessa condizione, niente doppio standard.

CLI:
  venv/bin/python pinnacle_oracle.py --from-cache          # 0 crediti
  venv/bin/python pinnacle_oracle.py --live soccer_usa_mls # 1 credito
  venv/bin/python pinnacle_oracle.py --from-cache --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from config import DATA_DIR, load_dotenv

load_dotenv()

logger = logging.getLogger("pinnacle_oracle")

#: Il book sharp di riferimento (chiave the-odds-api). Il confronto e' per
#: SOTTOSTRINGA su chiave o titolo: l'API espone `key` = "pinnacle".
SHARP_BOOKS: Tuple[str, ...] = ("pinnacle",)

#: Metodo di de-vig. Default = quello del progetto (`market_calib.devig`), per
#: non introdurre una seconda convenzione: "power" corregge il
#: favourite-longshot bias. "shin" e "multiplicative" restano disponibili
#: (override con `PINNACLE_DEVIG_METHOD` o per chiamata).
DEVIG_METHOD: str = os.getenv("PINNACLE_DEVIG_METHOD", "power")

try:                        # stessa soglia del gate di produzione, mai copiata
    from value_filter import EV_MIN as DEFAULT_EV_MIN
except Exception:                                                   # pragma: no cover
    DEFAULT_EV_MIN = 0.02

#: Endpoint della fonte (the-odds-api). Usato SOLO dal percorso `--live`.
ODDS_ENDPOINT = "https://api.the-odds-api.com/v4/sports/{sport}/odds"


# ---------------------------------------------------------------------------
# 1. ESTRAZIONE: Pinnacle dal payload che scarichiamo gia'
# ---------------------------------------------------------------------------

def _cf(value: Any) -> str:
    """Chiave di confronto per nomi (case-fold + spazi normalizzati)."""
    return " ".join(str(value or "").strip().casefold().split())


def is_sharp(bookmaker: Any) -> bool:
    """True se la chiave/titolo del bookmaker e' un book sharp (Pinnacle)."""
    name = _cf(bookmaker)
    return any(s in name for s in SHARP_BOOKS)


def h2h_odds_of(bookmaker: Dict[str, Any], home: str, away: str
                ) -> Optional[Dict[str, float]]:
    """Quote 1X2 di UN bookmaker del payload. None se incomplete.

    FAIL-CLOSED su tre esiti: per de-vigare un 1X2 servono TUTTI E TRE. Con due
    su tre il margine dell'esito mancante verrebbe attribuito agli altri e la
    probabilita' "vera" risulterebbe sbagliata **senza che nulla lo dica** —
    meglio nessun oracolo che un oracolo distorto.
    """
    if not isinstance(bookmaker, dict):
        return None
    h, a = _cf(home), _cf(away)
    out: Dict[str, float] = {}
    for mkt in bookmaker.get("markets") or []:
        if not isinstance(mkt, dict) or mkt.get("key") != "h2h":
            continue
        for o in mkt.get("outcomes") or []:
            if not isinstance(o, dict):
                continue
            try:
                price = float(o.get("price"))
            except (TypeError, ValueError):
                continue
            if price <= 1.0:
                continue
            name = _cf(o.get("name"))
            if name == h:
                out["1"] = price
            elif name == a:
                out["2"] = price
            elif name in ("draw", "pareggio", "x"):
                out["X"] = price
    return out if len(out) == 3 else None


def pinnacle_quotes(payload: Sequence[Dict[str, Any]], home: str, away: str
                    ) -> Optional[Dict[str, float]]:
    """Quote 1X2 di Pinnacle per UNA partita del payload. None se assenti.

    Funzione PURA: nessuna rete, nessun credito, nessuna scrittura.
    """
    for match in payload or []:
        if not isinstance(match, dict):
            continue
        if _cf(match.get("home_team")) != _cf(home) \
                or _cf(match.get("away_team")) != _cf(away):
            continue
        for bm in match.get("bookmakers") or []:
            if not isinstance(bm, dict):
                continue
            if not is_sharp(bm.get("key") or bm.get("title")):
                continue
            got = h2h_odds_of(bm, home, away)
            if got:
                return got
    return None


def iter_pinnacle_markets(payload: Sequence[Dict[str, Any]]
                          ) -> List[Tuple[Dict[str, Any], Dict[str, float]]]:
    """[(partita, quote 1X2 Pinnacle)] per ogni partita con oracolo completo."""
    out: List[Tuple[Dict[str, Any], Dict[str, float]]] = []
    for match in payload or []:
        if not isinstance(match, dict):
            continue
        home = match.get("home_team") or ""
        away = match.get("away_team") or ""
        if not home or not away:
            continue
        quotes = pinnacle_quotes([match], home, away)
        if quotes:
            out.append((match, quotes))
    return out


# ---------------------------------------------------------------------------
# 2. TRUE PROBABILITY: de-vig (delega a market_calib, nessuna copia)
# ---------------------------------------------------------------------------

def true_probabilities(odds_map: Dict[str, float], *,
                       method: Optional[str] = None
                       ) -> Optional[Dict[str, Any]]:
    """Quote Pinnacle -> probabilita' fair (somma 1) + `overround`.

    Delega a `market_calib.market_implied` (metodi: power / multiplicative /
    shin). Richiede **almeno 3 esiti**: su un 1X2 de-vigare 2 quote su 3
    sposterebbe sugli altri il margine dell'esito mancante.
    """
    if not odds_map or len(odds_map) < 3:
        return None
    try:
        from market_calib import market_implied
    except Exception as exc:                                    # pragma: no cover
        logger.warning("pinnacle_oracle: market_calib non disponibile (%s)", exc)
        return None
    result = market_implied({k: float(v) for k, v in odds_map.items()},
                            method=method or DEVIG_METHOD)
    if not result:
        return None
    return result


def fair_odds(true_probs: Dict[str, Any]) -> Dict[str, float]:
    """'True Odd' per esito = 1 / probabilita' fair (quota equa, senza vig).

    Ignora le chiavi di servizio (`overround`) e qualunque valore non
    interpretabile come probabilita' in (0, 1].
    """
    out: Dict[str, float] = {}
    for key, value in (true_probs or {}).items():
        if key == "overround":
            continue
        try:
            p = float(value)
        except (TypeError, ValueError):
            continue
        if 0.0 < p <= 1.0:
            out[str(key)] = 1.0 / p
    return out


# ---------------------------------------------------------------------------
# 3. TRIGGER: EV dello scarto fra vera probabilita' e prezzo SX
# ---------------------------------------------------------------------------

def ev_gate(true_probs: Dict[str, Any], prices: Dict[str, float], *,
            ev_min: Optional[float] = None, method: Optional[str] = None
            ) -> List[Dict[str, Any]]:
    """Candidati value: per ogni esito prezzato, EV e quota minima di trigger.

    `EV = p_true x (quota - 1) - (1 - p_true)`.

    Le due letture della direttiva coincidono esattamente:
        EV >= ev_min  <=>  quota >= true_odd x (1 + ev_min)
    quindi `required_price` (la "True Odd + margine") e il gate sull'EV sono
    LA STESSA condizione, qui esposta nei due modi. Ordinati per EV decrescente.
    """
    th = DEFAULT_EV_MIN if ev_min is None else float(ev_min)
    rows: List[Dict[str, Any]] = []
    for esito, prob in (true_probs or {}).items():
        if esito == "overround":
            continue
        try:
            p = float(prob)
            price = float(prices.get(esito))
        except (TypeError, ValueError):
            continue
        if not (0.0 < p <= 1.0) or price <= 1.0:
            continue
        true_odd = 1.0 / p
        ev = p * (price - 1.0) - (1.0 - p)
        required = true_odd * (1.0 + th)
        rows.append({
            "esito": str(esito),
            "prob": round(p, 6),
            "true_odd": round(true_odd, 4),
            "price": round(price, 4),
            "required_price": round(required, 4),
            "ev": round(ev, 6),
            "edge_pp": round((p - 1.0 / price) * 100.0, 2),
            "trigger": ev >= th,
        })
    rows.sort(key=lambda r: -r["ev"])
    return rows


def value_candidates(true_probs: Dict[str, Any], prices: Dict[str, float], *,
                     ev_min: Optional[float] = None
                     ) -> List[Dict[str, Any]]:
    """Solo gli esiti che fanno SCATTARE il trigger (EV >= ev_min)."""
    return [r for r in ev_gate(true_probs, prices, ev_min=ev_min)
            if r["trigger"]]


# ---------------------------------------------------------------------------
# 4. PERCORSO A COSTO ZERO: le cache che abbiamo gia' scaricato
# ---------------------------------------------------------------------------

def _cache_files(cache_dir: Path) -> List[Path]:
    """Cache delle QUOTE (`toa_<sport>.json`), mai quelle dei punteggi."""
    try:
        return sorted(p for p in cache_dir.glob("toa_*.json")
                      if not p.name.startswith("toa_scores_"))
    except Exception:
        return []


def scan_cache(cache_dir: Optional[Path] = None, *,
               ev_min: Optional[float] = None,
               price_lookup: Optional[Callable[[Dict[str, Any], str], Optional[float]]] = None,
               max_leagues: Optional[int] = None) -> Dict[str, Any]:
    """Copertura dell'oracolo sulle cache GIA' scaricate. **Zero crediti.**

    `price_lookup(partita, esito) -> quota SX | None` e' iniettabile: senza di
    esso si misura solo la COPERTURA (quante partite hanno un 1X2 Pinnacle
    completo e con che margine). Il collegamento a SX Bet e' fase 2.
    """
    folder = Path(cache_dir) if cache_dir else Path(DATA_DIR)
    leagues: List[Dict[str, Any]] = []
    totals = {"leagues": 0, "matches": 0, "with_pinnacle": 0,
              "candidates": 0, "price_errors": 0}
    candidates: List[Dict[str, Any]] = []
    for path in _cache_files(folder):
        if max_leagues is not None and totals["leagues"] >= int(max_leagues):
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("pinnacle_oracle: cache illeggibile %s (%s)", path, exc)
            continue
        payload = (data or {}).get("payload") or []
        if not payload:
            continue
        with_pin = 0
        overrounds: List[float] = []
        for match, quotes in iter_pinnacle_markets(payload):
            probs = true_probabilities(quotes)
            if not probs:
                continue
            with_pin += 1
            if probs.get("overround") is not None:
                overrounds.append(float(probs["overround"]))
            if price_lookup is None:
                continue
            # Un lookup che esplode NON deve essere indistinguibile da "il
            # book non offre nulla": altrimenti una lettura rotta si legge
            # come "zero value" (la lezione del probe BTTS del 25/09, dove
            # `_discover_type` inghiottiva l'eccezione). Si CONTA e si
            # dichiara, e la scansione prosegue sulle altre partite.
            try:
                prices = {e: price_lookup(match, e)
                          for e in ("1", "X", "2")}
            except Exception as exc:
                totals["price_errors"] += 1
                logger.debug("pinnacle_oracle: lookup prezzi fallito su %s vs "
                             "%s (%s)", match.get("home_team"),
                             match.get("away_team"), exc)
                continue
            prices = {k: v for k, v in prices.items() if v}
            for cand in value_candidates(probs, prices, ev_min=ev_min):
                candidates.append({
                    "sport": path.stem.replace("toa_", ""),
                    "event": f"{match.get('home_team')} vs {match.get('away_team')}",
                    "commence": match.get("commence_time"),
                    **cand,
                })
        totals["leagues"] += 1
        totals["matches"] += len(payload)
        totals["with_pinnacle"] += with_pin
        leagues.append({
            "sport": path.stem.replace("toa_", ""),
            "file": path.name,
            "matches": len(payload),
            "with_pinnacle": with_pin,
            "avg_overround": (round(sum(overrounds) / len(overrounds), 5)
                              if overrounds else None),
        })
    totals["candidates"] = len(candidates)
    candidates.sort(key=lambda c: -c["ev"])
    if totals["price_errors"]:
        logger.warning("pinnacle_oracle: lettura prezzi fallita in %d casi — "
                       "i candidati sono INCOMPLETI", totals["price_errors"])
    return {"cache_dir": str(folder), "leagues": leagues, "totals": totals,
            "candidates": candidates,
            "gate": {"ev_min": DEFAULT_EV_MIN if ev_min is None else ev_min,
                     "devig_method": DEVIG_METHOD, "sharp_book": SHARP_BOOKS[0]}}


# ---------------------------------------------------------------------------
# 5. PERCORSO LIVE (1 credito): SOLO diagnostica, misura il costo reale
# ---------------------------------------------------------------------------

def fetch_pinnacle_payload(sport_key: str, *, days_ahead: int = 7,
                           bookmakers: str = "pinnacle", regions: str = "eu",
                           timeout: int = 30) -> Dict[str, Any]:
    """UNA chiamata `/odds` con il filtro `bookmakers` — e misura il costo.

    Ritorna {"payload", "remaining", "last_cost", "status", "error"}.
    Fail-safe: non solleva mai. Rifiuta (senza chiamare) se i crediti sono
    sotto la soglia di blocco totale del progetto.

    Perche' esiste nonostante l'oracolo sia gratis: serve a DIMOSTRARE quanto
    costa una chiamata filtrata (`x-requests-last`) e quindi a decidere, con un
    numero e non con un'opinione, se un job di confronto continuo sia
    sostenibile col piano crediti attuale.
    """
    out: Dict[str, Any] = {"payload": [], "remaining": None, "last_cost": None,
                           "status": None, "error": None}
    key = (os.getenv("ODDS_API_KEY") or "").strip()
    if not key:
        out["error"] = "ODDS_API_KEY assente"
        return out
    try:
        from odds_api import credits_hard_stopped
        if credits_hard_stopped():
            out["error"] = "crediti sotto la soglia di blocco totale"
            return out
    except Exception:
        pass                                   # telemetria assente -> si procede
    try:
        import requests
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        r = requests.get(ODDS_ENDPOINT.format(sport=sport_key), params={
            "apiKey": key, "regions": regions, "markets": "h2h",
            "bookmakers": bookmakers, "oddsFormat": "decimal",
            "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "commenceTimeTo": (now + timedelta(days=days_ahead))
                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, timeout=timeout)
        out["status"] = r.status_code
        for header, field in (("x-requests-remaining", "remaining"),
                              ("x-requests-last", "last_cost"),
                              ("x-requests-used", "used")):
            raw = r.headers.get(header)
            if raw is not None:
                try:
                    out[field] = int(raw)
                except (TypeError, ValueError):
                    pass
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}: {r.text[:200]}"
            return out
        out["payload"] = r.json() or []
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_scan(res: Dict[str, Any]) -> None:
    t = res["totals"]
    gate = res["gate"]
    print("🔺 ORACOLO PINNACLE — copertura sulle cache (0 crediti)")
    print(f"Cache in {res['cache_dir']} | devig: {gate['devig_method']} | "
          f"EV_MIN {gate['ev_min'] * 100:.1f}%")
    print(f"Leghe con quote: {t['leagues']} | partite: {t['matches']} | "
          f"con 1X2 Pinnacle completo: {t['with_pinnacle']}")
    for lg in res["leagues"]:
        ov = lg["avg_overround"]
        print(f"  {lg['sport']:<46} partite={lg['matches']:<3} "
              f"pinnacle={lg['with_pinnacle']:<3} "
              f"overround={'-' if ov is None else f'{ov:.3%}'}")
    if t.get("price_errors"):
        print(f"⚠️ lettura prezzi fallita in {t['price_errors']} casi: "
              f"i candidati sono INCOMPLETI (non 'zero value')")
    if res["candidates"]:
        print(f"Candidati value (EV >= {gate['ev_min'] * 100:.1f}%): "
              f"{t['candidates']}")
        for c in res["candidates"][:15]:
            print(f"  {c['event']} {c['esito']} @ {c['price']} "
                  f"(true {c['true_odd']}) EV {c['ev'] * 100:+.2f}%")
    else:
        print("Candidati value: non valutati (SX non collegato in fase 1)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pinnacle come oracolo (fase probe: nessun ordine)")
    ap.add_argument("--from-cache", action="store_true",
                    help="scansiona le cache gia' scaricate (0 crediti)")
    ap.add_argument("--live", metavar="SPORT_KEY", default=None,
                    help="UNA chiamata /odds con bookmakers=pinnacle (1 credito)")
    ap.add_argument("--ev-min", type=float, default=None,
                    help=f"margine EV del trigger (default {DEFAULT_EV_MIN})")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.live:
        res = fetch_pinnacle_payload(args.live)
        if args.json:
            print(json.dumps({k: v for k, v in res.items() if k != "payload"},
                             indent=2, default=str))
        else:
            print(f"live {args.live}: status={res['status']} "
                  f"eventi={len(res['payload'])} "
                  f"crediti: rimasti={res['remaining']} "
                  f"costo_chiamata={res['last_cost']}")
            if res["error"]:
                print(f"errore: {res['error']}")
            hits = iter_pinnacle_markets(res["payload"])
            print(f"partite con 1X2 Pinnacle completo: {len(hits)}")
        return 0 if not res["error"] else 1

    res = scan_cache(ev_min=args.ev_min)
    print(json.dumps(res, indent=2, ensure_ascii=False) if args.json
          else "")
    if not args.json:
        _print_scan(res)
    return 0


if __name__ == "__main__":                                    # pragma: no cover
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
