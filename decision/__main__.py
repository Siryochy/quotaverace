"""CLI della catena di decisione (`venv/bin/python -m decision [comando]`).

Comandi:
  demo [--bankroll N] [--mode live|sim|off]
      fa passare tre segnali dimostrativi nella catena e mostra verdetto,
      motivo, stake (o il rifiuto) per ognuno: nessun DB, nessuna rete,
      nessun ordine;
  queue [--json]
      mostra la coda delle revisioni umane sul volume;
  review [--json] [--send] [--limit N] [--callback ID] [--reviewer NOME]
      revisioni su Telegram: senza flag mostra i prompt in attesa con le loro
      chiavi di callback; `--callback` SIMULA un click (idempotente, gateway
      shadow: nessun ordine); `--send` invia i prompt all'admin (rete, scelta
      esplicita: scrive il marker dei prompt inviati);
  feedback [--json] [--settle]
      telemetria del ledger decisioni (verdetti, motivi, ROI realizzato vs EV
      atteso, misure "shadow" dei segnali scartati). `--settle` chiude le
      decisioni coi risultati gia' in `match_results` (SCRITTURA: default off);
  status [--json]
      istantanea del fail-fast: catena dei blocchi, blocchi attivi per stadio,
      avvisi (legge i file di stato: nessuna scrittura, nessun ordine);
  shadow [--json] [--limit N]
      registro della shadow mode (comandi che SAREBBERO stati eseguiti);
  market [--file F | --stdin] [--gateway ID] [--source NOME] [--json] [--assume-utc]
      valida quote contro il contratto di mercato (schema, campi, quota minima
      0.1, timestamp UTC): senza input usa due esempi integrati, uno valido e
      uno respinto. Esce con 1 se c'e' almeno un rifiuto (uso in script);
  feed [--json] [--refresh] [--force]
      stato del gateway di mercato (SX Bet primaria): validazione, freschezza,
      identificatori tracciati (request/trace/gateway/schema/config hash) e
      verdetto del gate. `--refresh` esegue UN refresh (rete pubblica SX, zero
      crediti, nessun ordine); `--force` ignora la finestra di riuso.
      Esce con 1 se il gate bloccherebbe le puntate.

Tutti gli scenari sono OFFLINE: questa CLI serve a ispezionare i contratti, non
a piazzare niente. Nessun comando di questa CLI chiama provider o API a
pagamento (sviluppo senza consumare crediti).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

from .feedback import settle as feedback_settle, snapshot, format_report as feedback_report
from .feeds import DEFAULT_GATEWAY_ID, MarketFeed, feed_enabled
from .guards import SAFETY_CHAIN, snapshot as guard_snapshot
from .limits import RiskLimits
from .market import MARKET_SCHEMA_VERSION, MIN_ODDS, validate_batch
from .middleware import NullSink, Observability
from .shadow import format_report as shadow_report, shadow_summary
from .review_telegram import (
    CallbackStore, format_report as review_report, handle_callback,
    pending_prompts, send_prompts,
)
from .models import DataQuality, KillSwitchStatus, Signal, utcnow
from .pipeline import decide, summary
from .review_queue import ReviewQueue, format_report

DEMO_KICKOFF_HOURS = 6.0


def _demo_signal(*, label: str, price: float, market_prob: float, blended_prob: float,
                 league: str = "Premier League", confidence: float = 0.90,
                 tier: str = "value", depth: Optional[float] = None) -> Signal:
    return Signal(
        match_id=f"demo-{label}",
        league=league,
        outcome="1",
        selection_label=label,
        kickoff=utcnow() + timedelta(hours=DEMO_KICKOFF_HOURS),
        price=price,
        price_source="demo",
        market_prob=market_prob,
        model_prob=blended_prob + 0.01,
        blended_prob=blended_prob,
        tier=tier,                                    # type: ignore[arg-type]
        confidence=confidence,
        data_quality=DataQuality(model_coverage=0.8, calibrated=True, depth_usdc=depth),
    )


def _scenarios() -> list[tuple[str, Signal]]:
    """Tre esiti diversi della catena: approve, review, reject."""
    return [
        ("APPROVE  (favorito forte, confidenza alta)",
         _demo_signal(label="Inter (1)", price=1.60, market_prob=0.58, blended_prob=0.65,
                      tier="strong_value", depth=900.0)),
        ("REVIEW   (gate ok, confidenza bassa -> coda umana)",
         _demo_signal(label="Mainz (1)", price=1.72, market_prob=0.56, blended_prob=0.62,
                      league="Bundesliga", confidence=0.30, depth=200.0)),
        ("REJECT   (quota fuori fascia favoriti)",
         _demo_signal(label="Getafe (1)", price=2.35, market_prob=0.42, blended_prob=0.50,
                      league="La Liga", depth=500.0)),
    ]


def cmd_demo(args) -> int:
    limits = RiskLimits.from_env()
    kills = KillSwitchStatus(mode=args.mode, env_mode=args.mode, provider_ready=True)
    print(f"\n=== Catena di decisione (bankroll {args.bankroll:.2f}, modalita' {args.mode}) ===")
    print(f"    cap applicato: value/moderate {limits.cap_value*100:.2f}% | "
          f"strong {limits.cap_strong*100:.2f}% | review sotto confidenza "
          f"{limits.review_confidence_min:.2f}")
    if args.mode == "off":
        print("    ⚠️ modalita' off: il kill switch risponde per primo, "
              "nessun calcolo di stake parte")
    records = []
    for label, signal in _scenarios():
        record = decide(signal, kills=kills, limits=limits, bankroll=args.bankroll)
        records.append(record)
        stake = record.stake
        print(f"\n  {label}")
        print(f"    quota {signal.price:.2f} | mercato {signal.market_prob*100:.1f}% "
              f"| blend {signal.blended_prob*100:.1f}% | edge {signal.edge*100:+.1f}pp "
              f"| EV {signal.ev*100:+.1f}%")
        print(f"    verdetto : {record.risk.verdict} ({record.risk.reason.value})")
        if record.risk.detail:
            print(f"    motivo   : {record.risk.detail}")
        if stake is not None:
            print(f"    stake    : {stake.stake:.2f} (kelly {stake.kelly_fraction:.3f} x "
                  f"cap {stake.cap_pct*100:.2f}% [{stake.cap_source}]) "
                  f"{'ESEGUIBILE' if stake.executable else 'SALTATA'}")
            if stake.detail:
                print(f"    dettaglio: {stake.detail}")
        else:
            print("    stake    : nessuno")
    print("\n" + json.dumps(summary(records), ensure_ascii=False))
    return 0


def cmd_queue(args) -> int:
    queue = ReviewQueue()
    report = queue.summary()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report, queue))
    return 0


def cmd_feedback(args) -> int:
    if args.settle:
        result = feedback_settle()
        if result.get("error"):
            print(f"settlement non eseguito: {result['error']}")
        else:
            print(f"decisioni saldate: {result['settled']} "
                  f"(push {result['pushes']})")
    data = snapshot()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(feedback_report(data))
    return 0


def cmd_review(args) -> int:
    """Revisioni su Telegram: ispezione, simulazione di un click, invio.

    `--callback` percorre la catena COMPLETA (decisione, stake, comandi) con i
    gateway shadow: l'idempotenza e la mappa dei comandi si verificano senza
    Telegram e senza ordini. `--send` e' l'unico percorso di rete, ed e' per
    scelta esplicita.
    """
    # Sink nullo: la CLI ispeziona, non scrive eventi sul volume.
    obs = Observability(sink=NullSink())
    store = CallbackStore()
    queue = ReviewQueue()

    if args.callback:
        outcome = handle_callback(args.callback, queue=queue, store=store,
                                  reviewer=args.reviewer or "cli",
                                  bankroll=args.bankroll,
                                  observability=obs, mode=args.mode, capture=True)
        if args.json:
            payload = outcome.as_dict()
            payload["results"] = [r.model_dump(mode="json") for r in outcome.results]
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"\n=== Callback {outcome.callback_id} ({outcome.action}) ===")
            print(f"  esito    : {outcome.status}"
                  f"{' (duplicato: nessuna nuova decisione)' if outcome.duplicate else ''}")
            print(f"  revisione: {outcome.record_id or '-'}")
            if outcome.verdict:
                print(f"  verdetto : {outcome.verdict} ({outcome.reason})")
            if outcome.stake is not None:
                print(f"  stake    : {outcome.stake:.2f}")
            if outcome.commands:
                print(f"  comandi  : {', '.join(outcome.commands)}"
                      f"{' | ordine che SAREBBE partito' if outcome.would_order else ''}")
            for result in outcome.results:
                print(f"    · {result.kind.value} → {result.status} "
                      f"({result.gateway}) {result.detail}")
            if outcome.detail:
                print(f"  dettaglio: {outcome.detail}")
        return 1 if outcome.status in ("error", "unknown") else 0

    if args.send:
        result = send_prompts(queue=queue, store=store, limit=args.limit,
                              observability=obs)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"\n=== Invio prompt revisioni ===")
            print(f"  destinatari: {', '.join(result['targets']) or '-'}")
            print(f"  inviati: {result['sent']} | falliti: {result['skipped']}")
            for prompt in result["prompts"]:
                print(f"    · {prompt['record_id']} → {prompt['approve']}")
            for error in result["errors"]:
                print(f"  ⚠️ {error}")
        return 0 if not result["errors"] else 1

    prompts = pending_prompts(queue, store, include_prompted=args.all)
    data = {"store": store.summary(), "queue": queue.summary(),
            "pending_prompts": [{k: v for k, v in prompt.items() if k != "entry"}
                                for prompt in prompts]}
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print("\n" + review_report(store=store, queue=queue))
        print(f"\n  prompt da inviare: {len(prompts)}")
        for prompt in prompts:
            print(f"\n{prompt['text']}")
            print(f"  approva: {prompt['callback_ids']['approve']}"
                  f" | rifiuta: {prompt['callback_ids']['reject']}")
    return 0


def cmd_status(args) -> int:
    data = guard_snapshot()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print("\n=== Fail-fast: catena dei blocchi ===")
    for rule in SAFETY_CHAIN:
        print(f"  {rule.precedence}. {rule.name:18s} stadi: {', '.join(rule.stages)}")
        print(f"     {rule.label} — ripresa: {rule.hint}")
    print(f"\n  modalita': {data['mode']} (env {data['env_mode']}, "
          f"override {data['override']}, provider {'ok' if data['provider_ready'] else 'assente'})")
    for stage, items in data["stages"].items():
        label = "BLOCCATO" if items else "libero"
        detail = items[0]["detail"] if items else ""
        print(f"  stadio {stage:11s}: {label} {detail}")
    for stage, items in data["advisories"].items():
        for item in items:
            print(f"  avviso ({stage}): {item['name']} — {item['detail']}")
    return 0


def cmd_shadow(args) -> int:
    summary = shadow_summary(limit=args.limit)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(shadow_report(summary))
    return 0


#: Esempi per il comando `market`: una quota conforme al contratto e una che
#: viola quattro regole insieme (schema, mercato/selezione, quota, timestamp).
SAMPLE_QUOTES: tuple[dict[str, Any], ...] = (
    {
        "schema_version": MARKET_SCHEMA_VERSION,
        "event_id": "sx-L19947936",
        "market": "h2h",
        "selection": "home",
        "odds": "1.69",
        "timestamp": "2026-09-15T17:20:00Z",
        "source": "sxbet",
        "gateway_id": "sx-feed",
        "event_name": "CSKA Moscow - Rubin Kazan",
        "league": "Russian Premier League",
    },
    {
        "schema_version": "9.9",
        "event_id": "toa-abc123",
        "market": "1X2",
        "selection": "over",
        "odds": 0.05,
        "timestamp": "2026-09-15T17:20:00",
        "source": "the-odds-api",
        "gateway_id": "toa-feed",
    },
)


def cmd_market(args) -> int:
    origin = "esempi integrati"
    rows: Any = None
    if args.file or args.stdin:
        try:
            raw = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
            rows = json.loads(raw)
        except (OSError, ValueError) as exc:
            print(f"input non leggibile come JSON: {exc}", file=sys.stderr)
            return 2
        origin = args.file or "stdin"
    if isinstance(rows, dict):
        rows = [rows]
    if rows is None:
        rows = [dict(row) for row in SAMPLE_QUOTES]
    # Sink nullo: la CLI ispeziona i contratti, non scrive sul volume.
    batch = validate_batch(rows, gateway_id=args.gateway or "", source=args.source or "",
                           assume_utc=args.assume_utc, obs=Observability(sink=NullSink()))
    if args.json:
        print(json.dumps({
            "origin": origin,
            "schema_version": MARKET_SCHEMA_VERSION,
            "min_odds": MIN_ODDS,
            "summary": batch.as_dict(),
            "accepted": [quote.model_dump(mode="json") for quote in batch.accepted],
            "rejected": [rejection.as_dict() for rejection in batch.rejected],
        }, ensure_ascii=False, indent=2))
        return 1 if batch.rejected else 0
    print(f"\n=== Contratto di mercato (schema {MARKET_SCHEMA_VERSION}, "
          f"quota minima {MIN_ODDS}) ===")
    print(f"  origine: {origin} | gateway {batch.gateway_id or '-'} "
          f"| source {batch.source or '-'}")
    for quote in batch.accepted:
        print(f"  ✅ {quote.quote_id}  {quote.event_id}  {quote.market} "
              f"{quote.selection} @ {quote.odds}  ({quote.source})")
    for rejection in batch.rejected:
        print(f"  ❌ riga {rejection.index}: {rejection.code.value} "
              f"[{rejection.field or '-'}] {rejection.detail}")
    counts = batch.by_code()
    detail = (", ".join(f"{code}={count}" for code, count in counts.items())) if counts else ""
    rows_label = "riga" if batch.rejected_rows == 1 else "righe"
    line = (f"\n  accettate {len(batch.accepted)}/{batch.total} | "
            f"respinte {batch.rejected_rows} {rows_label}")
    if counts:
        line += f" ({batch.issues} problemi: {detail})"
    print(line)
    return 1 if batch.rejected else 0


def cmd_feed(args) -> int:
    """Stato del feed di mercato; con `--refresh` esegue un refresh reale.

    Il refresh e' una lettura PUBBLICA di SX Bet (zero crediti, nessun ordine),
    ma e' comunque rete: la CLI senza `--refresh` non la tocca mai.
    """
    # Sink nullo: la CLI ispeziona, non scrive eventi sul volume.
    feed = MarketFeed(observability=Observability(sink=NullSink()))
    if not feed.has_sources:
        print("nessuna sorgente di mercato configurata (fail-closed)", file=sys.stderr)
        return 2
    refreshed = False
    if args.refresh:
        snapshot = feed.refresh(force=args.force)
        refreshed = True
    gate = feed.gate(required=True)
    if args.json:
        payload: dict[str, Any] = {"gateway_id": feed.gateway_id,
                                   "state": feed.state().model_dump(mode="json"),
                                   "gate": gate.as_json()}
        if refreshed:
            payload["snapshot"] = snapshot.model_dump(mode="json")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if gate.allowed else 1
    print("\n" + feed.describe())
    if args.refresh:
        print(f"\n  refresh eseguito: {snapshot.describe()}")
    print(f"\n  gate: {'PASSA' if gate.allowed else 'BLOCCA'} ({gate.reason.value}) — {gate.detail}")
    if not gate.allowed:
        print(f"  ripresa: {gate.block.hint if gate.block else '-'}")
    return 0 if gate.allowed else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Catena di decisione (sola lettura)")
    sub = parser.add_subparsers(dest="command")

    demo = sub.add_parser("demo", help="esegue la catena su segnali dimostrativi")
    demo.add_argument("--bankroll", type=float, default=1000.0)
    demo.add_argument("--mode", choices=("live", "sim", "off"), default="live")
    demo.set_defaults(func=cmd_demo)

    queue = sub.add_parser("queue", help="mostra la coda delle revisioni umane")
    queue.add_argument("--json", action="store_true")
    queue.set_defaults(func=cmd_queue)

    review = sub.add_parser("review", help="revisioni su Telegram (callback idempotenti)")
    review.add_argument("--json", action="store_true")
    review.add_argument("--send", action="store_true",
                        help="invia i prompt all'admin (rete: scelta esplicita)")
    review.add_argument("--limit", type=int, default=5)
    review.add_argument("--all", action="store_true",
                        help="include anche le voci col prompt gia' inviato")
    review.add_argument("--callback", default="",
                        help="simula un click (es. rv:a:0123456789)")
    review.add_argument("--reviewer", default="", help="nome dell'operatore")
    review.add_argument("--bankroll", type=float, default=100.0,
                        help="bankroll virtuale della simulazione (default 100)")
    review.add_argument("--mode", choices=("live", "sim", "off"), default="sim",
                        help="modo della decisione simulata (default sim)")
    review.set_defaults(func=cmd_review)

    feedback = sub.add_parser("feedback", help="telemetria del ledger decisioni")
    feedback.add_argument("--json", action="store_true")
    feedback.add_argument("--settle", action="store_true",
                          help="chiude le decisioni coi risultati disponibili")
    feedback.set_defaults(func=cmd_feedback)

    status = sub.add_parser("status", help="istantanea del fail-fast")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    shadow = sub.add_parser("shadow", help="registro della shadow mode")
    shadow.add_argument("--json", action="store_true")
    shadow.add_argument("--limit", type=int, default=500)
    shadow.set_defaults(func=cmd_shadow)

    market = sub.add_parser("market", help="valida quote contro il contratto di mercato")
    market.add_argument("--file", help="file JSON (una quota o una lista)")
    market.add_argument("--stdin", action="store_true", help="legge il JSON da stdin")
    market.add_argument("--gateway", default="", help="gateway_id di default (se assente)")
    market.add_argument("--source", default="", help="source di default (se assente)")
    market.add_argument("--assume-utc", action="store_true",
                        help="dichiara UTC i timestamp senza fuso orario")
    market.add_argument("--json", action="store_true")
    market.set_defaults(func=cmd_market)

    feed = sub.add_parser("feed", help="stato/refresh del gateway di mercato")
    feed.add_argument("--json", action="store_true")
    feed.add_argument("--refresh", action="store_true",
                      help="esegue un refresh (lettura pubblica SX)")
    feed.add_argument("--force", action="store_true",
                      help="ignora la finestra di riuso (refresh sempre reale)")
    feed.set_defaults(func=cmd_feed)

    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
