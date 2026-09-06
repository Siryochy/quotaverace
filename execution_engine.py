"""execution_engine.py — ExecutionEngine: esecuzione ordini via aggregatore professionale.

Dal 06/09 l'esecuzione delle puntate passa dagli aggregatori professionali
(BetInAsia BLACK / MollyBet) al posto del conto Exchange diretto: un'unica
interfaccia Python che parla il protocollo Betfair-compatible JSON-RPC
(SportsAPING/v1.0) — lo stesso esposto da BetInAsia BLACK e MollyBet — con
credenziali dell'aggregatore (app key + username/password) lette SOLO da env.

Obiettivo immediato: MISURARE latenza e slippage reali con stake minimo
(EXECUTION_MIN_STAKE_EUR, default 1€) prima di passare a stake reali.
Ogni probe scrive una riga in data/execution/measurements.jsonl con
latency_ms, prezzo richiesto vs matched, slippage e stato dell'ordine.

Vincoli:
- Nessuna credenziale hardcoded: app key/username/password SOLO da env
  (EXECUTION_APP_KEY / EXECUTION_USERNAME / EXECUTION_PASSWORD), coerentemente
  col vault segreti del progetto. Tripwire test_secret_hygiene.py incluso.
- Senza credenziali (o con EXECUTION_DRY_RUN=1) il provider è DryRun:
  nessuna chiamata di rete, misura simulata (utile per i test e per
  preparare le prime chiamate reali quando arriveranno le credenziali).
- Il modulo NON importa tracker/bot (indipendente, come surebet_engine).

Uso:
    venv/bin/python execution_engine.py --status                 # provider + creds configurati?
    venv/bin/python execution_engine.py --probe --market <id> --selection <id> [--price 2.0]
    venv/bin/python execution_engine.py --probe --dry-run        # probe simulata (no rete)
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from config import DATA_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (env, con default sicuri)
# ---------------------------------------------------------------------------
EXECUTION_DATA_DIR = Path(os.getenv("EXECUTION_DATA_DIR", str(DATA_DIR / "execution")))
MEASUREMENTS_LOG = EXECUTION_DATA_DIR / "measurements.jsonl"

# Provider: "betinasia" | "mollybet" | "" (auto: dry_run senza credenziali)
EXECUTION_PROVIDER = os.getenv("EXECUTION_PROVIDER", "").strip().lower()

# Credenziali aggregatore — SOLO da env, mai hardcoded.
EXECUTION_APP_KEY = os.getenv("EXECUTION_APP_KEY", "")
EXECUTION_USERNAME = os.getenv("EXECUTION_USERNAME", "")
EXECUTION_PASSWORD = os.getenv("EXECUTION_PASSWORD", "")

# Endpoint Betfair-compatible (default: ufficiali Betfair; gli aggregatori
# usano lo stesso protocollo — personalizzabili via env se serve).
EXECUTION_API_BASE = os.getenv(
    "EXECUTION_API_BASE",
    "https://api.betfair.com/exchange/betting/json-rpc/v1")
EXECUTION_LOGIN_URL = os.getenv(
    "EXECUTION_LOGIN_URL",
    "https://identitysso.betfair.com/api/login")

# Stake minimo per il probe di latenza/slippage (default 1€, come richiesto).
# NB: l'exchange può imporre minimi reali più alti; qui si misura, non si
# fa profitto. EXECUTION_MAX_STAKE_EUR è un tetto di sicurezza.
EXECUTION_MIN_STAKE_EUR = float(os.getenv("EXECUTION_MIN_STAKE_EUR", "1.0"))
EXECUTION_MAX_STAKE_EUR = float(os.getenv("EXECUTION_MAX_STAKE_EUR", "10.0"))
EXECUTION_TIMEOUT = float(os.getenv("EXECUTION_TIMEOUT", "10"))

# Forza la modalità DryRun anche con credenziali presenti (per test/sicurezza).
EXECUTION_DRY_RUN = os.getenv("EXECUTION_DRY_RUN", "").lower() in (
    "1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Dataclass risultati
# ---------------------------------------------------------------------------

@dataclass
class OrderResult:
    ok: bool
    bet_id: Optional[str]
    status: str                      # SUCCESS / FAILURE / TIMEOUT / dry-run
    price_requested: float
    price_matched: Optional[float]
    size_matched: float
    latency_ms: float
    error: Optional[str] = None


@dataclass
class ProbeResult:
    provider: str
    timestamp: str
    market_id: str
    selection_id: str
    side: str
    stake: float
    price_best_available: Optional[float]   # prezzo migliore al momento del probe
    price_requested: float
    price_matched: Optional[float]
    slippage: Optional[float]               # matched - richiesto (BACK: negativo = peggio)
    slippage_vs_best: Optional[float]       # matched - best_available
    latency_ms: float
    order_status: str
    ok: bool
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class ExecutionProvider(ABC):
    """Interfaccia comune agli aggregatori (BetInAsia BLACK, MollyBet, ...)."""

    name: str = "base"

    def __init__(self, app_key: str = "", username: str = "",
                 password: str = "") -> None:
        self.app_key = app_key
        self.username = username
        self.password = password
        self._token: Optional[str] = None
        self._token_ts: float = 0.0

    @abstractmethod
    def get_balance(self) -> Dict:
        ...

    @abstractmethod
    def get_market_book(self, market_id: str) -> Dict:
        ...

    @abstractmethod
    def place_limit_order(self, market_id: str, selection_id: int,
                          side: str, price: float, size: float,
                          persistence: str = "LAPSE") -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, market_id: str, bet_id: str) -> bool:
        ...

    @abstractmethod
    def best_back_price(self, market_id: str, selection_id: int) -> Optional[float]:
        """Miglior prezzo BACK disponibile per la selezione (o None)."""


class BetfairJsonRpcProvider(ExecutionProvider):
    """Provider Betfair-compatible JSON-RPC (SportsAPING/v1.0).

    Protocollo usato anche dagli aggregatori BetInAsia BLACK e MollyBet:
    login via identitysso (form username/password + header X-Application)
    e JSON-RPC su /exchange/betting/json-rpc/v1 con X-Authentication.
    """

    name = "betfair-compatible"

    def _login(self) -> str:
        """Login e cache del token di sessione."""
        if self._token and (time.time() - self._token_ts) < 3600:
            return self._token
        if not (self.app_key and self.username and self.password):
            raise RuntimeError("credenziali aggregatore mancanti "
                               "(EXECUTION_APP_KEY/USERNAME/PASSWORD)")
        resp = requests.post(
            EXECUTION_LOGIN_URL,
            data={"username": self.username, "password": self.password},
            headers={"X-Application": self.app_key},
            timeout=EXECUTION_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "SUCCESS":
            raise RuntimeError(f"login aggregatore fallito: {data.get('error') or data}")
        self._token = data["token"]
        self._token_ts = time.time()
        return self._token

    def _rpc(self, method: str, params: Dict) -> tuple:
        """Esegue una chiamata JSON-RPC; ritorna (result, latency_ms)."""
        token = self._login()
        t0 = time.perf_counter()
        resp = requests.post(
            EXECUTION_API_BASE,
            json={"jsonrpc": "2.0", "method": f"SportsAPING/v1.0/{method}",
                  "params": params, "id": "1"},
            headers={"X-Application": self.app_key,
                     "X-Authentication": token,
                     "Content-Type": "application/json"},
            timeout=EXECUTION_TIMEOUT)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"{method} error: {data['error']}")
        return data.get("result"), latency_ms

    def get_balance(self) -> Dict:
        result, _ = self._rpc("getAccountFunds", {})
        return result or {}

    def get_market_book(self, market_id: str) -> Dict:
        result, _ = self._rpc("listMarketBook", {
            "marketIds": [market_id],
            "priceProjection": {"priceData": ["EX_BEST_AVAILABLE"]},
        })
        books = result or []
        return books[0] if books else {}

    def best_back_price(self, market_id: str, selection_id: int) -> Optional[float]:
        book = self.get_market_book(market_id)
        for runner in book.get("runners", []):
            if runner.get("selectionId") != selection_id:
                continue
            ex = runner.get("ex") or {}
            for price, _size in ex.get("availableToBack", []):
                return float(price)
        return None

    def place_limit_order(self, market_id: str, selection_id: int,
                          side: str, price: float, size: float,
                          persistence: str = "LAPSE") -> OrderResult:
        side = side.upper()
        if side not in ("BACK", "LAY"):
            return OrderResult(False, None, "FAILURE", price, None, 0.0,
                               0.0, error=f"side non valido: {side}")
        t0 = time.perf_counter()
        try:
            result, _ = self._rpc("placeOrders", {
                "marketId": market_id,
                "instructions": [{
                    "selectionId": selection_id,
                    "side": side,
                    "orderType": "LIMIT",
                    "limitOrder": {
                        "size": size, "price": price,
                        "persistenceType": persistence,
                    },
                }],
            })
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return OrderResult(False, None, "FAILURE", price, None, 0.0,
                               latency_ms, error=str(e))
        latency_ms = (time.perf_counter() - t0) * 1000.0
        reports = (result or {}).get("instructionReports", [])
        rep = reports[0] if reports else {}
        ok = (result or {}).get("status") == "SUCCESS" and \
            rep.get("status") == "SUCCESS"
        return OrderResult(
            ok=ok,
            bet_id=str(rep.get("betId")) if rep.get("betId") else None,
            status=rep.get("status") or (result or {}).get("status") or "UNKNOWN",
            price_requested=price,
            price_matched=rep.get("averagePriceMatched"),
            size_matched=float(rep.get("sizeMatched") or 0.0),
            latency_ms=latency_ms,
            error=None if ok else str(rep.get("errorCode") or "place failed"),
        )

    def cancel_order(self, market_id: str, bet_id: str) -> bool:
        try:
            result, _ = self._rpc("cancelOrders", {
                "marketId": market_id,
                "instructions": [{"betId": bet_id}],
            })
            reports = (result or {}).get("instructionReports", [])
            return bool(reports) and reports[0].get("status") == "SUCCESS"
        except Exception:
            return False


class BetInAsiaBlackProvider(BetfairJsonRpcProvider):
    """Aggregatore BetInAsia BLACK (protocollo Betfair-compatible)."""

    name = "betinasia"


class MollyBetProvider(BetfairJsonRpcProvider):
    """Aggregatore MollyBet (protocollo Betfair-compatible)."""

    name = "mollybet"


class DryRunProvider(ExecutionProvider):
    """Provider simulato: nessuna rete, latenza e slippage sintetici.

    Usato di default senza credenziali o con EXECUTION_DRY_RUN=1: permette
    di collaudare l'intero flusso del probe (misura, log, cancellazione)
    prima delle prime chiamate reali all'aggregatore.
    """

    name = "dry_run"

    def __init__(self, latency_ms: float = 25.0,
                 slippage: float = -0.01) -> None:
        super().__init__()
        self._latency_ms = latency_ms
        self._slippage = slippage

    def get_balance(self) -> Dict:
        return {"availableBalance": 1000.0, "exposure": 0.0}

    def get_market_book(self, market_id: str) -> Dict:
        return {"marketId": market_id, "status": "OPEN", "runners": []}

    def best_back_price(self, market_id: str, selection_id: int) -> Optional[float]:
        return None  # il prezzo lo passa il chiamante nel probe

    def place_limit_order(self, market_id: str, selection_id: int,
                          side: str, price: float, size: float,
                          persistence: str = "LAPSE") -> OrderResult:
        matched = round(price + self._slippage, 2)
        return OrderResult(True, f"dry-{int(time.time()*1000)}", "dry-run",
                           price, matched, size, self._latency_ms)

    def cancel_order(self, market_id: str, bet_id: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# Factory provider
# ---------------------------------------------------------------------------

def build_provider() -> ExecutionProvider:
    """Seleziona il provider da env: EXECUTION_PROVIDER + credenziali.

    Regole:
    - EXECUTION_DRY_RUN=1 o credenziali mancanti -> DryRunProvider;
    - EXECUTION_PROVIDER=betinasia  -> BetInAsiaBlackProvider;
    - EXECUTION_PROVIDER=mollybet   -> MollyBetProvider;
    - altrimenti -> DryRunProvider (default sicuro).
    """
    if EXECUTION_DRY_RUN or not (EXECUTION_APP_KEY and EXECUTION_USERNAME
                                 and EXECUTION_PASSWORD):
        if not (EXECUTION_APP_KEY and EXECUTION_USERNAME and EXECUTION_PASSWORD):
            logger.warning(
                "execution: credenziali aggregatore mancanti "
                "(EXECUTION_APP_KEY/USERNAME/PASSWORD) -> DryRunProvider")
        return DryRunProvider()
    if EXECUTION_PROVIDER == "betinasia":
        return BetInAsiaBlackProvider(EXECUTION_APP_KEY, EXECUTION_USERNAME,
                                      EXECUTION_PASSWORD)
    if EXECUTION_PROVIDER == "mollybet":
        return MollyBetProvider(EXECUTION_APP_KEY, EXECUTION_USERNAME,
                                EXECUTION_PASSWORD)
    logger.warning("execution: EXECUTION_PROVIDER='%s' non riconosciuto "
                   "(betinasia|mollybet) -> DryRunProvider", EXECUTION_PROVIDER)
    return DryRunProvider()


# ---------------------------------------------------------------------------
# ExecutionEngine
# ---------------------------------------------------------------------------

class ExecutionEngine:
    """Facade per l'esecuzione: probe a stake minimo con misura latenza/slippage."""

    def __init__(self, provider: Optional[ExecutionProvider] = None,
                 min_stake: Optional[float] = None) -> None:
        self.provider = provider or build_provider()
        self.min_stake = min_stake if min_stake is not None else EXECUTION_MIN_STAKE_EUR
        self.min_stake = max(0.0, self.min_stake)
        self.min_stake = min(self.min_stake, EXECUTION_MAX_STAKE_EUR)

    def status(self) -> Dict:
        """Stato del provider + credenziali configurate (senza mai stamparle)."""
        return {
            "provider": self.provider.name,
            "dry_run": isinstance(self.provider, DryRunProvider),
            "creds": bool(EXECUTION_APP_KEY and EXECUTION_USERNAME
                          and EXECUTION_PASSWORD),
            "min_stake_eur": self.min_stake,
            "max_stake_eur": EXECUTION_MAX_STAKE_EUR,
            "measurements_log": str(MEASUREMENTS_LOG),
        }

    # -- probe latenza/slippage ------------------------------------------
    def probe(self, market_id: str, selection_id: int,
              price: Optional[float] = None,
              side: str = "BACK",
              stake: Optional[float] = None,
              cancel_if_unfilled: bool = True) -> ProbeResult:
        """Piazza un ordine LIMIT a stake minimo e misura latenza + slippage.

        Flusso:
        1. best available price (se price non fornito);
        2. place order (stake minimo, prezzo = richiesto);
        3. slippage = matched - requested (e vs best available);
        4. cancella l'eventuale residuo non matched (default);
        5. log della misura in measurements.jsonl.

        Ritorna SEMPRE un ProbeResult (fail-closed: mai eccezioni verso il
        chiamante, l'errore è nel campo `error`).
        """
        ts = datetime.now(timezone.utc).isoformat()
        stake = stake if stake is not None else self.min_stake
        stake = max(0.0, min(stake, EXECUTION_MAX_STAKE_EUR))

        best = None
        try:
            best = self.provider.best_back_price(market_id, selection_id)
        except Exception as e:
            logger.debug("probe: best_back_price fallita (%s), uso price", e)
        price_req = float(price) if price else (best if best else 2.0)

        t0 = time.perf_counter()
        try:
            order = self.provider.place_limit_order(
                market_id, selection_id, side, price_req, stake)
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            res = ProbeResult(
                provider=self.provider.name, timestamp=ts,
                market_id=market_id, selection_id=selection_id, side=side,
                stake=stake, price_best_available=best,
                price_requested=price_req, price_matched=None,
                slippage=None, slippage_vs_best=None, latency_ms=latency_ms,
                order_status="FAILURE", ok=False, error=str(e))
            self._log_measurement(res)
            return res
        wall_ms = (time.perf_counter() - t0) * 1000.0
        # Latenza: quella misurata dal provider (include la chiamata di rete
        # nel provider reale; nel DryRun è il valore simulato). Se il
        # provider non l'ha misurata, si usa il wall-time del probe.
        latency_ms = order.latency_ms if order.latency_ms > 0 else wall_ms

        matched = order.price_matched
        slippage = (matched - price_req) if matched is not None else None
        slip_vs_best = (matched - best) if (matched is not None
                                            and best is not None) else None

        if cancel_if_unfilled and order.bet_id and order.size_matched < stake:
            try:
                self.provider.cancel_order(market_id, order.bet_id)
            except Exception as e:
                logger.warning("probe: cancel fallita (%s)", e)

        res = ProbeResult(
            provider=self.provider.name, timestamp=ts,
            market_id=market_id, selection_id=selection_id, side=side,
            stake=stake, price_best_available=best,
            price_requested=price_req, price_matched=matched,
            slippage=slippage, slippage_vs_best=slip_vs_best,
            latency_ms=latency_ms, order_status=order.status,
            ok=order.ok, error=order.error)
        self._log_measurement(res)
        return res

    def _log_measurement(self, res: ProbeResult) -> None:
        """Appende la misura al log JSONL (data/execution/measurements.jsonl)."""
        try:
            EXECUTION_DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(MEASUREMENTS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(res)) + "\n")
        except Exception as e:
            logger.warning("execution: log misura fallito: %s", e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="ExecutionEngine — esecuzione via aggregatore "
                    "(BetInAsia BLACK / MollyBet)")
    ap.add_argument("--status", action="store_true",
                    help="stato provider + credenziali (senza stamparle)")
    ap.add_argument("--probe", action="store_true",
                    help="probe a stake minimo: misura latenza e slippage")
    ap.add_argument("--market", type=str, default="",
                    help="market_id (es. 1.234567890)")
    ap.add_argument("--selection", type=int, default=0,
                    help="selection_id dell'esito")
    ap.add_argument("--price", type=float, default=None,
                    help="prezzo LIMIT (default: best available)")
    ap.add_argument("--side", type=str, default="BACK", choices=("BACK", "LAY"))
    ap.add_argument("--stake", type=float, default=None,
                    help=f"stake probe (default EXECUTION_MIN_STAKE_EUR="
                         f"{EXECUTION_MIN_STAKE_EUR})")
    ap.add_argument("--dry-run", action="store_true",
                    help="forza DryRunProvider (nessuna chiamata di rete)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.dry_run:
        os.environ["EXECUTION_DRY_RUN"] = "1"
        # ricarica i flag di config letti a import
        global EXECUTION_DRY_RUN
        EXECUTION_DRY_RUN = True

    engine = ExecutionEngine()

    if args.status:
        print(json.dumps(engine.status(), indent=2))
        return 0

    if args.probe:
        if not args.market or not args.selection:
            print("ERRORE: --probe richiede --market <id> --selection <id>")
            return 2
        res = engine.probe(args.market, args.selection,
                           price=args.price, side=args.side, stake=args.stake)
        print(json.dumps(asdict(res), indent=2))
        return 0 if res.ok else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())