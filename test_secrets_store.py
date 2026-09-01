"""Test per secrets_store: caricamento, precedenze e file ignorati."""
import os

import pytest

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


def test_vault_roundtrip(tmp_path, monkeypatch):
    """Crea il vault dai file plaintext e lo decripta con la stessa chiave."""
    monkeypatch.delenv("SECRETS_MASTER_KEY", raising=False)
    (tmp_path / "TKN_D").write_text("valore-segreto-d\n")
    (tmp_path / "TKN_E").write_text("valore-segreto-e")
    (tmp_path / "README.md").write_text("non deve finire nel vault")

    n = secrets_store.create_vault(master="chiave-maestra", secrets_dir=tmp_path)
    assert n == 2
    data = secrets_store.load_vault(master="chiave-maestra", secrets_dir=tmp_path)
    assert data == {"TKN_D": "valore-segreto-d", "TKN_E": "valore-segreto-e"}


def test_vault_wrong_key_returns_empty(tmp_path):
    """Chiave sbagliata → vault non decriptabile → fallback pulito."""
    (tmp_path / "TKN_F").write_text("x")
    secrets_store.create_vault(master="giusta", secrets_dir=tmp_path)
    assert secrets_store.load_vault(master="sbagliata", secrets_dir=tmp_path) == {}


def test_vault_requires_master_key(tmp_path, monkeypatch):
    """Senza chiave maestra il vault non si crea (ValueError)."""
    monkeypatch.setattr(secrets_store, "_master_key", lambda: "")
    (tmp_path / "TKN_G").write_text("x")
    with pytest.raises(ValueError):
        secrets_store.create_vault(master="", secrets_dir=tmp_path)


def test_vault_commit_deletes_plaintext(tmp_path):
    """Con --commit i file plaintext sorgente vengono rimossi."""
    (tmp_path / "TKN_H").write_text("valore")
    secrets_store.create_vault(master="k", secrets_dir=tmp_path, delete_plaintext=True)
    assert not (tmp_path / "TKN_H").exists()
    assert (tmp_path / secrets_store.VAULT_FILE).exists()


def test_load_prefers_vault_over_plaintext(tmp_path, monkeypatch):
    """Se il vault decripta, ha precedenza sui file plaintext residui."""
    monkeypatch.delenv("TKN_I", raising=False)
    monkeypatch.setenv("SECRETS_MASTER_KEY", "k")
    (tmp_path / "TKN_I").write_text("valore-plain")
    secrets_store.create_vault(master="k", secrets_dir=tmp_path, delete_plaintext=True)
    # rigenera un plaintext differente dopo il vault: non deve vincere
    (tmp_path / "TKN_I").write_text("valore-falso")
    n = secrets_store.load_secrets_dir(tmp_path)
    assert n == 1
    assert os.environ["TKN_I"] == "valore-plain"


def test_get_secret_from_vault(tmp_path, monkeypatch):
    """get_secret legge dal vault quando la chiave e' disponibile."""
    (tmp_path / "TKN_J").write_text("valore-vault")
    secrets_store.create_vault(master="k", secrets_dir=tmp_path, delete_plaintext=True)
    monkeypatch.setattr(secrets_store, "SECRETS_DIR", tmp_path)
    monkeypatch.setenv("SECRETS_MASTER_KEY", "k")
    assert secrets_store.get_secret("TKN_J") == "valore-vault"