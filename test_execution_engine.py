"""Test di execution_engine.py — esecuzione via aggregatore (BetInAsia BLACK/MollyBet).

Copre: interfaccia provider, protocollo JSON-RPC Betfair-compatible (login,
placeOrders con header corretti), DryRunProvider (default senza credenziali),
probe a stake minimo con misura latenza/slippage, log JSONL delle misure,
factory provider da env e sicurezza (nessuna credenziale hardcoded — il
tripwire test_secret_hygiene.py la verifica a livello repo).
"""

import json
import time

import pytest

import execution_engine as ee


# ---------------------------------------------------------------------------
# Factory provider
# ---------------------------------------------------------------------------

class TestBuildProvider:
    def test_senza_credenziali_dry_run(self, monkeypatch):
        monkeypatch.delenv("EXECUTION_APP_KEY", raising=False)
        monkeypatch.delenv("EXECUTION_USERNAME", raising=False)
        monkeypatch.delenv("EXECUTION_PASSWORD", raising=False)
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_APP_KEY", "")
        monkeypatch.setattr(ee, "EXECUTION_USERNAME", "")
        monkeypatch.setattr(ee, "EXECUTION_PASSWORD", "")
        p = ee.build_provider()
        assert isinstance(p, ee.DryRunProvider)

    def test_dry_run_forzato_con_credenziali(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", True)
        monkeypatch.setattr(ee, "EXECUTION_APP_KEY", "key-test")
        monkeypatch.setattr(ee, "EXECUTION_USERNAME", "user-test")
        monkeypatch.setattr(ee, "EXECUTION_PASSWORD", "pass-test")
        assert isinstance(ee.build_provider(), ee.DryRunProvider)

    def test_provider_betinasia_con_credenziali(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "betinasia")
        monkeypatch.setattr(ee, "EXECUTION_APP_KEY", "key-test")
        monkeypatch.setattr(ee, "EXECUTION_USERNAME", "user-test")
        monkeypatch.setattr(ee, "EXECUTION_PASSWORD", "pass-test")
        p = ee.build_provider()
        assert isinstance(p, ee.BetInAsiaBlackProvider)

    def test_provider_mollybet_con_credenziali(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "mollybet")
        monkeypatch.setattr(ee, "EXECUTION_APP_KEY", "key-test")
        monkeypatch.setattr(ee, "EXECUTION_USERNAME", "user-test")
        monkeypatch.setattr(ee, "EXECUTION_PASSWORD", "pass-test")
        p = ee.build_provider()
        assert isinstance(p, ee.MollyBetProvider)

    def test_provider_sconosciuto_dry_run(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "altro")
        monkeypatch.setattr(ee, "EXECUTION_APP_KEY", "key-test")
        monkeypatch.setattr(ee, "EXECUTION_USERNAME", "user-test")
        monkeypatch.setattr(ee, "EXECUTION_PASSWORD", "pass-test")
        assert isinstance(ee.build_provider(), ee.DryRunProvider)


# ---------------------------------------------------------------------------
# Protocollo JSON-RPC Betfair-compatible
# ---------------------------------------------------------------------------

def _fake_response(payload, status_code=200):
    class R:
        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"http {self.status_code}")

        def json(self):
            return payload
    return R()


