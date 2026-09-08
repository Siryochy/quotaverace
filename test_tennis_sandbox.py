"""Test del modulo tennis_sandbox.py (paper trading tennis su SX Bet).

Copre: client di lettura SX (parsing + paginazione + fail-closed), baseline
Weighted ELO (formula, seeding a coppia coerente, recency, persistenza),
staking Kelly, scansione +EV (filtro coerenza mercato, dedup, osservazioni),
settlement (won/lost/void, apprendimento ELO) e reportistica (ROI, win rate).
Vincolo architetturale: nessun import da tracker/bot (indipendenza, pattern
surebet_engine) e nessuna credenziale usata (solo letture pubbliche).
"""

import json
import sqlite3

import pytest

import tennis_sandbox as ts


# ---------------------------------------------------------------------------
# Helper: client fake per test hermetici (niente rete)
# ---------------------------------------------------------------------------
class FakeClient:
    def __init__(self, markets=None, books=None, outcomes=None):
        self.markets = markets or []
        self.books = books or {}        # market_hash -> {outcomeOne: [...], outcomeTwo: [...]}
        self.outcomes = outcomes or {}  # market_hash -> outcome (1|2|0)
        self.orderbook_calls = 0

    def active_markets(self, max_markets=500):
        return self.markets[:max_markets]

    def orderbook(self, market_hash):
        self.orderbook_calls += 1
        return self.books.get(market_hash)

    def market(self, market_hash):
        if market_hash not in self.outcomes:
            return {"status": "ACTIVE", "marketHash": market_hash}
        return {"status": "INACTIVE", "marketHash": market_hash,
                "outcome": self.outcomes[market_hash],
                "reportedDate": "2026-09-08T22:00:00Z"}


def mk_market(hash_, a, b, event="L1", league="ATP US Open"):
    return {"marketHash": hash_, "sportXeventId": event, "teamOneName": a,
            "teamTwoName": b, "outcomeOneName": a, "outcomeTwoName": b,
            "leagueLabel": league, "type": 52}


FAIR_A, FAIR_B = 1.95, 2.05  # inv_sum = 1/1.95 + 1/2.05 = 1.0006 (coerente)


def mk_book(price_a, price_b):
    """Book con best price sugli esiti (percentageOdds scalati 1e20)."""
    pct = lambda p: str(int(round(p * ts.SX_PROB_SCALE)))
    return {"outcomeOne": [{"percentageOdds": pct(1.0 / price_a),
                            "size": "1000"}],
            "outcomeTwo": [{"percentageOdds": pct(1.0 / price_b),
                            "size": "1000"}]}


@pytest.fixture
def sb(tmp_path):
    """Sandbox con ledger/ratings in tmp_path e client fake."""
    client = FakeClient()
    return ts.TennisSandbox(client=client, data_dir=tmp_path)


# ---------------------------------------------------------------------------
# pct_scaled_to_decimal
# ---------------------------------------------------------------------------
class TestPctScaled:
    def test_conversione_quota(self):
        # 87.625% -> 1.1412
        assert ts.pct_scaled_to_decimal("87625000000000000000") \
            == pytest.approx(1.1412, abs=0.001)

    def test_garbage(self):
        assert ts.pct_scaled_to_decimal(None) is None
        assert ts.pct_scaled_to_decimal("abc") is None
        assert ts.pct_scaled_to_decimal("0") is None       # prob 0
        assert ts.pct_scaled_to_decimal("100000000000000000000000") is None


