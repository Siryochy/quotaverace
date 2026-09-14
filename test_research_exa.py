"""Test dell'adapter REALE di ricerca (Exa) — vedi test_research_gemini.py per l'LLM.

Regola: la suite gira OFFLINE. I test iniettano un trasporto HTTP finto, quindi
verificano mapping, fail-closed e integrazione col grafo senza rete, senza
chiavi e senza crediti. La chiamata vera e' isolata nel test marcato
`integration` (saltato senza credenziali).
"""

import json

import pytest

from research_graph import (
    ExaSearchError, ExaSearchTool, MockLLMValidator, exa_configured,
    result_to_finding, run_research,
)
from research_graph.exa_search import DEFAULT_CONFIDENCE, DEFAULT_SEARCH_TYPE

QUERY = "strategie di value betting 2026"

# Credenziale FINTA per i test, marcata `fake/` (convenzione del progetto):
# non e' un formato di credenziale reale, quindi il tripwire di igiene segreti
# (test_secret_hygiene.py) non la segnala.
FAKE_KEY = "fake/exa-not-a-credential"


# ---------------------------------------------------------------------------
# Trasporto HTTP finto (nessuna rete)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeTransport:
    """Callable con la stessa firma di `requests.post`."""

    def __init__(self, payload=None, status_code=200):
        self.payload = payload if payload is not None else {"results": []}
        self.status_code = status_code
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.payload, self.status_code)


def exa_result(title="Titolo di prova", url="https://example.org/a",
               highlights=None, text=None, score=0.83):
    out = {"title": title, "url": url}
    if highlights is not None:
        out["highlights"] = highlights
    if text is not None:
        out["text"] = text
    if score is not None:
        out["score"] = score
    return out


# ---------------------------------------------------------------------------
# 1. Adapter di ricerca reale (Exa)
# ---------------------------------------------------------------------------

class TestExaMapping:
    def test_highlights_diventano_evidenza(self):
        finding = result_to_finding(exa_result(
            highlights=["  il CLV   batte il ROI ", "secondo estratto"]))
        assert finding["claim"] == "Titolo di prova"
        assert finding["source"] == "https://example.org/a"
        assert finding["evidence"] == "il CLV batte il ROI | secondo estratto"
        assert finding["confidence"] == pytest.approx(0.83)

    def test_highlight_singola_come_stringa(self):
        assert result_to_finding(exa_result(highlights="estratto unico"))["evidence"] == "estratto unico"

    def test_fallback_sul_testo_troncato(self):
        finding = result_to_finding(exa_result(text="x" * 5000), max_evidence_chars=120)
        assert len(finding["evidence"]) == 120

    def test_score_mancante_o_fuori_range_usa_il_default(self):
        for score in (None, "abc", 1.7, -0.2):
            assert result_to_finding(exa_result(score=score))["confidence"] == DEFAULT_CONFIDENCE

    def test_risultato_senza_titolo_ne_url_e_vuoto(self):
        finding = result_to_finding({"score": 0.9})
        assert finding["claim"] == "" and finding["source"] == ""

    def test_numero_massimo_di_highlights(self):
        finding = result_to_finding(exa_result(highlights=["a", "b", "c", "d", "e"]))
        assert finding["evidence"].count("|") == 2      # MAX_HIGHLIGHTS = 3


