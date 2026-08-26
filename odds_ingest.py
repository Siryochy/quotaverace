"""
odds_ingest.py – Ingestione e normalizzazione quote da file JSON

Legge un file JSON con schema normalizzato, lo trasforma in un DataFrame pandas
tipizzato e validato, pronto per l'analisi Poisson.

Schema JSON atteso (lista di oggetti):
    {
        "bookmaker":     str,
        "evento":        str,
        "sport":         str,
        "esito":         str,
        "quota_decimale": float  (> 1.0),
        "timestamp":     str     (ISO 8601, es. "2024-09-17T15:00:00Z")
    }

Colonne del DataFrame in uscita:
    bookmaker       object
    evento          object
    sport           category
    esito           object
    quota_decimale  float64
    timestamp       datetime64[ns, UTC]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)

REQUIRED_COLS = ["bookmaker", "evento", "sport", "esito", "quota_decimale", "timestamp"]


def load_odds(path: str) -> pd.DataFrame:
    """
    Carica e normalizza un file JSON di quote.

    Parametri
    ----------
    path : str
        Percorso del file JSON.

    Ritorna
    -------
    pd.DataFrame
        DataFrame tipizzato e validato.

    Solleva
    -------
    FileNotFoundError
        Se il file non esiste.
    ValueError
        Se una o più quote decimali sono <= 1.0.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File non trovato: {file_path.resolve()}")

    with file_path.open("r", encoding="utf-8") as fh:
        raw: List[Dict[str, Any]] = json.load(fh)

    if not isinstance(raw, list):
        raise ValueError("Il file JSON deve contenere una lista di oggetti.")

    df = pd.DataFrame(raw)

    # 1. Scarta righe con campi mancanti
    missing_mask = df[REQUIRED_COLS].isna().any(axis=1)
    dropped = df[missing_mask]
    if not dropped.empty:
        logger.warning(
            "Scartate %d righe su %d per campi mancanti: %s",
            len(dropped),
            len(df),
            dropped.index.tolist(),
        )
        df = df[~missing_mask].reset_index(drop=True)

    if df.empty:
        logger.warning("Nessuna riga valida rimasta dopo lo scarto.")
        return _build_typed_frame(df)

    # 2. Validazione quote decimali
    invalid = df["quota_decimale"] <= 1.0
    if invalid.any():
        bad_rows = df.loc[invalid, ["bookmaker", "evento", "esito", "quota_decimale"]]
        raise ValueError(
            f"Trovate {invalid.sum()} quote decimali <= 1.0. "
            f"Righe problematiche:\n{bad_rows.to_dict(orient='records')}"
        )

    # 3. Tipizzazione
    return _build_typed_frame(df)


def _build_typed_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Applica i tipi corretti al DataFrame."""
    if df.empty:
        return pd.DataFrame(columns=REQUIRED_COLS).astype(
            {
                "bookmaker": "object",
                "evento": "object",
                "sport": "category",
                "esito": "object",
                "quota_decimale": "float64",
                "timestamp": "datetime64[ns, UTC]",
            }
        )

    df = df.copy()
    df["sport"] = df["sport"].astype("category")
    df["quota_decimale"] = df["quota_decimale"].astype("float64")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    df = load_odds("data/odds_sample.json")
    print(df)
    print("\nTipi:\n", df.dtypes)
