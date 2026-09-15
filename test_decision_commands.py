"""Test del Command pattern (`decision/commands.py` + `decision/engine.py`).

Verificano la proprieta' centrale: **il motore emette comandi, non effetti**.
Nessun test tocca DB, rete o provider: se servisse un effetto per far passare un
test, il pattern sarebbe rotto.

Il fail-fast e' provato dove conta: con un blocco attivo il piano NON contiene
uno stake (il record esce con `stake is None`), perche' nessun calcolo a valle
e' stato eseguito.
"""

import subprocess
import sys
from datetime import timedelta

import pytest

from decision import KillSwitchStatus, RiskLimits, build_plan
from decision.commands import (
    Command, CommandKind, notify_command, persist_decision_command,
    place_order_command,
)
from decision.engine import BLOCKED_NOTIFY_SCOPE
from decision.middleware import ListSink, Observability
from decision.models import DataQuality, ReasonCode, Signal, make_signal_id, utcnow

ALLOWED_LEAGUE = "Premier League"


def live_kills(**kwargs):
    base = {"mode": "live", "env_mode": "live", "provider_ready": True}
    base.update(kwargs)
    return KillSwitchStatus(**base)


@pytest.fixture
def limits():
    return RiskLimits.from_env()


def make_signal(*, match_id="sx-1", outcome="1", price=1.60, market_prob=0.58,
                blended_prob=0.65, league=ALLOWED_LEAGUE, tier="strong_value",
                confidence=0.90, coverage=1.0, depth=900.0):
    return Signal(
        match_id=match_id, league=league, outcome=outcome,
        selection_label=f"{match_id} ({outcome})",
        kickoff=utcnow() + timedelta(hours=6), price=price, price_source="test",
        market_prob=market_prob, model_prob=blended_prob + 0.01,
        blended_prob=blended_prob, tier=tier, confidence=confidence,
        data_quality=DataQuality(model_coverage=coverage, calibrated=True,
                                 depth_usdc=depth))


def plan_for(signal=None, *, kills=None, limits=None, mode="live", bankroll=1000.0,
             sink=None, **kwargs):
    obs = Observability(sink=sink or ListSink(), component="test")
    return build_plan(signal or make_signal(), kills=kills or live_kills(),
                      limits=limits or RiskLimits.from_env(), bankroll=bankroll,
                      mode=mode, observability=obs, **kwargs)


# ---------------------------------------------------------------------------
# 1. Emissione dei comandi per verdetto
# ---------------------------------------------------------------------------

class TestComandiPerVerdetto:
    def test_approvato_live_emette_ordine(self, limits):
        plan = plan_for(limits=limits)
        assert plan.record.risk.verdict == "approve"
        assert plan.kinds() == ["persist_decision", "place_order"]
        assert plan.places_order

        order = plan.of_kind(CommandKind.PLACE_ORDER)[0]
        assert order.payload["stake"] == pytest.approx(plan.record.stake.stake)
        assert order.payload["price"] == pytest.approx(1.60)   # bound = quota segnale
        assert order.payload["match_id"] == "sx-1"
        assert order.payload["outcome"] == "1"
        assert order.mode == "live"

    def test_approvato_sim_non_emette_ordine(self, limits):
        """In sim il ledger delle puntate resta di auto_bet: niente PlaceOrder."""
        plan = plan_for(limits=limits, mode="sim")
        assert plan.record.risk.verdict == "approve"
        assert plan.kinds() == ["persist_decision"]
        assert not plan.places_order

    def test_rifiutato_solo_audit(self, limits):
        """Un reject si registra e basta: notificarlo sarebbe rumore."""
        plan = plan_for(make_signal(price=2.35, market_prob=0.42, blended_prob=0.50,
                                    tier="value"), limits=limits)
        assert plan.record.risk.verdict == "reject"
        assert plan.record.risk.reason == ReasonCode.ODDS_TOO_HIGH
        assert plan.kinds() == ["persist_decision"]

    def test_review_chiede_un_umano(self, limits):
        plan = plan_for(make_signal(confidence=0.30), limits=limits)
        assert plan.record.risk.verdict == "review"
        assert plan.record.risk.reason == ReasonCode.CONFIDENCE_LOW
        assert plan.kinds() == ["persist_decision", "notify_operators"]
        note = plan.of_kind(CommandKind.NOTIFY_OPERATORS)[0]
        assert note.payload["kind"] == "review_pending"
        assert "Revisione umana" in note.payload["text"]

    def test_stake_non_eseguibile_non_emette_ordine(self, limits):
        """Liquidita' insufficiente -> nessun ordine, solo l'audit nel ledger."""
        plan = plan_for(make_signal(depth=2.0), limits=limits)
        assert plan.record.risk.verdict == "approve"
        assert plan.record.stake.executable is False
        assert plan.record.stake.reason == ReasonCode.LIQUIDITY_LOW
        assert not plan.places_order


