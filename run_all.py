"""Entrypoint unificato: avvia web_api (HTTP) e bot (Telegram polling) nello
stesso processo, così condividono un unico volume su /app/data (Railway non
supporta volumi condivisi fra servizi).

Usato come CMD del Dockerfile unico (o via Railway startCommand):
    python run_all.py
"""

import logging
import threading

import web_api


def _serve_api() -> None:
    """Avvia il server HTTP di web_api in un thread demone dedicato."""
    try:
        web_api.main()
    except Exception:
        logging.exception("Server web_api terminato con errore")


def main() -> None:
    import secure_logging
    secure_logging.setup()  # maschera segreti nei log + httpx a WARNING
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # init DB condiviso prima di tutto
    from tracker import init_db
    init_db()

    api_thread = threading.Thread(target=_serve_api, name="web_api", daemon=True)
    api_thread.start()
    logging.info("web_api avviato in thread separato (PORT=%s)", web_api.PORT)

    import bot
    bot.main()  # bloccante: run_polling


if __name__ == "__main__":
    main()