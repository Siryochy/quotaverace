"""secrets_store.py — Archivio segreti del bot, protetto e cifrato.

Due livelli di custodia, entrambi fuori dal repository (cartella gitignored):

1. **Plaintext** (sviluppo): `secrets/API_FOOTBALL_KEY` ... file per variabile
   con permessi 600 (lettura solo per l'utente del processo).
2. **Vault cifrato** (raccomandato): `secrets/vault.bin` contiene tutti i
   segreti in un unico blob Fernet (AES-128-CBC + HMAC), cifrato con una chiave
   derivata (PBKDF2-HMAC-SHA256) dalla passphrase segreta SECRETS_MASTER_KEY.
   Anche chi legge il filesystem vede solo ciphertext.

Regole comuni:
- Caricati SOLO in memoria (os.environ) al bootstrap; mai loggati, mai
  stampati, mai committati.
- MAI sovrascritti: precedenza env reale > .env > vault/plaintext.
- Se il vault esiste e la chiave e' disponibile, il vault e' l'unica fonte
  (i file plaintext residui vengono ignorati). Senza chiave si fallisce
  gentilmente ai file plaintext (es. ambiente senza SECRETS_MASTER_KEY).

CLI:
  python secrets_store.py vault [--commit]   # ricrea vault.bin dai file
                                             # plaintext; --commit li cancella
  python secrets_store.py check              # verifica che il vault decripti
  python secrets_store.py get NOME           # stampa UN segreto (uso interno,
                                             # es. askpass git)

Su Railway i segreti restano nelle variabili d'ambiente del progetto
(gestiti dal dashboard): il vault serve agli ambienti locali/agentici.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sys
import warnings
from pathlib import Path

logger = logging.getLogger("secrets_store")

SECRETS_DIR = Path(__file__).resolve().parent / "secrets"
VAULT_FILE = "vault.bin"
_SKIP_NAMES = (VAULT_FILE, "README", "README.md")
_VIT = 250_000


def _master_key() -> str:
    """Chiave maestra: env reale > .env del progetto (mai stampata)."""
    master = os.getenv("SECRETS_MASTER_KEY", "")
    if master:
        return master
    try:
        env_file = Path(__file__).resolve().parent / ".env"
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("SECRETS_MASTER_KEY="):
                return line[len("SECRETS_MASTER_KEY="):].strip()\
                    .strip('"').strip("'")
    except Exception:
        pass
    return master


def _fernet_from_master(master: str):
    """Chiave Fernet derivata dalla passphrase (PBKDF2-HMAC-SHA256)."""
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    digest = hashlib.sha256(master.encode()).digest()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=b"quotaverace:vault", iterations=_VIT)
    key = base64.urlsafe_b64encode(kdf.derive(digest))
    return Fernet(key)


def create_vault(master: str | None = None, secrets_dir: Path | str | None = None,
                 delete_plaintext: bool = False) -> int:
    """Cifra i file plaintext di secrets/ in secrets/vault.bin.

    Ritorna il numero di segreti cifrati. Con `delete_plaintext=True` i file
    sorgente vengono rimossi (la cartella resta solo con il vault).
    """
    master = master or _master_key()
    if not master:
        logger.error("vault: serve SECRETS_MASTER_KEY (env o --master)")
        raise ValueError("SECRETS_MASTER_KEY mancante")
    path = Path(secrets_dir) if secrets_dir else SECRETS_DIR
    path.mkdir(parents=True, exist_ok=True)

    payload: dict[str, str] = {}
    for p in sorted(path.iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.name in _SKIP_NAMES:
            continue
        value = p.read_text(encoding="utf-8").strip()
        if value:
            payload[p.name] = value
    if not payload:
        logger.warning("vault: nessun segreto plaintext da cifrare")
        return 0

    f = _fernet_from_master(master)
    token = f.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    vault = path / VAULT_FILE
    vault.write_bytes(token)
    try:
        vault.chmod(0o600)
    except Exception:
        pass

    if delete_plaintext:
        for name in payload:
            (path / name).unlink(missing_ok=True)
    logger.info("vault: cifrati %d segreti in %s", len(payload), vault.name)
    return len(payload)


def load_vault(master: str | None = None,
               secrets_dir: Path | str | None = None) -> dict[str, str]:
    """Decripta vault.bin e ritorna {NOME: valore}. {} se assente o chiave errata."""
    master = master if master is not None else _master_key()
    path = Path(secrets_dir) if secrets_dir else SECRETS_DIR
    vault = path / VAULT_FILE
    if not vault.exists():
        return {}
    if not master:
        logger.warning("vault: vault.bin presente ma SECRETS_MASTER_KEY mancante — "
                       "fallback ai file plaintext")
        return {}
    try:
        f = _fernet_from_master(master)
        raw = f.decrypt(vault.read_bytes())
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        logger.warning("vault: decrittazione fallita (%s) — chiave errata o file "
                       "alterato; fallback ai file plaintext", type(e).__name__)
        return {}


def _load_plaintext(path: Path) -> int:
    loaded = 0
    for p in sorted(path.iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.name in _SKIP_NAMES:
            continue
        if os.getenv(p.name) is not None:
            continue
        try:
            value = p.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning("secrets_store: impossibile leggere %s: %s", p.name, e)
            continue
        if not value:
            continue
        try:
            mode = p.stat().st_mode & 0o777
            if mode & 0o077:
                logger.warning("secrets_store: permessi %o su %s — usa chmod 600",
                               mode, p.name)
        except Exception:
            pass
        os.environ.setdefault(p.name, value)
        loaded += 1
    return loaded


def load_secrets_dir(secrets_dir: Path | str | None = None) -> int:
    """Carica in os.environ i segreti disponibili (setdefault, mai override).

    Ordine con cui popola SOLO le variabili mancanti:
      1. vault cifrato (se chiave disponibile) — fonte raccomandata;
      2. file plaintext (se il vault manca o la chiave non decripta).

    Ritorna il numero di variabili caricate.
    """
    path = Path(secrets_dir) if secrets_dir else SECRETS_DIR
    if not path.is_dir():
        return 0

    loaded = 0
    vault = path / VAULT_FILE
    if vault.exists():
        for name, value in load_vault(secrets_dir=path).items():
            if os.getenv(name) is None and value:
                os.environ.setdefault(name, value)
                loaded += 1
    return loaded + _load_plaintext(path)


def get_secret(name: str, master: str | None = None) -> str:
    """Ritorna il valore di un segreto (vault preferito, poi plaintext)."""
    vault = load_vault(master)
    if name in vault:
        return vault[name]
    p = SECRETS_DIR / name
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return os.getenv(name, "")


def check_vault(master: str | None = None) -> dict:
    vault = load_vault(master)
    return {"ok": bool(vault), "n_segreti": len(vault),
            "nomi": sorted(vault.keys())}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = sys.argv[1:]
    if args and args[0] == "vault":
        commit = "--commit" in args
        n = create_vault(delete_plaintext=commit)
        print(f"✅ vault aggiornato con {n} segreti"
              + (" (file plaintext rimossi)" if commit else ""))
    elif args and args[0] == "check":
        r = check_vault()
        if r["ok"]:
            print(f"✅ vault OK: {r['n_segreti']} segreti → {', '.join(r['nomi'])}")
        else:
            print("❌ vault non decriptabile (chiave mancante o errata)")
    elif args and args[0] == "get" and len(args) > 1:
        sys.stdout.write(get_secret(args[1]))
    else:
        print(__doc__)