"""Test delle revisioni umane su Telegram (`decision/review_telegram.py`).

Tutto OFFLINE: nessuna rete, nessun token, nessun ordine, nessun credito API.
Il client Telegram e' finto e i gateway sono shadow (default del modulo), cosi'
questi test verificano *esattamente* cio' che la catena farebbe in produzione
senza poter toccare nulla.

Coprono le proprieta' che contano davvero:

1. **Idempotenza**: due click sullo stesso bottone -> UNA decisione, UN
   dispatch. Un redelivery di Telegram, un riavvio a meta' lavoro, un doppio
   invio del job: tutto deve finire nello stesso esito.
2. **Chiavi stabili**: il callback e' una funzione pura di (revisione, azione).
3. **Nessuna esecuzione**: senza gateway espliciti si registra soltanto.
4. **Fail-safe**: input ostili e store corrotto non sollevano mai.
5. **La coda non si allaga**: la deduplicazione per `signal_id` tiene una
   revisione per opportunita', anche col job che gira ogni 60s.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from decision import engine, pipeline
from decision.limits import RiskLimits
from decision.models import DataQuality, KillSwitchStatus, ReasonCode, Signal, utcnow
from decision.review_queue import STATUS_APPROVED, STATUS_REJECTED, ReviewQueue
from decision.review_telegram import (
    CALLBACK_PREFIX, CallbackError, CallbackStore, TOKEN_LEN, answer_callback,
    build_prompt, callback_id, callback_token, find_entry, handle_callback,
    is_ours, parse_callback, pending_prompts, send_prompts,
)

KILLS = KillSwitchStatus(mode="live", env_mode="live", provider_ready=True)


# ---------------------------------------------------------------------------
# Aiutanti
# ---------------------------------------------------------------------------

def make_signal(*, label: str = "Mainz (1)", match_id: str = "toa-mainz",
                price: float = 1.72, market_prob: float = 0.56,
                blended_prob: float = 0.62, league: str = "Bundesliga",
                confidence: float = 0.30, coverage: float = 0.8,
                depth: float | None = 900.0) -> Signal:
    return Signal(
        match_id=match_id, league=league, outcome="1", selection_label=label,
        kickoff=utcnow() + timedelta(hours=6), price=price, price_source="test",
        market_prob=market_prob, model_prob=blended_prob + 0.01,
        blended_prob=blended_prob, tier="value", confidence=confidence,
        data_quality=DataQuality(model_coverage=coverage, calibrated=True,
                                 depth_usdc=depth),
    )


def queue_with_review(tmp_path, signal: Signal | None = None) -> tuple[ReviewQueue, dict]:
    """Coda con UNA revisione in attesa (percorso reale: `pipeline.decide`)."""
    queue = ReviewQueue(tmp_path / "reviews.json")
    record = pipeline.decide(signal or make_signal(), kills=KILLS,
                             limits=RiskLimits.from_env(), bankroll=100.0,
                             review_queue=queue)
    assert record.risk.verdict == "review", record.risk.reason.value
    item = queue.pending()[0]
    return queue, item


class FakeTelegram:
    """Client Telegram finto: registra tutto, non chiama nessuno."""

    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple] = []
        self.answers: list[tuple] = []
        self.edits: list[tuple] = []

    def send_message(self, chat_id, text, *, reply_markup=None):
        if self.fail:
            raise RuntimeError("rete giu'")
        self.sent.append((chat_id, text, reply_markup))
        return {"status_code": 200,
                "body": json.dumps({"result": {"message_id": 42}})}

    def answer_callback(self, callback_query_id, text, *, show_alert=False):
        self.answers.append((callback_query_id, text, show_alert))
        return True

    def edit_message(self, chat_id, message_id, text, *, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))
        return True


def shadow_log_text(path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------
# 1. Chiavi dei callback
# ---------------------------------------------------------------------------

class TestChiaviCallback:
    def test_stabile_e_funzione_pura(self):
        first = callback_id("rec-1", "approve")
        assert first == callback_id("rec-1", "approve")
        assert first.startswith(f"{CALLBACK_PREFIX}:a:")
        assert first == f"{CALLBACK_PREFIX}:a:{callback_token('rec-1')}"

    def test_approve_e_rifiuta_sono_diversi(self):
        assert callback_id("rec-1", "approve") != callback_id("rec-1", "reject")
        assert callback_id("rec-1", "reject").startswith(f"{CALLBACK_PREFIX}:r:")

    def test_dentro_il_limite_di_telegram(self):
        # callback_data: massimo 64 byte. Il nostro e' ~14.
        assert len(callback_id("x" * 200, "approve")) < 64

    def test_parse_round_trip(self):
        key = callback_id("rec-1", "reject")
        parsed = parse_callback(key)
        assert parsed.action == "reject"
        assert parsed.token == callback_token("rec-1")
        assert parsed.callback_id == key
        assert not parsed.approve

    @pytest.mark.parametrize("data", [
        "", None, 123, "rv", "rv:a", "rv:a:short", "rv:a:Z" * 1,
        "rv:x:0123456789", "xx:a:0123456789", "rv:a:0123456789:extra",
        "rv:a:012345678g", "rv:a:" + "0" * (TOKEN_LEN + 1),
    ])
    def test_payload_non_conforme_solleva(self, data):
        with pytest.raises(CallbackError):
            parse_callback(data)

    def test_is_ours(self):
        assert is_ours("rv:a:0123456789")
        assert not is_ours("other:1")
        assert not is_ours(None)

    def test_find_entry_per_token(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        assert find_entry(queue, callback_token(item["record_id"])) is not None
        assert find_entry(queue, "0" * TOKEN_LEN) is None


# ---------------------------------------------------------------------------
# 2. Store idempotente
# ---------------------------------------------------------------------------

class TestCallbackStore:
    def test_claim_una_volta_sola(self, tmp_path):
        store = CallbackStore(tmp_path / "cb.json")
        key = callback_id("rec-1", "approve")
        assert store.claim(key, record_id="rec-1", action="approve") is None
        again = store.claim(key, record_id="rec-1", action="approve")
        assert again is not None and again["status"] == "claimed"
        assert store.resolved(key)["record_id"] == "rec-1"

    def test_complete_arricchisce_la_voce(self, tmp_path):
        store = CallbackStore(tmp_path / "cb.json")
        key = callback_id("rec-1", "approve")
        store.claim(key, record_id="rec-1", action="approve")
        assert store.complete(key, status="approved", stake=1.0, plan_id="p1")
        entry = store.resolved(key)
        assert entry["status"] == "approved" and entry["stake"] == 1.0
        assert entry["completed_at"] and entry["claimed_at"]

    def test_file_corrotto_trattato_come_vuoto_e_non_sovrascritto(self, tmp_path):
        path = tmp_path / "cb.json"
        path.write_text("{non json", encoding="utf-8")
        store = CallbackStore(path)
        assert store.load() == {"resolved": {}, "prompts": {}}
        assert store.resolved("rv:a:0123456789") is None
        assert path.read_text(encoding="utf-8") == "{non json"   # non distrutto

    def test_scrittura_impossibile_non_solleva(self, tmp_path):
        # Il path e' una DIRECTORY: mkstemp/replace falliscono.
        target = tmp_path / "dir.json"
        target.mkdir()
        store = CallbackStore(target)
        assert store.save({"a": 1}) is False
        assert store.claim("rv:a:0123456789", record_id="r", action="approve") is None

    def test_prompt_marker(self, tmp_path):
        store = CallbackStore(tmp_path / "cb.json")
        assert store.prompted("rec-1") is None
        store.mark_prompted("rec-1", message_id=7, chat_id="1")
        assert store.prompted("rec-1")["message_id"] == 7
        assert store.forget_prompt("rec-1") is True
        assert store.prompted("rec-1") is None

    def test_release_solo_per_errori(self, tmp_path):
        store = CallbackStore(tmp_path / "cb.json")
        key = callback_id("rec-1", "approve")
        store.claim(key, record_id="rec-1", action="approve")
        store.complete(key, status="approved")
        assert store.release(key) is False              # una decisione NON si sblocca
        key2 = callback_id("rec-2", "approve")
        store.claim(key2, record_id="rec-2", action="approve")
        store.complete(key2, status="error")
        assert store.release(key2) is True
        assert store.resolved(key2) is None

    def test_summary(self, tmp_path):
        store = CallbackStore(tmp_path / "cb.json")
        key = callback_id("rec-1", "approve")
        store.claim(key, record_id="rec-1", action="approve")
        store.complete(key, status="approved")
        store.mark_prompted("rec-1")
        summary = store.summary()
        assert summary["resolved"] == 1 and summary["prompts"] == 1
        assert summary["by_status"] == {"approved": 1}


# ---------------------------------------------------------------------------
# 3. Prompt
# ---------------------------------------------------------------------------

class TestPrompt:
    def test_prompt_contiene_i_dati_e_i_bottoni(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        prompt = build_prompt(item)
        assert "REVISIONE UMANA" in prompt["text"]
        assert "Mainz (1)" in prompt["text"] and "Bundesliga" in prompt["text"]
        buttons = prompt["reply_markup"]["inline_keyboard"][0]
        assert len(buttons) == 2
        assert buttons[0]["callback_data"] == callback_id(item["record_id"], "approve")
        assert buttons[1]["callback_data"] == callback_id(item["record_id"], "reject")
        assert prompt["callback_ids"]["approve"].startswith("rv:a:")

    def test_nessun_token_o_credenziale_nel_testo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "123456:FAKE")
        queue, item = queue_with_review(tmp_path)
        prompt = build_prompt(item)
        assert "123456:FAKE" not in prompt["text"]
        assert "bot" not in prompt["text"].lower() or "bottoni" in prompt["text"]

    def test_pending_esclude_i_prompt_gia_inviati(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        assert len(pending_prompts(queue, store)) == 1
        store.mark_prompted(item["record_id"])
        assert pending_prompts(queue, store) == []
        assert len(pending_prompts(queue, store, include_prompted=True)) == 1

    def test_send_marca_il_prompt_e_non_ripete(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        client = FakeTelegram()
        first = send_prompts(queue=queue, store=store, client=client, targets=["123"])
        assert first["sent"] == 1 and len(client.sent) == 1
        assert store.prompted(item["record_id"]) is not None
        second = send_prompts(queue=queue, store=store, client=client, targets=["123"])
        assert second["sent"] == 0 and len(client.sent) == 1

    def test_senza_destinatari_non_chiama_nessuno(self, tmp_path):
        queue, _ = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        client = FakeTelegram()
        out = send_prompts(queue=queue, store=store, client=client, targets=[])
        assert out["sent"] == 0 and client.sent == []
        assert "ADMIN_CHAT_ID" in (out["errors"] or [""])[0] or out["skipped"] == 0

    def test_invio_fallito_non_marca_il_prompt(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        client = FakeTelegram(fail=True)
        out = send_prompts(queue=queue, store=store, client=client, targets=["123"])
        assert out["sent"] == 0 and out["errors"]
        assert store.prompted(item["record_id"]) is None   # si ritenta al giro dopo

    def test_messaggio_id_e_limite(self, tmp_path):
        queue = ReviewQueue(tmp_path / "reviews.json")
        for index in range(3):
            pipeline.decide(make_signal(match_id=f"toa-{index}", label=f"T{index}"),
                            kills=KILLS, limits=RiskLimits.from_env(), bankroll=100.0,
                            review_queue=queue)
        store = CallbackStore(tmp_path / "cb.json")
        client = FakeTelegram()
        out = send_prompts(queue=queue, store=store, client=client,
                           targets=["1", "2"], limit=1)
        assert out["sent"] == 1 and len(client.sent) == 2      # 2 destinatari


# ---------------------------------------------------------------------------
# 4. handle_callback: decisione, idempotenza, nessuna esecuzione
# ---------------------------------------------------------------------------

class TestHandleCallback:
    def test_unknown_token_non_decide_nulla(self, tmp_path):
        queue = ReviewQueue(tmp_path / "reviews.json")
        store = CallbackStore(tmp_path / "cb.json")
        out = handle_callback(f"rv:a:{'0' * TOKEN_LEN}", queue=queue, store=store,
                              mode="live")
        assert out.status == "unknown" and not out.duplicate
        assert out.commands == [] and queue.load() == []

    def test_malformed_non_solleva(self, tmp_path):
        out = handle_callback("spazzatura", queue=ReviewQueue(tmp_path / "r.json"),
                              store=CallbackStore(tmp_path / "c.json"))
        assert out.status == "error"
        assert "non conforme" in out.detail

    @pytest.mark.parametrize("data", [None, 5, {"a": 1}, [], "rv:" * 400])
    def test_input_ostile_non_solleva(self, tmp_path, data):
        out = handle_callback(data, queue=ReviewQueue(tmp_path / "r.json"),
                              store=CallbackStore(tmp_path / "c.json"))
        assert out.status == "error"

    def test_approvazione_decide_e_non_esegue(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        log = tmp_path / "shadow.jsonl"
        out = handle_callback(callback_id(item["record_id"], "approve"), queue=queue,
                              store=store, bankroll=100.0, mode="live",
                              reviewer="siryo", capture=True)
        assert out.status == "approved" and out.decided
        assert out.verdict == "approve" and out.reason == ReasonCode.REVIEW_APPROVED.value
        assert out.stake and out.stake > 0
        assert out.commands == ["persist_decision", "place_order"]
        assert out.would_order is True
        # NESSUN esecutore reale: il default e' shadow, quindi niente ordini.
        assert all(not r.ok or r.dry_run for r in out.results)
        assert queue.get(item["record_id"])["status"] == STATUS_APPROVED
        assert queue.get(item["record_id"])["reviewer"] == "siryo"

    def test_rifiuto_chiude_senza_ordine(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        out = handle_callback(callback_id(item["record_id"], "reject"), queue=queue,
                              store=store, bankroll=100.0, mode="live",
                              reviewer="siryo")
        assert out.status == "rejected"
        assert out.reason == ReasonCode.REVIEW_REJECTED.value
        assert out.commands == ["persist_decision"] and not out.would_order
        assert queue.get(item["record_id"])["status"] == STATUS_REJECTED

    def test_secondo_click_e_duplicato(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        key = callback_id(item["record_id"], "approve")
        first = handle_callback(key, queue=queue, store=store, bankroll=100.0,
                               mode="live", reviewer="a")
        second = handle_callback(key, queue=queue, store=store, bankroll=100.0,
                                mode="live", reviewer="b")
        assert first.status == "approved" and not first.duplicate
        assert second.duplicate and second.status == "approved"
        assert second.plan_id == first.plan_id
        assert second.executed == first.executed
        assert store.summary()["resolved"] == 1

    def test_click_inverso_dopo_l_approvazione_non_cambia_nulla(self, tmp_path):
        """Il bottone opposto non annulla una decisione gia' presa."""
        queue, item = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        handle_callback(callback_id(item["record_id"], "approve"), queue=queue,
                        store=store, bankroll=100.0, mode="live", reviewer="a")
        out = handle_callback(callback_id(item["record_id"], "reject"), queue=queue,
                              store=store, bankroll=100.0, mode="live", reviewer="b")
        assert out.status == "rejected"            # decisione registrata...
        assert queue.get(item["record_id"])["status"] == STATUS_APPROVED  # ...la prima vince

    def test_revisione_scaduta_al_kickoff(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        out = handle_callback(callback_id(item["record_id"], "approve"), queue=queue,
                              store=store, bankroll=100.0, mode="live",
                              reviewer="a", now=utcnow() + timedelta(days=1))
        assert out.status == "expired" and out.commands == []
        assert out.answer_text() == "Scaduta: partita già iniziata"
        assert store.resolved(callback_id(item["record_id"], "approve"))["status"] == "expired"

    def test_sim_non_emette_ordine(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        out = handle_callback(callback_id(item["record_id"], "approve"),
                              queue=queue, store=CallbackStore(tmp_path / "cb.json"),
                              bankroll=100.0, mode="sim", reviewer="a")
        assert out.status == "approved"
        assert out.commands == ["persist_decision"] and not out.would_order

    def test_gateway_esplicito_viene_usato(self, tmp_path):
        """L'esecuzione reale e' possibile SOLO con gateway passati a mano."""
        from decision.gateways import BaseGateway, CommandResult
        from decision.commands import CommandKind

        class Recorder(BaseGateway):
            name = "recorder"
            kinds = (CommandKind.PERSIST_DECISION, CommandKind.PLACE_ORDER)

            def __init__(self):
                self.seen: list[str] = []

            def _run(self, command, *, ctx, obs):
                self.seen.append(command.kind.value)
                return CommandResult(kind=command.kind, ok=True, status="executed")

        queue, item = queue_with_review(tmp_path)
        recorder = Recorder()
        out = handle_callback(callback_id(item["record_id"], "approve"), queue=queue,
                              store=CallbackStore(tmp_path / "cb.json"), bankroll=100.0,
                              mode="live", reviewer="a", gateways=[recorder])
        assert recorder.seen == ["persist_decision", "place_order"]
        assert out.executed == 2
        assert out.status == "approved"

    def test_reviewer_e_nota_vanno_sulla_coda(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        handle_callback(callback_id(item["record_id"], "reject"),
                        queue=queue, store=CallbackStore(tmp_path / "cb.json"),
                        bankroll=100.0, reviewer="siryo", note="modello cieco")
        entry = queue.get(item["record_id"])
        assert entry["reviewer"] == "siryo" and entry["note"] == "modello cieco"

    def test_errore_imprevisto_diventa_un_esito(self, tmp_path, monkeypatch):
        queue, item = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        monkeypatch.setattr(pipeline, "resolve_review",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        out = handle_callback(callback_id(item["record_id"], "approve"), queue=queue,
                              store=store, bankroll=100.0, mode="live")
        assert out.status == "error" and "boom" in out.detail
        # il claim resta: nessun re-dispatch silenzioso
        assert store.resolved(callback_id(item["record_id"], "approve"))["status"] == "error"


# ---------------------------------------------------------------------------
# 5. answer_callback: risposta Telegram + modifica del messaggio
# ---------------------------------------------------------------------------

def query_payload(item: dict, action: str = "approve") -> dict:
    return {
        "id": "cbq-1",
        "data": callback_id(item["record_id"], action),
        "from": {"id": 7718157436, "username": "siryo", "first_name": "Giuseppe"},
        "message": {"message_id": 55, "text": "🕓 REVISIONE UMANA\n  partita : Mainz (1)",
                    "chat": {"id": 7718157436}},
    }


class TestAnswerCallback:
    def test_risponde_e_toglie_i_bottoni(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        client = FakeTelegram()
        out = answer_callback(query_payload(item), queue=queue,
                             store=CallbackStore(tmp_path / "cb.json"),
                             client=client, bankroll=100.0, mode="live")
        assert out.status == "approved"
        assert client.answers and client.answers[0][0] == "cbq-1"
        assert "Approva" in client.answers[0][1]
        assert client.edits and "APPROVATA" in client.edits[0][2]
        assert client.edits[0][3] == {"inline_keyboard": []}
        assert "siryo" in client.edits[0][2]

    def test_duplicato_risponde_senza_ridecidere(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        cli1, cli2 = FakeTelegram(), FakeTelegram()
        answer_callback(query_payload(item), queue=queue, store=store, client=cli1,
                        bankroll=100.0, mode="live")
        out = answer_callback(query_payload(item), queue=queue, store=store, client=cli2,
                              bankroll=100.0, mode="live")
        assert out.duplicate
        assert cli2.answers[0][1] == "Già approvata"

    def test_errore_di_editing_non_solleva(self, tmp_path):
        queue, item = queue_with_review(tmp_path)

        class Broken(FakeTelegram):
            def edit_message(self, *a, **k):
                raise RuntimeError("message can't be edited")

        client = Broken()
        out = answer_callback(query_payload(item), queue=queue,
                              store=CallbackStore(tmp_path / "cb.json"), client=client,
                              bankroll=100.0, mode="live")
        assert out.status == "approved" and client.answers

    def test_query_vuota_non_solleva(self, tmp_path):
        out = answer_callback({}, queue=ReviewQueue(tmp_path / "r.json"),
                              store=CallbackStore(tmp_path / "c.json"),
                              client=FakeTelegram())
        assert out.status == "error"


# ---------------------------------------------------------------------------
# 6. End-to-end: motore -> coda -> prompt -> callback -> shadow ledger
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_catena_completa_su_shadow(self, tmp_path):
        """Il verdetto REVIEW della catena finisce su Telegram e si approva."""
        queue = ReviewQueue(tmp_path / "reviews.json")
        store = CallbackStore(tmp_path / "cb.json")
        shadow = tmp_path / "shadow.jsonl"
        signal = make_signal()

        plan = engine.build_plan(signal, kills=KILLS, limits=RiskLimits.from_env(),
                                 bankroll=100.0, mode="live", review_queue=queue,
                                 feed_required=False)
        assert plan.record.risk.verdict == "review"
        assert "notify_operators" in plan.kinds()      # il motore chiede il prompt
        assert plan.places_order is False              # nessuno stake senza l'umano

        item = queue.pending()[0]
        prompt = build_prompt(item)
        key = prompt["callback_ids"]["approve"]
        out = handle_callback(key, queue=queue, store=store, bankroll=100.0,
                              mode="live", reviewer="siryo",
                              gateways=None)
        assert out.status == "approved" and out.would_order
        # Il comando d'ordine e' REGISTRATO, non eseguito: nessun ordine reale.
        assert out.executed >= 1
        assert queue.pending() == []
        assert store.resolved(key)["status"] == "approved"

    def test_segnali_senza_review_non_entrano_in_coda(self, tmp_path):
        queue = ReviewQueue(tmp_path / "reviews.json")
        plan = engine.build_plan(make_signal(confidence=0.95, price=1.55,
                                            market_prob=0.60, blended_prob=0.68,
                                            league="Premier League"),
                                 kills=KILLS, limits=RiskLimits.from_env(),
                                 bankroll=1000.0, mode="live", review_queue=queue,
                                 feed_required=False)
        assert plan.record.risk.verdict == "approve"
        assert queue.load() == []

    def test_un_click_una_sola_voce_nel_registro_shadow(self, tmp_path):
        """Il doppio click non scrive due volte lo stesso comando."""
        from decision.gateways import ShadowGateway
        queue, item = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        log = tmp_path / "shadow.jsonl"
        gateways = [ShadowGateway(log)]
        key = callback_id(item["record_id"], "approve")
        for _ in range(3):
            handle_callback(key, queue=queue, store=store, bankroll=100.0,
                            mode="live", reviewer="a", gateways=gateways)
        lines = [json.loads(line) for line in shadow_log_text(log).splitlines() if line]
        orders = [entry for entry in lines
                  if entry["command"]["kind"] == "place_order"]
        assert len(orders) == 1


# ---------------------------------------------------------------------------
# 7. La coda non si allaga (dedup per segnale)
# ---------------------------------------------------------------------------

class TestCodaPerSegnale:
    def test_stesso_segnale_una_sola_revisione(self, tmp_path):
        """Il job gira ogni 60s: il record_id cambia, la revisione no."""
        from decision.models import DecisionRecord
        queue = ReviewQueue(tmp_path / "reviews.json")
        signal = make_signal()
        first = pipeline.decide(signal, kills=KILLS, limits=RiskLimits.from_env(),
                                bankroll=100.0, review_queue=queue)
        # Stesso segnale, un minuto dopo: il record_id porta i secondi, quindi
        # cambia a ogni giro (e' proprio il caso che allagava la coda).
        later = DecisionRecord(signal=signal, risk=first.risk, mode="sim",
                               created_at=first.created_at + timedelta(minutes=1))
        assert later.record_id != first.record_id
        returned = queue.add(later)
        assert len(queue.load()) == 1                    # opportunita' una sola
        assert returned["record_id"] == first.record_id  # vince la prima

    def test_dopo_una_decisione_il_segnale_non_torna(self, tmp_path):
        queue, item = queue_with_review(tmp_path)
        store = CallbackStore(tmp_path / "cb.json")
        handle_callback(callback_id(item["record_id"], "reject"), queue=queue,
                        store=store, bankroll=100.0, reviewer="a")
        pipeline.decide(make_signal(), kills=KILLS, limits=RiskLimits.from_env(),
                        bankroll=100.0, review_queue=queue)
        assert queue.pending() == []                      # l'umano ha detto no
        assert len(queue.load()) == 1
        assert queue.load()[0]["status"] == STATUS_REJECTED


# ---------------------------------------------------------------------------
# 8. Igiene: il nuovo modulo resta leggero e senza credenziali
# ---------------------------------------------------------------------------

class TestIgiene:
    def test_niente_produzione_a_livello_di_modulo(self):
        import subprocess
        import sys
        code = ("import sys; import decision.review_telegram; "
                "print([m for m in ('tracker','bot','auto_bet') if m in sys.modules])")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, cwd=str(__import__("pathlib").Path.cwd()))
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "[]", result.stdout

    def test_cli_review_offline(self, tmp_path):
        """La CLI ispeziona la coda senza toccare la rete."""
        from decision.__main__ import main
        assert main(["review", "--json"]) == 0


class TestWiringBot:
    """Tripwire sul wiring del bot (source-level: `main()` avvia il polling)."""

    @staticmethod
    def _source() -> str:
        from pathlib import Path
        return Path("bot.py").read_text(encoding="utf-8")

    def test_handler_registrato_col_nostro_pattern(self):
        src = self._source()
        assert "CallbackQueryHandler(review_callback_handler" in src
        assert 'pattern=r"^rv:"' in src

    def test_comando_e_job_registrati(self):
        src = self._source()
        assert 'CommandHandler("revisioni", cmd_revisioni)' in src
        assert "decision_review_job" in src
        assert "run_repeating(decision_review_job" in src

    def test_il_callback_risponde_sempre_e_non_esegue(self):
        """Il percorso del bot usa `answer_callback` (risposta obbligatoria) e
        non passa gateway: nessuna esecuzione reale dal percorso Telegram."""
        src = self._source()
        assert "answer_callback(payload" in src
        assert "gateways=" not in src.split("def _review_callback_pass")[1].split("async def")[0]

    def test_review_callback_pass_torna_un_esito(self, tmp_path):
        """La funzione bloccante del bot non solleva (esito sconosciuto)."""
        import bot
        payload = {"id": "x", "data": f"rv:a:{'0' * TOKEN_LEN}",
                   "from": {"username": "test"}, "message": {"chat": {"id": 1}}}
        out = bot._review_callback_pass(payload)
        assert out["status"] == "unknown"
        assert out["duplicate"] is False
