"""Test ML Ensemble: Logistic Regression, ensemble prediction, serialization."""
import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from ml_ensemble import (
    LogisticRegressor, EnsemblePredictor, _build_features,
    FEATURE_NAMES, predict_ensemble, train_ensemble,
)


# --- Logistic Regressor ---

class TestLogisticRegressor:
    def test_fit_and_predict(self):
        """Addestramento su dati separabili: dovrebbe raggiungere alta accuracy."""
        rng = np.random.RandomState(42)
        X = rng.randn(200, 5)
        # Label basata su combinazione lineare
        y = (X[:, 0] + X[:, 1] > 0).astype(float)
        lr = LogisticRegressor(lr=0.05, epochs=300, l2=0.01)
        metrics = lr.fit(X, y)
        assert metrics["accuracy"] > 0.70
        preds = lr.predict_proba(X)
        assert all(0 <= p <= 1 for p in preds)

    def test_predict_senza_fit(self):
        """Predict senza fit ritorna 0.5 per tutti."""
        lr = LogisticRegressor()
        preds = lr.predict_proba(np.array([[1, 2], [3, 4]]))
        assert all(p == 0.5 for p in preds)

    def test_serialization(self):
        """Save/load del modello."""
        rng = np.random.RandomState(42)
        X = rng.randn(100, 4)
        y = (X[:, 0] > 0).astype(float)
        lr = LogisticRegressor(lr=0.05, epochs=200)
        lr.fit(X, y, feature_names=["a", "b", "c", "d"])

        data = lr.to_dict()
        lr2 = LogisticRegressor.from_dict(data)

        preds1 = lr.predict_proba(X[:5])
        preds2 = lr2.predict_proba(X[:5])
        np.testing.assert_array_almost_equal(preds1, preds2, decimal=5)


# ---

class TestBuildFeatures:
    def test_features_hanno_lunghezza_corretta(self):
        row = {
            "prob_1": 0.45, "prob_X": 0.25, "prob_2": 0.30,
            "prob_over": 0.52, "lam_h": 1.5, "lam_a": 1.2,
            "quota": 2.10, "market_prob": 0.42, "market_edge": 0.03,
            "ev": 0.05, "prob": 0.48,
        }
        features = _build_features(row)
        assert len(features) == len(FEATURE_NAMES)

    def test_features_valori_none(self):
        """Righe con None non devono crashare."""
        row = {"quota": 2.0}
        features = _build_features(row)
        assert len(features) == len(FEATURE_NAMES)


# --- Ensemble Predictor ---

