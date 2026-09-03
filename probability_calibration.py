"""probability_calibration.py — Calibrazione isotonica numpy-only (PAVA).

Corregge l'overconfidence dei classificatori (XGBoost, Logistic Regression):
le probabilita' grezze di un modello addestrato con loss non calibrata
tendono a essere troppo vicine a 0/1. La calibrazione isotonica mappa gli
score del modello sulle frequenze EMPIRICHE osservate nel dataset storico,
senza assumere una forma parametrica (a differenza del Platt scaling).

Implementazione numpy-only (nessuna dipendenza da scikit-learn), coerente
con la filosofia del progetto:
  - PAVA (Pool Adjacent Violators) per la regressione isotonica;
  - interpolazione lineare ai bordi piatta (come sklearn);
  - metriche: Brier score e Expected Calibration Error (ECE);
  - persistenza JSON (to_dict/from_dict) dentro ensemble_model.json.

Uso (integrato in ml_ensemble.EnsemblePredictor):
  cal = IsotonicCalibrator()
  cal.fit(scores_cal, y_cal)          # score OUT-OF-SAMPLE del modello
  p = cal.predict(scores_new)          # probabilita' calibrate

Soglia minima di campioni per un calibratore affidabile (fit su un set
di calibrazione separato, MAI sugli stessi dati di training):
  MIN_CALIB_SAMPLES = 60  (30% ~ 18 punti di calibrazione con split 70/30)
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

# --- Config ---
MIN_CALIB_SAMPLES = 60   # campioni totali minimi per attivare la calibrazione
CALIB_FRACTION = 0.30    # frazione di campioni riservata al calibratore
CALIB_RANDOM_STATE = 42  # seed fisso: split riproducibile
ECE_BINS = 10            # bin per l'Expected Calibration Error


def isotonic_regression(y: np.ndarray) -> np.ndarray:
    """PAVA: serie monotona NON decrescente che minimizza SSE.

    Dato y (es. esiti 0/1 ordinati per score crescente), ritorna la
    versione piu' vicina in L2 che sia non decrescente, conservando la
    somma (le medie dei blocchi sono pesate per il numero di campioni).

    Args:
        y: array 1D di valori (tipicamente binari).

    Returns:
        Array 1D con la stessa lunghezza, monotono non decrescente.
    """
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n <= 1:
        return y.copy()

    # PAVA a stack (implementazione standard): ogni blocco e' (somma,
    # conteggio, inizio). Si fondono i blocchi adiacenti finche' le medie
    # non sono non decrescenti, conservando la somma (medie pesate).
    blocks: List[tuple] = []  # (somma, conteggio, inizio)
    for i in range(n):
        s = float(y[i])
        c = 1.0
        start = i
        while blocks and blocks[-1][0] / blocks[-1][1] > s / c:
            ps, pc, pstart = blocks.pop()
            s += ps
            c += pc
            start = pstart
        blocks.append((s, c, start))

    out = np.empty(n)
    for b, nxt in zip(blocks, blocks[1:] + [(0.0, 0.0, n)]):
        out[b[2]:nxt[2]] = b[0] / b[1]
    return out


class IsotonicCalibrator:
    """Mappa score grezzi -> probabilita' empiriche (monotona).

    Fit su score OUT-OF-SAMPLE (mai sugli stessi dati usati per allenare
    il modello base): altrimenti la calibrazione imparerebbe l'overfit e
    non correggerebbe nulla.

    Attributi:
        x_: punti di score (crescenti, senza duplicati)
        y_: probabilita' calibrate corrispondenti
        fitted_: True dopo fit()
        min_score_ / max_score_: bordi per l'estrapolazione piatta
    """

    def __init__(self):
        self.x_: Optional[np.ndarray] = None
        self.y_: Optional[np.ndarray] = None
        self.fitted_: bool = False

    def fit(self, scores: np.ndarray, y: np.ndarray) -> "IsotonicCalibrator":
        """Adatta la curva isotonica su (score, esiti reali).

        Args:
            scores: probabilita' grezze del modello (out-of-sample).
            y: esito binario (1=vinta, 0=persa).

        Returns:
            self (per il chaining).
        """
        scores = np.asarray(scores, dtype=float).ravel()
        y = np.asarray(y, dtype=float).ravel()
        if len(scores) != len(y) or len(scores) == 0:
            raise ValueError("scores e y devono avere la stessa lunghezza > 0")

        order = np.argsort(scores)
        xs = scores[order]
        ys = y[order]
        y_iso = isotonic_regression(ys)

        # Collassa i punti con lo stesso score (media dei valori isotonici):
        # np.interp richiede x strettamente crescente.
        uniq_x, first_idx = np.unique(xs, return_index=True)
        uniq_y = np.array([np.mean(y_iso[first_idx[j]:(
            first_idx[j + 1] if j + 1 < len(first_idx) else len(y_iso))])
            for j in range(len(first_idx))])

        self.x_ = uniq_x
        self.y_ = uniq_y
        self.fitted_ = True
        return self

    def predict(self, scores: np.ndarray) -> np.ndarray:
        """Probabilita' calibrate (interpolazione lineare, piatta ai bordi)."""
        scores = np.asarray(scores, dtype=float).ravel()
        if not self.fitted_ or self.x_ is None or self.y_ is None:
            return scores
        return np.interp(scores, self.x_, self.y_)

    def to_dict(self) -> Dict:
        """Serializza per ensemble_model.json."""
        return {
            "x": self.x_.tolist() if self.x_ is not None else [],
            "y": self.y_.tolist() if self.y_ is not None else [],
            "fitted": self.fitted_,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict]) -> Optional["IsotonicCalibrator"]:
        """Ricostruisce da dict (None -> None)."""
        if not d or not d.get("fitted"):
            return None
        cal = cls()
        cal.x_ = np.array(d.get("x", []), dtype=float)
        cal.y_ = np.array(d.get("y", []), dtype=float)
        cal.fitted_ = bool(d.get("fitted", False))
        return cal if cal.fitted_ and len(cal.x_) > 0 else None