# ---------------------------------------------------------------------------
# 2. Fail fast: nessun comando d'ordine, nessuno stake calcolato
# ---------------------------------------------------------------------------

class TestFailFast:
    def test_kill_switch_off_blocca_prima_di_tutto(self, limits):
        plan = plan_for(kills=KillSwitchStatus(mode="off", env_mode="live"),
                        limits=limits)
        assert plan.blocked is not None
        assert plan.blocked["name"] == "manual"
        assert plan.blocked["reason"] == ReasonCode.KILL_SWITCH_OFF.value
        assert plan.record.risk.reason == ReasonCode.KILL_SWITCH_OFF
        # IL punto del fail-fast: nessuno stake e' stato calcolato.
        assert plan.record.stake is None
        assert not plan.places_order

    def test_stop_loss_blocca_le_puntate(self, limits):
        plan = plan_for(kills=live_kills(daily_stop_active=True,
                                        daily_stop_detail="perdita 6.0%"),
                        limits=limits)
        assert plan.blocked["name"] == "daily_stop"
        assert plan.record.risk.reason == ReasonCode.DAILY_STOP_LOSS
        assert plan.record.stake is None
        assert plan.kinds() == ["persist_decision", "notify_operators"]

    def test_kill_switch_ha_la_precedenza_sullo_stop_loss(self, limits):
        kills = KillSwitchStatus(mode="off", env_mode="live",
                                 daily_stop_active=True,
                                 daily_stop_detail="perdita 7.0%")
        plan = plan_for(kills=kills, limits=limits)
        assert plan.blocked["name"] == "manual"        # precedenza assoluta
        assert plan.record.risk.reason == ReasonCode.KILL_SWITCH_OFF

    def test_notifica_di_blocco_una_volta_al_giorno(self, limits):
        """La chiave anti-spam non dipende dal record (job ogni 60s)."""
        kills = KillSwitchStatus(mode="off", env_mode="live")
        first = plan_for(kills=kills, limits=limits)
        second = plan_for(kills=kills, limits=limits)
        key_a = first.of_kind(CommandKind.NOTIFY_OPERATORS)[0].dedup_key
        key_b = second.of_kind(CommandKind.NOTIFY_OPERATORS)[0].dedup_key
        assert key_a == key_b
        assert key_a == notify_command(first.record, kind="blocked", text="x",
                                      scope=BLOCKED_NOTIFY_SCOPE).dedup_key

    def test_pausa_settlement_non_blocca_la_puntata(self, limits):
        """Semantica confermata il 15/09: ferma il referto, non il rischio."""
        plan = plan_for(kills=live_kills(settlement_paused=True), limits=limits)
        assert plan.blocked is None
        assert plan.record.risk.verdict == "approve"
        assert ReasonCode.SETTLEMENT_PAUSED in plan.record.risk.advisories
        assert plan.places_order

    def test_evento_guards_blocked_emesso(self, limits):
        sink = ListSink()
        plan_for(kills=KillSwitchStatus(mode="off"), limits=limits, sink=sink)
        blocked = sink.of("guards.blocked")
        assert blocked and blocked[0]["block"] == "manual"
        assert blocked[0]["outcome"] == "blocked"

    def test_nessuno_stake_senza_approvazione(self, limits):
        """Invariante del piano: PlaceOrder esiste solo con verdetto approve."""
        for signal in (make_signal(price=2.35, tier="value", market_prob=0.42,
                                   blended_prob=0.50),
                       make_signal(confidence=0.30),
                       make_signal(depth=1.0)):
            plan = plan_for(signal, limits=limits, mode="live")
            if plan.places_order:
                assert plan.record.risk.verdict == "approve"
                assert plan.record.stake.executable


# ---------------------------------------------------------------------------
# 3. Comandi: forma, idempotenza, purezza
# ---------------------------------------------------------------------------

