"""Test per secrets_store: caricamento, precedenze e file ignorati."""
import os

import secrets_store


def test_loads_values_from_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("TKN_A", raising=False)
    (tmp_path / "TKN_A").write_text("super-segreto\n")
    n = secrets_store.load_secrets_dir(tmp_path)
    assert n == 1
    assert os.environ["TKN_A"] == "super-segreto"


def test_never_overrides_existing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TKN_B", "gia-presente")
    (tmp_path / "TKN_B").write_text("altro-valore")
    n = secrets_store.load_secrets_dir(tmp_path)
    assert n == 0
    assert os.environ["TKN_B"] == "gia-presente"


def test_skips_hidden_and_readme(tmp_path):
    (tmp_path / ".nascosto").write_text("x")
    (tmp_path / "README.md").write_text("x")
    (tmp_path / "README").write_text("x")
    assert secrets_store.load_secrets_dir(tmp_path) == 0


def test_missing_dir_returns_zero(tmp_path):
    assert secrets_store.load_secrets_dir(tmp_path / "non-esiste") == 0


def test_skips_empty_values(tmp_path, monkeypatch):
    monkeypatch.delenv("TKN_C", raising=False)
    (tmp_path / "TKN_C").write_text("   \n")
    assert secrets_store.load_secrets_dir(tmp_path) == 0
    assert os.getenv("TKN_C") is None