# --- Metriche di calibrazione ---

def brier_score(scores: np.ndarray, y: np.ndarray) -> float:
    """Brier: media di (prob - esito)^2. 0 = perfetto, 0.25 = random."""
    return float(np.mean((np.asarray(scores, float) - np.asarray(y, float)) ** 2))


def expected_calibration_error(scores: np.ndarray, y: np.ndarray,
                               n_bins: int = ECE_BINS) -> float:
    """ECE: deviazione media pesata tra confidenza e accuratezza per bin.

    ECE = sum_b (n_b / N) * |conf_b - acc_b|. 0 = calibrazione perfetta.
    """
    scores = np.asarray(scores, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    n = len(scores)
    if n == 0:
        return 0.0
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        if hi < 1.0:
            mask = (scores >= lo) & (scores < hi)
        else:
            mask = (scores >= lo) & (scores <= hi)
        m = int(mask.sum())
        if m == 0:
            continue
        conf = float(scores[mask].mean())
        acc = float(y[mask].mean())
        ece += (m / n) * abs(conf - acc)
    return ece


def calibration_report(scores_raw: np.ndarray, y: np.ndarray,
                       calibrator: Optional[IsotonicCalibrator]) -> Dict:
    """Metriche di calibrazione PRE/POST per il log e il report.

    Args:
        scores_raw: score del modello (out-of-sample).
        y: esiti binari.
        calibrator: calibratore gia' fit sugli stessi score.

    Returns:
        Dict con brier/ece pre e post (se calibrator disponibile).
    """
    pre_brier = brier_score(scores_raw, y)
    pre_ece = expected_calibration_error(scores_raw, y)
    report = {
        "pre_brier": round(pre_brier, 4),
        "pre_ece": round(pre_ece, 4),
        "n": int(len(y)),
    }
    if calibrator is not None and calibrator.fitted_:
        post = calibrator.predict(scores_raw)
        report["post_brier"] = round(brier_score(post, y), 4)
        report["post_ece"] = round(expected_calibration_error(post, y), 4)
        report["brier_improvement"] = round(pre_brier - report["post_brier"], 4)
    return report


if __name__ == "__main__":
    # Self-test CLI: verifica che l'isotonica corregga un overconfidence
    # sintetico (logit amplificato -> score compressi verso 0/1).
    rng = np.random.RandomState(0)
    n = 400
    p = rng.uniform(0.25, 0.85, n)
    y = (rng.random(n) < p).astype(float)
    z = np.log(p / (1.0 - p)) * 1.8  # overconfident
    scores = 1.0 / (1.0 + np.exp(-z))
    cal = IsotonicCalibrator().fit(scores, y)
    rep = calibration_report(scores, y, cal)
    print(f"campioni: {n}")
    print(f"Brier pre:  {rep['pre_brier']:.4f}  ECE pre:  {rep['pre_ece']:.4f}")
    print(f"Brier post: {rep['post_brier']:.4f}  ECE post: {rep['post_ece']:.4f}")
    print(f"Miglioramento Brier: {rep['brier_improvement']:+.4f}")