"""
Test unitari per betfair_client.py
==================================

Copertura:
- dry-run abilitato di default (fail-safe);
- doppia condizione per andare live (BETFAIR_DRY_RUN=0 E BETFAIR_LIVE=1);
- kill-switch: attivazione/disattivazione e blocco ordini;
- regole Exchange Italia: stake minimo 2.00, step 0.50, cap vincite, max 50 istruzioni;
- place_orders in dry-run: logga su data/orders.jsonl e simula SUCCESS senza chiamate API.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import betfair_client as bc
from betfair_client import (
    BetfairClient,
    BetfairError,
    normalize_stake,
    dry_run_enabled,
    kill_switch_active,
    set_kill_switch,
)


# ---------------------------------------------------------------------------
# Fixture / helper
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path):
    """Redirige i log ordini e il kill-switch su una directory temporanea."""
    orders_log = tmp_path / "orders.jsonl"
    kill_switch = tmp_path / "kill_switch"
    with patch.object(bc, "ORDERS_LOG", orders_log), \
         patch.object(bc, "KILL_SWITCH", kill_switch):
        yield {"orders_log": orders_log, "kill_switch": kill_switch}


@pytest.fixture
def client():
    """Client con credenziali minime, dry-run forzato."""
    return BetfairClient(
        app_key="test-app-key",
        username="test-user",
        password="test-pass",
        cert_path="/tmp/fake-cert.pem",
        dry_run=True,
    )


def _back_instruction(size=10.0, price=2.5):
    return {"limitOrder": {"size": size, "price": price, "persistenceType": "LAPSE"}}


# ---------------------------------------------------------------------------
# dry-run / fail-safe
# ---------------------------------------------------------------------------

class TestDryRunDefaults:

    def test_dry_run_default_senza_env(self, monkeypatch):
        for var in ("BETFAIR_DRY_RUN", "BETFAIR_LIVE"):
            monkeypatch.delenv(var, raising=False)
        assert dry_run_enabled() is True

    def test_live_richiede_doppia_condizione(self, monkeypatch):
        # Solo LIVE=1 non basta: DRY_RUN resta 1 → dry-run attivo
        monkeypatch.setenv("BETFAIR_LIVE", "1")
        monkeypatch.delenv("BETFAIR_DRY_RUN", raising=False)
        assert dry_run_enabled() is True

        # DRY_RUN=0 da solo non basta
        monkeypatch.setenv("BETFAIR_DRY_RUN", "0")
        monkeypatch.delenv("BETFAIR_LIVE", raising=False)
        assert dry_run_enabled() is True

        # Entrambe → live
        monkeypatch.setenv("BETFAIR_DRY_RUN", "0")
        monkeypatch.setenv("BETFAIR_LIVE", "1")
        assert dry_run_enabled() is False

    def test_dry_run_non_chiama_api(self, client, _isolated_paths):
        with patch.object(client, "_rpc") as rpc_mock:
            result = client.place_orders("1.234", [_back_instruction()])
            rpc_mock.assert_not_called()
        assert result["status"] == "SUCCESS"
        assert result["bet_id"].startswith("DRYRUN-")

    def test_dry_run_logga_su_orders_jsonl(self, client, _isolated_paths):
        client.place_orders("1.234", [_back_instruction(size=10.0, price=2.5)])
        assert _isolated_paths["orders_log"].exists()
        entry = json.loads(_isolated_paths["orders_log"].read_text().strip())
        assert entry["mode"] == "dry-run"
        assert entry["market_id"] == "1.234"
        assert entry["instructions"][0]["limitOrder"]["size"] == 10.0
        assert "ts" in entry


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------

class TestKillSwitch:

    def test_inizialmente_inattivo(self):
        assert kill_switch_active() is False

    def test_attivazione_e_disattivazione(self):
        assert set_kill_switch(True) is True
        assert kill_switch_active() is True
        assert set_kill_switch(False) is False
        assert kill_switch_active() is False

    def test_blocca_ordini_live(self):
        # Kill-switch hanya dicek di mode live: buat client live (dry_run=False)
        live_client = BetfairClient(
            app_key="test-app-key",
            username="test-user",
            password="test-pass",
            cert_path="/tmp/fake-cert.pem",
            dry_run=False,
        )
        set_kill_switch(True)
        with pytest.raises(BetfairError, match="kill-switch"):
            live_client.place_orders("1.234", [_back_instruction()])

    def test_dry_run_ignora_kill_switch(self, client):
        # In dry-run il kill-switch non viene nemmeno consultato: simula SUCCESS
        set_kill_switch(True)
        result = client.place_orders("1.234", [_back_instruction()])
        assert result["status"] == "SUCCESS"


# ---------------------------------------------------------------------------
# Regole Exchange Italia
# ---------------------------------------------------------------------------

class TestRegoleItalia:

    def test_normalize_stake_minimo(self):
        assert normalize_stake(1.00) == 0.0  # sotto minimo → 0 (non piazzare)
        assert normalize_stake(2.00) == 2.00
        assert normalize_stake(0.75) == 0.0

    def test_normalize_stake_step(self):
        assert normalize_stake(10.30) == 10.50  # arrotondato allo step
        assert normalize_stake(10.10) == 10.00
        assert normalize_stake(7.50) == 7.50

    def test_normalize_stake_zero_e_negativo(self):
        assert normalize_stake(0) == 0.0
        assert normalize_stake(-5) == 0.0

    def test_stake_sotto_minimo_bloccato(self, client):
        with pytest.raises(BetfairError, match="sotto il minimo"):
            client.place_orders("1.234", [_back_instruction(size=1.00, price=2.0)])

    def test_stake_non_multiplo_bloccato(self, client):
        # bypassa normalize: chiama place_orders direttamente con stake non valido
        with pytest.raises(BetfairError, match="multiplo"):
            client.place_orders("1.234", [{"limitOrder": {"size": 10.30, "price": 2.0}}])

    def test_vincita_sopra_cap_bloccata(self, client):
        # 100 * 101 = 10100 > 10000
        with pytest.raises(BetfairError, match="cap"):
            client.place_orders("1.234", [_back_instruction(size=100.0, price=101.0)])

    def test_troppe_istruzioni_bloccate(self, client):
        with pytest.raises(BetfairError, match="istruzioni"):
            client.place_orders("1.234", [_back_instruction() for _ in range(51)])

    def test_istruzioni_vuote_bloccate(self, client):
        with pytest.raises(BetfairError, match="nessuna istruzione"):
            client.place_orders("1.234", [])

    def test_ordine_valido_passa_validazione(self, client, _isolated_paths):
        # 10 * 2.5 = 25 < 10000, stake 10 multiplo di 0.50 e >= 2.00
        result = client.place_orders("1.234", [_back_instruction(size=10.0, price=2.5)])
        assert result["status"] == "SUCCESS"


# ---------------------------------------------------------------------------
# Login / RPC (mockati)
# ---------------------------------------------------------------------------

class TestLoginAndRpc:

    def test_login_success(self, client):
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"loginStatus": "SUCCESS", "sessionToken": "tok-123"}
        with patch("betfair_client.requests.post", return_value=fake_resp) as post_mock:
            token = client.login()
        assert token == "tok-123"
        assert client._session == "tok-123"
        assert post_mock.call_count == 1

    def test_login_fallito(self, client):
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"loginStatus": "FAILED_CLIENT_AUTH"}
        with patch("betfair_client.requests.post", return_value=fake_resp):
            with pytest.raises(BetfairError, match="certlogin fallito"):
                client.login()

    def test_login_credenziali_mancanti(self):
        empty = BetfairClient(app_key="", username="", password="", cert_path="")
        with pytest.raises(BetfairError, match="Credenziali Betfair incomplete"):
            empty.login()

    def test_rpc_retry_dopo_401(self, client):
        client._session = "tok-old"  # pre-autenticato: evita login interno

        fake_401 = MagicMock()
        fake_401.status_code = 401
        fake_401.raise_for_status.return_value = None
        fake_401.json.return_value = [{"result": {"ok": True}}]

        fake_ok = MagicMock()
        fake_ok.status_code = 200
        fake_ok.raise_for_status.return_value = None
        fake_ok.json.return_value = [{"result": {"ok": True}}]

        def fake_login():
            client._session = "tok-new"
            return client._session

        with patch.object(client, "login", side_effect=fake_login) as login_mock, \
             patch("betfair_client.requests.post",
                   side_effect=[fake_401, fake_ok]) as post_mock:
            result = client._rpc("SportsAPING/v1.0/listMarketBook", {"marketIds": ["1.1"]})
        assert result == {"ok": True}
        assert login_mock.call_count == 1
        assert post_mock.call_count == 2
        assert client._session == "tok-new"

    def test_rpc_error_api(self, client):
        client._session = "tok-test"  # pre-autenticato: evita login interno
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.raise_for_status.return_value = None
        fake_resp.json.return_value = [{"error": {"data": "INVALID_MARKET_ID"}}]
        with patch("betfair_client.requests.post", return_value=fake_resp):
            with pytest.raises(BetfairError, match="INVALID_MARKET_ID"):
                client._rpc("SportsAPING/v1.0/listMarketBook", {"marketIds": ["1.1"]})

    def test_get_client_senza_app_key(self, monkeypatch):
        monkeypatch.delenv("BETFAIR_APP_KEY", raising=False)
        assert bc.get_client() is None

    def test_get_client_con_app_key(self, monkeypatch):
        monkeypatch.setenv("BETFAIR_APP_KEY", "key-123")
        c = bc.get_client()
        assert isinstance(c, BetfairClient)
