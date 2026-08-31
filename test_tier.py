"""Test sistema free/premium: tier, scadenza abbonamento, filtri.

Copre le funzioni di tracker.py aggiunte per la monetizzazione:
add_subscriber(tier), set_tier, get_subscription, is_premium,
get_subscribers(tier=...).
"""
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tracker


@pytest.fixture()
def temp_db(monkeypatch):
    """DB SQLite temporaneo per isolare i test."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def test_add_subscriber_default_free(temp_db):
    tracker.add_subscriber(111)
    assert tracker.get_subscription(111) == ("free", None)
    assert tracker.is_premium(111) is False


def test_add_subscriber_premium_tier(temp_db):
    tracker.add_subscriber(222, tier="premium")
    # premium senza scadenza resta premium
    tier, until = tracker.get_subscription(222)
    assert tier == "premium"
    assert tracker.is_premium(222) is True


def test_add_subscriber_idempotent(temp_db):
    """Re-iscriversi non deve degradare un premium esistente."""
    tracker.add_subscriber(333, tier="premium")
    tracker.add_subscriber(333)  # INSERT OR IGNORE: non tocca il tier
    assert tracker.is_premium(333) is True


def test_set_tier_premium_with_expiry(temp_db):
    tracker.add_subscriber(444)
    future = (datetime.now() + timedelta(days=30)).isoformat()
    tracker.set_tier(444, "premium", future)
    assert tracker.is_premium(444) is True
    tier, until = tracker.get_subscription(444)
    assert tier == "premium"
    assert until == future


def test_premium_expired_degrades_to_free(temp_db):
    tracker.add_subscriber(555)
    past = (datetime.now() - timedelta(days=1)).isoformat()
    tracker.set_tier(555, "premium", past)
    tier, _ = tracker.get_subscription(555)
    assert tier == "free"
    assert tracker.is_premium(555) is False


def test_premium_no_expiry_never_expires(temp_db):
    tracker.add_subscriber(666, tier="premium")
    assert tracker.is_premium(666) is True


def test_get_subscription_unknown_chat(temp_db):
    assert tracker.get_subscription(999999) is None
    assert tracker.is_premium(999999) is False


def test_get_subscribers_all(temp_db):
    tracker.add_subscriber(1)
    tracker.add_subscriber(2, tier="premium")
    subs = tracker.get_subscribers()
    assert sorted(subs) == [1, 2]


def test_get_subscribers_premium_filter(temp_db):
    tracker.add_subscriber(10)  # free
    tracker.add_subscriber(20, tier="premium")
    tracker.add_subscriber(30)
    future = (datetime.now() + timedelta(days=7)).isoformat()
    tracker.set_tier(30, "premium", future)
    premium = tracker.get_subscribers(tier="premium")
    assert sorted(premium) == [20, 30]


def test_get_subscribers_premium_excludes_expired(temp_db):
    tracker.add_subscriber(40, tier="premium")
    past = (datetime.now() - timedelta(hours=2)).isoformat()
    tracker.set_tier(40, "premium", past)
    assert tracker.get_subscribers(tier="premium") == []
    # ma resta un iscritto (free degradato)
    assert 40 in tracker.get_subscribers()


def test_get_subscribers_free_filter(temp_db):
    tracker.add_subscriber(50)
    tracker.add_subscriber(60, tier="premium")
    free = tracker.get_subscribers(tier="free")
    assert free == [50]


def test_remove_subscriber(temp_db):
    tracker.add_subscriber(70)
    tracker.remove_subscriber(70)
    assert tracker.get_subscription(70) is None
    assert tracker.get_subscribers() == []


def test_malformed_expiry_degrades_to_free(temp_db):
    """Scadenza non ISO: comportamento prudente = degrada a free."""
    tracker.add_subscriber(80, tier="premium")
    import sqlite3
    conn = sqlite3.connect(str(temp_db))
    conn.execute("UPDATE subscribers SET premium_until='non-una-data' WHERE chat_id=80")
    conn.commit(); conn.close()
    assert tracker.is_premium(80) is False
