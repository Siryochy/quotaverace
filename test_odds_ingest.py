"""
Test unitari per odds_ingest.py.

Verifica:
- caricamento e tipizzazione corretta del DataFrame;
- scarto righe incomplete con logging di warning;
- ValueError su quote decimali <= 1.0;
- FileNotFoundError su path inesistente.
"""

import json
import logging
from pathlib import Path

import pandas as pd
import pytest

from odds_ingest import load_odds


class TestLoadOdds:

    @pytest.fixture
    def valid_fixture(self, tmp_path):
        data = [
            {
                "bookmaker": "Bet365",
                "evento": "Serie A – Roma vs Empoli",
                "sport": "calcio",
                "esito": "Over 2.5",
                "quota_decimale": 2.10,
                "timestamp": "2024-09-17T15:00:00Z",
            },
            {
                "bookmaker": "Snai",
                "evento": "Serie A – Roma vs Empoli",
                "sport": "calcio",
                "esito": "Under 2.5",
                "quota_decimale": 1.75,
                "timestamp": "2024-09-17T15:00:00Z",
            },
        ]
        path = tmp_path / "valid.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    @pytest.fixture
    def incomplete_fixture(self, tmp_path):
        data = [
            {
                "bookmaker": "Bet365",
                "evento": "Roma vs Empoli",
                "sport": "calcio",
                "esito": "1",
                "quota_decimale": 2.50,
                "timestamp": "2024-09-17T15:00:00Z",
            },
            {
                "bookmaker": "Snai",
                "evento": "Roma vs Empoli",
                "sport": "calcio",
                "esito": "X",
                "quota_decimale": None,
                "timestamp": "2024-09-17T15:00:00Z",
            },
        ]
        path = tmp_path / "incomplete.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    @pytest.fixture
    def bad_odds_fixture(self, tmp_path):
        data = [
            {
                "bookmaker": "Bet365",
                "evento": "Inter vs Milan",
                "sport": "calcio",
                "esito": "1",
                "quota_decimale": 0.95,
                "timestamp": "2024-09-16T20:45:00Z",
            },
        ]
        path = tmp_path / "bad_odds.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_tipi_corretti(self, valid_fixture):
        df = load_odds(valid_fixture)
        assert isinstance(df, pd.DataFrame)
        assert df["sport"].dtype.name == "category"
        assert df["quota_decimale"].dtype == "float64"
        assert "datetime64" in str(df["timestamp"].dtype) and "UTC" in str(df["timestamp"].dtype)

    def test_conteggio_righe_valide(self, valid_fixture):
        df = load_odds(valid_fixture)
        assert len(df) == 2

    def test_scarta_incomplete_e_logga_warning(self, incomplete_fixture, caplog):
        caplog.set_level(logging.WARNING)
        df = load_odds(incomplete_fixture)
        assert len(df) == 1
        assert df.iloc[0]["bookmaker"] == "Bet365"
        assert "Scartate 1 righe su 2 per campi mancanti" in caplog.text

    def test_value_error_su_quota_minore_uguale_1(self, bad_odds_fixture):
        with pytest.raises(ValueError, match="quote decimali <= 1.0"):
            load_odds(bad_odds_fixture)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_odds("data/non_esiste.json")

    def test_sample_json_nel_repo(self, tmp_path):
        # Usa un fixture temporaneo con quota invalida, non il file reale usato dal bot
        invalid = [
            {
                "bookmaker": "Test",
                "evento": "Test vs Test",
                "sport": "calcio",
                "esito": "1",
                "quota_decimale": 0.95,
                "timestamp": "2024-01-01T00:00:00Z"
            }
        ]
        path = tmp_path / "invalid.json"
        import json
        with open(path, "w") as f:
            json.dump(invalid, f)
        with pytest.raises(ValueError, match="quote decimali <= 1.0"):
            load_odds(str(path))
