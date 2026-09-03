"""Logging sicuro: nessun segreto nei log di Railway.

Il bug del 01/09: httpx (usato da python-telegram-bot) logga l'URL completo
delle richieste, incluso il token del bot:
    POST https://api.telegram.org/bot<TOKEN>/getUpdates
su Railway i log sono visibili a chiunque abbia accesso al progetto.

Difese (tutte attive di default via setup()):
  1. SensitiveDataFilter: filtro logging che maschera i valori dei segreti
     noti (dagli env var sensibili) e i pattern di token riconoscibili.
  2. httpx e python-telegram-bot a WARNING: gli HttpRequestInfoLogged di
     httpx (che contengono l'URL col token) non passano piu' a INFO.
  3. urllib3 a WARNING (riduce rumore, nessun URL di richieste in log).

Chiamare setup() al bootstrap di OGNI entrypoint (run_all, web_api, bot).
Idempotente: aggiunge il filtro una sola volta.
"""
import logging
import os
import re

logger = logging.getLogger(__name__)

# Variabili d'ambiente i cui valori sono segreti da non stampare mai.
SECRET_ENV_KEYS = (
    "QUOTAVERACE_BOT_TOKEN", "TELEGRAM_BOT_TOKEN",
    "ODDS_API_KEY", "API_FOOTBALL_KEY", "GITHUB_TOKEN",
    "GOOGLE_API_KEY", "SECRETS_MASTER_KEY",
    "TEST_NOTIFY_KEY", "RAILWAY_TOKEN",
)

# Fallback: pattern di segreti riconoscibili anche se la variabile non e'
# tra quelle sopra (nuove chiavi, token passati per errore).
# NB: niente \b iniziale nel token Telegram: negli URL e' attaccato a 'bot'
# (api.telegram.org/bot<TOKEN>/getUpdates) e li' non c'e' word boundary.
_BOT_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{25,}")  # token Telegram
_HEX_KEY_RE = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)     # chiavi hex 32

_REDACTED = "***REDACTED***"


def collect_secrets() -> set[str]:
    """Valori dei segreti presenti nell'ambiente (filtrati per lunghezza minima
    per non mascherare stringhe banali tipo '1' o 'true')."""
    secrets = set()
    for key in SECRET_ENV_KEYS:
        val = os.getenv(key, "")
        if val and len(val) >= 8:
            secrets.add(val)
    return secrets


class SensitiveDataFilter(logging.Filter):
    """Maschera i segreti nei messaggi di log (record.getMessage())."""

    def __init__(self):
        super().__init__()
        self._secrets: set[str] = set()

    def refresh(self) -> None:
        """Ricarica i segreti dall'ambiente (chiamare dopo load_secrets_dir)."""
        self._secrets = collect_secrets()

    @staticmethod
    def scrub(text: str, secrets: set[str]) -> str:
        for s in secrets:
            if s in text:
                text = text.replace(s, _REDACTED)
        # Pattern generici (anche per segreti non in env al momento del log)
        text = _BOT_TOKEN_RE.sub(_REDACTED, text)
        text = _HEX_KEY_RE.sub(_REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            self.refresh()
        try:
            msg = record.getMessage()
        except Exception:
            return True  # mai bloccare il logging per un formato rotto
        redacted = self.scrub(msg, self._secrets)
        if redacted != msg:
            record.msg = redacted
            record.args = None
        # Gli URL delle richieste httpx contengono il token: maschera anche
        # gli args (es. logger.info("%s", url)).
        if record.args:
            try:
                record.args = tuple(
                    self.scrub(a, self._secrets) if isinstance(a, str) else a
                    for a in record.args
                )
            except Exception:
                pass
        return True


_filter_instance: SensitiveDataFilter | None = None


def _get_filter() -> SensitiveDataFilter:
    global _filter_instance
    if _filter_instance is None:
        _filter_instance = SensitiveDataFilter()
    return _filter_instance


def _apply_to_root() -> None:
    """Attacca il filtro a tutti gli handler del root logger (e ai logger
    senza handler propri, che propagano al root)."""
    flt = _get_filter()
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(f, SensitiveDataFilter) for f in h.filters):
            h.addFilter(flt)
    # Filtro anche sul logger di libreria che ha handler propri (raro, ma sicuro)
    for name in ("httpx", "httpcore", "urllib3", "telegram"):
        lg = logging.getLogger(name)
        for h in lg.handlers:
            if not any(isinstance(f, SensitiveDataFilter) for f in h.filters):
                h.addFilter(flt)


def setup(level: int = logging.INFO) -> None:
    """Configura il logging sicuro per l'entrypoint. Da chiamare al bootstrap.

    - root handler con formato standard + SensitiveDataFilter
    - libreria httpx (e affini) a WARNING: niente URL col token a INFO
    """
    flt = _get_filter()
    flt.refresh()
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            level=level,
        )
    else:
        root.setLevel(level)
    _apply_to_root()
    # httpx logga a INFO ogni richiesta con URL completo (che per Telegram
    # include il token): a WARNING sparisce. I messaggi httpx che passano
    # sono comunque mascherati da SensitiveDataFilter.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def refresh_secrets() -> None:
    """Ricarica i segreti nel filtro (dopo load_secrets_dir tardivo)."""
    if _filter_instance is not None:
        _filter_instance.refresh()
