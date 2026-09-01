"""secrets_store.py — Archivio segreti del bot, protetto e fuori dal repo.

I segreti (token, chiavi API) vivono in `secrets/` (cartella gitignored,
permessi consigliati 600 sul file, 700 sulla cartella):

  secrets/
    QUOTAVERACE_BOT_TOKEN      <- nome file = nome variabile
    API_FOOTBALL_KEY           <- contenuto = valore (una riga)
    ...

Regole:
- Caricati SOLO in memoria al bootstrap (os.environ), mai loggati, mai
  stampati, mai committati (cartella in .gitignore).
- MAI sovrascritti: precedenza env reale > .env > cartella secrets.
- Avviso (warning) se i permessi del file sono piu' aperti di 600.

Su Railway i segreti restano nelle variabili d'ambiente del progetto
(gestiti dal dashboard, protetti dal login): la cartella qui serve agli
ambienti locali/agentici dove il filesystem e' l'unico canale.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("secrets_store")

SECRETS_DIR = Path(__file__).resolve().parent / "secrets"


def load_secrets_dir(secrets_dir: Path | str | None = None) -> int:
    """Carica in os.environ i segreti della cartella (setdefault).

    Passa SOLO i valori mancanti: non tocca mai le variabili gia' presenti
    nell'ambiente reale o nel .env. Ritorna il numero di variabili caricate.
    """
    path = Path(secrets_dir) if secrets_dir else SECRETS_DIR
    if not path.is_dir():
        return 0
    loaded = 0
    for p in sorted(path.iterdir()):
        if not p.is_file():
            continue
        if p.name.startswith(".") or p.name in ("README", "README.md"):
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
            if mode & 0o077:  # leggibile/scrivibile da altri
                logger.warning(
                    "secrets_store: permessi %o su %s — usa chmod 600",
                    mode, p.name)
        except Exception:
            pass
        os.environ.setdefault(p.name, value)
        loaded += 1
    if loaded:
        logger.info("secrets_store: caricati %d segreti da %s", loaded, path)
    return loaded