class TestEnsemblePredictor:
    def _make_dataset(self, n=100):
        """Dataset sintetico per test."""
        rng = np.random.RandomState(42)
        rows = []
        for _ in range(n):
            lam_h = rng.uniform(0.5, 3.0)
            lam_a = rng.uniform(0.5, 3.0)
            quota = rng.uniform(1.5, 5.0)
            prob = rng.uniform(0.2, 0.8)
            label = 1 if rng.random() < prob else 0
            rows.append({
                "prob_1": prob, "prob_X": 0.25, "prob_2": 1 - prob - 0.25,
                "prob_over": 0.5, "lam_h": lam_h, "lam_a": lam_a,
                "quota": quota, "market_prob": prob * 0.95,
                "market_edge": 0.03, "ev": 0.05, "prob": prob,
                "label_ml": label,
            })
        return rows

    def test_train_con_dati_sufficienti(self):
        ds = self._make_dataset(100)
        ens = EnsemblePredictor()
        metrics = ens.train(ds)
        assert metrics["status"] == "trained"
        assert ens.trained

    def test_train_dati_insufficienti(self):
        ds = self._make_dataset(10)
        ens = EnsemblePredictor()
        metrics = ens.train(ds)
        assert metrics["status"] == "insufficient_data"

    def test_predict_dopo_train(self):
        ds = self._make_dataset(100)
        ens = EnsemblePredictor()
        ens.train(ds)
        row = ds[0]
        result = ens.predict(row)
        assert "ensemble_prob" in result
        assert 0 <= result["ensemble_prob"] <= 1
        assert result["ml_available"]

    def test_predict_senza_train(self):
        ens = EnsemblePredictor()
        result = ens.predict({"prob": 0.5, "quota": 2.0})
        assert not result["ml_available"]
        assert result["ensemble_prob"] == 0.5

    def test_save_load(self):
        ds = self._make_dataset(100)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "model.json"
            ens = EnsemblePredictor()
            ens.train(ds)
            ens.save(path)
            assert path.exists()

            ens2 = EnsemblePredictor()
            assert ens2.load(path)
            assert ens2.trained
            # Predizioni identiche
            row = ds[0]
            r1 = ens.predict(row)
            r2 = ens2.predict(row)
            assert abs(r1["ensemble_prob"] - r2["ensemble_prob"]) < 0.001

    def test_ensemble_weight_dipende_da_brier(self):
        ds = self._make_dataset(100)
        ens = EnsemblePredictor()
        ens.train(ds)
        # Brier basso → peso ML alto
        assert ens.ensemble_weight > 0.15

    def test_train_attiva_calibrazione_con_60_campioni(self):
        """Con >= MIN_CALIB_SAMPLES il calibratore isotonico viene fit su
        score out-of-sample e il report di calibrazione e' nelle metriche."""
        ds = self._make_dataset(100)
        ens = EnsemblePredictor()
        metrics = ens.train(ds)
        assert ens.calibrator is not None
        assert ens.calibrator.fitted_
        cal = metrics.get("calibration", {})
        assert "pre_brier" in cal and "post_brier" in cal
        assert cal["post_brier"] <= cal["pre_brier"] + 1e-6

    def test_calibrazione_skippata_con_pochi_campioni(self):
        ds = self._make_dataset(40)
        ens = EnsemblePredictor()
        metrics = ens.train(ds)
        assert ens.calibrator is None
        assert metrics["calibration"]["status"] == "skipped"

    def test_predict_applica_calibrazione(self):
        ds = self._make_dataset(100)
        ens = EnsemblePredictor()
        ens.train(ds)
        row = ds[0]
        result = ens.predict(row)
        assert result["calibrated"] is True
        assert 0.0 <= result["ensemble_prob"] <= 1.0

    def test_save_load_preserva_calibratore(self):
        ds = self._make_dataset(100)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "model.json"
            ens = EnsemblePredictor()
            ens.train(ds)
            ens.save(path)
            ens2 = EnsemblePredictor()
            assert ens2.load(path)
            assert ens2.calibrator is not None
            assert ens2.calibrator.fitted_
            row = ds[0]
            r1 = ens.predict(row)
            r2 = ens2.predict(row)
            assert abs(r1["ensemble_prob"] - r2["ensemble_prob"]) < 0.001

    def test_calibrazione_mantiene_prob_in_range(self):
        """Dopo la calibrazione le prob restano in [0,1] anche su score
        estremi (estrapolazione piatta ai bordi)."""
        ds = self._make_dataset(100)
        ens = EnsemblePredictor()
        ens.train(ds)
        for prob in (0.01, 0.5, 0.99):
            row = dict(ds[0], prob=prob)
            r = ens.predict(row)
            assert 0.0 <= r["ensemble_prob"] <= 1.0


# --- train_ensemble shortcut ---

def test_train_ensemble_con_lista():
    # 50 righe con ENTRAMBE le classi: 30 vinte + 20 perse. Con tutte le
    # label a 1 il training set resta a classe singola e XGBoost fallisce
    # ("Invalid classes inferred from unique values of y").
    ds = [{"prob_1": 0.5, "prob_X": 0.25, "prob_2": 0.25,
            "prob_over": 0.5, "lam_h": 1.5, "lam_a": 1.2,
            "quota": 2.0, "market_prob": 0.45, "market_edge": 0.05,
            "ev": 0.10, "prob": 0.5, "label_ml": 1 if i < 30 else 0}
          for i in range(50)]
    metrics = train_ensemble(ds)
    assert metrics.get("status") == "trained"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
