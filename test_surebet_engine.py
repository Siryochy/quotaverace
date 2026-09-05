"""Test del modulo surebet_engine.py (arbitraggio h2h a 2 esiti, The Odds API).

Copre: trigger matematico (1/A+1/B<1), allocazione stake con
SUREBET_BUDGET, classificazione bookmaker soft/sharp, scan di un payload
the-odds-api reale, dedup, formato Telegram dedicato e payload webhook n8n.
Vincolo architetturale: nessun import da tracker/bot (indipendenza).
"""

import json

import pytest

import surebet_engine as se


# ---------------------------------------------------------------------------
# Trigger matematico
# ---------------------------------------------------------------------------

class TestInverseSum:
    def test_somma_inversi_surebet(self):
        # 1/2.20 + 1/2.30 = 0.4545 + 0.4348 = 0.8893 < 1
        inv = se.inverse_sum(2.20, 2.30)
        assert inv == pytest.approx(0.8893, abs=0.001)
        assert inv < 1.0

    def test_mercato_efficiente(self):
        assert se.inverse_sum(2.0, 2.0) == pytest.approx(1.0)

    def test_vantaggio_bookmaker(self):
        assert se.inverse_sum(1.90, 1.90) > 1.0

    def test_quote_invalide(self):
        assert se.inverse_sum(0.0, 2.0) is None
        assert se.inverse_sum(1.0, 2.0) is None
        assert se.inverse_sum(1.5, 0.9) is None


class TestIsArbitrage:
    def test_trigger_scatta(self):
        assert se.is_arbitrage(2.20, 2.30, min_margin=0.005)

    def test_trigger_non_scatta_con_margine_piccolo(self):
        # 1/1.99 + 1/2.01 = 0.5025 + 0.4975 = 1.0000
        assert not se.is_arbitrage(1.99, 2.01, min_margin=0.005)

    def test_trigger_min_margin_zero(self):
        # 1/2.005 + 1/2.005 ≈ 0.9975 < 1 → con min_margin=0 scatta
        assert se.is_arbitrage(2.01, 2.01, min_margin=0.0)


# ---------------------------------------------------------------------------
# Allocazione stake (SUREBET_BUDGET)
# ---------------------------------------------------------------------------

class TestComputeStakes:
    def test_allocazione_bilancia_profitto(self):
        stake_a, stake_b, profit, roi = se.compute_stakes(2.20, 2.30, budget=100)
        assert stake_a + stake_b == pytest.approx(100.0, abs=0.1)
        # profitto identico (entro arrotondamento) su entrambi gli esiti
        assert stake_a * 2.20 - 100 == pytest.approx(stake_b * 2.30 - 100, abs=0.2)
        assert profit > 0
        assert roi > 0

    def test_quota_piu_bassa_più_stake(self):
        stake_a, stake_b, *_ = se.compute_stakes(2.20, 2.30, budget=100)
        assert stake_a > stake_b  # quota 2.20 più bassa → stake più alto

    def test_budget_env_viene_usato(self, monkeypatch):
        monkeypatch.setenv("SUREBET_BUDGET", "500")
        # ricarica il modulo per applicare l'env
        import importlib
        mod = importlib.reload(se)
        stake_a, stake_b, profit, roi = mod.compute_stakes(2.20, 2.30, budget=500)
        assert stake_a + stake_b == pytest.approx(500.0, abs=0.5)

    def test_nessuna_allocazione_se_non_arbitrabile(self):
        assert se.compute_stakes(1.90, 1.90, budget=100) is None


# ---------------------------------------------------------------------------
# Classificazione bookmaker
# ---------------------------------------------------------------------------

