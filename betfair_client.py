"""
Betfair Exchange client (Italy jurisdiction)
============================================

Client REST/JSON-RPC per il Betfair Exchange con:
 - Login non-interattivo via certificato SSL (identitysso-cert.betfair.it)
 - Chiamate Betting API JSON-RPC (listMarketCatalogue, listMarketBook, ...)
 - Modalita' DRY-RUN di default: nessun ordine reale viene mai inviato
   finche' BETFAIR_LIVE=1 non e' impostato esplicitamente. Ogni ordine
   (reale o simulato) viene loggato su data/orders.jsonl.

Sicurezza:
 - Fail-closed: ogni errore API ritorna None/{} senza mai piazzare ordini
   "alla cieca".
 - Il dry-run e' il default: BETFAIR_DRY_RUN=1 e' il comportamento se la
   variabile manca. Servono DUE condizioni per andare live:
   BETFAIR_DRY_RUN=0  E  BETFAIR_LIVE=1.
 - Un file kill-switch (data/kill_switch) blocca qualsiasi placeOrders.

Credenziali (env o .env):
  BETFAIR_USERNAME, BETFAIR_PASSWORD, BETFAIR_APP_KEY,
  BETFAIR_CERT_PATH (pem: cert+key uniti), BETFAIR_CERT_KEY_PATH (opz.,
  chiave separata), BETFAIR_DRY_RUN (default "1"), BETFAIR_LIVE (default "0")

Regole Exchange Italia gestite qui:
 - stake back minimo 2.00 EUR, multipli di 0.50
 - max 50 istruzioni per placeOrders
 - vincita massima 10.000 EUR per ordine (cap pre-lancio)
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from config import DATA_DIR

logger = logging.getLogger("betfair_client")

CERT_LOGIN_URL = "https://identitysso-cert.betfair.it/api/certlogin"
KEEP_ALIVE_URL = "https://identitysso-cert.betfair.it/api/keepAlive"
BETTING_RPC_URL = "https://api.betfair.com/exchange/betting/json-rpc/v1"
ACCOUNT_RPC_URL = "https://api.betfair.com/exchange/account/json-rpc/v1"

EVENT_TYPE_SOCCER = "1"
ITALY_MIN_BACK_STAKE = 2.00
ITALY_STAKE_STEP = 0.50
ITALY_MAX_INSTRUCTIONS = 50
ITALY_MAX_WINNINGS = 10000.0

ORDERS_LOG = DATA_DIR / "orders.jsonl"
KILL_SWITCH = DATA_DIR / "kill_switch"

_session_cache: dict[str, Any] = {"token": None, "ts": 0.0}


def _env(name: str, default: str = "") -> str:
    exact = os.getenv(name)
    if exact is not None:
        return exact.strip()
    for k, v in os.environ.items():
        if k.strip() == name:
            return v.strip()
    return default


def dry_run_enabled() -> bool:
    """True a meno che BETFAIR_DRY_RUN=0 E BETFAIR_LIVE=1 entrambi impostati."""
    try:
        dry = _env("BETFAIR_DRY_RUN", "1").lower() not in ("0", "false")
        live = _env("BETFAIR_LIVE", "0").lower() in ("1", "true")
        return not (live and not dry)
    except Exception:
        return True


def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


def set_kill_switch(active: bool) -> bool:
    KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
    if active:
        KILL_SWITCH.write_text(datetime.now().isoformat())
    elif KILL_SWITCH.exists():
        KILL_SWITCH.unlink()
    return kill_switch_active()


def _log_order(entry: dict) -> None:
    ORDERS_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(entry)
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    with ORDERS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def normalize_stake(stake: float) -> float:
    """Arrotonda allo step 0.50 e forza il minimo 2.00 (regole Exchange Italia)."""
    if stake <= 0:
        return 0.0
    stepped = round(stake / ITALY_STAKE_STEP) * ITALY_STAKE_STEP
    stepped = round(stepped, 2)
    if stepped < ITALY_MIN_BACK_STAKE:
        return 0.0  # sotto il minimo: non piazzare affatto
    return stepped


class BetfairError(Exception):
    pass


class BetfairClient:
    """Client Betfair Exchange con dry-run di default."""

    def __init__(self, app_key: str | None = None, username: str | None = None,
                 password: str | None = None, cert_path: str | None = None,
                 cert_key_path: str | None = None, dry_run: bool | None = None):
        self.app_key = app_key or _env("BETFAIR_APP_KEY")
        self.username = username or _env("BETFAIR_USERNAME")
        self.password = password or _env("BETFAIR_PASSWORD")
        self.cert_path = cert_path or _env("BETFAIR_CERT_PATH")
        self.cert_key_path = cert_key_path or _env("BETFAIR_CERT_KEY_PATH")
        self.dry_run = dry_run_enabled() if dry_run is None else bool(dry_run)
        self._session: str | None = None

    # -- session -------------------------------------------------------------
    def _cert_tuple(self):
        if not self.cert_path:
            return None
        if self.cert_key_path:
            return (self.cert_path, self.cert_key_path)
        return self.cert_path  # pem unico: cert+key

    def login(self) -> str:
        """Certlogin non-interattivo. Ritorna il session token."""
        if not (self.app_key and self.username and self.password and self.cert_path):
            raise BetfairError(
                "Credenziali Betfair incomplete: servono BETFAIR_APP_KEY, "
                "BETFAIR_USERNAME, BETFAIR_PASSWORD, BETFAIR_CERT_PATH")
        try:
            r = requests.post(
                CERT_LOGIN_URL,
                headers={"X-Application": self.app_key,
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"username": self.username, "password": self.password},
                cert=self._cert_tuple(),
                timeout=30)
        except Exception as e:
            raise BetfairError(f"certlogin network error: {e}") from e
        try:
            body = r.json()
        except Exception as e:
            raise BetfairError(f"certlogin risposta non-JSON (HTTP {r.status_code})") from e
        if body.get("loginStatus") != "SUCCESS":
            raise BetfairError(f"certlogin fallito: {body.get('loginStatus')}")
        self._session = body.get("sessionToken")
        _session_cache["token"] = self._session
        _session_cache["ts"] = time.time()
        return self._session

    def _token(self) -> str:
        if not self._session:
            self.login()
        return self._session

    def keep_alive(self) -> bool:
        if not self._session:
            return False
        r = requests.post(KEEP_ALIVE_URL,
                          headers={"X-Application": self.app_key,
                                   "X-Authentication": self._session,
                                   "Content-Type": "application/x-www-form-urlencoded"},
                          cert=self._cert_tuple(), timeout=30)
        return r.status_code == 200 and r.json().get("status") == "SUCCESS"

    def logout(self) -> None:
        if self._session:
            try:
                requests.post("https://identitysso-cert.betfair.it/api/logout",
                              headers={"X-Application": self.app_key,
                                       "X-Authentication": self._session,
                                       "Content-Type": "application/x-www-form-urlencoded"},
                              cert=self._cert_tuple(), timeout=30)
            except Exception:
                pass
            self._session = None

    # -- JSON-RPC ------------------------------------------------------------
    def _rpc(self, method: str, params: dict, rpc_url: str = BETTING_RPC_URL) -> Any:
        token = self._token()
        payload = [{"jsonrpc": "2.0", "method": method, "params": params, "id": 1}]
        r = requests.post(
            rpc_url,
            headers={"X-Application": self.app_key,
                     "X-Authentication": token,
                     "Content-Type": "application/json"},
            data=json.dumps(payload), timeout=30)
        if r.status_code in (401, 403):
            # sessione scaduta: un retry dopo re-login
            self.login()
            token = self._session
            r = requests.post(
                rpc_url,
                headers={"X-Application": self.app_key,
                         "X-Authentication": token,
                         "Content-Type": "application/json"},
                data=json.dumps(payload), timeout=30)
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, list) or not body:
            raise BetfairError(f"risposta RPC inattesa per {method}")
        entry = body[0]
        if "error" in entry:
            raise BetfairError(f"{method}: {entry['error'].get('data', entry['error'])}")
        return entry.get("result")

    # -- betting -------------------------------------------------------------
    def list_market_catalogue(self, market_filter: dict, max_results: int = 200,
                              market_projection: list[str] | None = None) -> list[dict]:
        params: dict[str, Any] = {
            "filter": market_filter,
            "maxResults": max(1, min(max_results, 1000)),
            "sort": "FIRST_TO_START",
        }
        if market_projection:
            params["marketProjection"] = market_projection
        return self._rpc("SportsAPING/v1.0/listMarketCatalogue", params) or []

    def list_market_book(self, market_ids: list[str], price_projection: dict | None = None) -> list[dict]:
        params: dict[str, Any] = {"marketIds": market_ids[:200]}
        if price_projection:
            params["priceProjection"] = price_projection
        return self._rpc("SportsAPING/v1.0/listMarketBook", params) or []

    def get_account_funds(self) -> dict | None:
        try:
            return self._rpc("AccountAPING/v1.0/getAccountFunds", {},
                             rpc_url=ACCOUNT_RPC_URL)
        except BetfairError:
            raise
        except Exception as e:
            raise BetfairError(f"getAccountFunds: {e}") from e

    # -- ordini ----------------------------------------------------------------
    def place_orders(self, market_id: str, instructions: list[dict],
                     customer_ref: str | None = None, market_version: int | None = None) -> dict:
        """Piazza ordini. In dry-run NON chiama l'API: logga e simula SUCCESS.

        Valida le regole Exchange Italia PRIMA di qualsiasi chiamata.
        """
        if not instructions:
            raise BetfairError("nessuna istruzione")
        if len(instructions) > ITALY_MAX_INSTRUCTIONS:
            raise BetfairError(
                f"troppo istruzioni ({len(instructions)} > {ITALY_MAX_INSTRUCTIONS})")
        for ins in instructions:
            lo = ins.get("limitOrder", {})
            size = float(lo.get("size", 0))
            price = float(lo.get("price", 0))
            if size < ITALY_MIN_BACK_STAKE:
                raise BetfairError(
                    f"stake {size:.2f} sotto il minimo Italia ({ITALY_MIN_BACK_STAKE:.2f})")
            if abs(size / ITALY_STAKE_STEP - round(size / ITALY_STAKE_STEP)) > 1e-9:
                raise BetfairError(f"stake {size:.2f} non e' multiplo di {ITALY_STAKE_STEP:.2f}")
            if size * price > ITALY_MAX_WINNINGS:
                raise BetfairError(
                    f"vincita potenziale {size * price:.2f} supera il cap {ITALY_MAX_WINNINGS:.0f}")

        entry: dict[str, Any] = {
            "mode": "dry-run" if self.dry_run else "live",
            "market_id": market_id,
            "instructions": instructions,
            "customer_ref": customer_ref,
        }
        if self.dry_run:
            entry["simulated"] = {
                "status": "SUCCESS",
                "bet_id": f"DRYRUN-{int(time.time() * 1000)}",
                "placed_date": datetime.now(timezone.utc).isoformat(),
            }
            _log_order(entry)
            return entry["simulated"]

        if kill_switch_active():
            raise BetfairError("kill-switch attivo (data/kill_switch) — ordine bloccato")
        params: dict[str, Any] = {"marketId": market_id, "instructions": instructions}
        if customer_ref:
            params["customerRef"] = customer_ref
        if market_version:
            params["marketVersion"] = {"version": market_version}
        result = self._rpc("SportsAPING/v1.0/placeOrders", params)
        entry["result"] = result
        _log_order(entry)
        return result


def enabled() -> bool:
    """Switch master Betfair (env BETFAIR_ENABLED, default '1' = attivo).

    Con '0'/'false'/'off'/'no' l'integrazione e' in STAND-BY VOLUTO: i job e
    i comandi Exchange non girano e nessun log segnala chiavi mancanti (non
    e' un errore, e' una scelta). NON tocca la refertazione: risultati e
    saldaggio usano SEMPRE e SOLO the-odds-api; auto_bet prosegue in SIM.
    """
    return os.getenv("BETFAIR_ENABLED", "1").strip().lower() not in (
        "0", "false", "off", "no")


def get_client(dry_run: bool | None = None) -> BetfairClient:
    """Client pronto dalle variabili d'ambiente (o None se non configurato
    o disabilitato via BETFAIR_ENABLED=0 — choke point unico: auto_bet
    degenera in SIM e la scansione in skip)."""
    if not enabled():
        return None
    if not _env("BETFAIR_APP_KEY"):
        return None
    return BetfairClient(dry_run=dry_run)


def check_setup() -> dict:
    """Diagnostica credenziali SENZA mai stampare i valori dei segreti.

    Ritorna {"ok": bool, "vars": {nome: "ok"|"MANCANTE"|"non_trovato"}}.
    """
    required = ["BETFAIR_APP_KEY", "BETFAIR_USERNAME", "BETFAIR_PASSWORD",
                "BETFAIR_CERT_PATH"]
    optional = ["BETFAIR_CERT_KEY_PATH", "BETFAIR_DRY_RUN", "BETFAIR_LIVE"]
    status: dict[str, str] = {}
    for var in required + optional:
        val = _env(var)
        if var in required and not val:
            status[var] = "MANCANTE"
        elif var in ("BETFAIR_CERT_PATH", "BETFAIR_CERT_KEY_PATH") and val:
            status[var] = "ok" if Path(val).exists() else "non_trovato"
        elif val:
            status[var] = "ok"
        else:
            status[var] = "default"
    status["_dry_run_attivo"] = "si" if dry_run_enabled() else "NO (LIVE!)"
    return {
        "ok": all(status[v] == "ok" for v in required),
        "vars": status,
    }


if __name__ == "__main__":
    report = check_setup()
    print("🔍 Diagnostica credenziali Betfair\n" + "─" * 40)
    for var, st in report["vars"].items():
        icon = "✅" if st in ("ok", "default", "si") else "❌"
        print(f"{icon} {var}: {st}")
    print("─" * 40)
    print("✅ Configurazione completa" if report["ok"]
          else "❌ Configura le variabili MANCANTE nel file .env (vedi DEPLOY.md §1bis)")
    print(f"🔒 Modalità: {'DRY-RUN (nessun ordine reale)' if report['vars']['_dry_run_attivo'] == 'si' else '⚠️ LIVE — ordini reali abilitati'}")