# ---------------------------------------------------------------------------
# ELO
# ---------------------------------------------------------------------------
class TestElo:
    def test_prob_50_50(self):
        assert ts.TennisElo.prob(1500, 1500) == pytest.approx(0.5)

    def test_prob_favorito(self):
        assert ts.TennisElo.prob(1600, 1400) > 0.5
        # differenza 400 punti -> ~0.91
        assert ts.TennisElo.prob(1700, 1500) == pytest.approx(
            1 / (1 + 10 ** (-200 / 400)), abs=0.001)

    def test_implied_rating_roundtrip(self):
        elo = ts.TennisElo()
        for p in (0.3, 0.5, 0.75, 0.9):
            r = elo.implied_rating(p)
            assert elo.prob(r, 1500) == pytest.approx(p, abs=1e-6)

    def test_ensure_pair_coerente(self):
        # Il bug storico: seminare i due giocatori separatamente distorceva
        # la probabilita' (0.773 -> 0.920). Ora prob(r_a, r_b) == p_a.
        elo = ts.TennisElo()
        elo.ensure_pair("A", 0.7728, "B", 0.2272)
        assert elo.match_prob("A", "B") == pytest.approx(0.7728, abs=0.005)

    def test_ensure_pair_rispetta_noti(self):
        elo = ts.TennisElo()
        elo.ensure_pair("A", 0.9, "B", 0.1)
        r_b = elo.players["B"].rating
        elo.ensure_pair("A", 0.5, "B", 0.5)  # A gia' noto: non si tocca
        assert elo.players["A"].rating != pytest.approx(1500.0)
        assert elo.players["B"].rating == r_b

    def test_update_direzione(self):
        elo = ts.TennisElo()
        elo.ensure_pair("A", 0.7, "B", 0.3)
        ra, rb = elo.players["A"].rating, elo.players["B"].rating
        elo.update("B", "A")  # upset: il debole vince
        assert elo.players["A"].rating < ra
        assert elo.players["B"].rating > rb
        assert elo.players["A"].n == 1 and elo.players["B"].n == 1

    def test_decay_rating_fresca_invariata(self, tmp_path):
        # Risultati negli ultimi 30 giorni: peso PIENO (nessun fade)
        elo = ts.TennisElo(ratings_file=tmp_path / "r.json")
        now = 2_000_000_000.0
        pr = ts.PlayerRating(1600.0, 5, now - 10 * 86400)
        elo.players["A"] = pr
        assert elo.overall_effective("A", now) == 1600.0

    def test_decay_eta_60_giorni_meta_peso(self, tmp_path):
        # ~50% a 60 giorni (RECENT_DAYS 30 + HALF_LIFE 30): 1550 da 1600
        elo = ts.TennisElo(ratings_file=tmp_path / "r.json")
        now = 2_000_000_000.0
        elo.players["A"] = ts.PlayerRating(1600.0, 5, now - 60 * 86400)
        eff = elo.overall_effective("A", now)
        assert eff == pytest.approx(1500.0 + 100.0 * 0.5, abs=0.1)

    def test_decay_risultato_antico_dimenticato(self, tmp_path):
        # Oltre la finestra (2 anni > ELO_WINDOW_DAYS): rating = neutro 1500
        elo = ts.TennisElo(ratings_file=tmp_path / "r.json")
        now = 2_000_000_000.0
        elo.players["A"] = ts.PlayerRating(
            1700.0, 5, now - 2 * 365 * 86400)
        assert elo.overall_effective("A", now) == 1500.0

    def test_decay_non_tocca_seed_dal_mercato(self, tmp_path):
        # Rating seminati dal mercato (n=0): non invecchiano mai
        elo = ts.TennisElo(ratings_file=tmp_path / "r.json")
        now = 2_000_000_000.0
        elo.ensure_pair("A", 0.65, "B", 0.35)
        assert elo.overall_effective("A", now) == elo.players["A"].rating

    def test_risultati_recenti_pesano_piu(self, tmp_path):
        # Stesso esito, stesso rating di partenza: chi ha risultati
        # recenti (5 giorni fa) mantiene una rating piu' alta di chi ha lo
        # stesso rating ma e' inattivo da ~4 mesi (il contesto vecchio
        # viene fade-out dal time-decay PRIMA dell'update).
        elo = ts.TennisElo(ratings_file=tmp_path / "r.json")
        now = 2_000_000_000.0
        recent = now - 5 * 86400
        elo.players["A"] = ts.PlayerRating(1600.0, 5, recent)
        elo.players["B"] = ts.PlayerRating(1400.0, 5, recent)
        elo.update("A", "B", ts=now)
        r_recent = elo.players["A"].rating
        old = now - 120 * 86400
        elo.players["A"] = ts.PlayerRating(1600.0, 5, old)
        elo.players["B"] = ts.PlayerRating(1400.0, 5, old)
        elo.update("A", "B", ts=now)
        r_old = elo.players["A"].rating
        assert r_recent > r_old
        # e in assenza di update, un A inattivo da 120gg predice un match
        # piu' equilibrato di un A attivo (decay visibile nella stima)
        elo.players["C"] = ts.PlayerRating(1600.0, 5, recent)
        elo.players["D"] = ts.PlayerRating(1600.0, 5, old)
        eff_c = elo.overall_effective("C", now)
        eff_d = elo.overall_effective("D", now)
        assert eff_c == 1600.0
        assert eff_d < 1600.0  # regredito verso il neutro 1500

    # -- superficie-specifico -------------------------------------------
    def test_update_superficie_solo_quella(self, tmp_path):
        # Un match su terra aggiorna overall + terra, NON cemento/erba
        elo = ts.TennisElo(ratings_file=tmp_path / "r.json")
        now = 2_000_000_000.0
        elo.ensure_pair("A", 0.65, "B", 0.35)
        elo.update("A", "B", surface="clay", ts=now)
        a = elo.players["A"]
        assert a.n == 1
        assert "clay" in a.surfaces and a.surfaces["clay"].n == 1
        assert "hard" not in a.surfaces and "grass" not in a.surfaces
        assert elo.players["B"].surfaces["clay"].n == 1

    def test_update_superficie_alias(self, tmp_path):
        # Alias accettati (cemento/terra/erba)
        elo = ts.TennisElo(ratings_file=tmp_path / "r.json")
        now = 2_000_000_000.0
        elo.ensure_pair("A", 0.6, "B", 0.4)
        elo.update("A", "B", surface="Terra battuta", ts=now)
        assert "clay" in elo.players["A"].surfaces

    def test_update_superficie_sconosciuta_solo_overall(self, tmp_path):
        elo = ts.TennisElo(ratings_file=tmp_path / "r.json")
        now = 2_000_000_000.0
        elo.ensure_pair("A", 0.6, "B", 0.4)
        elo.update("A", "B", surface=None, ts=now)
        elo.update("B", "A", surface="torneo generico", ts=now)
        assert elo.players["A"].n == 2
        assert elo.players["A"].surfaces == {}

    def test_match_prob_usa_superficie(self, tmp_path):
        # Con storico sulla superficie la probabilita' riflette la rating
        # superficie-specifica, non solo l'overall.
        elo = ts.TennisElo(ratings_file=tmp_path / "r.json")
        now = 2_000_000_000.0
        elo.players["A"] = ts.PlayerRating(
            1600.0, 10, now - 86400,
            surfaces={"clay": ts.SurfaceRating(1800.0, 5, now - 86400)})
        # B senza storico su terra -> si usa l'overall
        elo.players["B"] = ts.PlayerRating(1400.0, 10, now - 86400)
        p_overall = elo.match_prob("A", "B")
        p_clay = elo.match_prob("A", "B", surface="clay")
        assert p_overall == pytest.approx(1 / (1 + 10 ** (-200 / 400)),
                                          abs=0.001)
        assert p_clay == pytest.approx(1 / (1 + 10 ** (-400 / 400)),
                                       abs=0.001)
        assert p_clay > p_overall  # clay-dominante su terra

    def test_persistenza_superfici(self, tmp_path):
        rfile = tmp_path / "ratings.json"
        elo = ts.TennisElo(ratings_file=rfile)
        now = 2_000_000_000.0
        elo.ensure_pair("A", 0.65, "B", 0.35)
        elo.update("A", "B", surface="grass", ts=now)
        elo.save()
        elo2 = ts.TennisElo(ratings_file=rfile)
        assert elo2.players["A"].rating == elo.players["A"].rating
        assert elo2.players["A"].surfaces["grass"].rating \
            == elo.players["A"].surfaces["grass"].rating
        assert elo2.players["B"].surfaces["grass"].n == 1

    def test_caricamento_retrocompatibile(self, tmp_path):
        # Vecchio formato (senza `surfaces`) -> overall + superfici vuote
        rfile = tmp_path / "ratings.json"
        rfile.write_text(json.dumps({
            "Nadal": {"rating": 1700.0, "n": 3, "last_ts": 123.0},
        }), encoding="utf-8")
        elo = ts.TennisElo(ratings_file=rfile)
        assert elo.players["Nadal"].rating == 1700.0
        assert elo.players["Nadal"].n == 3
        assert elo.players["Nadal"].surfaces == {}


