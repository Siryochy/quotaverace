"""ml_ensemble.py — Ensemble Poisson + Logistic Regression (numpy-only).

Combina le probabilità del modello Poisson/Dixon-Coles con un classificatore
logistico addestrato sul dataset storico (predictions + match_analysis).

Perché un ensemble?
- Il Poisson è un modello parametrico: funziona bene ma è rigido.
- La Logistic Regression è un modello non-parametrico che impara i pattern
  dai dati reali (forma,-quote,edge,model_prob) e calibra meglio le
  probabilità quando il modello è sistematicamente troppo ottimista/pessimista.
- L'ensemble (weighted average) riduce la varianza e migliora la calibrazione.

Requisiti: solo numpy e pandas (già nel progetto).

CLI:
  venv/bin/python ml_ensemble.py                # allena e mostra metriche
  venv/bin/python ml_ensemble.py --predict m1  # predice su un match
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# --- Config ---
MODEL_DIR = Path(__file__).parent / "data"
MODEL_PATH = MODEL_DIR / "ensemble_model.json"
MIN_SAMPLES = 30  # minimi campioni per addestrare l'ensemble


class LogisticRegressor:
    """Logistic Regression con gradient descent (numpy-only, zero deps).

    Addestramento con regularizzazione L2 per evitare overfitting.
    """

    def __init__(self, lr: float = 0.01, epochs: int = 500,
                 l2: float = 0.1):
        self.lr = lr
        self.epochs = epochs
        self.l2 = l2
        self.weights: Optional[np.ndarray] = None
        self.bias: float = 0.0
        self.feature_means: Optional[np.ndarray] = None
        self.feature_stds: Optional[np.ndarray] = None
        self.feature_names: List[str] = []

    def _sigmoid(self, z: np.ndarray) -> np.ndarray:
        z = np.clip(z, -500, 500)
        return 1.0 / (1.0 + np.exp(-z))

    def _standardize(self, X: np.ndarray, fit: bool = False) -> np.ndarray:
        if fit:
            self.feature_means = np.mean(X, axis=0)
            self.feature_stds = np.std(X, axis=0)
            self.feature_stds[self.feature_stds < 1e-8] = 1.0
        if self.feature_means is None or self.feature_stds is None:
            return X
        return (X - self.feature_means) / self.feature_stds

    def fit(self, X: np.ndarray, y: np.ndarray,
            feature_names: List[str] = None) -> Dict:
        """Allena il modello. Ritorna le metriche di training."""
        self.feature_names = feature_names or [f"f{i}" for i in range(X.shape[1])]
        X_std = self._standardize(X, fit=True)
        n_features = X_std.shape[1]

        # Inizializzazione
        self.weights = np.zeros(n_features)
        self.bias = 0.0

        # Gradient descent
        losses = []
        for epoch in range(self.epochs):
            z = X_std @ self.weights + self.bias
            preds = self._sigmoid(z)

            # Binary cross-entropy + L2
            eps = 1e-7
            loss = -np.mean(y * np.log(preds + eps) +
                             (1 - y) * np.log(1 - preds + eps))
            loss += self.l2 * np.sum(self.weights ** 2)
            losses.append(loss)

            # Gradienti
            error = preds - y
            grad_w = (X_std.T @ error) / len(y) + 2 * self.l2 * self.weights
            grad_b = np.mean(error)

            self.weights -= self.lr * grad_w
            self.bias -= self.lr * grad_b

            if epoch % 100 == 0:
                acc = np.mean((preds >= 0.5) == y)
                logger.debug(f"Epoch {epoch}: loss={loss:.4f} acc={acc:.3f}")

        # Metriche finali
        preds_final = self.predict_proba(X)
        acc = np.mean((preds_final >= 0.5) == y)
        brier = np.mean((preds_final - y) ** 2)

        return {
            "epochs": self.epochs,
            "final_loss": float(losses[-1]) if losses else 0.0,
            "accuracy": float(acc),
            "brier_score": float(brier),
            "n_samples": len(y),
            "n_features": n_features,
        }

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Predice probabilità [0,1] per ogni riga."""
        if self.weights is None:
            return np.full(len(X), 0.5)
        X_std = self._standardize(X, fit=False)
        z = X_std @ self.weights + self.bias
        return self._sigmoid(z)

    def to_dict(self) -> Dict:
        """Serializza il modello."""
        return {
            "weights": self.weights.tolist() if self.weights is not None else [],
            "bias": self.bias,
            "feature_means": (self.feature_means.tolist()
                              if self.feature_means is not None else []),
            "feature_stds": (self.feature_stds.tolist()
                             if self.feature_stds is not None else []),
            "feature_names": self.feature_names,
            "lr": self.lr, "epochs": self.epochs, "l2": self.l2,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "LogisticRegressor":
        """Deserializza il modello."""
        m = cls(lr=d.get("lr", 0.01), epochs=d.get("epochs", 500),
                l2=d.get("l2", 0.1))
        m.weights = np.array(d["weights"]) if d.get("weights") else None
        m.bias = d.get("bias", 0.0)
        m.feature_means = (np.array(d["feature_means"])
                           if d.get("feature_means") else None)
        m.feature_stds = (np.array(d["feature_stds"])
                          if d.get("feature_stds") else None)
        m.feature_names = d.get("feature_names", [])
        return m


# --- XGBoost (opzionale) ---
# Se xgboost e' installato, viene usato come modello ML principale.
# Altrimenti il fallback e' LogisticRegressor (numpy-only).
try:
    import xgboost as xgb
    HAS_XGBOOST = True
except ImportError:
    HAS_XGBOOST = False
    xgb = None



class XGBoostClassifier:
    """Wrapper per XGBoost con interfaccia compatibile a LogisticRegressor.

    Quando xgboost e' disponibile, produce predizioni piu' accurate della
    Logistic Regression pura (Brier score tipicamente 5-10% migliore).
    """

    def __init__(self, n_estimators: int = 100, max_depth: int = 4,
                 learning_rate: float = 0.1, subsample: float = 0.8):
        self.params = {
            "n_estimators": n_estimators,
            "max_depth": max_depth,
            "learning_rate": learning_rate,
            "subsample": subsample,
        }
        self.model = None
        self.feature_names: List[str] = []
        self.brier_score: float = 0.0
        self.accuracy: float = 0.0

    def fit(self, X: np.ndarray, y: np.ndarray,
            feature_names: List[str] = None) -> Dict:
        """Allena il modello XGBoost. Ritorna le metriche."""
        if not HAS_XGBOOST:
            raise RuntimeError("xgboost non installato")

        self.feature_names = feature_names or [f"f{i}" for i in range(X.shape[1])]

        self.model = xgb.XGBClassifier(
            n_estimators=self.params["n_estimators"],
            max_depth=self.params["max_depth"],
            learning_rate=self.params["learning_rate"],
            subsample=self.params["subsample"],
            objective="binary:logistic",
            eval_metric="logloss",
            use_label_encoder=False,
            verbosity=0,
        )
        self.model.fit(X, y)

        # Metriche
        preds = self.predict_proba(X)
        acc = float(np.mean((preds >= 0.5) == y))
        brier = float(np.mean((preds - y) ** 2))
        self.accuracy = acc
        self.brier_score = brier

        return {
            "accuracy": acc,
            "brier_score": brier,
            "n_samples": len(y),
            "n_features": X.shape[1],
            "model": "xgboost",
            "params": self.params,
        }

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Predice probabilita' [0,1]."""
        if self.model is None:
            return np.full(len(X), 0.5)
        return self.model.predict_proba(X)[:, 1]

    def to_dict(self) -> Dict:
        """Serializza il modello."""
        return {
            "model_type": "xgboost",
            "params": self.params,
            "feature_names": self.feature_names,
            "brier_score": self.brier_score,
            "accuracy": self.accuracy,
            # XGBoost ha bisogno di save/load separato
            "booster": self.model.get_booster().save_raw().decode("latin-1")
            if self.model else None,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "XGBoostClassifier":
        """Deserializza il modello."""
        m = cls(**d.get("params", {}))
        m.feature_names = d.get("feature_names", [])
        m.brier_score = d.get("brier_score", 0.0)
        m.accuracy = d.get("accuracy", 0.0)
        if d.get("booster") and HAS_XGBOOST:
            booster_raw = d["booster"].encode("latin-1")
            booster = xgb.Booster()
            booster.load_model(bytearray(booster_raw))
            m.model = xgb.XGBClassifier()
            m.model._Booster = booster
        return m


def _build_features(row: Dict) -> List[float]:
    """Estrae le feature numeriche da una riga del dataset.

    Feature:
    - poisson probabilities (prob_1, prob_X, prob_2, prob_over)
    - lam_h, lam_a (gol attesi)
    - quota, market_prob, market_edge
    - ev, prob (blend)
    - diff_model_market (modello vs mercato)
    - odds_implied (1/quota)
    """
    def _f(v, default=0.0):
        if v is None:
            return default
        try:
            return float(v)
        except (ValueError, TypeError):
            return default

    p1 = _f(row.get("prob_1"))
    px = _f(row.get("prob_X"))
    p2 = _f(row.get("prob_2"))
    p_over = _f(row.get("prob_over"))
    lam_h = _f(row.get("lam_h"))
    lam_a = _f(row.get("lam_a"))
    quota = _f(row.get("quota"), 1.0)
    market_prob = _f(row.get("market_prob"))
    market_edge = _f(row.get("market_edge"))
    ev = _f(row.get("ev"))
    prob = _f(row.get("prob"))

    # Feature derivate
    odds_implied = 1.0 / quota if quota > 1.0 else 0.5
    diff_model_market = prob - market_prob if market_prob > 0 else 0.0
    goal_diff = lam_h - lam_a
    total_goals = lam_h + lam_a

    return [p1, px, p2, p_over, lam_h, lam_a,
            quota, market_prob, market_edge, ev, prob,
            odds_implied, diff_model_market, goal_diff, total_goals]


FEATURE_NAMES = [
    "prob_1", "prob_X", "prob_2", "prob_over",
    "lam_h", "lam_a", "quota", "market_prob", "market_edge",
    "ev", "prob_blend", "odds_implied", "diff_model_market",
    "goal_diff", "total_goals",
]


class EnsemblePredictor:
    """Ensemble: media ponderata tra Poisson (via prob_blend) e Logistic Regression.

    Il peso dell'ensemble dipende dalla confidenza del modello ML
    (brier score più basso = più peso al ML).
    """

    def __init__(self):
        self.lr_model = None  # LogisticRegressor o XGBoostClassifier
        self.model_type: str = "none"  # "logistic" | "xgboost"
        self.ensemble_weight: float = 0.4  # peso ML nell'ensemble (0-1)
        self.trained: bool = False
        self.train_metrics: Dict = {}

    def train(self, dataset: List[Dict]) -> Dict:
        """Allena il modello ML sul dataset storico.

        Se XGBoost e' disponibile, lo usa (Brier score 5-10% migliore).
        Altrimenti fallback a LogisticRegressor numpy-only.

        dataset: lista di dict dal build_training_rows() di ml_dataset.py
        Ritorna le metriche di training.
        """
        if len(dataset) < MIN_SAMPLES:
            logger.warning(f"Dataset troppo piccolo ({len(dataset)} < "
                           f"{MIN_SAMPLES}): ensemble disabilitato.")
            return {"status": "insufficient_data", "n": len(dataset)}

        X_list = []
        y_list = []
        for row in dataset:
            if row.get("label_ml") is None:
                continue
            features = _build_features(row)
            # Salta righe con tutti zeri (dati mancanti)
            if all(f == 0.0 for f in features):
                continue
            X_list.append(features)
            y_list.append(int(row["label_ml"]))

        if len(X_list) < MIN_SAMPLES:
            return {"status": "insufficient_data", "n": len(X_list)}

        X = np.array(X_list)
        y = np.array(y_list)

        # Seleziona il modello: XGBoost se disponibile, altrimenti LR
        if HAS_XGBOOST and len(X_list) >= 50:
            logger.info("Ensemble: uso XGBoost (%d campioni)", len(X_list))
            self.lr_model = XGBoostClassifier(
                n_estimators=100, max_depth=4, learning_rate=0.1)
            metrics = self.lr_model.fit(X, y, feature_names=FEATURE_NAMES)
            self.model_type = "xgboost"
        else:
            logger.info("Ensemble: uso Logistic Regression (%d campioni)",
                        len(X_list))
            self.lr_model = LogisticRegressor(lr=0.01, epochs=500, l2=0.1)
            metrics = self.lr_model.fit(X, y, feature_names=FEATURE_NAMES)
            self.model_type = "logistic"

        # Calcola ensemble weight: basato sul Brier score
        brier = metrics.get("brier_score", 0.25)
        self.ensemble_weight = max(0.15, min(0.55, 0.55 - brier))
        self.trained = True
        self.train_metrics = metrics
        metrics["ensemble_weight"] = self.ensemble_weight
        metrics["model_type"] = self.model_type
        metrics["status"] = "trained"

        logger.info(f"Ensemble ML addestrato: acc={metrics['accuracy']:.3f}, "
                    f"brier={brier:.4f}, weight={self.ensemble_weight:.2f}")

        return metrics

    def predict(self, row: Dict) -> Dict:
        """Predice la probabilità di un singolo evento.

        Ritorna:
            - ml_prob: probabilità del modello ML
            - poisson_prob: probabilità del modello Poisson (via prob blend)
            - ensemble_prob: media ponderata
            - confidence: confidenza del ML (inversamente proporzionale al brier)
        """
        poisson_prob = float(row.get("prob") or row.get("prob_1") or 0.5)

        if not self.trained or self.lr_model is None:
            return {
                "ml_prob": poisson_prob,
                "poisson_prob": poisson_prob,
                "ensemble_prob": poisson_prob,
                "confidence": 0.0,
                "ml_available": False,
            }

        features = np.array([_build_features(row)])
        ml_prob = float(self.lr_model.predict_proba(features)[0])

        # Ensemble: media ponderata
        w = self.ensemble_weight
        ensemble_prob = w * ml_prob + (1.0 - w) * poisson_prob

        # Confidence: 1 - brier_score (normalizzato)
        brier = self.train_metrics.get("brier_score", 0.25)
        confidence = max(0.0, min(1.0, 1.0 - brier * 3))

        return {
            "ml_prob": round(ml_prob, 4),
            "poisson_prob": round(poisson_prob, 4),
            "ensemble_prob": round(ensemble_prob, 4),
            "confidence": round(confidence, 3),
            "ml_available": True,
            "ensemble_weight": round(w, 3),
        }

    def save(self, path: Path = MODEL_PATH) -> None:
        """Salva il modello su disco (Logistic o XGBoost)."""
        if not self.trained or self.lr_model is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "model_type": self.model_type,
            "lr_model": self.lr_model.to_dict(),
            "ensemble_weight": self.ensemble_weight,
            "train_metrics": self.train_metrics,
        }
        path.write_text(json.dumps(data, indent=2))
        logger.info("Ensemble salvato: %s (%s)", path, self.model_type)

    def load(self, path: Path = MODEL_PATH) -> bool:
        """Carica il modello da disco (Logistic o XGBoost)."""
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text())
            model_type = data.get("model_type", "logistic")

            if model_type == "xgboost" and HAS_XGBOOST:
                self.lr_model = XGBoostClassifier.from_dict(data["lr_model"])
                self.model_type = "xgboost"
                logger.info("Ensemble XGBoost caricato da %s", path)
            elif model_type == "xgboost" and not HAS_XGBOOST:
                logger.warning("Modello XGBoost trovato ma xgboost non installato, "
                               "uso LogisticRegressor")
                self.lr_model = LogisticRegressor.from_dict(data["lr_model"])
                self.model_type = "logistic_fallback"
            else:
                self.lr_model = LogisticRegressor.from_dict(data["lr_model"])
                self.model_type = "logistic"

            self.ensemble_weight = data.get("ensemble_weight", 0.4)
            self.train_metrics = data.get("train_metrics", {})
            self.trained = True
            return True
        except Exception as e:
            logger.warning(f"Impossibile caricare ensemble: {e}")
            return False


