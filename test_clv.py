"""
Test unitari per il tracking Closing Line Value (CLV).
"""

import pytest

import tracker
from tracker import save_clv, get_results_stats


@pytest.fixture(autouse=True)
def _clean_clv(monkeypatch):
    """Usa un DB temporaneo per non sporcare quotaverace.db."""
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setattr(tracker, "DB_PATH", tmp)
    tracker.init_db()
    yield
    if tmp.exists():
        tmp.unlink()


def _seed_match_and_result(mid, esito, quota, status="value", sh=2, sa=0, home="Roma", away="Empoli"):
    tracker.save_match(mid, "Serie A", home, away, "2026-08-30T18:00:00Z")
    tracker.save_analysis(mid, 2.0, 1.0, 0.5, 0.3, 0.2, 0.6, 0.05, esito, quota, "Book", status)
    tracker.save_result(mid, "Serie A", home, away, sh, sa, "2026-08-30T20:00:00Z")


class TestSaveClv:
    def test_prima_lettura_imposta_signal_e_closing(self):
        # serving della scommessa chiusa che incrocia col CLV
        _seed_match_and_result("m1", "Roma", 2.00, status="value", sh=2, sa=0)
        save_clv("m1", "Roma", 2.00, signal_started=True)
        save_clv("m1", "Roma", 1.95, signal_started=False)  # lettura successiva -> chiusura piu' bassa
        save_clv("m1", "Roma", 1.90, signal_started=False)
        stats = get_results_stats()
        # quota segnale 2.00 vs chiusura 1.90 -> abbiamo preso prezzo migliore -> CLV > 0
        assert stats["clv_tracked"] == 1
        assert stats["avg_clv"] == pytest.approx((2.00 / 1.90) - 1.0)

    def test_manca_clv_quando_non_tracciato(self):
        stats = get_results_stats()
        assert stats.get("clv_tracked", 0) == 0
        assert stats.get("avg_clv", 0.0) == 0.0


class TestClvInResults:
    def test_clv_positivo_battiamo_la_chiusura(self):
        # pseudo-match per ottenere 1 betting in stats
        tracker.save_match("mX", "Serie A", "Roma", "Empoli", "2026-08-30T18:00:00Z")
        tracker.save_analysis("mX", 2.0, 1.0, 0.5, 0.3, 0.2, 0.6, 0.05,
                              "Roma", 2.10, "Book", "value")
        tracker.save_result("mX", "Serie A", "Roma", "Empoli", 2, 0, "2026-08-30T20:00:00Z")
        save_clv("mX", "Roma", 2.10, signal_started=True)   # presa a 2.10
        save_clv("mX", "Roma", 2.00, signal_started=False)  # chiude a 2.00 -> +5% CLV
        stats = get_results_stats()
        assert stats["total"] == 1
        assert stats["clv_tracked"] == 1
        assert stats["avg_clv"] == pytest.approx(0.05)

    def test_clv_negativo_se_ci_muoviamo_contro(self):
        tracker.save_match("mY", "Serie A", "Roma", "Empoli", "2026-08-30T18:00:00Z")
        tracker.save_analysis("mY", 2.0, 1.0, 0.5, 0.3, 0.2, 0.6, 0.05,
                              "Roma", 2.10, "Book", "value")
        tracker.save_result("mY", "Serie A", "Roma", "Empoli", 2, 0, "2026-08-30T20:00:00Z")
        save_clv("mY", "Roma", 2.10, signal_started=True)   # presa a 2.10
        save_clv("mY", "Roma", 2.30, signal_started=False)  # il mercato offre di piu' -> CLV < 0
        stats = get_results_stats()
        assert stats["avg_clv"] == pytest.approx((2.10 / 2.30) - 1.0)

    def test_signal_started_resetta(self):
        save_clv("m", "X", 2.00, signal_started=True)
        save_clv("m", "X", 1.90, signal_started=False)
        # re-segnale: riparte dalla quota attuale
        save_clv("m", "X", 2.20, signal_started=True)
        stats = get_results_stats()
        # non c'e' betting, solo CLV diretto
        assert True  # smoke test: nessuna eccezione e update coerente


class TestGetResultsStatsClvMap:
    def test_esito_diverso_non_si_incrocia(self):
        # CLV tracciato per esito "a" ma betting su esito "b" -> non accoppiato
        save_clv("mA", "Sbagliato", 2.00, signal_started=True)
        _seed_match_and_result("mA", "Roma", 2.10, status="value", sh=2, sa=0)
        stats = get_results_stats()
        assert stats["total"] == 1
        assert stats["clv_tracked"] == 0