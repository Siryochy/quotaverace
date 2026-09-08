#!/usr/bin/env python3
"""Cross-check SX Bet vs modello di Poisson (sola lettura, nessun ordine).

Flusso:
1. scan live palinsesto 1X2 calcio SX Bet (book + profondita');
2. risoluzione dei nomi squadre SX verso i rating di produzione
   (GET /api/ratings su Railway, batch);
3. probabilita' 1X2 del modello Poisson per ogni partita risolta
   (GET /api/segnali, che usa i rating di produzione);
4. EV per ogni esito usando la QUOTA DEL BOOK SX (best back del mercato
   binario "X vs Not X"), con i filtri di sicurezza del progetto
   (quota range, EV>=3%, inv_sum coerenza, liquidita', guardia 15', ecc.);
5. stake Kelly frazionato sul saldo reale (env BANKROLL_USDC, default 47.16)
   con i cap di produzione (value 10% / strong_value 25%, floor 1 USDC,
   esposizione giornaliera 40%).
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from execution_engine import SxBetProvider, pct_scaled_to_decimal, sx_units_to_stake

API_BASE = "https://api-production-dffd.up.railway.app"
BANKROLL = float(__import__("os").getenv("BANKROLL_USDC", "47.16"))

MIN_LIQ_USDC = 1.0
MIN_DEPTH_USDC = 5.0
MIN_INV_SUM, MAX_INV_SUM = 0.98, 1.08
ODDS_MIN, ODDS_MAX = 1.30, 30.0
EV_MIN = 0.03
MARKET_EDGE_MIN = 0.03
GUARDIA_MIN = 15  # minuti prima del kickoff
KELLY_FRACTION = 0.25
CAP_VALUE = 0.10
CAP_STRONG = 0.25
CAP_ESP = 0.40

p = SxBetProvider()  # letture pubbliche SX


def http_get_json(url: str, timeout: float = 20.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def norm(name: str) -> str:
    """Normalizza un nome squadra per il matching."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\b(fc|sk|sc|cf|ca|cr|ec|ac|as|ss|ud|cd|if|ik|fk|sp|de|do|da|afc|bsc|kv|ks|ts|mj|boca|racing|club|atletico|athletic|internacional)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def best_team_match(sx_name: str, ratings: dict) -> str | None:
    """Cerca il miglior match del nome SX tra i rating di produzione."""
    n = norm(sx_name)
    if not n:
        return None
    # match esatto normalizzato
    for rn in ratings:
        if norm(rn) == n:
            return rn
    # match per token: il nome SX deve essere sottoinsieme o contenere il rating
    toks = set(n.split())
    best, best_score = None, 0
    for rn in ratings:
        rt = set(norm(rn).split())
        if not rt:
            continue
        inter = len(toks & rt)
        if inter >= 2 and inter > best_score:
            best, best_score = rn, inter
    return best


def book_snapshot(mid: str) -> dict:
    """Best back + profondita' per i due esiti del mercato binario."""
    data = p._get("orderbook-v3/snapshot", params={
        "marketHash": mid, "showTakerPerspective": "true"})
    d = data.get("data") or {}
    out = {}
    for key, name in (("outcomeOne", 1), ("outcomeTwo", 2)):
        levels = d.get(key) or []
        best, depth = None, 0.0
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