class TestSurface:
    def test_detect_cemento(self):
        assert ts.detect_surface("US Open 2026", "ATP") == "hard"
        assert ts.detect_surface("Australian Open") == "hard"
        assert ts.detect_surface("BNP Paribas Open Indian Wells") == "hard"

    def test_detect_terra(self):
        assert ts.detect_surface("Roland Garros", "Grand Slam") == "clay"
        assert ts.detect_surface("Monte-Carlo Masters") == "clay"
        assert ts.detect_surface("Internazionali d'Italia Roma") == "clay"

    def test_detect_erba(self):
        assert ts.detect_surface("Wimbledon 2026") == "grass"
        assert ts.detect_surface("Queen's Club") == "grass"

    def test_sconosciuto_e_ambiguo(self):
        # Torneo non riconosciuto o testo ambiguo: MAI indovinare
        assert ts.detect_surface("Challenger di tennis generico") is None
        assert ts.detect_surface("Laver Cup") == "hard"
        assert ts.detect_surface("Porsche Tennis Stuttgart") is None
        assert ts.detect_surface("") is None
        assert ts.detect_surface(None, None) is None

    def test_market_surface(self):
        assert ts.market_surface({"leagueLabel": "ATP US Open",
                                  "group1": "US Open"}) == "hard"
        assert ts.market_surface({"leagueLabel": "WTA Roland Garros"}) \
            == "clay"
        assert ts.market_surface({"leagueLabel": "ATP Cup",
                                  "eventName": "Sinner vs Alcaraz"}) \
            == "hard"
        assert ts.market_surface({"leagueLabel": "Generico"}) is None

    def test_normalize_alias(self):
        assert ts.normalize_surface("Cemento") == "hard"
        assert ts.normalize_surface("terra battuta") == "clay"
        assert ts.normalize_surface("erba") == "grass"
        assert ts.normalize_surface("clay") == "clay"
        assert ts.normalize_surface("hard court") == "hard"
        assert ts.normalize_surface("") is None
        assert ts.normalize_surface(None) is None

    def test_persistenza(self, tmp_path):
        rfile = tmp_path / "ratings.json"
        elo = ts.TennisElo(ratings_file=rfile)
        elo.ensure_pair("A", 0.65, "B", 0.35)
        elo.save()
        elo2 = ts.TennisElo(ratings_file=rfile)
        assert elo2.players["A"].rating == elo.players["A"].rating


