"""Test della calibrazione isotonica (probability_calibration.py).

Verifica che la regressione isotonica (PAVA) sia monotona e conservi le
somme, che il calibratore corregga l'overconfidence di un classificatore
sintetico (Brier/ECE migliorano), e che la persistenza JSON funzioni.
"""

import numpy as np
import pytest

from probability_calibration import (
    IsotonicCalibrator,
    brier_score,
    calibration_report,
    expected_calibration_error,
    isotonic_regression,
)


class TestIsotonicRegression:
    def test_monotona_e_conserva_somma(self):
        y = np.array([0.0, 1, 0, 1, 1, 0])
        out = isotonic_regression(y)
        assert np.all(np.diff(out) >= -1e-9)      # non decrescente
        assert abs(out.sum() - y.sum()) < 1e-9    # somma conservata

    def test_gia_monotona_invariata(self):
        y = np.array([0.1, 0.3, 0.5, 0.9])
        out = isotonic_regression(y)
        assert np.allclose(out, y)

    def test_casi_limite(self):
        assert np.allclose(isotonic_regression(np.array([1.0])), [1.0])
        assert len(isotonic_regression(np.array([]))) == 0
        out = isotonic_regression(np.array([0.5, 0.5, 0.5]))
        assert np.allclose(out, [0.5, 0.5, 0.5])

    def test_binari_raggruppano_frequenze(self):
        # y binari ordinati: i blocchi devono riflettere le frequenze
        # empiriche locali, non i singoli 0/1.
        y = np.array([0, 0, 1, 1, 1])
        out = isotonic_regression(y)
        assert np.allclose(out, [0, 0, 1, 1, 1])


def _overconfident_scores(n=400, seed=0, logit_gain=1.8):
    """Score sintetici overconfident: logit amplificato -> compresso verso 0/1."""
    rng = np.random.RandomState(seed)
    p = rng.uniform(0.25, 0.85, n)
    y = (rng.random(n) < p).astype(float)
    z = np.log(p / (1.0 - p)) * logit_gain
    scores = 1.0 / (1.0 + np.exp(-z))
    return scores, y


class TestIsotonicCalibrator:
    def test_corregge_overconfidence(self):
        scores, y = _overconfident_scores()
        cal = IsotonicCalibrator().fit(scores, y)
        post = cal.predict(scores)
        assert brier_score(post, y) < brier_score(scores, y) - 0.01
        # La curva e' monotona non decrescente
        grid = np.linspace(0, 1, 100)
        assert np.all(np.diff(cal.predict(grid)) >= -1e-9)

    def test_riduce_ece(self):
        scores, y = _overconfident_scores(n=800)
        cal = IsotonicCalibrator().fit(scores, y)
        post = cal.predict(scores)
        assert expected_calibration_error(post, y) < \
            expected_calibration_error(scores, y) - 0.01

    def test_predict_senza_fit_ritorna_score(self):
        scores = np.array([0.3, 0.7])
        cal = IsotonicCalibrator()
        assert np.allclose(cal.predict(scores), scores)

    def test_mappa_verso_frequenze_empiriche(self):
        # Su un segmento di score con frequenza nota, la calibrazione
        # deve riportare la prob vicino alla frequenza empirica.
        rng = np.random.RandomState(1)
        n = 300
        y = (rng.random(n) < 0.72).astype(float)
        scores = rng.uniform(0.65, 0.80, n)  # tutti nello stesso range
        cal = IsotonicCalibrator().fit(scores, y)
        p = float(cal.predict(np.array([0.72]))[0])
        assert abs(p - 0.72) < 0.08

    def test_persistenza(self):
        scores, y = _overconfident_scores(n=200)
        cal = IsotonicCalibrator().fit(scores, y)
        cal2 = IsotonicCalibrator.from_dict(cal.to_dict())
        assert cal2 is not None and cal2.fitted_
        x = np.array([0.1, 0.4, 0.6, 0.9])
        assert np.allclose(cal.predict(x), cal2.predict(x))

    def test_from_dict_none(self):
        assert IsotonicCalibrator.from_dict(None) is None
        assert IsotonicCalibrator.from_dict({}) is None


class TestCalibrationReport:
    def test_metriche_pre_post(self):
        scores, y = _overconfident_scores(n=300)
        cal = IsotonicCalibrator().fit(scores, y)
        rep = calibration_report(scores, y, cal)
        assert "pre_brier" in rep and "post_brier" in rep
        assert rep["post_brier"] < rep["pre_brier"]
        assert rep["n"] == 300

    def test_senza_calibratore_solo_pre(self):
        scores, y = _overconfident_scores(n=100)
        rep = calibration_report(scores, y, None)
        assert "pre_brier" in rep
        assert "post_brier" not in rep


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))