class TestClassifyBookmaker:
    def test_pinnacle_sharp(self):
        assert se.classify_bookmaker("Pinnacle") == "sharp"

    def test_snai_soft(self):
        assert se.classify_bookmaker("Snai") == "soft"

    def test_goldbet_soft(self):
        assert se.classify_bookmaker("GoldBet") == "soft"

    def test_altri_soft_europei(self):
        for b in ("Bet365", "William Hill", "Bwin", "Unibet", "Sisal", "Eurobet"):
            assert se.classify_bookmaker(b) == "soft", b

    def test_sconosciuto_other(self):
        assert se.classify_bookmaker("BookmakerStrano") == "other"

    def test_pair_type_ammesso(self):
        assert se._pair_type("Pinnacle", "Snai") == "sharp-soft"
        assert se._pair_type("Snai", "Pinnacle") == "soft-sharp"
        assert se._pair_type("Snai", "GoldBet") == "soft-soft"
        assert se._pair_type("Pinnacle", "Pinnacle") is None  # sharp-sharp no
        assert se._pair_type("Snai", "Sconosciuto") is None    # serve sharp o soft


# ---------------------------------------------------------------------------
# Payload the-odds-api → scan
# ---------------------------------------------------------------------------

def _make_match(home="Celtics", away="Lakers",
                books=(("Pinnacle", 1.91), ("Snai", 2.00),
                       ("GoldBet", 2.10), ("Bet365", 1.95)),
                away_books=None):
    """Match NBA sintetico: h2h con 2 esiti (home/away)."""
    away_books = away_books if away_books is not None else books
    payload = {
        "id": "m1", "sport_key": "basketball_nba",
        "home_team": home, "away_team": away,
        "commence_time": "2026-09-10T01:00:00Z",
        "bookmakers": [],
    }
    for (title, price), (_, away_price) in zip(books, away_books):
        payload["bookmakers"].append({
            "title": title,
            "markets": [{"key": "h2h", "outcomes": [
                {"name": home, "price": price},
                {"name": away, "price": away_price},
            ]}],
        })
    return payload


class TestScanMatch:
    def test_nessuna_surebet_con_quote_simili(self):
        # 1/1.95 + 1/1.95 = 1.0256 > 1 → nessuna opportunita'
        m = _make_match(books=(("Pinnacle", 1.95), ("Snai", 1.90)),
                        away_books=(("Pinnacle", 1.95), ("Snai", 1.90)))
        assert se.scan_match(m, "basketball_nba") == []

    def test_surebet_individuata_soft_sharp(self):
        # home: Snai 2.10, away: Pinnacle 2.60 → 1/2.10+1/2.60=0.8608 <1
        m = _make_match(books=(("Pinnacle", 1.91), ("Snai", 2.10)),
                        away_books=(("Pinnacle", 2.60), ("Snai", 1.95)))
        opps = se.scan_match(m, "basketball_nba")
        assert len(opps) >= 1
        opp = opps[0]
        assert opp.inverse_sum < 1.0
        assert opp.margin < 0
        assert opp.roi_pct > 0
        assert opp.tipo in ("soft-sharp", "sharp-soft", "soft-soft")

    def test_surebet_soft_soft(self):
        # entrambi soft: GoldBet home 2.20, Bet365 away 2.60
        m = _make_match(books=(("GoldBet", 2.20), ("Bet365", 1.95)),
                        away_books=(("GoldBet", 1.90), ("Bet365", 2.60)))
        opps = se.scan_match(m, "basketball_nba")
        assert len(opps) >= 1
        assert all(o.tipo == "soft-soft" for o in opps)

    def test_stesso_bookmaker_escluso(self):
        # la coppia Pinnacle-Pinnacle non e' eseguibile
        m = _make_match(books=(("Pinnacle", 2.20), ("Pinnacle", 2.60)),
                        away_books=(("Pinnacle", 2.20), ("Pinnacle", 2.60)))
        assert se.scan_match(m, "basketball_nba") == []

    def test_mercato_con_3_esiti_scartato(self):
        # h2h con 3 outcomes (es. calcio) NON e' un mercato a 2 esiti
        m = {
            "id": "m2", "home_team": "A", "away_team": "B",
            "commence_time": "2026-09-10T01:00:00Z",
            "bookmakers": [{"title": "Pinnacle", "markets": [{"key": "h2h",
                "outcomes": [{"name": "A", "price": 2.0},
                             {"name": "B", "price": 3.0},
                             {"name": "Draw", "price": 3.5}]}]}],
        }
        assert se.scan_match(m, "soccer_epl") == []

    def test_match_senza_bookmakers(self):
        m = {"id": "m3", "home_team": "A", "away_team": "B",
             "commence_time": "", "bookmakers": []}
        assert se.scan_match(m, "tennis_atp_us_open") == []

    def test_stake_sum_budget(self):
        m = _make_match(books=(("Snai", 2.20), ("Pinnacle", 2.60)))
        opps = se.scan_match(m, "basketball_nba")
        assert opps
        for o in opps:
            assert o.stake_a + o.stake_b == pytest.approx(se.SUREBET_BUDGET, abs=0.1)


