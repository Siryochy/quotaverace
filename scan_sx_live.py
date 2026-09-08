#!/usr/bin/env python3
"""Controllo rapido SX Bet (sola lettura pubblica, nessun ordine).

Stampa:
1. i mercati 1X2 calcio (sportId 5, type 1 = "X vs Not X") attualmente aperti;
2. per ogni partita, le quote di scambio (order book) dei tre esiti con
   liquidita' (profondita' in USDC) e i filtri di valore/coerenza usati
   dal progetto (inv_sum 0.98-1.08, quote sane, profondita' >= minimo ordine).
"""
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from execution_engine import SxBetProvider, pct_scaled_to_decimal, sx_units_to_stake

MIN_LIQ_USDC = 1.0          # minimo ordine SX Bet (1 USDC)
MIN_DEPTH_USDC = 5.0        # soglia "liquidita' sufficiente" per il report
MIN_INV_SUM, MAX_INV_SUM = 0.98, 1.08
ODDS_MIN, ODDS_MAX = 1.30, 30.0
MAX_MARKETS = 300          # mercati binari da scansionare (100 partite)
BOOK_TIMEOUT = 8.0         # timeout per singolo snapshot

p = SxBetProvider()  # letture pubbliche: nessuna chiave


def book(market_id: str) -> dict:
    """Snapshot order book: best back per esito + profondita' totale."""
    data = p._get("orderbook-v3/snapshot", params={
        "marketHash": market_id, "showTakerPerspective": "true"})
    d = data.get("data") or {}
    out = {}
    for key, name in (("outcomeOne", 1), ("outcomeTwo", 2)):
        levels = d.get(key) or []
        best = None
        depth = 0.0
        for lv in levels:
            if not isinstance(lv, dict):
                continue
            q = pct_scaled_to_decimal(lv.get("percentageOdds"))
            size = sx_units_to_stake(lv.get("size"))
            if q is None or q <= 0:
                continue
            depth += size
            if best is None or q > best["price"]:
                best = {"price": q, "size": size}
        out[name] = {"best": best, "depth": round(depth, 2), "levels": len(levels)}
    return out


def fetch_book(mid: str):
    """Wrapper per il pool: None su errore/timeout."""
    try:
        return mid, book(mid)
    except Exception as e:
        return mid, {"error": str(e)}


def books_parallel(market_ids):
    """Snapshot paralleli (max 10 thread)."""
    out = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_book, mid): mid for mid in market_ids}
        for fut in as_completed(futs):
            mid, res = fut.result()
            out[mid] = res
    return out

print("=" * 100)
print("SX BET — SCAN LIVE MERCATI 1X2 CALCIO (sportId 5, type 1)")
print("=" * 100)

try:
    markets = p.list_market_catalogue(event_type_ids=("5",), max_results=MAX_MARKETS)
except Exception as e:
    print(f"ERRORE discovery: {e}")
    sys.exit(1)

if not markets:
    print("Nessun mercato 1X2 calcio attivo al momento.")
    sys.exit(0)

# raggruppa i 3 mercati binari per evento
by_event = defaultdict(list)
for m in markets:
    by_event[m["event_id"]].append(m)

print(f"\n{len(markets)} mercati binari 1X2 trovati, {len(by_event)} partite distinte.")

# snapshot paralleli di TUTTI i book prima di stampare
all_ids = [m["market_id"] for m in markets]
print(f"Snapshot order book in corso su {len(all_ids)} mercati (parallelo)...")
books = books_parallel(all_ids)

def show_match(event_id, legs):
    ev = legs[0]["event_name"]
    ko = (legs[0].get("open_date") or "?")[:16]
    print("-" * 100)
    print(f"⚽ {ev}  (kickoff {ko} UTC, event_id {event_id})")
    for leg in sorted(legs, key=lambda x: (x.get("outcome_one_name") or "")):
        mid = leg["market_id"]
        o1, o2 = leg.get("outcome_one_name"), leg.get("outcome_two_name")
        b = books.get(mid) or {}
        if "error" in b:
            print(f"   [{mid[:12]}…] book non disponibile: {b['error']}")
            continue
        sel1, sel2 = b.get(1, {}), b.get(2, {})
        q1 = sel1["best"]["price"] if sel1.get("best") else None
        q2 = sel2["best"]["price"] if sel2.get("best") else None
        d1, d2 = sel1.get("depth", 0.0), sel2.get("depth", 0.0)
        tot = d1 + d2
        inv_sum = None
        if q1 and q2:
            inv_sum = round(1.0 / q1 + 1.0 / q2, 4)
        sane_odds = q1 and q2 and ODDS_MIN <= q1 <= ODDS_MAX and ODDS_MIN <= q2 <= ODDS_MAX
        coherent = inv_sum is not None and MIN_INV_SUM <= inv_sum <= MAX_INV_SUM
        liquid = tot >= MIN_DEPTH_USDC and (d1 >= MIN_LIQ_USDC and d2 >= MIN_LIQ_USDC)
        flags = []
        if coherent and sane_odds:
            flags.append("✅ coerenza inv_sum ok")
        elif inv_sum is not None:
            flags.append(f"⚠️ inv_sum {inv_sum} fuori range")
        flags.append("🟢 liquido" if liquid else "🔴 illiquido")
        flags.append(f"book {sel1.get('levels', 0)}+{sel2.get('levels', 0)} livelli")
        print(f"   ▸ {o1} | {o2}")
        print(f"     back: {q1 or '-'} (prof. {d1:8.2f} USDC)   |   {q2 or '-'} (prof. {d2:8.2f} USDC)")
        print(f"     inv_sum={inv_sum}  liquidita' tot {tot:.2f} USDC  —  {', '.join(flags)}")

# stampa le partite con kickoff piu' vicino prima (max 25 per leggibilita')
ordered = sorted(by_event.items(),
                 key=lambda kv: (kv[1][0].get("open_date") or "") or "9999")
for eid, legs in ordered[:25]:
    show_match(eid, legs)

n_ok = 0
print("\n" + "=" * 100)
print("PARTITE CON LIQUIDITA' SUFFICIENTE E MERCATO COERENTE (candidati value)")
print("=" * 100)
for eid, legs in ordered:
    ok_legs = 0
    for leg in legs:
        mid = leg["market_id"]
        b = books.get(mid) or {}
        if "error" in b:
            continue
        s1, s2 = b.get(1, {}), b.get(2, {})
        q1 = s1["best"]["price"] if s1.get("best") else None
        q2 = s2["best"]["price"] if s2.get("best") else None
        tot = s1.get("depth", 0.0) + s2.get("depth", 0.0)
        inv_sum = (round(1.0 / q1 + 1.0 / q2, 4) if q1 and q2 else None)
        coherent = inv_sum is not None and MIN_INV_SUM <= inv_sum <= MAX_INV_SUM
        sane = q1 and q2 and ODDS_MIN <= q1 <= ODDS_MAX and ODDS_MIN <= q2 <= ODDS_MAX
        liquid = tot >= MIN_DEPTH_USDC and s1.get("depth", 0) >= MIN_LIQ_USDC and s2.get("depth", 0) >= MIN_LIQ_USDC
        if coherent and sane and liquid:
            ok_legs += 1
    if ok_legs >= 2:  # almeno 2 esiti del 1X2 liquidi e coerenti
        n_ok += 1
        ev = legs[0]["event_name"]
        ko = (legs[0].get("open_date") or "?")[:16]
        print(f"   ⚽ {ev} (kickoff {ko}) — {ok_legs}/3 esiti ok")

print(f"\nTotale candidati con liquidita' sufficiente: {n_ok}/{len(by_event)}")