# ---------------------------------------------------------------------------
# Kelly e devig
# ---------------------------------------------------------------------------
class TestKelly:
    def test_ev_positivo(self):
        stake = ts.kelly_stake(0.6, 2.0, bankroll=1000)
        assert stake > 0
        # full kelly = (0.6*2-1)/(2-1) = 0.2 -> *0.25 = 5% di 1000 = 50
        assert stake == pytest.approx(50.0, abs=0.01)

    def test_ev_negativo_zero(self):
        assert ts.kelly_stake(0.4, 2.0, bankroll=1000) == 0.0

    def test_cap_max_pct(self):
        stake = ts.kelly_stake(0.95, 1.2, bankroll=1000)
        # full = (1.14-1)/0.2 = 0.7 -> 700; cap al 5% = 50
        assert stake == pytest.approx(50.0, abs=0.01)

    def test_input_invalidi(self):
        assert ts.kelly_stake(0.0, 2.0, 1000) == 0.0
        assert ts.kelly_stake(0.5, 1.0, 1000) == 0.0
        assert ts.kelly_stake(0.5, 2.0, 0) == 0.0


class TestDevig:
    def test_normalizza(self):
        assert ts.devig(0.3, 0.1) == pytest.approx(0.75)
        assert ts.devig(0.0, 0.0) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Scansione
