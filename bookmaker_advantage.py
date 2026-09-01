"""bookmaker_advantage.py — Vantaggio multi-bookmaker: soft book lag detection.

I bookmaker retail ("soft books") aggiornano le quote con ritardo rispetto
ai mercati sharp (Pinnacle, Betfair Exchange). Quando Pinnacle muove,
i soft book impiegano 30-120 secondi a seguirti. Questo modulo:

1. CONFRONTA le quote Pinnacle (sharp) con i soft books
2. RILEVA quando un soft book è "indietro" rispetto a Pinnacle
3. CALCOLA l'edge aggiuntivo dal lag (soft book quote troppo generosa)
4. IDENTIFICA opportunità di price advantage (non arbitraggio puro, ma
   il miglior prezzo possibile sfruttando la lentezza dei soft book)

CLI:
  venv/bin/python bookmaker_advantage.py               # analisi corrente
  venv/bin/python bookmaker_advantage.py --match m1     # singolo match
  venv/bin/python bookmaker_advantage.py --json          # output JSON
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Soglie
LAG_THRESHOLD_PCT = 2.0    # soft book >= 2% "più generoso" di Pinnacle → lag
SHARP_BOOKS = {"pinnacle"}  # bookmaker considerati sharp
EDGE_FROM_LAG_MIN = 0.01   # edge minimo 1% dal lag per essere rilevante


def identify_sharp_prices(bookmakers: List[Dict],
                          home_api: str = "", away_api: str = ""
                          ) -> Dict[str, float]:
    """Identifica le quote sharp (Pinnacle) per ogni esito.

    Ritorna {esito: quota_sharp} dove esito è "1"/"X"/"2"/"Over 2.5"/"Under 2.5".
    """
    sharp = {}
    for bm in bookmakers:
        bname = (bm.get("title") or bm.get("key") or "").lower()
        is_sharp = any(s in bname for s in SHARP_BOOKS)
        if not is_sharp:
            continue
        for mkt in bm.get("markets", []):
            if mkt.get("key") == "h2h":
                for out in mkt.get("outcomes", []):
                    name = (out.get("name") or "").strip().lower()
                    price = out.get("price")
                    if not price or float(price) <= 1.0:
                        continue
                    if name == home_api:
                        sharp["1"] = float(price)
                    elif name == away_api:
                        sharp["2"] = float(price)
                    elif name in ("draw", "pareggio"):
                        sharp["X"] = float(price)
            elif mkt.get("key") == "totals":
                for out in mkt.get("outcomes", []):
                    name = (out.get("name") or "").strip().lower()
                    price = out.get("price")
                    point = out.get("point")
                    if not price or not point or float(price) <= 1.0:
                        continue
                    if float(point) == 2.5:
                        if "over" in name:
                            sharp["Over 2.5"] = float(price)
                        elif "under" in name:
                            sharp["Under 2.5"] = float(price)
    return sharp


def detect_soft_book_lag(bookmakers: List[Dict],
                         home_api: str = "", away_api: str = ""
                         ) -> List[Dict]:
    """Rileva soft book "indietro" rispetto a Pinnacle.

    Per ogni soft book, confronta le sue quote con quelle di Pinnacle.
    Se il soft book offre una quota significativamente più alta (più generosa)
    per lo stesso esito, significa che non ha ancora aggiornato → lag.

    Ritorna lista di {bookmaker, esito, sharp_price, soft_price,
                      advantage_pct, edge_from_lag}.
    """
    sharp = identify_sharp_prices(bookmakers, home_api, away_api)
    if not sharp:
        return []

    lags = []
    for bm in bookmakers:
        bname = (bm.get("title") or bm.get("key") or "Unknown")
        bname_lower = bname.lower()
        is_sharp = any(s in bname_lower for s in SHARP_BOOKS)
        if is_sharp:
            continue

        for mkt in bm.get("markets", []):
            mkt_key = mkt.get("key")
            if mkt_key not in ("h2h", "totals"):
                continue
            for out in mkt.get("outcomes", []):
                name = (out.get("name") or "").strip().lower()
                price = out.get("price")
                point = out.get("point")
                if not price or float(price) <= 1.0:
                    continue

                # Mappa esito
                if mkt_key == "h2h":
                    if name == home_api:
                        esito = "1"
                    elif name == away_api:
                        esito = "2"
                    elif name in ("draw", "pareggio"):
                        esito = "X"
                    else:
                        continue
                elif mkt_key == "totals":
                    if not point or float(point) != 2.5:
                        continue
                    if "over" in name:
                        esito = "Over 2.5"
                    elif "under" in name:
                        esito = "Under 2.5"
                    else:
                        continue
                else:
                    continue

                soft_price = float(price)
                sharp_price = sharp.get(esito)
                if not sharp_price or sharp_price <= 1.0:
                    continue

                # Vantaggio: soft book offre di più → "lag" verso il valore
                advantage_pct = (soft_price / sharp_price - 1.0) * 100
                if advantage_pct >= LAG_THRESHOLD_PCT:
                    # Edge dal lag: quanto è "sottovalutato" il soft book
                    # rispetto alla probabilità fair di Pinnacle
                    implied_sharp = 1.0 / sharp_price
                    edge_from_lag = implied_sharp - (1.0 / soft_price)
                    if edge_from_lag >= EDGE_FROM_LAG_MIN:
                        lags.append({
                            "bookmaker": bname,
                            "esito": esito,
                            "sharp_price": round(sharp_price, 3),
                            "soft_price": round(soft_price, 3),
                            "advantage_pct": round(advantage_pct, 2),
                            "edge_from_lag": round(edge_from_lag, 4),
                            "sharp_implied": round(implied_sharp, 4),
                        })

    lags.sort(key=lambda x: x["edge_from_lag"], reverse=True)
    return lags


def find_best_prices_with_advantage(bookmakers: List[Dict],
                                    home_api: str = "", away_api: str = ""
                                    ) -> Dict[str, Dict]:
    """Per ogni esito, trova il miglior prezzo E identifica il vantaggio.

    Ritorna {esito: {best_price, best_book, sharp_price, sharp_book,
                      advantage_pct, lag_book}}.
    """
    sharp = identify_sharp_prices(bookmakers, home_api, away_api)

    # Raccogli tutti i prezzi per esito
    prices: Dict[str, List[Tuple[float, str]]] = {}
    for bm in bookmakers:
        bname = (bm.get("title") or bm.get("key") or "Unknown")
        for mkt in bm.get("markets", []):
            if mkt.get("key") == "h2h":
                for out in mkt.get("outcomes", []):
                    name = (out.get("name") or "").strip().lower()
                    price = out.get("price")
                    if not price or float(price) <= 1.0:
                        continue
                    if name == home_api:
                        esito = "1"
                    elif name == away_api:
                        esito = "2"
                    elif name in ("draw", "pareggio"):
                        esito = "X"
                    else:
                        continue
                    prices.setdefault(esito, []).append((float(price), bname))
            elif mkt.get("key") == "totals":
                for out in mkt.get("outcomes", []):
                    name = (out.get("name") or "").strip().lower()
                    price = out.get("price")
                    point = out.get("point")
                    if not price or not point or float(price) <= 1.0:
                        continue
                    if float(point) == 2.5:
                        if "over" in name:
                            esito = "Over 2.5"
                        elif "under" in name:
                            esito = "Under 2.5"
                        else:
                            continue
                        prices.setdefault(esito, []).append(
                            (float(price), bname))

    result = {}
    for esito, price_list in prices.items():
        if not price_list:
            continue
        best_price, best_book = max(price_list, key=lambda x: x[0])
        sharp_price = sharp.get(esito)
        sharp_book = "Pinnacle" if sharp_price else None

        advantage_pct = 0.0
        lag_book = None
        if sharp_price and sharp_price > 0:
            advantage_pct = (best_price / sharp_price - 1.0) * 100
            # Se il miglior prezzo non è da Pinnacle, c'è un lag
            if "pinnacle" not in best_book.lower():
                lag_book = best_book

        result[esito] = {
            "best_price": round(best_price, 3),
            "best_book": best_book,
            "sharp_price": round(sharp_price, 3) if sharp_price else None,
            "sharp_book": sharp_book,
            "advantage_pct": round(advantage_pct, 2),
            "lag_book": lag_book,
            "n_bookmakers": len(price_list),
        }

    return result


def analyze_match_bookmakers(match: Dict) -> Dict:
    """Analisi completa dei bookmaker per un match.

    Ritorna lag detected, best prices, e opportunities.
    """
    home_api = (match.get("home_team") or "").strip().lower()
    away_api = (match.get("away_team") or "").strip().lower()
    bookmakers = match.get("bookmakers", [])

    lags = detect_soft_book_lag(bookmakers, home_api, away_api)
    best_prices = find_best_prices_with_advantage(
        bookmakers, home_api, away_api)

    # Calcola edge totale dal lag
    total_lag_edge = sum(l["edge_from_lag"] for l in lags)

    return {
        "match_id": match.get("id", ""),
        "evento": f"{match.get('home_team', '?')} vs {match.get('away_team', '?')}",
        "n_bookmakers": len(bookmakers),
        "lags_detected": len(lags),
        "lag_details": lags,
        "best_prices": best_prices,
        "total_lag_edge": round(total_lag_edge, 4),
        "has_sharp": any(
            any(s in (bm.get("title") or "").lower() for s in SHARP_BOOKS)
            for bm in bookmakers
        ),
    }


# --- CLI ---

def _report(data: Dict) -> str:
    lines = [f"📊 Analisi multi-bookmaker: {data['evento']}"]
    lines.append(f"   Bookmaker: {data['n_bookmakers']} "
                 f"| Sharp: {'✅' if data['has_sharp'] else '❌'}")
    lines.append("")

    if data["lag_details"]:
        lines.append(f"⚠️  {data['lags_detected']} soft book(s) in lag:")
        for lag in data["lag_details"]:
            lines.append(
                f"   • {lag['bookmaker']} — {lag['esito']}: "
                f"{lag['soft_price']:.2f} (sharp: {lag['sharp_price']:.2f}) "
                f"| +{lag['advantage_pct']:.1f}% | edge +{lag['edge_from_lag']:.2%}")
    else:
        lines.append("✅ Nessun lag rilevato: i soft book sono allineati.")

    lines.append("")
    lines.append("Migliori prezzi per esito:")
    for esito, info in data["best_prices"].items():
        sharp_note = f" (vs sharp {info['sharp_price']:.2f})" if info["sharp_price"] else ""
        lag_note = f" ⚠️ LAG" if info["lag_book"] else ""
        lines.append(f"   {esito}: {info['best_price']:.2f} @ {info['best_book']}"
                     f"{sharp_note}{lag_note}")

    if data["total_lag_edge"] > 0:
        lines.append(f"\n📈 Edge totale dal lag: +{data['total_lag_edge']:.2%}")

    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Multi-bookmaker price advantage e soft book lag detection")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--match", type=str, default=None)
    args = ap.parse_args(argv)

    if args.match:
        from tracker import _get_conn
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, home_team, away_team FROM matches WHERE id=?",
            (args.match,)).fetchone()
        conn.close()
        if not row:
            print(f"❌ Match {args.match} non trovato")
            return 1
        # Load bookmakers from cache
        from config import DATA_DIR
        cache_file = DATA_DIR / "odds_cache.json"
        if cache_file.exists():
            cache = json.loads(cache_file.read_text())
            match_data = None
            for m in cache.get("data", []):
                if m.get("id") == args.match:
                    match_data = m
                    break
            if match_data:
                result = analyze_match_bookmakers(match_data)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    print(_report(result))
                return 0
        print(f"❌ Nessun dato bookmaker per {args.match}")
        return 1

    print("ℹ️  Usa --match ID per analizzare un match specifico.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
