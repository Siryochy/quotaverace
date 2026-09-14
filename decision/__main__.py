"""CLI della catena di decisione (`venv/bin/python -m decision [comando]`).

Comandi:
  demo [--bankroll N] [--mode live|sim|off]
      fa passare tre segnali dimostrativi nella catena e mostra verdetto,
      motivo, stake (o il rifiuto) per ognuno: nessun DB, nessuna rete,
      nessun ordine;
  queue [--json]
      mostra la coda delle revisioni umane sul volume.

Tutti gli scenari sono OFFLINE: questa CLI serve a ispezionare i contratti, non
a piazzare niente.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from typing import Optional, Sequence

from .limits import RiskLimits
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

    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