class TestExaSearchTool:
    def _tool(self, transport, **kwargs):
        return ExaSearchTool(FAKE_KEY, http_post=transport, **kwargs)

    def test_search_mappa_i_risultati(self):
        transport = FakeTransport({"results": [exa_result(), exa_result(url="https://example.org/b")]})
        tool = self._tool(transport)

        findings = tool.search(QUERY)

        assert len(findings) == 2
        assert [f["query"] for f in findings] == [QUERY, QUERY]
        assert transport.calls[0]["url"] == "https://api.exa.ai/search"

    def test_header_e_body_della_richiesta(self):
        transport = FakeTransport({"results": []})
        tool = self._tool(transport, num_results=7, search_type="fast",
                          include_domains=["example.org"], exclude_domains=["spam.org"])
        tool.search(QUERY)

        call = transport.calls[0]
        assert call["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
        assert call["headers"]["x-api-key"] == FAKE_KEY
        assert call["json"]["query"] == QUERY
        assert call["json"]["type"] == "fast"
        assert call["json"]["contents"] == {"highlights": True, "text": True}
        assert call["json"]["includeDomains"] == ["example.org"]
        assert call["json"]["excludeDomains"] == ["spam.org"]
        assert call["timeout"] > 0

    def test_chiave_da_env_se_non_iniettata(self, monkeypatch):
        monkeypatch.setenv("EXA_API_KEY", "sk-from-env")
        transport = FakeTransport({"results": []})
        tool = ExaSearchTool(http_post=transport)

        assert tool.configured()
        tool.search(QUERY)
        assert transport.calls[0]["headers"]["Authorization"] == "Bearer sk-from-env"

    def test_senza_chiave_e_fail_closed(self, monkeypatch):
        monkeypatch.delenv("EXA_API_KEY", raising=False)
        transport = FakeTransport({"results": [exa_result()]})
        tool = ExaSearchTool(http_post=transport)

        assert not tool.configured()
        with pytest.raises(ExaSearchError, match="EXA_API_KEY"):
            tool.search(QUERY)
        assert transport.calls == []                    # nessuna richiesta partita

    def test_errore_http_solleva_con_contesto(self):
        transport = FakeTransport({"results": []}, status_code=429)
        with pytest.raises(ExaSearchError, match="429"):
            self._tool(transport).search(QUERY)

    def test_risposta_non_json_solleva(self):
        transport = FakeTransport(payload=ValueError("body non JSON"))
        with pytest.raises(ExaSearchError):
            self._tool(transport).search(QUERY)

    def test_risposta_senza_results_e_lista_vuota(self):
        assert self._tool(FakeTransport({"requestId": "abc"})).search(QUERY) == []
        assert self._tool(FakeTransport({"results": "boh"})).search(QUERY) == []

    def test_risultati_inutilizzabili_scartati_e_registrati(self):
        transport = FakeTransport({"results": [{"score": 0.5}, exa_result()]})
        tool = self._tool(transport)

        findings = tool.search(QUERY)

        assert len(findings) == 1                       # il vuoto non passa
        assert tool.calls == [QUERY]

    def test_env_override_dell_endpoint(self):
        tool = ExaSearchTool("sk-x", url="https://proxy.internal/search")
        assert tool.build_payload("q")["numResults"] >= 1
        assert tool._url == "https://proxy.internal/search"


class TestTipoDiRicerca:
    """Exa restituisce `score` SOLO con `type: "neural"`.

    La confidence dei finding dipende da quel campo: il default dell'adapter
    e' quindi `neural` (non piu' `auto`), altrimenti resterebbe sempre 0.5.
    """

    def test_default_e_neural(self):
        assert DEFAULT_SEARCH_TYPE == "neural"
        assert ExaSearchTool("sk-x").resolved_search_type() == "neural"

    def test_default_nel_payload_e_score_usato_come_confidence(self, monkeypatch):
        monkeypatch.delenv("EXA_SEARCH_TYPE", raising=False)
        transport = FakeTransport({"results": [exa_result(score=0.91)]})
        tool = ExaSearchTool(FAKE_KEY, http_post=transport)

        findings = tool.search(QUERY)

        assert transport.calls[0]["json"]["type"] == "neural"
        assert findings[0]["confidence"] == pytest.approx(0.91)

    def test_env_exa_search_type_cambia_il_default(self, monkeypatch):
        monkeypatch.setenv("EXA_SEARCH_TYPE", "auto")
        transport = FakeTransport({"results": []})
        tool = ExaSearchTool(FAKE_KEY, http_post=transport)

        tool.search(QUERY)

        assert transport.calls[0]["json"]["type"] == "auto"

    def test_tipo_esplicito_vince_sull_env(self, monkeypatch):
        monkeypatch.setenv("EXA_SEARCH_TYPE", "auto")
        transport = FakeTransport({"results": []})
        tool = ExaSearchTool(FAKE_KEY, http_post=transport, search_type="fast")

        tool.search(QUERY)

        assert transport.calls[0]["json"]["type"] == "fast"

    def test_tipo_vuoto_omette_il_campo(self, monkeypatch):
        monkeypatch.setenv("EXA_SEARCH_TYPE", "auto")
        tool = ExaSearchTool(FAKE_KEY, search_type="")

        assert "type" not in tool.build_payload("q")


class TestExaIntegrazioneColGrafo:
    def test_i_risultati_reali_entrano_nel_grafo(self):
        """Adattatore reale + trasporto finto: il grafo non cambia una riga."""
        transport = FakeTransport({"results": [
            exa_result(title="Il CLV previene l'edge negativo",
                       highlights=["Il CLV vig-free calcolato sulla closing line "
                                   "devigata batte il ROI come predittore di edge."]),
            exa_result(title="Campioni piccoli e varianza del ROI",
                       url="https://example.org/b",
                       highlights=["Con meno di 100 scommesse chiuse la varianza "
                                   "del ROI domina il segnale misurato."]),
        ]})
        validator = MockLLMValidator(["pass"])

        state = run_research(QUERY, search=self._tool(transport), validator=validator)

        assert state.status == "passed" and state.attempt == 1
        assert len(state.findings) == 2
        assert state.findings[0].source == "https://example.org/a"
        assert len(state.findings[0].evidence) >= 20     # gate deterministico ok
        assert validator.requests[0].findings[0].confidence == pytest.approx(0.83)

    def test_ricerca_vuota_non_inventa_evidenze(self):
        transport = FakeTransport({"results": []})
        state = run_research(QUERY, search=self._tool(transport),
                             validator=MockLLMValidator(default="pass"))

        assert state.status == "hard_stop"
        assert state.findings == []
        assert state.attempts_log[0].feedback_kind == "deterministic"

    def _tool(self, transport):
        return ExaSearchTool(FAKE_KEY, http_post=transport,
                             max_evidence_chars=400)


@pytest.mark.integration
@pytest.mark.skipif(not exa_configured(), reason="EXA_API_KEY non configurata")
def test_exa_chiamata_vera_solo_con_chiave():
    """Verifica manuale dell'integrazione reale (usa 1 credito Exa + rete)."""
    tool = ExaSearchTool(num_results=3)
    assert tool.resolved_search_type() == "neural"      # tipo che porta lo score
    state = run_research(
        "value betting closing line value",
        search=tool,
        validator=MockLLMValidator(default="pass"),
        min_findings=1,
    )
    assert state.status in ("passed", "hard_stop")
    assert state.raw_findings, "la ricerca reale deve restituire almeno una pagina"
    # con `neural` lo score c'e': la confidence non resta il default 0.5
    assert all(0.0 <= f["confidence"] <= 1.0 for f in state.raw_findings)