class TestComandi:
    def test_ordine_ha_id_di_dedup_stabile(self, limits):
        """Stesso match/esito -> stessa chiave, ANCHE fra giri diversi.

        Il `record_id` e' idempotente solo entro il secondo (granularita'
        `%Y%m%dT%H%M%S`): se la chiave dell'ordine dipendesse da lui, un job
        che gira ogni 60s riempierebbe il ledger di ordini duplicati. Qui si
        verifica che l'ordine dipenda dalla PARTITA, non dal record.
        """
        plan = plan_for(limits=limits)
        order = plan.of_kind(CommandKind.PLACE_ORDER)[0]
        persist = plan.of_kind(CommandKind.PERSIST_DECISION)[0]
        later = plan.record.model_copy(
            update={"record_id": "record-del-giro-successivo",
                    "created_at": utcnow() + timedelta(minutes=1)})
        assert place_order_command(later).dedup_key == order.dedup_key
        assert persist_decision_command(later).dedup_key != persist.dedup_key
        assert plan_for(limits=limits).of_kind(
            CommandKind.PLACE_ORDER)[0].dedup_key == order.dedup_key

    def test_comandi_nell_ordine_dichiarato(self, limits):
        plan = plan_for(make_signal(confidence=0.30), limits=limits)
        assert [c.order for c in plan.commands] == sorted(c.order for c in plan.commands)
        assert plan.kinds()[0] == "persist_decision"

    def test_payload_json_serializzabile(self, limits):
        plan = plan_for(limits=limits)
        import json
        dumped = json.dumps(plan.as_json())
        assert "persist_decision" in dumped and "place_order" in dumped
        assert plan.as_json()["verdict"] == "approve"

    def test_riga_di_decisione_completa_nel_payload(self, limits):
        plan = plan_for(limits=limits)
        row = plan.of_kind(CommandKind.PERSIST_DECISION)[0].payload["row"]
        for key in ("record_id", "signal_id", "match_id", "market", "outcome",
                    "price", "verdict", "reason", "stake", "selection_label",
                    "model_coverage"):
            assert key in row, key
        assert row["verdict"] == "approve"

    def test_comando_valida_il_payload(self, limits):
        """Un comando malformato non nasce (validazione all'emissione)."""
        from decision.commands import place_order_command
        plan = plan_for(limits=limits)
        with pytest.raises(Exception):
            place_order_command(plan.record, stake=-5.0)

    def test_signal_id_stabile(self):
        assert make_signal_id("sx-1", "1X2", "1") == make_signal_id("sx-1", "1X2", "1")

    def test_command_senza_payload_ha_id(self):
        command = Command(kind=CommandKind.PERSIST_DECISION)
        assert command.command_id and len(command.command_id) == 12


class TestEmitMany:
    def test_piani_con_request_id_condiviso_e_trace_distinte(self, limits):
        """Un giro = un request_id, ma un trace_id per OGNI decisione."""
        from decision.engine import emit_many
        sink = ListSink()
        obs = Observability(sink=sink, component="test")
        signals = [make_signal(match_id="sx-1"), make_signal(match_id="sx-2")]
        plans = emit_many(signals, kills=live_kills(), limits=limits, bankroll=1000.0,
                          observability=obs, request_id="giro-1", mode="live")
        assert len(plans) == 2
        assert [p.record.signal.match_id for p in plans] == ["sx-1", "sx-2"]
        assert all(p.record.risk.verdict == "approve" for p in plans)

        starts = sink.of("span.start")
        assert {e["request_id"] for e in starts} == {"giro-1"}
        assert len({e["trace_id"] for e in starts}) == 2      # una trace a piano

    def test_un_blocco_vale_per_tutti_i_segnali(self, limits):
        from decision.engine import emit_many
        plans = emit_many([make_signal(match_id="sx-1"), make_signal(match_id="sx-2")],
                          kills=KillSwitchStatus(mode="off"), limits=limits,
                          observability=Observability(sink=ListSink()))
        assert all(p.blocked and p.blocked["name"] == "manual" for p in plans)
        assert not any(p.places_order for p in plans)


class TestPurezza:
    def test_build_plan_non_carica_la_produzione(self):
        """Il motore non importa tracker/auto_bet/bot per emettere comandi."""
        code = ("from decision.engine import build_plan;"
                "from decision.models import Signal, DataQuality;"
                "import sys;"
                "print(any(m in sys.modules for m in ('tracker','auto_bet','bot')))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.stdout.strip() == "False", out.stdout + out.stderr

    def test_nessun_accesso_al_db_o_alla_rete(self, limits, monkeypatch):
        """Con la rete e il DB 'avvelenati' la catena funziona comunque."""
        import socket
        import sqlite3

        def boom_socket(*args, **kwargs):
            raise AssertionError("la catena ha toccato la rete")

        monkeypatch.setattr(socket, "create_connection", boom_socket)
        monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("la catena ha toccato il DB")))

        plan = plan_for(limits=limits)
        assert plan.record.risk.verdict == "approve"
        assert plan.places_order