class TestJsonRpcProtocol:
    def test_login_invia_credenziali_e_come_headers(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = headers
            return _fake_response({"status": "SUCCESS", "token": "tok-test"})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.BetInAsiaBlackProvider("app-test", "user-test", "pass-test")
        tok = p._login()
        assert tok == "tok-test"
        assert captured["headers"]["X-Application"] == "app-test"
        assert captured["data"]["username"] == "user-test"
        assert captured["data"]["password"] == "pass-test"

    def test_login_fallito_raise(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post",
                            lambda *a, **k: _fake_response(
                                {"status": "FAIL", "error": "INVALID_USERNAME_OR_PASSWORD"}))
        p = ee.BetInAsiaBlackProvider("app-test", "user-test", "pass-test")
        with pytest.raises(RuntimeError, match="login"):
            p._login()

    def test_rpc_place_orders_payload_e_header_auth(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None):
            captured["json"] = json
            captured["headers"] = headers
            return _fake_response({"result": {
                "status": "SUCCESS",
                "instructionReports": [{
                    "status": "SUCCESS", "betId": "123456",
                    "averagePriceMatched": 2.0, "sizeMatched": 1.0}]}})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.BetInAsiaBlackProvider("app-test", "user-test", "pass-test")
        p._token = "tok-test"
        p._token_ts = time.time()
        res = p.place_limit_order("1.234", 98765, "BACK", 2.0, 1.0)
        assert res.ok
        assert res.bet_id == "123456"
        assert res.price_matched == 2.0
        assert res.size_matched == 1.0
        body = captured["json"]
        assert body["method"] == "SportsAPING/v1.0/placeOrders"
        instr = body["params"]["instructions"][0]
        assert instr["selectionId"] == 98765
        assert instr["side"] == "BACK"
        assert instr["limitOrder"]["size"] == 1.0
        assert instr["limitOrder"]["price"] == 2.0
        assert captured["headers"]["X-Authentication"] == "tok-test"

    def test_rpc_errore_api_raise(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post",
                            lambda *a, **k: _fake_response(
                                {"error": {"message": "NO_LIQUIDITY"}}))
        p = ee.BetInAsiaBlackProvider("app-test", "user-test", "pass-test")
        p._token = "tok-test"
        p._token_ts = time.time()
        with pytest.raises(RuntimeError, match="NO_LIQUIDITY"):
            p._rpc("listMarketBook", {})

    def test_side_non_valido_non_chiama_rete(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("non deve chiamare la rete")
        monkeypatch.setattr(ee.requests, "post", boom)
        p = ee.BetInAsiaBlackProvider("app-test", "user-test", "pass-test")
        res = p.place_limit_order("1.234", 98765, "MIDDLE", 2.0, 1.0)
        assert not res.ok
        assert "side non valido" in (res.error or "")


# ---------------------------------------------------------------------------
# DryRunProvider
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_place_order_simulato(self):
        p = ee.DryRunProvider(latency_ms=15.0, slippage=-0.01)
        res = p.place_limit_order("1.234", 1, "BACK", 2.0, 1.0)
        assert res.ok
        assert res.status == "dry-run"
        assert res.price_matched == pytest.approx(1.99)
        assert res.latency_ms == pytest.approx(15.0)
        assert p.cancel_order("1.234", res.bet_id)

    def test_balance_simulato(self):
        assert ee.DryRunProvider().get_balance()["availableBalance"] == 1000.0


# ---------------------------------------------------------------------------
# Probe latenza/slippage
# ---------------------------------------------------------------------------

class TestProbe:
    def test_probe_dry_run_misura_e_logga(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ee, "MEASUREMENTS_LOG", tmp_path / "m.jsonl")
        engine = ee.ExecutionEngine(provider=ee.DryRunProvider(
            latency_ms=20.0, slippage=-0.01))
        res = engine.probe("1.234", 98765, price=2.0, stake=1.0)
        assert res.ok
        assert res.provider == "dry_run"
        assert res.stake == pytest.approx(1.0)
        assert res.price_requested == pytest.approx(2.0)
        assert res.price_matched == pytest.approx(1.99)
        assert res.slippage == pytest.approx(-0.01)
        assert res.latency_ms == pytest.approx(20.0)
        assert res.slippage_vs_best is None  # dry-run: best = None
        # log JSONL scritto
        lines = tmp_path.joinpath("m.jsonl").read_text().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["provider"] == "dry_run"
        assert row["market_id"] == "1.234"
        assert row["latency_ms"] == pytest.approx(20.0)

    def test_probe_stake_minimo_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ee, "MEASUREMENTS_LOG", tmp_path / "m.jsonl")
        engine = ee.ExecutionEngine(provider=ee.DryRunProvider())
        res = engine.probe("1.234", 98765, price=2.5)
        assert res.stake == pytest.approx(engine.min_stake)
        assert engine.min_stake == pytest.approx(ee.EXECUTION_MIN_STAKE_EUR)

    def test_probe_stake_cappato(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ee, "MEASUREMENTS_LOG", tmp_path / "m.jsonl")
        monkeypatch.setattr(ee, "EXECUTION_MAX_STAKE_EUR", 10.0)
        engine = ee.ExecutionEngine(provider=ee.DryRunProvider(),
                                    min_stake=100.0)
        assert engine.min_stake == pytest.approx(10.0)

    def test_probe_con_provider_reale_mockato(self, monkeypatch, tmp_path):
        """Probe su provider reale: prezzo best disponibile + slippage dal fill."""
        monkeypatch.setattr(ee, "MEASUREMENTS_LOG", tmp_path / "m.jsonl")
        calls = []

        class FakeProv(ee.BetInAsiaBlackProvider):
            def best_back_price(self, market_id, selection_id):
                return 2.10

            def place_limit_order(self, market_id, selection_id, side,
                                  price, size, persistence="LAPSE"):
                calls.append((market_id, selection_id, price, size))
                return ee.OrderResult(True, "bet-1", "SUCCESS", price, 2.08,
                                      1.0, 40.0)

            def cancel_order(self, market_id, bet_id):
                return True

        engine = ee.ExecutionEngine(provider=FakeProv("k", "u", "p"))
        res = engine.probe("1.234", 98765, stake=1.0)
        assert res.ok
        assert res.price_best_available == pytest.approx(2.10)
        assert res.price_requested == pytest.approx(2.10)  # usa best
        assert res.price_matched == pytest.approx(2.08)
        assert res.slippage == pytest.approx(-0.02)
        assert res.slippage_vs_best == pytest.approx(-0.02)
        assert len(calls) == 1

    def test_probe_errore_fail_closed(self, monkeypatch, tmp_path):
        """Un'eccezione del provider non propaga: ProbeResult con error."""
        monkeypatch.setattr(ee, "MEASUREMENTS_LOG", tmp_path / "m.jsonl")

        class Boom:
            name = "boom"
            min_stake = 1.0

            def best_back_price(self, *a, **k):
                raise RuntimeError("API giu'")

            def place_limit_order(self, *a, **k):
                raise RuntimeError("API giu'")

            def cancel_order(self, *a, **k):
                return True

        engine = ee.ExecutionEngine(provider=Boom())  # type: ignore[arg-type]
        res = engine.probe("1.234", 98765, price=2.0)
        assert not res.ok
        assert "API giu'" in (res.error or "")
        # anche il fallimento viene loggato
        assert tmp_path.joinpath("m.jsonl").exists()


# ---------------------------------------------------------------------------
# Sicurezza
# ---------------------------------------------------------------------------

class TestSegreti:
    def test_credenziali_lette_solo_da_env(self):
        """Nessun valore credenziale hardcoded nel modulo: solo os.getenv."""
        src = open("execution_engine.py").read()
        # le assegnazioni delle credenziali passano da os.getenv, mai letterali
        assert 'EXECUTION_APP_KEY = os.getenv("EXECUTION_APP_KEY", "")' in src
        assert 'EXECUTION_USERNAME = os.getenv("EXECUTION_USERNAME", "")' in src
        assert 'EXECUTION_PASSWORD = os.getenv("EXECUTION_PASSWORD", "")' in src
        # nessuna stringa che assomigli a un token/secret reale
        import re
        assert not re.search(r'"\d{8,10}:[A-Za-z0-9_-]{30,}"', src)
        assert "ghp_" not in src

    def test_indipendenza_da_tracker_e_bot(self):
        """Il modulo non importa lo stato del bot Value Bet (come surebet_engine)."""
        import ast
        src = open("execution_engine.py").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in ("tracker", "bot", "auto_bet",
                                           "fixture_engine", "ml_ensemble"), \
                    f"execution_engine non deve importare {node.module}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])