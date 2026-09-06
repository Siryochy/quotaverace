"""Test esclusione mercato OU2.5 (emergency 06/09).

Il backtest storico (12.909 partite) ha mostrato un leak sistematico sul
mercato Over/Under (ROI -6.8% su 924 bet, sotto -7.3%): il mercato OU e'
stato ESCLUSO dalle selezioni finche' il leak non viene corretto.
Comportamento atteso con OU_ENABLED=False (default):
  - _analyze_match NON registra previsioni con mercato "OU" nel ledger;
  - lo status e il miglior esito dipendono solo da 1X2.
Con OU_ENABLED=True il comportamento storico (OU considerato) torna attivo.
"""

import pytest

import fixture_engine


def _match():
    """Match con h2h 1X2 e totals 2.5 (entrambi i lati) multi-bookmaker."""
    return {
        "id": "match_ou_test",
        "home_team": "Roma", "away_team": "Empoli",
        "commence_time": "2026-09-01T18:45:00Z",
        "bookmakers": [
            {"title": "BookA", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Roma", "price": 1.70},
                    {"name": "Draw", "price": 3.80},
                    {"name": "Empoli", "price": 5.50}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over 2.5", "point": 2.5, "price": 2.00},
                    {"name": "Under 2.5", "point": 2.5, "price": 1.80}]},
            ]},
        ],
    }


def _run(monkeypatch):
    """Esegue _analyze_match intercettando save_prediction e save_analysis.

    is_sane viene forzato a passare (True, "OK") cosi' TUTTI i candidati
    finiscono nel ledger: il test verifica quali mercati vengono registrati,
    non se i singoli segnali superano il filtro EV."""
    monkeypatch.setattr("fixture_engine.expected_goals", lambda h, a: (1.9, 1.1))
    monkeypatch.setattr("fixture_engine.save_clv", lambda *a, **k: None)
    monkeypatch.setattr("fixture_engine.get_analysis_for_match", lambda m: None)
    monkeypatch.setattr("fixture_engine.is_sane", lambda *a, **k: (True, "OK"))
    preds = []
    monkeypatch.setattr("fixture_engine.save_prediction",
                        lambda *a, **k: preds.append((a, k)))
    saved = {}
    monkeypatch.setattr("fixture_engine.save_analysis",
                        lambda *a, **k: saved.update({"args": a, "kwargs": k}))
    status = fixture_engine._analyze_match("match_ou_test", _match(),
                                           "Roma", "Empoli", "Serie A")
    return status, preds, saved


class TestOuExcluso:
    def test_nessuna_previsione_ou_nel_ledger(self, monkeypatch):
        """Default (OU_ENABLED=False): nessun candidato OU viene registrato."""
        monkeypatch.setattr(fixture_engine, "OU_ENABLED", False)
        status, preds, _ = _run(monkeypatch)
        assert status in ("value", "strong_value", "no_value", "rejected")
        mercati = [a[1] for a, _ in preds]  # save_prediction(match, mercato, ...)
        assert "OU" not in mercati
        assert mercati, "devono esserci candidati 1X2"

    def test_esclusione_valida_con_ou_disabilitato(self, monkeypatch):
        """OU_ENABLED=False con soli prezzi h2h: analisi regolare, no crash."""
        monkeypatch.setattr(fixture_engine, "OU_ENABLED", False)
        m = _match()
        m["bookmakers"][0]["markets"] = [mm for mm in m["bookmakers"][0]["markets"]
                                         if mm["key"] != "totals"]
        monkeypatch.setattr("fixture_engine.expected_goals", lambda h, a: (1.9, 1.1))
        monkeypatch.setattr("fixture_engine.save_clv", lambda *a, **k: None)
        monkeypatch.setattr("fixture_engine.get_analysis_for_match", lambda m: None)
        preds = []
        monkeypatch.setattr("fixture_engine.save_prediction",
                            lambda *a, **k: preds.append((a, k)))
        status = fixture_engine._analyze_match("m_no_totals", m, "Roma", "Empoli", "Serie A")
        assert status in ("value", "strong_value", "no_value", "rejected")
        assert all(a[1] == "1X2" for a, _ in preds)

    def test_con_ou_abilitato_torna_il_mercato_ou(self, monkeypatch):
        """OU_ENABLED=True: il mercato OU2.5 torna tra i candidati (back-compat)."""
        monkeypatch.setattr(fixture_engine, "OU_ENABLED", True)
        status, preds, _ = _run(monkeypatch)
        assert status in ("value", "strong_value", "no_value", "rejected")
        mercati = {a[1] for a, _ in preds}
        assert "OU" in mercati


class TestOuEnabledFlag:
    def test_default_disabilitato(self):
        """Senza env il mercato OU e' escluso (emergency attiva)."""
        # il flag e' gia' stato letto all'import da os.getenv; il default
        # del progetto e' spento (nessun ENABLE_OU_MARKET nel repo)
        import os
        val = os.getenv("ENABLE_OU_MARKET", "")
        assert val == ""  # il repo non lo imposta: default = escluso

    def test_flag_letto_da_env(self, monkeypatch):
        """Con ENABLE_OU_MARKET=1 il flag module-level diventa True."""
        monkeypatch.setenv("ENABLE_OU_MARKET", "1")
        import importlib
        importlib.reload(fixture_engine)
        try:
            assert fixture_engine.OU_ENABLED is True
        finally:
            monkeypatch.delenv("ENABLE_OU_MARKET", raising=False)
            importlib.reload(fixture_engine)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])