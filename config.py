import os
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """Carica le variabili da un file .env (KEY=VALUE) senza sovrascrivere
    quelle gia' presenti nell'ambiente.

    Formato accettato:
      - righe vuote e commenti (iniziano con #) ignorati
      - KEY=VALUE, con spazio bianco attorno a KEY opzionale
    Nessuna dipendenza esterna: il progetto usa solo variabili d'ambiente.
    """
    env_file = Path(os.path.dirname(os.path.abspath(__file__))) / path
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key.upper(), value.strip())


def _get_env(name: str, default: str = "") -> str:
    """Legge una variabile d'ambiente tollerando spazi o newline nel nome."""
    exact = os.getenv(name)
    if exact is not None:
        return exact
    for key, value in os.environ.items():
        if key.strip() == name:
            return value
    return default


# Carica .env appena importato, cosi' TOKEN e le variabili lette dagli altri
# moduli (ODDS_API_KEY, API_FOOTBALL_KEY, ...) sono disponibili ovunque.
load_dotenv()


TOKEN = _get_env("QUOTAVERACE_BOT_TOKEN", "")
BANKROLL_DEFAULT = 100.0

# Directory dei dati persistenti (DB, data/, log, cache, kill-switch).
# Su Railway punta a /app/data: monta un Volume su QUOTAVERACE_DATA_DIR
# (o sul default /app/data) cosi' i dati sopravvivono ai redeploy. Non
# montare MAI sul volume la root di /app: Railway NON usa overlay e
# nasconderebbe i sorgenti (vedi DEPLOY.md §persistenza).
_DATA_DIR_ENV = _get_env("QUOTAVERACE_DATA_DIR", "")
DATA_DIR = Path(_DATA_DIR_ENV).resolve() if _DATA_DIR_ENV else Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
