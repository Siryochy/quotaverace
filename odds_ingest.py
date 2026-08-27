"""Carica quote statiche da file JSON (senza pandas)"""
import json
import os
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

def load_odds(filepath=None):
    if filepath is None:
        filepath = DATA_DIR / "odds_sample.json"
    if not os.path.exists(filepath):
        return _default_odds()
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)

def _default_odds():
    return [
        {"evento": "Serie A – Roma vs Empoli", "esito": "Over 2.5", "quota_decimale": 2.10, "bookmaker": "Bet365", "probabilita": 0.0},
        {"evento": "Serie A – Inter vs Milan", "esito": "1", "quota_decimale": 1.90, "bookmaker": "Snai", "probabilita": 0.0},
        {"evento": "Serie A – Atalanta vs Milan", "esito": "1", "quota_decimale": 2.00, "bookmaker": "Bet365", "probabilita": 0.0},
    ]