# ---------------------------------------------------------------------------
# Fetch / indipendenza
# ---------------------------------------------------------------------------

class TestFetch:
    def test_fetch_senza_chiave_ritorna_vuoto(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ODDS_API_KEY", raising=False)
        monkeypatch.setattr(se, "CACHE_DIR", tmp_path)
        assert se.fetch_odds_sport("basketball_nba") == []

    def test_fetch_usa_cache_propria(self, monkeypatch, tmp_path):
        monkeypatch.setattr(se, "CACHE_DIR", tmp_path)
        cache_file = tmp_path / "toa_basketball_nba.json"
        cache_file.write_text(json.dumps(
            {"ts": __import__("time").time(), "payload": [{"id": "x"}]}))
        assert se.fetch_odds_sport("basketball_nba") == [{"id": "x"}]

    def test_fetch_errore_rete_ritorna_vuoto(self, monkeypatch, tmp_path):
        monkeypatch.setattr(se, "CACHE_DIR", tmp_path)
        monkeypatch.setenv("ODDS_API_KEY", "test-key")

        class Boom:
            def raise_for_status(self):
                raise RuntimeError("rete giu'")
        monkeypatch.setattr(
            se.requests, "get",
            lambda *a, **k: type("R", (), {"status_code": 500,
                                           "raise_for_status": Boom().raise_for_status,
                                           "headers": {"x-requests-remaining": "99"}})())
        assert se.fetch_odds_sport("basketball_nba") == []


class TestIndipendenza:
    def test_nessun_import_da_tracker_o_bot(self):
        """Il modulo NON deve dipendere dallo stato del bot Value Bet."""
        import ast
        src = open("surebet_engine.py").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in ("tracker", "bot", "auto_bet",
                                           "fixture_engine", "ml_ensemble"), \
                    f"surebet_engine non deve importare {node.module}"
        assert "betfair" not in src.lower()  # vincolo tripwire

    def test_cache_dir_separata(self):
        assert "surebet" in str(se.CACHE_DIR)
        assert se.CACHE_DIR != se.DATA_DIR  # noqa: non condivide la cache del bot


# ---------------------------------------------------------------------------
# Persistenza / dedup
# ---------------------------------------------------------------------------

def _opp(**kw) -> se.SurebetOpportunity:
    base = dict(
        timestamp="2026-09-10T00:00:00+00:00", sport_key="basketball_nba",
        evento="Celtics vs Lakers", commence_time="2026-09-10T01:00:00Z",
        esito_a="Celtics", esito_b="Lakers",
        bookmaker_a="Snai", bookmaker_b="Pinnacle",
        quota_a=2.20, quota_b=2.60, inverse_sum=0.8608, margin=-0.1392,
        stake_a=54.17, stake_b=45.83, budget=100.0, profit=19.17, roi_pct=19.17,
        tipo="soft-sharp",
    )
    base.update(kw)
    return se.SurebetOpportunity(**base)


class TestLogDedup:
    def test_dedup_riconosce_opportunita_identica(self, tmp_path, monkeypatch):
        monkeypatch.setattr(se, "LOG_FILE", tmp_path / "opp.jsonl")
        opp = _opp()
        se.log_opportunity(opp)
        assert se.already_reported(se._seen_signature(opp))
        # opportunita' con quota diversa NON e' un duplicato
        opp2 = _opp(quota_a=2.30, inverse_sum=0.8423, margin=-0.1577,
                    stake_a=52.6, stake_b=47.4, profit=20.98, roi_pct=20.98)
        assert not se.already_reported(se._seen_signature(opp2))

    def test_log_file_ha_signature(self, tmp_path, monkeypatch):
        monkeypatch.setattr(se, "LOG_FILE", tmp_path / "opp.jsonl")
        se.log_opportunity(_opp())
        row = json.loads(tmp_path.joinpath("opp.jsonl").read_text().splitlines()[0])
        assert "signature" in row and "roi_pct" in row


# ---------------------------------------------------------------------------
# Delivery: formato Telegram + payload webhook n8n
# ---------------------------------------------------------------------------

class TestDelivery:
    def test_formato_telegram_completo(self):
        text = se.format_telegram_alert(_opp())
        assert "ROI netto: +19.17%" in text
        assert "Celtics vs Lakers" in text
        assert "2 esiti" in text
        assert "Snai" in text and "Pinnacle" in text
        assert "€54.17" in text and "€45.83" in text
        assert "0.8608" in text
        assert "Gioca responsabilmente" in text

    def test_json_payload_n8n_strutturato(self):
        payload = se.build_json_payload(_opp())
        assert payload["type"] == "surebet"
        assert payload["event"] == "Celtics vs Lakers"
        assert payload["roi_pct"] > 0
        assert len(payload["outcomes"]) == 2
        assert payload["outcomes"][0]["bookmaker"] == "Snai"
        assert payload["outcomes"][0]["stake"] > 0
        assert payload["margin"] < 0
        # deve essere serializzabile JSON (pronto per il webhook)
        json.dumps(payload)

    def test_webhook_solo_se_configurato(self, monkeypatch):
        monkeypatch.setattr(se, "N8N_WEBHOOK_URL", "")
        assert se._send_webhook(_opp()) is False

    def test_webhook_invia_payload(self, monkeypatch):
        monkeypatch.setattr(se, "N8N_WEBHOOK_URL", "https://n8n.example/hook")
        sent = {}

        def fake_post(url, json=None, timeout=None):
            sent["url"] = url
            sent["json"] = json
            return type("R", (), {"status_code": 200})()

        monkeypatch.setattr(se.requests, "post", fake_post)
        assert se._send_webhook(_opp()) is True
        assert sent["json"]["type"] == "surebet"

    def test_telegram_senza_token_non_invia(self, monkeypatch):
        monkeypatch.setattr(se, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(se, "ADMIN_CHAT_IDS", [1])
        assert se._send_telegram(_opp()) is False

    def test_deliver_predispone_canali(self, monkeypatch):
        """deliver_alert ritorna lo stato di ogni canale (extensible)."""
        monkeypatch.setattr(se, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(se, "ADMIN_CHAT_IDS", [7718157436])
        monkeypatch.setattr(se, "N8N_WEBHOOK_URL", "")

        class R:
            status_code = 200
        monkeypatch.setattr(se.requests, "post", lambda *a, **k: R())
        tg, wh = se.deliver_alert(_opp())
        assert tg is True
        assert wh is False  # webhook non configurato → non attivo


# ---------------------------------------------------------------------------
# Scan orchestratore
# ---------------------------------------------------------------------------

class TestScanAllSports:
    def test_scan_all_sports_con_fetch_mock(self, monkeypatch):
        m = _make_match(books=(("Snai", 2.20), ("Pinnacle", 2.60)))
        monkeypatch.setattr(se, "SPORTS", ["basketball_nba"])
        monkeypatch.setattr(se, "fetch_odds_sport",
                            lambda sk: [m] if sk == "basketball_nba" else [])
        opps = se.scan_all_sports()
        assert len(opps) >= 1
        assert opps[0].sport_key == "basketball_nba"

    def test_run_scan_dedup_doppio(self, tmp_path, monkeypatch):
        m = _make_match(books=(("Snai", 2.20), ("Pinnacle", 2.60)))
        monkeypatch.setattr(se, "LOG_FILE", tmp_path / "opp.jsonl")
        monkeypatch.setattr(se, "SPORTS", ["basketball_nba"])
        monkeypatch.setattr(se, "fetch_odds_sport",
                            lambda sk: [m] if sk == "basketball_nba" else [])
        monkeypatch.setattr(se, "deliver_alert", lambda opp: (False, False))
        first = se.run_scan()
        second = se.run_scan()
        assert len(first) >= 1
        assert second == []  # gia' riportata → dedup