# --- Singleton globale ---
_ensemble: Optional[EnsemblePredictor] = None


def get_ensemble() -> EnsemblePredictor:
    """Ritorna l'ensemble (carica da disco se disponibile)."""
    global _ensemble
    if _ensemble is None:
        _ensemble = EnsemblePredictor()
        _ensemble.load()
    return _ensemble


def predict_ensemble(row: Dict) -> Dict:
    """Shortcut: predice usando l'ensemble globale."""
    return get_ensemble().predict(row)


def train_ensemble(dataset: List[Dict] = None) -> Dict:
    """Allena l'ensemble e lo salva. Se dataset è None, legge dal DB."""
    if dataset is None:
        try:
            from ml_dataset import build_training_rows
            dataset = build_training_rows()
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    ens = get_ensemble()
    metrics = ens.train(dataset)
    if metrics.get("status") == "trained":
        ens.save()
    return metrics


# --- CLI ---

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="ML Ensemble: Poisson + Logistic Regression")
    ap.add_argument("--predict", type=str, default=None,
                    help="Predici su un match_id (usa dati dal DB)")
    ap.add_argument("--retrain", action="store_true",
                    help="Ri-allena il modello")
    ap.add_argument("--stats", action="store_true",
                    help="Mostra statistiche modello")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.retrain:
        print("🔄 Addestramento ensemble ML...")
        metrics = train_ensemble()
        print(json.dumps(metrics, indent=2, ensure_ascii=False))
        return 0

    if args.predict:
        from tracker import get_analysis_for_match
        analysis = get_analysis_for_match(args.predict)
        if not analysis:
            print(f"❌ Nessuna analisi trovata per {args.predict}")
            return 1
        # Costruisci row dal DB
        row = {
            "lam_h": analysis[1], "lam_a": analysis[2],
            "prob_1": analysis[3], "prob_X": analysis[4],
            "prob_2": analysis[5], "prob_over": analysis[6],
            "quota": analysis[9], "prob": analysis[7],
            "ev": analysis[7],  # approx
            "market_prob": analysis[13] if len(analysis) > 13 else None,
            "market_edge": analysis[14] if len(analysis) > 14 else None,
        }
        result = predict_ensemble(row)
        print(f"📊 Ensemble per {args.predict}:")
        print(f"   ML: {result['ml_prob']:.1%}")
        print(f"   Poisson: {result['poisson_prob']:.1%}")
        print(f"   Ensemble: {result['ensemble_prob']:.1%}")
        print(f"   Confidence: {result['confidence']:.1%}")
        return 0

    # Default: mostra stats
    ens = get_ensemble()
    if ens.trained:
        print("✅ Ensemble ML caricato:")
        print(f"   Peso ML: {ens.ensemble_weight:.1%}")
        print(f"   Metriche: {json.dumps(ens.train_metrics, indent=2)}")
    else:
        print("⚠️  Ensemble non addestrato. Esegui: venv/bin/python ml_ensemble.py --retrain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
