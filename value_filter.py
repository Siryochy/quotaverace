"""
value_filter.py – Filtro value bet da DataFrame quote e probabilità Poisson

Prende in input:
    • DataFrame normalizzato da odds_ingest (colonne: evento, esito, quota_decimale, ...)
    • DataFrame probabilità stimate da poisson_engine (colonne: evento, esito, probabilità)

Restituisce:
    • DataFrame dei value bet candidati con EV > soglia configurabile.
"""

from __future__ import annotations

import pandas as pd


def compute_ev(prob: float, odds: float) -> float:
    """
    Calcola l'Expected Value (EV) di una scommessa.

    Formula
    -------
    EV = (probabilità × quota) − 1

    Parametri
    ----------
    prob : float
        Probabilità stimata dell'esito (0–1).
    odds : float
        Quota decimale offerta dal bookmaker (> 1.0).

    Ritorna
    -------
    float
        Valore atteso espresso in unità (es. 0.10 = +10 %).
    """
    return prob * odds - 1.0


def filter_value_bets(
    odds_df: pd.DataFrame,
    probs_df: pd.DataFrame,
    threshold: float = 0.05,
) -> pd.DataFrame:
    """
    Unisce quote e probabilità, calcola EV e filtra i value bet.

    Parametri
    ----------
    odds_df : pd.DataFrame
        DataFrame con colonne minime: evento, esito, quota_decimale, sport, timestamp.
    probs_df : pd.DataFrame
        DataFrame con colonne: evento, esito, probabilità.
    threshold : float, default 0.05
        Soglia minima di EV per considerare un segnale value bet (es. 0.05 = 5 %).

    Ritorna
    -------
    pd.DataFrame
        DataFrame filtrato con colonne:
        sport, evento, esito, quota_decimale, probabilità, ev, timestamp.
        Ordinato per EV decrescente.
    """
    # Merge inner su evento + esito
    merged = odds_df.merge(
        probs_df,
        on=["evento", "esito"],
        how="inner",
        suffixes=("", "_prob"),
    )

    if merged.empty:
        return pd.DataFrame(
            columns=["sport", "evento", "esito", "quota_decimale",
                     "probabilità", "ev", "timestamp"]
        )

    # Calcola EV
    merged["ev"] = merged.apply(
        lambda row: compute_ev(row["probabilità"], row["quota_decimale"]),
        axis=1,
    )

    # Filtra per soglia
    mask = merged["ev"] > threshold
    value_bets = merged.loc[mask].copy()

    # Seleziona e rinomina colonne
    cols_out = ["sport", "evento", "esito", "quota_decimale",
                "probabilità", "ev", "timestamp"]
    cols_out = [c for c in cols_out if c in value_bets.columns]
    value_bets = value_bets[cols_out]

    # Ordina per EV decrescente
    value_bets = value_bets.sort_values("ev", ascending=False).reset_index(drop=True)

    return value_bets
