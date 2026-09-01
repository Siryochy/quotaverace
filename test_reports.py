"""Test destinatari report: parsing ADMIN_CHAT_ID."""
import bot


def test_admin_chat_ids_parsing(monkeypatch):
    monkeypatch.setenv("ADMIN_CHAT_ID", "123456789, -987654321, abc")
    assert bot._admin_chat_ids() == [123456789, -987654321]


def test_admin_chat_ids_empty(monkeypatch):
    monkeypatch.delenv("ADMIN_CHAT_ID", raising=False)
    assert bot._admin_chat_ids() == []


def test_admin_chat_ids_negative_only(monkeypatch):
    monkeypatch.setenv("ADMIN_CHAT_ID", "-1001234567890")
    assert bot._admin_chat_ids() == [-1001234567890]
