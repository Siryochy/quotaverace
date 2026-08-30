import os

def _get_env(name: str, default: str = "") -> str:
    """Legge una variabile d'ambiente tollerando spazi o newline nel nome."""
    exact = os.getenv(name)
    if exact is not None:
        return exact
    for key, value in os.environ.items():
        if key.strip() == name:
            return value
    return default


TOKEN = _get_env("QUOTAVERACE_BOT_TOKEN", "")
BANKROLL_DEFAULT = 100.0
