"""research_graph/_env.py — Bootstrap dell'ambiente per gli adapter REALI.

Convenzione del progetto (vedi `config.py`): le chiavi vivono nel `.env`
gitignored e/o nella cartella cifrata `secrets/` (vault), mai nel codice.
L'import di `config` fa partire il bootstrap (`load_dotenv()` + `load_secrets_dir()`).

Lo facciamo in modo PIGRO e idempotente: `import research_graph` resta senza
side effect (nessuna cartella `data/` creata, nessun file letto) e l'ambiente
viene preparato solo quando un adapter prova davvero a usare una credenziale.
Fail-safe: se il bootstrap non e' disponibile, si prosegue (l'adapter dira'
chiaramente che la chiave manca).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("research_graph")

_loaded = False


def ensure_env() -> None:
    """Carica `.env` + vault una volta sola (idempotente, mai eccezioni)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        import config  # noqa: F401 - l'import esegue load_dotenv()/vault
    except Exception as exc:  # pragma: no cover - difensivo
        logger.debug("bootstrap ambiente non disponibile: %s", exc)


__all__ = ["ensure_env"]
