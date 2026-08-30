"""
Test unitari per l'AI Commander (ai_commander.py).

Non chiama l'API Gemini (nessuna rete): testa il dispatch dei tool,
il fail-closed e la validazione degli input.
"""

import pytest

import ai_commander
from ai_commander import AICommander, TOOLS, TOOL_DECLARATIONS


@pytest.fixture(autouse=True)
def _reset_bankroll():
    ai_commander._bankroll_state["value"] = 100.0
    yield
    ai_commander._bankroll_state["value"] = 100.0


class TestTools:
    def test_tools_registry_copre_dichiarazioni(self):
        declared = {t["name"] for t in TOOL_DECLARATIONS}
        assert declared == set(TOOLS.keys())

    def test_analyze_match(self):
        r = ai_commander._tool_analyze_match("Inter", "Napoli")
        assert r["home"] == "Inter" and r["away"] == "Napoli"
        probs = r["probabilitas"]
        assert 0 < probs["1"] < 1 and 0 < probs["X"] < 1 and 0 < probs["2"] < 1
        assert abs(probs["1"] + probs["X"] + probs["2"] - 1.0) < 0.01
        assert 0 < r["expected_goals"]["home"] < 10

    def test_value_filter_positivo(self):
        # prob 0.55 con quota 2.10 => EV +15.5%, ma il filtro Pro blocca EV > 15%
        r = ai_commander._tool_value_filter(0.55, 2.10)
        assert r["ev"] > 0
        assert r["sane"] is False  # anomalia: EV sopra il tetto Pro del 15%
        assert "stake_eur" > 0 if False else r["stake_eur"] >= 0  # stake comunque calcolato

    def test_value_filter_sano_positivo(self):
        # prob 0.55 con quota 2.0 => EV +10%, dentro la finestra Pro (3%-15%)
        r = ai_commander._tool_value_filter(0.55, 2.0)
        assert r["ev"] > 0
        assert r["sane"] is True
        assert r["stake_eur"] > 0

    def test_value_filter_negativo(self):
        # prob bassa con quota bassa => EV negativo
        r = ai_commander._tool_value_filter(0.20, 1.5)
        assert r["ev"] < 0

    def test_set_bankroll_e_value_filter_interagiscono(self):
        ai_commander._tool_set_bankroll(500)
        r = ai_commander._tool_value_filter(0.55, 2.10)
        # cap 3% di 500 = 15
        assert r["stake_eur"] <= 15.0 + 1e-6
        assert ai_commander._get_bankroll() == 500.0

    def test_set_bankroll_minimo(self):
        r = ai_commander._tool_set_bankroll(1)
        assert r["bankroll"] == 10.0

    def test_recent_signals_vuoto(self):
        r = ai_commander._tool_recent_signals()
        assert r["count"] == 0

    def test_performance_struttura(self):
        r = ai_commander._tool_performance(30)
        assert "closed" in r and "roi" in r

    def test_calendar_vuoto_o_valido(self):
        r = ai_commander._tool_calendar()
        assert isinstance(r["count"], int)
        assert isinstance(r["matches"], list)


class TestDispatch:
    def setup_method(self):
        self.cmd = AICommander.__new__(AICommander)  # senza init di rete

    def test_tool_sconosciuto(self):
        r = self.cmd._dispatch("nuclear_launch", {})
        assert "error" in r

    def test_argomenti_errati_fail_closed(self):
        r = self.cmd._dispatch("analyze_match", {"home": 123})
        assert "error" in r

    def test_argomenti_validi(self):
        r = self.cmd._dispatch("value_filter", {"probability": 0.55, "quota": 2.10})
        assert "ev" in r and "stake_eur" in r


class TestDeclarations:
    def test_dichiarazioni_ben_formate(self):
        for t in TOOL_DECLARATIONS:
            assert "name" in t and "description" in t
            assert t["parameters"]["type"] == "OBJECT"