def main():
    # 1) palinsesto SX
    print("1) Scan palinsesto SX Bet 1X2 calcio...")
    markets = p.list_market_catalogue(event_type_ids=("5",), max_results=300)
    by_event = defaultdict(list)
    for m in markets:
        by_event[m["event_id"]].append(m)
    print(f"   {len(markets)} mercati binari, {len(by_event)} partite")

    # snapshot book parallelo
    def fetch(mid):
        try:
            return mid, book_snapshot(mid)
        except Exception as e:
            return mid, {"error": str(e)}

    books = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for mid, res in ex.map(fetch, [m["market_id"] for m in markets]):
            books[mid] = res

    # 2) risoluzione nomi vs rating produzione
    print("2) Risoluzione nomi squadre vs rating di produzione...")
    sx_teams = set()
    for legs in by_event.values():
        sx_teams.add(legs[0].get("team_one_name") or "")
        sx_teams.add(legs[0].get("team_two_name") or "")
    sx_teams.discard("")

    # scarica i rating: prova a risolvere ogni nome con /api/ratings
    def fetch_rating(team):
        q = urllib.parse.quote(team)
        try:
            d = http_get_json(f"{API_BASE}/api/ratings?teams={q}")
        except Exception:
            return team, None
        rows = (d or {}).get("ratings") or []
        r = rows[0] if rows else None
        return team, (r or {}).get("rating")

    ratings = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for team, r in ex.map(fetch_rating, sorted(sx_teams)):
            ratings[team] = r

    known = {t: r for t, r in ratings.items() if r}
    print(f"   {len(sx_teams)} squadre SX, {len(known)} con rating di produzione")

    # mappa nome SX -> nome rating risolto
    resolved = {}
    for t in sorted(sx_teams):
        rn = best_team_match(t, set(known.keys()))
        resolved[t] = rn
    resolved_cnt = sum(1 for v in resolved.values() if v)
    print(f"   {resolved_cnt}/{len(sx_teams)} squadre risolte verso il modello")

    # 3) prob modello per ogni partita risolta (via /api/segnali)
    def calc_signal(home, away):
        q = urllib.parse.urlencode({"home": home, "away": away})
        try:
            d = http_get_json(f"{API_BASE}/api/segnali?{q}")
        except Exception as e:
            return (home, away), {"error": str(e)}
        return (home, away), d

    rows = []
    for eid, legs in by_event.items():
        leg0 = legs[0]
        h = resolved.get(leg0.get("team_one_name") or "")
        a = resolved.get(leg0.get("team_two_name") or "")
        if not h or not a:
            continue
        rows.append((eid, legs, h, a))

    print(f"3) Calcolo modello Poisson per {len(rows)} partite risolte...")
    sig = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(calc_signal, h, a): (h, a) for _, _, h, a in rows}
        for fut in as_completed(futs):
            k, d = fut.result()
            sig[k] = d

    # 4) incrocio: EV con quota book SX + filtri
    now = datetime.now(timezone.utc)
    print("\n" + "=" * 110)
    print("CROSS-CHECK SX BET vs MODELLO POISSON (quota di scambio = book SX)")
    print("=" * 110)

    candidates = []
    for eid, legs, h, a in rows:
        s = sig.get((h, a)) or {}
        if "error" in s:
            continue
        p1, pX, p2 = s.get("p1", 0), s.get("pX", 0), s.get("p2", 0)
        lam = f"{s.get('lam_h','?')}-{s.get('lam_a','?')}"
        # trova il book di ogni esito: mercato binario il cui outcome_one
        # e' il nome squadra (o Tie)
        leg_book = {}
        for leg in legs:
            o1 = (leg.get("outcome_one_name") or "").lower()
            if "tie" in o1 or "pareggio" in o1:
                leg_book["X"] = books.get(leg["market_id"])
            elif norm(o1) == norm(h) or norm(o1) == norm(leg0.get("team_one_name") or ""):
                leg_book["1"] = books.get(leg["market_id"])
            elif norm(o1) == norm(a) or norm(o1) == norm(leg0.get("team_two_name") or ""):
                leg_book["2"] = books.get(leg["market_id"])
        evs = {}
        for esito, prob in (("1", p1), ("X", pX), ("2", p2)):
            b = leg_book.get(esito)
            if not b or "error" in b or not b.get(1, {}).get("best"):
                continue
            q = b[1]["best"]["price"]
            d1 = b[1].get("depth", 0)
            d2 = b[2].get("depth", 0)
            tot = d1 + d2
            ev = prob * q - 1
            evs[esito] = {"q": q, "ev": ev, "depth": tot,
                          "prob": prob, "d1": d1, "d2": d2}
        if not evs:
            continue

        ko_raw = (legs[0].get("open_date") or "")[:16]
        try:
            kdt = datetime.fromisoformat(ko_raw).replace(tzinfo=timezone.utc)
            min_to_ko = (kdt - now).total_seconds() / 60
        except Exception:
            min_to_ko = None
        ev_name = (legs[0].get("event_name") or "").strip()

        for esito, info in sorted(evs.items(), key=lambda kv: -kv[1]["ev"]):
            q, ev, depth = info["q"], info["ev"], info["depth"]
            prob = info["prob"]
            sane = (q and ODDS_MIN <= q <= ODDS_MAX and ev >= EV_MIN
                    and depth >= MIN_DEPTH_USDC
                    and info["d1"] >= MIN_LIQ_USDC and info["d2"] >= MIN_LIQ_USDC)
            # inv_sum del book dell'esito (i due lati del binario)
            inv_sum = None
            b = leg_book.get(esito)
            if b and not ("error" in b) and b.get(1, {}).get("best") and b.get(2, {}).get("best"):
                inv_sum = round(1.0 / b[1]["best"]["price"] + 1.0 / b[2]["best"]["price"], 4)
            coherent = inv_sum is not None and MIN_INV_SUM <= inv_sum <= MAX_INV_SUM
            # guardia 15' e match non iniziato
            not_started = min_to_ko is None or min_to_ko > GUARDIA_MIN
            label = {"1": f"1 {h}", "X": "X pareggio", "2": f"2 {a}"}[esito]
            ok = sane and coherent and not_started
            if ok:
                stake = BANKROLL * KELLY_FRACTION * (ev / (q - 1))
                stake = min(stake, BANKROLL * (CAP_STRONG if ev >= 0.08 else CAP_VALUE))
                stake = max(1.0, round(stake, 2)) if stake >= 1.0 else 0.0
                candidates.append({
                    "partita": ev_name, "ko": ko_raw, "min_to_ko": min_to_ko,
                    "esito": label, "esito_key": esito, "q_sx": q,
                    "ev": ev, "prob": prob, "depth": depth, "inv_sum": inv_sum,
                    "stake": stake, "lam": lam, "market_edge": None,
                })
            tag = ("🟢 OK" if ok else
                   "⏱ guardia" if not not_started else
                   "❌ filtri" if not sane else
                   "⚠️ inv_sum" if not coherent else "❌")
            extra = []
            if ok:
                extra.append(f"stake {candidates[-1]['stake']:.2f} USDC" if candidates and candidates[-1]["partita"] == ev_name else "")
            print(f"   [{tag}] {ev_name} | {label} | qSX {q:.2f} | EV {ev*100:+.1f}% "
                  f"| p {prob*100:.0f}% | liq {depth:.0f} USDC | inv {inv_sum} "
                  f"| lam {lam} | ko {ko_raw}" + (f" | {extra[0]}" if extra and extra[0] else ""))

    # 5) report candidati
    print("\n" + "=" * 110)
    print(f"CANDIDATI PIAZZABILI (filtri superati, quota book SX, saldo {BANKROLL:.2f} USDC)")
    print("=" * 110)
    if not candidates:
        print("   Nessun candidato: nessun esito supera EV>=3% sulla quota SX con filtri ok.")
    for c in sorted(candidates, key=lambda x: -x["ev"]):
        print(f"   ⚽ {c['partita']} (ko {c['ko']} UTC, tra {c['min_to_ko']:.0f}')")
        print(f"      {c['esito']} @ {c['q_sx']:.2f} | EV {c['ev']*100:+.1f}% | "
              f"p modello {c['prob']*100:.0f}% | liq {c['depth']:.0f} USDC | "
              f"stake suggerito {c['stake']:.2f} USDC")
    print(f"\nTotale candidati: {len(candidates)}")


if __name__ == "__main__":
    sys.exit(main())