"""Test secure_logging: nessun segreto nei log (token Telegram, API key)
e setup idempotente.

Regressione 01/09: httpx loggava a INFO gli URL delle richieste Telegram,
che contengono il token del bot in chiaro nei log Railway.
"""

import logging

import secure_logging
from secure_logging import SensitiveDataFilter, collect_secrets, setup


def test_scrub_maschera_token_telegram():
    msg = ("HTTP Request: POST https://api.telegram.org/bot"
           "8372645521:AAEOEM3lSXAT4e3wTpqMKnhjsa5OZFpv-2U/getUpdates")
    out = SensitiveDataFilter.scrub(msg, set())
    assert "AAEOEM3lSX" not in out
    assert out.startswith("HTTP Request: POST https://api.telegram.org/bot***")


def test_scrub_maschera_hex_key():
    out = SensitiveDataFilter.scrub("key df59346f72e7c52b910f56a30f6011f4 non valida", set())
    assert "df59346f" not in out
    assert "***" in out


def test_scrub_maschera_valori_env():
    filt = SensitiveDataFilter()
    filt._secrets = {"supersecretvalue123"}
    record = logging.LogRecord("x", logging.INFO, "f", 1,
                               "token=%s ok", ("supersecretvalue123",), None)
    assert filt.filter(record) is True
    assert "supersecretvalue123" not in record.getMessage()


def test_collect_secrets_legge_env(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "abcdefgh12345678")
    monkeypatch.delenv("QUOTAVERACE_BOT_TOKEN", raising=False)
    s = collect_secrets()
    assert "abcdefgh12345678" in s


def test_setup_httpx_a_warning():
    setup()
    assert logging.getLogger("httpx").level == logging.WARNING


def test_setup_idempotente_handler_root():
    root = logging.getLogger()
    n_before = len(root.handlers)
    setup()
    setup()
    assert len(root.handlers) == max(n_before, 1)


def test_filtro_su_handler_root_non_duplicato():
    setup()
    setup()
    root = logging.getLogger()
    for h in root.handlers:
        n = sum(isinstance(f, secure_logging.SensitiveDataFilter)
                for f in h.filters)
        assert n <= 1