# ---------------------------------------------------------------------------
class TestScan:
    def test_nessun_segnale_senza_edge(self, sb):
        # Mercato coerente (inv_sum ~1.0): l'ELO seminato conferma il
        # mercato -> EV ~ 0 -> nessun segnale (anti-EV-spurio)
        sb.client.markets = [mk_market("h1", "A", "B")]
        sb.client.books = {"h1": mk_book(FAIR_A, FAIR_B)}
        res = sb.scan()
        assert res["markets"] == 1 and res["with_book"] == 1
        assert res["signals"] == 0
        assert sb.conn.execute("SELECT COUNT(*) n FROM observations"
                               ).fetchone()["n"] == 1

    def test_segnale_con_edge_reale(self, sb):
        # Modello con edge: dopo aver forzato il rating di A piu' alto del
        # mercato, A @2.0 diventa +EV.
        sb.client.markets = [mk_market("h1", "A", "B")]
        sb.client.books = {"h1": mk_book(FAIR_A, FAIR_B)}
        # seeding coerente, poi alziamo A
        sb.elo.ensure_pair("A", 0.5, "B", 0.5)
        sb.elo.players["A"].rating = 1750.0
        res = sb.scan()
        assert res["signals"] >= 1
        row = sb.conn.execute(
            "SELECT * FROM signals WHERE selection='A'").fetchone()
        assert row["status"] == "open"
        assert row["ev"] > 0.03

    def test_mercato_incoerente_saltato(self, sb):
        # Book sporco (sfavorito a quota enorme -> inv_sum < 0.98): salta
        sb.client.markets = [mk_market("h1", "A", "B")]
        sb.client.books = {"h1": mk_book(1.13, 50.0)}
        res = sb.scan()
        assert res["with_book"] == 1
        assert res["signals"] == 0
        assert sb.conn.execute("SELECT COUNT(*) n FROM observations"
                               ).fetchone()["n"] == 0

    def test_dedup_riscansione(self, sb):
        sb.client.markets = [mk_market("h1", "A", "B")]
        sb.client.books = {"h1": mk_book(FAIR_A, FAIR_B)}
        sb.elo.ensure_pair("A", 0.5, "B", 0.5)
        sb.elo.players["A"].rating = 1750.0
        sb.scan()
        sb.scan()  # seconda scansione: stesso (market, selection)
        n = sb.conn.execute("SELECT COUNT(*) n FROM signals").fetchone()["n"]
        assert n == 1  # UNIQUE(market_hash, selection)

    def test_market_senza_book_fail_closed(self, sb):
        sb.client.markets = [mk_market("h1", "A", "B")]
        sb.client.books = {}
        res = sb.scan()  # orderbook None -> niente crash
        assert res["errors"] == 0 and res["signals"] == 0

    def test_nessuna_credenziale_usata(self):
        # Il client reale non ha mai una api_key: solo letture pubbliche
        c = ts.SxTennisClient()
        assert not hasattr(c, "api_key")

    def test_osservazione_registra_superficie(self, sb):
        # La superficie del torneo viene rilevata e salvata sul ledger
        sb.client.markets = [mk_market("h1", "A", "B",
                                       league="Wimbledon 2026")]
        sb.client.books = {"h1": mk_book(FAIR_A, FAIR_B)}
        sb.scan()
        obs = sb.conn.execute(
            "SELECT surface FROM observations WHERE market_hash='h1'"
        ).fetchone()
        assert obs["surface"] == "grass"

    def test_segnale_registra_superficie(self, sb):
        sb.client.markets = [mk_market("h1", "A", "B",
                                       league="Roland Garros")]
        sb.client.books = {"h1": mk_book(1.8, 2.1)}
        sb.elo.ensure_pair("A", 0.5, "B", 0.5)
        sb.elo.players["A"].rating = 1750.0
        sb.scan()
        row = sb.conn.execute(
            "SELECT surface FROM signals WHERE selection='A'").fetchone()
        assert row["surface"] == "clay"


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------
class TestSettle:
    def _seed_open_bet(self, sb, price=FAIR_A, ev=0.05, stake=50.0, sel="A"):
        sb.client.markets = [mk_market("h1", "A", "B")]
        sb.client.books = {"h1": mk_book(price, FAIR_B)}
        sb.elo.ensure_pair("A", 0.5, "B", 0.5)
        sb.elo.players["A"].rating = 1750.0
        sb.scan()
        return sb.conn.execute("SELECT * FROM signals WHERE selection=?",
                               (sel,)).fetchone()

    def test_settle_won(self, sb):
        sb.client.outcomes = {"h1": 1}  # vince A
        row = self._seed_open_bet(sb)
        settled = sb.settle()
        assert len(settled) == 1
        assert settled[0]["status"] == "won"
        assert settled[0]["profit"] == pytest.approx(
            row["stake"] * (row["price"] - 1.0), 0.01)
        # observation saldata e ELO aggiornato
        obs = sb.conn.execute("SELECT * FROM observations WHERE "
                              "market_hash='h1'").fetchone()
        assert obs["status"] == "settled" and obs["winner"] == "A"
        assert sb.elo.players["A"].n == 1

    def test_settle_lost(self, sb):
        sb.client.outcomes = {"h1": 2}  # vince B
        row = self._seed_open_bet(sb)
        settled = sb.settle()
        assert settled[0]["status"] == "lost"
        assert settled[0]["profit"] == pytest.approx(-row["stake"], 0.01)

    def test_settle_void(self, sb):
        sb.client.outcomes = {"h1": 0}
        self._seed_open_bet(sb)
        settled = sb.settle()
        assert settled[0]["status"] == "void"
        assert settled[0]["profit"] == 0.0
        # void: nessun apprendimento ELO
        assert sb.elo.players["A"].n == 0

    def test_mercato_ancora_aperto_resta_open(self, sb):
        self._seed_open_bet(sb)  # nessun outcome settato -> ACTIVE
        assert sb.settle() == []
        row = sb.conn.execute("SELECT * FROM signals WHERE "
                              "market_hash='h1'").fetchone()
        assert row["status"] == "open"

    def test_elo_aggiornato_una_sola_volta(self, sb):
        # Anche con segnale + scambio, l'ELO si aggiorna solo dal ramo
        # observations (niente doppio conteggio)
        sb.client.outcomes = {"h1": 1}
        self._seed_open_bet(sb)
        sb.settle()
        assert sb.elo.players["A"].n == 1

    def test_settle_allena_rating_superficie(self, sb):
        # Match su terra (Roland Garros): il settlement aggiorna sia
        # l'overall sia il rating clay (e NON hard/grass)
        sb.client.markets = [mk_market("h1", "A", "B",
                                       league="Roland Garros")]
        sb.client.books = {"h1": mk_book(1.8, 2.1)}
        sb.elo.ensure_pair("A", 0.55, "B", 0.45)
        sb.elo.players["A"].rating = 1700.0
        sb.scan()
        obs = sb.conn.execute(
            "SELECT surface FROM observations WHERE market_hash='h1'"
        ).fetchone()
        assert obs["surface"] == "clay"
        sb.client.outcomes = {"h1": 1}  # vince A
        sb.settle()
        assert sb.elo.players["A"].n == 1
        assert sb.elo.players["A"].surfaces["clay"].n == 1
        assert "hard" not in sb.elo.players["A"].surfaces
        assert "grass" not in sb.elo.players["A"].surfaces

    def test_settle_void_non_allena_superficie(self, sb):
        sb.client.markets = [mk_market("h1", "A", "B",
                                       league="Wimbledon")]
        sb.client.books = {"h1": mk_book(FAIR_A, FAIR_B)}
        sb.elo.ensure_pair("A", 0.5, "B", 0.5)
        sb.scan()
        sb.client.outcomes = {"h1": 0}  # void
        sb.settle()
        assert sb.elo.players["A"].n == 0
        assert "grass" not in sb.elo.players["A"].surfaces


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
class TestReport:
    def test_report_vuoto(self, sb):
        rep = sb.report()
        assert rep["total_signals"] == 0
        assert rep["roi_pct"] is None and rep["win_rate_pct"] is None

    def test_roi_e_winrate(self, sb):
        # simula: 2 won (stake 50 a quota 2.0), 1 lost, 1 open
        conn = sb.conn
        now = "2026-09-08T12:00:00Z"
        for i, st in enumerate(("won", "won", "lost")):
            conn.execute(
                "INSERT INTO signals (ts, day, market_hash, event_id, "
                "player_a, player_b, selection, price, model_prob, ev, "
                "stake, status, settled_at, profit) VALUES "
                "(?, '2026-09-08', ?, 'L1', 'A', 'B', 'A', 2.0, 0.55, "
                "0.05, 50.0, ?, ?, ?)",
                (now, f"h{i}", st, now,
                 50.0 * (2.0 - 1.0) if st == "won" else -50.0))
        conn.execute(
            "INSERT INTO signals (ts, day, market_hash, event_id, player_a, "
            "player_b, selection, price, model_prob, ev, stake, status) "
            "VALUES (?, '2026-09-08', 'h3', 'L1', 'A', 'B', 'A', 2.0, "
            "0.55, 0.05, 50.0, 'open')",
            (now,))
        conn.commit()
        rep = sb.report()
        assert rep["total_signals"] == 4
        assert rep["closed"] == 3 and rep["open"] == 1
        assert rep["won"] == 2 and rep["lost"] == 1
        assert rep["win_rate_pct"] == pytest.approx(66.67, abs=0.01)
        # profit = +50 +50 -50 = +50 su stake_closed 150 -> ROI +33.33%
        assert rep["profit_total"] == pytest.approx(50.0, abs=0.01)
        assert rep["roi_pct"] == pytest.approx(33.33, abs=0.01)
        assert len(rep["daily"]) >= 1
        assert rep["daily"][0]["n"] == 4

    def test_format_report(self, sb):
        rep = sb.report()
        text = ts.format_report(rep)
        assert "SANDBOX TENNIS" in text
        assert "Segnali +EV" in text


# ---------------------------------------------------------------------------
# Indipendenza architetturale (pattern surebet_engine)
# ---------------------------------------------------------------------------
class TestIndipendenza:
    def test_nessun_import_da_tracker_o_bot(self):
        src = ts.__file__
        with open(src, encoding="utf-8") as f:
            text = f.read()
        for banned in ("import tracker", "from tracker",
                       "import bot", "from bot",
                       "import surebet_engine", "from surebet_engine",
                       "import odds_api", "from odds_api",
                       "import execution_engine", "from execution_engine"):
            assert banned not in text, f"{banned} trovato nel modulo"

    def test_nessun_ordine_o_firma(self):
        with open(ts.__file__, encoding="utf-8") as f:
            text = f.read()
        for banned in ("orders-v3", "place_limit_order", "private_key",
                       "eth_account", "x-sx-api-key", "sign_message",
                       "SX_PRIVATE_KEY", "SX_API_KEY"):
            assert banned not in text, f"{banned} nel modulo (deve essere read-only)"