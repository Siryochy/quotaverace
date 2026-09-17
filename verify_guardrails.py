#!/usr/bin/env python3
"""Verifica FORZATA dei guardrail di rischio (11/09/2026).

Dimostra, con i log reali, che i quattro guardrail bloccano davvero le
puntate. Non tocca la produzione: usa un DATA_DIR temporaneo, un DB
temporaneo e MAI un provider reale (nessun ordine, nessuna rete).

Scenari:
  A. Kill-switch OFF           -> il giro non parte
  B. Stop-loss giornaliero -5% -> puntate bloccate 24h
  C. Cap stake severo 1-2%     -> stake cappato < minimo ordine = ordine saltato
  D. Limiti quota 1.30-1.80    -> segnali fuori fascia mai candidati
  E. Liquidita' SX             -> book sottile: ordine rifiutato (no slippage)
  F. Lega STRATEGY_LEAGUES     -> campionati non vincenti mai candidati
  G. Circuit breakers T-60     -> finestra T-60..T-50, CB1 cap per ordine,
                                  CB2 kill switch patrimoniale 30 USDC

Uso: venv/bin/python verify_guardrails.py
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- Isolamento totale PRIMA degli import: DATA_DIR temporaneo ---------------
TMP = Path(tempfile.mkdtemp(prefix="qv_guardrails_"))
os.environ["QUOTAVERACE_DATA_DIR"] = str(TMP)
os.environ.pop("AUTO_BET_MODE", None)          # default: sim
os.environ.pop("EXECUTION_PROVIDER", None)     # nessun provider reale
os.environ.pop("EXECUTION_APP_KEY", None)
os.environ["STAKE_CAP_HARD"] = "1"
os.environ.pop("SETTLEMENT_PAUSED", None)
# Feed di mercato OFF: questa diagnostica non deve toccare la rete (il gate
# vero e' coperto da `test_decision_feed.py` e da `python -m decision feed`).
os.environ["DECISION_FEED_ENABLED"] = "0"

# --- Cattura dei log --------------------------------------------------------
_RECORDS: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _RECORDS.append(f"[{record.levelname:7}] {record.name}: "
                        f"{record.getMessage()}")


logging.basicConfig(level=logging.INFO,
                    format="%(levelname)-7s %(name)s: %(message)s")
logging.getLogger().addHandler(_Capture())
for _noisy in ("httpx", "urllib3", "telegram"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

import tracker            # noqa: E402  (dopo aver fissato DATA_DIR)
import auto_bet           # noqa: E402
import value_filter       # noqa: E402

tracker.init_db()

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"


def _head(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 68}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 68}{RESET}")


def _logs_since(mark: int) -> list[str]:
    return _RECORDS[mark:]


def _print_logs(mark: int) -> None:
    for line in _logs_since(mark):
        print(f"  {DIM}│{RESET} {line}")


def _start(hours: float = 3.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)) \
        .isoformat().replace("+00:00", "Z")


# Lega AMMESSA dalla strategia (value_filter.STRATEGY_LEAGUES): dal 15/09 la
# corsia ordini applica il gate di lega, quindi il seed della diagnostica deve
# stare in una lega ammessa — altrimenti OGNI scenario misura solo il gate di
# lega e non il guardrail che vuole verificare.
ALLOWED_LEAGUE = "Premier League"


def _seed(mid: str, esito: str, quota: float, market_prob: float,
          edge: float, home: str = "Osasuna", away: str = "Getafe") -> None:
    tracker.save_match(mid, ALLOWED_LEAGUE, home, away, _start())
    tracker.save_prediction(mid, "1X2", esito, quota, 0.60, 0.08,
                            market_prob=market_prob, market_edge=edge,
                            status="value")


def _reset_state() -> None:
    auto_bet.clear_kill_switch()
    auto_bet.clear_daily_stop()


class _LiqProv:
    """Provider finto per lo scenario E: nessuna rete, nessun ordine reale.

    Espone lo STESSO contratto che `_live_fill` usa su SX Bet (best price,
    order book taker, place_limit_order) cosi' il guardrail viene esercitato
    sul codice VERO, senza credenziali ne' chiamate esterne.
    """

    name = "sxbet-stub"

    def __init__(self, depth: float, order=None) -> None:
        self.depth = depth
        self.order = order
        self.place_calls: list = []

    def best_back_price(self, market_id, selection_id):
        return 1.70

    def get_market_book(self, market_id):
        return {"runners": [{"selectionId": 1, "availableToBack": [
            {"price": 1.65, "size": self.depth}]}]}

    def place_limit_order(self, market_id, selection_id, side, price, size,
                          persistence="LAPSE"):
        self.place_calls.append((market_id, selection_id, side, price, size))
        return self.order


def main() -> int:
    print(f"{BOLD}VERIFICA GUARDRAIL — DATA_DIR temporaneo: {TMP}{RESET}")
    import liquidity_monitor
    import sx_signals
    print(f"STAKE_CAP_HARD={auto_bet.cap_hard_active()} "
          f"MIN_STAKE_EUR={auto_bet.MIN_STAKE_EUR} "
          f"ODDS_MIN={value_filter.ODDS_MIN} "
          f"ODDS_MAX={value_filter.ODDS_MAX}")
    print(f"LIQUIDITA': totale {sx_signals.MIN_DEPTH_USDC:.0f} | "
          f"esito {sx_signals.MIN_LEG_DEPTH_USDC:.0f} | leg giocata "
          f"{auto_bet.MIN_EXEC_DEPTH_USDC:.0f} | margine "
          f"x{auto_bet.SX_DEPTH_MULTIPLIER:.1f} sullo stake")

    # Seed: un segnale perfettamente valido per la strategia favoriti.
    _seed("g-valid", "Osasuna", 1.65, 0.60, 0.07)

    # --------------------------------------------------------------- A. OFF ---
    _head("A. KILL-SWITCH OFF — il giro delle puntate non deve partire")
    _reset_state()
    auto_bet.set_kill_switch("off")
    mark = len(_RECORDS)
    placed = auto_bet.run_today_bets()
    _print_logs(mark)
    ok_a = (placed == [] and tracker.get_bets() == [])
    print(f"  {GREEN if ok_a else RED}→ puntate piazzate: "
          f"{len(placed)}  (atteso 0){RESET}")
    st = auto_bet.kill_switch_status()
    print(f"  {DIM}stato kill-switch: effective={st['effective']} "
          f"override={st['override']}{RESET}")

    # ---------------------------------------------------------- B. STOP-LOSS ---
    _head("B. STOP-LOSS GIORNALIERO -5% — puntate bloccate per 24h")
    _reset_state()
    # Trigger reale della soglia (-6% dal bankroll di inizio giornata).
    mark = len(_RECORDS)
    auto_bet.check_daily_stop(100.0)
    trigger = auto_bet.check_daily_stop(94.0)
    _print_logs(mark)
    print(f"  {DIM}trigger: stopped={trigger['stopped']} "
          f"loss={trigger['loss_pct']:.1f}% until={trigger['until']}{RESET}")
    mark = len(_RECORDS)
    placed = auto_bet.run_today_bets()
    _print_logs(mark)
    ok_b = (trigger["stopped"] and placed == []
            and tracker.get_bets() == [])
    print(f"  {GREEN if ok_b else RED}→ puntate piazzate: "
          f"{len(placed)}  (atteso 0){RESET}")

    # ---------------------------------------------------------- C. CAP 1-2% ---
    _head("C. CAP STAKE SEVERO 1% — wallet 38 USDC: cap 0.38 < min ordine 1.0")
    _reset_state()
    # Forza la modalita' LIVE con un wallet di 38 USDC (mai un ordine vero:
    # _live_fill e' sostituito da uno stub che conta le chiamate).
    auto_bet._execution_mode = lambda allow_sim=True: "live"
    # Wallet reale: 36 USDC liberi + 2 in gioco (escrow) = 38 di EQUITY.
    # Il cap 1% si misura sull'equity (fix stop-loss/equity del 15/09):
    # sul solo disponibile sarebbe 0.36, non 0.38.
    auto_bet._live_wallet_snapshot = lambda: {
        "available": 36.0, "exposure": 2.0, "equity": 38.0}
    calls = {"fill": 0}
    _real_live_fill = auto_bet._live_fill   # ripristinata nello scenario E

    def _stub_fill(pick, stake, floor):  # noqa: ANN001
        calls["fill"] += 1
        return None

    auto_bet._live_fill = _stub_fill
    mark = len(_RECORDS)
    placed = auto_bet.run_today_bets()
    _print_logs(mark)
    ok_c = (auto_bet.cap_hard_active() and placed == []
            and calls["fill"] == 0)
    print(f"  {DIM}stake teorico Kelly×cap1% su 38 = "
          f"{38.0 * 0.01:.2f} USDC{RESET}")
    print(f"  {GREEN if ok_c else RED}→ ordini inviati al provider: "
          f"{calls['fill']}  (atteso 0){RESET}")
    # Controprova: con STAKE_CAP_HARD=0 vale il floor exchange (1 USDC).
    auto_bet.STAKE_CAP_HARD = False
    auto_bet.clear_daily_stop()
    calls["fill"] = 0
    placed = auto_bet.run_today_bets()
    print(f"  {DIM}controprova STAKE_CAP_HARD=0 → il floor viene accettato: "
          f"ordini inviati {calls['fill']} (stake forzato al minimo "
          f"{auto_bet.MIN_STAKE_EUR} USDC){RESET}")
    auto_bet.STAKE_CAP_HARD = True

    # ------------------------------------------------------------ D. QUOTE ----
    _head("D. LIMITI QUOTA 1.30–1.80 — fuori fascia mai candidati")
    _reset_state()
    auto_bet._execution_mode = lambda allow_sim=True: "sim"
    tracker.save_match("g-high", ALLOWED_LEAGUE, "Osasuna", "Getafe", _start())
    # quota 1.90 > 1.80 (favorito troppo "lungo")
    tracker.save_prediction("g-high", "1X2", "Osasuna", 1.90, 0.55, 0.08,
                            market_prob=0.52, market_edge=0.05, status="value")
    tracker.save_match("g-low", ALLOWED_LEAGUE, "Osasuna", "Getafe", _start())
    # quota 1.20 < 1.30 (ritorno troppo basso)
    tracker.save_prediction("g-low", "1X2", "Osasuna", 1.20, 0.72, 0.05,
                            market_prob=0.75, market_edge=0.05, status="value")
    tracker.save_match("g-nfav", ALLOWED_LEAGUE, "Osasuna", "Getafe", _start())
    # quota ok ma NON e' il favorito di mercato (prob 0.30 < 0.50)
    tracker.save_prediction("g-nfav", "1X2", "Getafe", 1.70, 0.45, 0.05,
                            market_prob=0.30, market_edge=0.15, status="value")
    mark = len(_RECORDS)
    picks = auto_bet._today_value_picks()
    _print_logs(mark)
    ids = sorted(p["match_id"] for p in picks)
    ok_d = ids == ["g-valid"]
    print(f"  {GREEN if ok_d else RED}→ candidati ammessi: {ids}  "
          f"(atteso solo ['g-valid']){RESET}")
    # Gate a livello motore (stessa soglia, difesa in profondita').
    sane, reason = value_filter.is_sane(0.72, 1.20, 0.05, market_prob=0.80)
    print(f"  {DIM}is_sane(prob .72, quota 1.20): ok={sane} → "
          f"{reason}{RESET}")
    sane2, reason2 = value_filter.is_sane(0.80, 1.31, 0.048, market_prob=0.75)
    print(f"  {DIM}is_sane(prob .80, quota 1.31): ok={sane2} "
          f"(dentro fascia){RESET}")

    # --------------------------------------------------------- E. LIQUIDITA' ---
    _head("E. LIQUIDITA' SX — book sottile: ordine RIFIUTATO (no slippage)")
    import execution_engine as ee
    auto_bet._live_fill = _real_live_fill   # codice VERO, non lo stub di C
    _reset_state()
    # Risoluzione mercato forzata (lo scenario misura il guardrail di
    # liquidita', non il matching evento->mercato gia' coperto dai test).
    ee.resolve_match_market = lambda *a, **k: {"market_id": "m-home",
                                              "selection_id": 1}
    pick = {"match_id": "g-valid", "home": "Osasuna", "away": "Getafe",
            "esito_key": "1", "mercato": "1X2", "commence": _start()}
    stake, floor = 5.0, 1.65
    need = auto_bet.required_depth(stake)
    print(f"  {DIM}stake {stake:.2f} USDC -> richiesti "
          f"max({stake:.2f} x {auto_bet.SX_DEPTH_MULTIPLIER:.1f}, "
          f"{auto_bet.MIN_EXEC_DEPTH_USDC:.1f}) = {need:.2f} USDC al floor "
          f"{floor:.2f}{RESET}")

    def _engine(prov):
        eng = type("_Eng", (), {"provider": prov})()
        ee.ExecutionEngine = lambda *a, **k: eng

    # 1) Book sottile (4 USDC < 25 richiesti): l'ordine NON deve partire.
    thin = _LiqProv(depth=4.0)
    _engine(thin)
    mark = len(_RECORDS)
    res_thin = auto_bet._live_fill(pick, stake=stake, floor=floor)
    _print_logs(mark)
    ok_e1 = (res_thin is None and thin.place_calls == [])
    print(f"  {GREEN if ok_e1 else RED}→ book 4.00 USDC → ordini inviati: "
          f"{len(thin.place_calls)}  (atteso 0){RESET}")
    evts = liquidity_monitor.iter_events(days=1)
    if evts:
        e0 = evts[0]
        print(f"  {DIM}scarto registrato: kind={e0.get('kind')} "
              f"reason={e0.get('reason')} depth={e0.get('depth')} "
              f"richiesto={e0.get('threshold')}{RESET}")
    ok_e2 = bool(evts) and evts[0].get("kind") == "order"

    # 2) Controprova: book profondo (>= soglia) -> l'ordine parte.
    deep = _LiqProv(depth=need + 1.0, order=ee.OrderResult(
        True, "0xe2e", "FULLY_FILLED", floor, floor, stake, stake))
    _engine(deep)
    res_deep = auto_bet._live_fill(pick, stake=stake, floor=floor)
    ok_e3 = (res_deep is not None and res_deep.get("ok") is True
             and len(deep.place_calls) == 1)
    print(f"  {DIM}controprova book {need + 1.0:.2f} USDC → ordini inviati: "
          f"{len(deep.place_calls)} (atteso 1, stake {stake:.2f}){RESET}")
    ok_e = ok_e1 and ok_e2 and ok_e3

    # --------------------------------------------------------------- F. LEGA ---
    _head("F. LEGA (STRATEGY_LEAGUES) — campionati non vincenti mai candidati")
    _reset_state()
    auto_bet._execution_mode = lambda allow_sim=True: "sim"
    # Lega vietata (La Liga: ROI negativo nel backtest) con quota, favorito ed
    # EV perfetti: prima del 15/09 questa riga finiva DRITTA in un ordine
    # (candidati senza lega -> gate cieco). Ora deve fermarsi.
    tracker.save_match("gl-bad", "La Liga", "Osasuna", "Getafe", _start())
    tracker.save_prediction("gl-bad", "1X2", "Osasuna", 1.65, 0.62, 0.08,
                            market_prob=0.58, market_edge=0.07, status="value")
    # Controprova: riga IDENTICA in una lega ammessa -> ammessa.
    tracker.save_match("gl-ok", ALLOWED_LEAGUE, "Osasuna", "Getafe", _start())
    tracker.save_prediction("gl-ok", "1X2", "Osasuna", 1.65, 0.62, 0.08,
                            market_prob=0.58, market_edge=0.07, status="value")
    # Lega assente: senza sapere cosa si gioca non si ordina (fail-closed).
    tracker.save_match("gl-vuota", "", "Osasuna", "Getafe", _start())
    tracker.save_prediction("gl-vuota", "1X2", "Osasuna", 1.65, 0.62, 0.08,
                            market_prob=0.58, market_edge=0.07, status="value")
    mark = len(_RECORDS)
    picks = auto_bet._today_value_picks()
    _print_logs(mark)
    ids = sorted(p["match_id"] for p in picks)
    ok_f = ("gl-bad" not in ids and "gl-vuota" not in ids
            and "gl-ok" in ids)
    print(f"  {GREEN if ok_f else RED}→ lega vietata ammessa: "
          f"{'gl-bad' in ids} (atteso no) · lega assente ammessa: "
          f"{'gl-vuota' in ids} (atteso no) · lega ammessa ammessa: "
          f"{'gl-ok' in ids} (atteso si){RESET}")

    # ------------------------------------------- G. T-60 / CIRCUIT BREAKERS ---
    _head("G. STRATEGIA T-60 — finestra esecutiva + circuit breakers")
    _reset_state()
    # L'alert di emergenza del CB2 farebbe un POST Telegram REALE: qui lo
    # intercettiamo (la diagnostica non deve mandare messaggi a nessuno).
    auto_bet._t60_emergency_alert = lambda reason: print(
        f"  {DIM}│ (alert CB2 intercettato) {reason}{RESET}")
    auto_bet._execution_mode = lambda allow_sim=True: "sim"

    # G1 — FINESTRA T-60: il giro esecutivo ordina SOLO fra T-60 e T-50.
    _seed("t60-nofin", "Osasuna", 1.65, 0.58, 0.07)   # kickoff a +3h
    auto_bet.T60_EXECUTION_ONLY = True
    mark = len(_RECORDS)
    fuori = auto_bet.run_today_bets(stake_eur=1.0)
    _print_logs(mark)
    ok_g1 = fuori == []
    print(f"  {GREEN if ok_g1 else RED}→ kickoff a +3h con T-60 attivo: "
          f"ordini {len(fuori)} (atteso 0: solo scansione){RESET}")
    # Controprova: la stessa riga ordina se la finestra T-60 e' disattivata.
    # (Si verifica la PRESENZA di t60-nofin, non il totale: il ledger della
    # diagnostica contiene anche le righe degli scenari A-F, anch'esse
    # giocabili a orizzonte aperto.)
    auto_bet.T60_EXECUTION_ONLY = False
    dentro = auto_bet.run_today_bets(stake_eur=1.0)
    ok_g2 = any(p.get("match_id") == "t60-nofin" for p in dentro)
    print(f"  {DIM}controprova con T60_EXECUTION_ONLY=0: t60-nofin ordinata: "
          f"{'si' if ok_g2 else 'NO'} ({len(dentro)} ordini totali nel "
          f"palinsesto della diagnostica){RESET}")
    auto_bet.T60_EXECUTION_ONLY = True

    # G2 — CB1 HARD CAP: nessun calcolo dinamico supera il tetto per ordine.
    cap = auto_bet.T60_MAX_STAKE_USDC
    stake_big = auto_bet.t60_stake(10_000.0, mode="live")
    ok_g3 = stake_big <= cap + 1e-9
    print(f"  {GREEN if ok_g3 else RED}→ CB1: bankroll 10000 USDC -> stake "
          f"{stake_big:.2f} (cap {cap:.2f}, Kelly sovrascritto){RESET}")
    _ok, _contract, errs = auto_bet.validate_order_payload({
        "signal_id": "s", "record_id": "r", "match_id": "g-cb1",
        "league": ALLOWED_LEAGUE, "market": "1X2", "outcome": "1",
        "home": "Osasuna", "away": "Getafe", "price": 1.65, "stake": 5.0,
        "verdict": "approve", "mode": "live", "provider": "sxbet",
        "kickoff": _start(), "created_at": datetime.now(timezone.utc).isoformat()})
    ok_g4 = (not _ok) and any("circuit breaker" in e for e in errs)
    print(f"  {GREEN if ok_g4 else RED}→ CB3: payload con stake 5.00 -> scartato: "
          f"{'si' if ok_g4 else 'NO'} ({'; '.join(errs)[:80]}){RESET}")

    # G3 — CB2 KILL SWITCH PATRIMONIALE: equity wallet <= 30 USDC = arresto.
    armed = auto_bet.t60_check_wallet_kill(25.0)
    _seed("t60-kill", "Osasuna", 1.65, 0.58, 0.07)
    mark = len(_RECORDS)
    bloccato = auto_bet.run_today_bets(stake_eur=1.0)
    _print_logs(mark)
    ok_g5 = (armed is True and bloccato == []
             and auto_bet.t60_kill_switch_status().get("triggered") is True)
    print(f"  {GREEN if ok_g5 else RED}→ CB2: equity 25.00 <= soglia "
          f"{auto_bet.T60_KILL_WALLET_USDC:.2f} -> flag armato e "
          f"{len(bloccato)} ordini (atteso 0){RESET}")
    auto_bet.t60_clear_kill()
    ok_g = ok_g1 and ok_g2 and ok_g3 and ok_g4 and ok_g5

    # ------------------------------------------------------------- ESITO ------
    _head("ESITO")
    all_ok = ok_a and ok_b and ok_c and ok_d and ok_e and ok_f and ok_g
    for name, ok in (("A kill-switch OFF", ok_a),
                     ("B stop-loss 24h", ok_b),
                     ("C cap severo 1%", ok_c),
                     ("D limiti quota", ok_d),
                     ("E liquidita' SX", ok_e),
                     ("F lega strategia", ok_f),
                     ("G circuit breakers T-60", ok_g)):
        print(f"  {GREEN + '✅' if ok else RED + '❌'} {name}{RESET}")
    print(f"\n  {BOLD}{GREEN + 'TUTTI I GUARDRAIL BLOCCANO' if all_ok else RED + 'QUALCOSA NON BLOCCA'}{RESET}\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
