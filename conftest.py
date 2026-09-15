"""Configurazione della suite di test.

Un principio: **i test non scrivono nei file di produzione**. La catena di
decisione ha due sink sul volume (eventi di osservabilita' e registro shadow) e
il job `auto_bet` li usa davvero: senza isolamento, una sessione di test lascia
centinaia di righe in `data/decision/` (osservato il 15/09/2026: 1051 eventi in
pochi minuti). Qui entrambi i percorsi vengono spostati su una directory
temporanea per test.

I test che verificano il sink lo fanno con un oggetto iniettato: un sink
esplicito vince sull'ambiente (`Observability(sink=...)`), quindi questa
configurazione non li disturba.

Stesso principio per il **feed di mercato** (`decision/feeds.py`): lo stato
della validazione finisce in `data/decision/feed_state.json` e il feed primario
chiama l'API pubblica di SX Bet. In test entrambe le cose sono spente: lo stato
va in una directory temporanea e `DECISION_FEED_ENABLED=0` fa valutare la
catena senza il gate di mercato — cosi' nessun test tocca la rete per sbaglio
(i test del feed lo riaccendono esplicitamente, con sorgenti finte).

Terzo sink: lo **store dei callback** delle revisioni
(`decision/review_telegram.py`), che registra i callback risolti e i prompt
inviati. Anche quello va nella tmp: senza isolamento un test scriverebbe sul
volume la memoria dell'idempotenza — e il test successivo la troverebbe.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_decision_io(tmp_path, monkeypatch):
    """Log spenti, registro shadow nella tmp e feed di mercato disattivato."""
    monkeypatch.setenv("DECISION_LOG_SINK", "off")
    monkeypatch.setenv("DECISION_SHADOW_LOG", str(tmp_path / "shadow_commands.jsonl"))
    monkeypatch.setenv("DECISION_FEED_STATE", str(tmp_path / "feed_state.json"))
    monkeypatch.setenv("DECISION_FEED_ENABLED", "0")
    monkeypatch.setenv("DECISION_CALLBACK_STORE", str(tmp_path / "review_callbacks.json"))
    monkeypatch.setenv("DECISION_REVIEW_QUEUE", str(tmp_path / "reviews.json"))
    # Nessun token/ destinatario Telegram in test: la catena non deve poter
    # incidere su un canale reale nemmeno per sbaglio.
    monkeypatch.delenv("QUOTAVERACE_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_CHAT_ID", raising=False)
    yield
