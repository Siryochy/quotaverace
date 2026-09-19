"""Test del ledger QUOTE MULTI-MERCATO (`tracker.market_quotes`) + gateway.

OFFLINE per costruzione: SQLite TEMPORANEO (monkeypatch di `tracker.DB_PATH`),
nessuna rete, nessun provider, nessuna credenziale, **zero crediti API**.
Copre cinque cose:

1. lo **schema**: chiave composta (partita, mercato, linea, esito), colonne di
   chiave NOT NULL, indici strategici, migrazione idempotente su una tabella
   parziale — con l'ORDINE tabella -> colonne -> indici (lezione del 14/09, se
   gli indici precedono le colonne `_get_conn` fallisce e con lui il bot);
2. l'**upsert**: la stessa quota letta due volte aggiorna il prezzo e NON
   duplica — e' cio' che rende ripetibile la lettura del palinsesto;
3. le **difese**: righe sporche scartate senza eccezioni, due linee MAI fuse
   nella stessa riga, lotto ostile che non fa cadere il salvataggio;
4. la **copertura del contratto**: ogni campo di `MARKET_ROW_FIELDS` finisce in
   una colonna, cosi' un campo nuovo non puo' sparire in silenzio;
5. il **gateway di storage** (`MarketQuotesGateway`): scrive sul ledger, e' un
   gateway di solo audit e in shadow mode non esegue niente.
"""

import sqlite3
import subprocess
import sys

import pytest

import tracker
from decision.commands import COMMAND_ORDER, CommandKind, save_quotes_command
from decision.gateways import MarketQuotesGateway
from decision.market import (
    MARKET_ROW_FIELDS, MARKET_SCHEMA_VERSION, FixtureQuotes, parse_quote,
)

FIXTURE = "sx-L20067612"
OTHER = "sx-L20099999"


# ---------------------------------------------------------------------------
# Helper: ledger temporaneo + quote del contratto 2.0
# ---------------------------------------------------------------------------

@pytest.fixture
def db(monkeypatch, tmp_path):
    """DB temporaneo (schema di produzione creato da tracker)."""
    path = tmp_path / "ledger.db"
    monkeypatch.setattr(tracker, "DB_PATH", path)
    conn = tracker._get_conn()
    conn.close()
    return path


def payload(**overrides):
    """Riga conforme al contratto 2.0 (1X2 casa); gli override la cambiano."""
    base = {
        "schema_version": MARKET_SCHEMA_VERSION,
        "event_id": FIXTURE,
        "market": "1X2",
        "selection": "1",
        "odds": 1.69,
        "timestamp": "2026-09-19T10:00:00+00:00",
        "source": "sxbet",
        "gateway_id": "sxbet-feed",
        "event_name": "Central Cordoba - Defensa y Justicia",
        "league": "Liga Profesional",
        "home": "Central Cordoba",
        "away": "Defensa y Justicia",
        "kickoff": "2026-09-19T23:30:00+00:00",
    }
    base.update(overrides)
    return base


def quote(**overrides):
    """Una quota VALIDA del contratto (l'unica porta d'ingresso)."""
    return parse_quote(payload(**overrides))


def ou_quote(line=2.5, selection="over", **overrides):
    return quote(market="OU", selection=selection, line=line, **overrides)


def row(**overrides):
    """Riga flat come la produce `MarketQuote.as_row()` (+ override)."""
    base = {
        "fixture_id": FIXTURE,
        "market_type": "OU",
        "line_key": "2.5",
        "line": 2.5,
        "selection": "over",
        "selection_label": "Over 2.5",
        "ledger_esito": "Over 2.5",
        "odds": 1.95,
        "main_line": True,
        "origin": "native",
        "derived_from": [],
        "depth_usdc": 120.0,
        "source": "sxbet",
        "gateway_id": "sxbet-feed",
        "schema_version": MARKET_SCHEMA_VERSION,
        "observed_at": "2026-09-19T10:00:00+00:00",
        "kickoff": "2026-09-19T23:30:00+00:00",
        "event_name": "Central Cordoba - Defensa y Justicia",
        "league": "Liga Profesional",
        "home": "Central Cordoba",
        "away": "Defensa y Justicia",
        "identity_key": "identity-1",
        "quote_id": "quote-1",
        "extra": {"market_hash": "0xabc", "inv_sum": 1.01},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Schema, chiave composta, indici
# ---------------------------------------------------------------------------

class TestSchema:
    def test_tabella_colonne_e_chiave(self, db):
        conn = tracker._get_conn()
        info = {r[1]: r for r in conn.execute("PRAGMA table_info(market_quotes)")}
        assert set(tracker.MARKET_QUOTE_FIELDS) <= set(info)
        # La chiave e' la stessa identita' del contratto, nell'ordine dichiarato.
        pk = [name for name, _ in sorted(
            ((name, r[5]) for name, r in info.items() if r[5]), key=lambda kv: kv[1])]
        assert tuple(pk) == tracker.MARKET_QUOTE_KEYS
        # Le colonne di chiave NON ammettono NULL: in SQLite i NULL sono
        # distinti fra loro e ammetterebbero righe "uguali" all'infinito.
        for name in tracker.MARKET_QUOTE_KEYS:
            assert info[name][3] == 1, f"{name} deve essere NOT NULL"
        conn.close()

    def test_indici_strategici(self, db):
        conn = tracker._get_conn()
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='market_quotes'")}
        assert "idx_market_quotes_lookup" in indexes
        assert "idx_market_quotes_market" in indexes
        lookup = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_market_quotes_lookup'").fetchone()[0]
        # L'indice del lookup copre esattamente lo snapshot di un mercato.
        assert "(fixture_id, market_type, line_key)" in lookup
        conn.close()

    def test_origin_default_native(self, db):
        conn = tracker._get_conn()
        conn.execute("INSERT INTO market_quotes (fixture_id, market_type, line_key, "
                     "selection, price) VALUES ('sx-1', '1X2', '', '1', 1.7)")
        conn.commit()
        assert conn.execute("SELECT origin FROM market_quotes").fetchone()[0] == "native"
        conn.close()

    def test_migrazione_su_tabella_vecchia_parziale(self, monkeypatch, tmp_path):
        """Una tabella parziale (deploy precedente) si completa, senza perdere righe."""
        db = tmp_path / "vecchio.db"
        monkeypatch.setattr(tracker, "DB_PATH", db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE market_quotes (fixture_id TEXT NOT NULL, market_type TEXT NOT NULL, "
            "line_key TEXT NOT NULL DEFAULT '', selection TEXT NOT NULL, price REAL NOT NULL, "
            "PRIMARY KEY (fixture_id, market_type, line_key, selection))")
        conn.execute("INSERT INTO market_quotes VALUES ('sx-1', '1X2', '', '1', 1.7)")
        conn.commit()
        conn.close()
        conn = tracker._get_conn()                      # l'avvio migra tutto
        cols = [r[1] for r in conn.execute("PRAGMA table_info(market_quotes)")]
        for name in tracker.MARKET_QUOTE_FIELDS:
            assert name in cols, name
        assert conn.execute("SELECT price FROM market_quotes").fetchone()[0] == 1.7
        # Una riga MIGRATA non ha il default (ALTER TABLE non lo consente con
        # un default non costante): nasce vuota e si riempie alla riscrittura.
        assert conn.execute("SELECT origin, updated_at FROM market_quotes").fetchone() == (None, None)
        conn.close()

    def test_creazione_idempotente_su_giri_ripetuti(self, db):
        for _ in range(3):
            tracker._get_conn().close()
        assert tracker.count_market_quotes() == 0


# ---------------------------------------------------------------------------
# 2. Upsert: aggiorna, non duplica
# ---------------------------------------------------------------------------

class TestUpsert:
    def test_stessa_quota_due_volte_aggiorna_il_prezzo(self, db):
        assert tracker.save_market_quotes([row()])["saved"] == 1
        first = tracker.market_quote(FIXTURE, "OU", "over", "2.5")
        assert tracker.save_market_quotes([row(odds=2.05)])["saved"] == 1
        second = tracker.market_quote(FIXTURE, "OU", "over", "2.5")
        assert tracker.count_market_quotes(FIXTURE) == 1     # nessun duplicato
        assert first["price"] == 1.95 and second["price"] == 2.05
        assert second["implied_prob"] == pytest.approx(1 / 2.05)

    def test_upsert_sopravvive_a_una_nuova_connessione(self, db):
        tracker.save_market_quotes([row()])
        tracker.save_market_quotes([row(odds=2.10)])         # connessione nuova
        assert tracker.count_market_quotes() == 1
        assert tracker.market_quote(FIXTURE, "OU", "over", "2.5")["price"] == 2.10

    def test_la_linea_distingue_i_mercati(self, db):
        tracker.save_market_quotes([
            row(line_key="2.5", line=2.5),
            row(line_key="3.5", line=3.5),
            row(line_key="2.5", line=2.5, selection="under", ledger_esito="Under 2.5"),
            row(fixture_id=OTHER, market_type="1X2", line_key="", line=None,
                selection="1", ledger_esito="1"),
        ])
        assert tracker.count_market_quotes() == 4
        assert tracker.count_market_quotes(FIXTURE) == 3
        assert tracker.market_quote(FIXTURE, "OU", "under", "2.5")["ledger_esito"] == "Under 2.5"
        assert tracker.market_quote(OTHER, "1X2", "1", "")["line_key"] == ""

    def test_conn_esterna_non_viene_chiusa(self, db):
        conn = tracker._get_conn()
        tracker.save_market_quotes([row()], conn=conn)
        assert conn.execute("SELECT COUNT(*) FROM market_quotes").fetchone()[0] == 1
        conn.close()

    def test_lotto_di_fixture_intera(self, db):
        """Un `FixtureQuotes` (N mercati di UNA partita) si salva in un colpo."""
        fixture = FixtureQuotes(
            fixture_id=FIXTURE,
            quotes=[quote(market="1X2", selection="1", odds=1.69),
                    ou_quote(2.5, "over", odds=1.95),
                    quote(market="BTTS", selection="yes", odds=1.8)],
        )
        out = tracker.save_market_quotes(fixture)
        assert out == {"saved": 3, "skipped": 0, "fixtures": 1, "by_reason": {}, "error": None}
        assert {q["market_type"] for q in tracker.get_market_quotes(FIXTURE)} == {"1X2", "OU", "BTTS"}


# ---------------------------------------------------------------------------
# 3. Difese: righe sporche, linee mai fuse, lotto ostile
# ---------------------------------------------------------------------------

class TestDifese:
    def test_righe_sporche_scartate_senza_eccezioni(self, db):
        out = tracker.save_market_quotes([
            row(),                                            # valida
            row(fixture_id=""),                               # senza partita
            row(fixture_id="sx-2", odds=0),                   # prezzo impossibile
            row(fixture_id="sx-3", odds="molto"),             # prezzo non numerico
            row(fixture_id="sx-4", selection=""),
            row(fixture_id="sx-5", market_type=""),
        ])
        assert out["saved"] == 1 and out["skipped"] == 5
        assert out["by_reason"]["fixture_id_mancante"] == 1
        assert out["by_reason"]["price_non_valido"] == 2
        assert out["by_reason"]["selection_mancante"] == 1
        assert out["by_reason"]["market_type_mancante"] == 1
        assert tracker.count_market_quotes() == 1             # solo la valida

    def test_linee_mai_fuse_senza_line_key(self, db):
        """Senza `line_key` due totali diversi collasserebbero nella stessa riga."""
        out = tracker.save_market_quotes([row(line_key="", line=2.5),
                                          row(line_key=None, line=3.5)])
        assert out["saved"] == 0 and out["skipped"] == 2
        assert out["by_reason"] == {"line_key_mancante": 2}
        assert tracker.count_market_quotes() == 0

    def test_senza_linea_la_line_key_vuota_e_regolare(self, db):
        """Un mercato SENZA linea (1X2) ha `line_key` vuota: non e' un errore."""
        out = tracker.save_market_quotes([row(market_type="1X2", line=None,
                                              line_key="", selection="1")])
        assert out["saved"] == 1
        assert tracker.market_quote(FIXTURE, "1X2", "1", "")["line"] is None

    def test_riga_ostile_non_fa_cadere_il_lotto(self, db):
        class Ostile(dict):
            def get(self, key, default=None):
                raise RuntimeError("riga ostile")

        out = tracker.save_market_quotes([Ostile(), row()])
        assert out["error"] is None
        assert out["saved"] == 1 and out["skipped"] == 1
        assert tracker.count_market_quotes() == 1

    def test_lotto_non_leggibile_non_solleva(self, db):
        def rotto():
            raise RuntimeError("lotto rotto")
            yield 1                                        # pragma: no cover

        out = tracker.save_market_quotes(rotto())
        assert out["saved"] == 0 and "lotto non leggibile" in out["error"]
        assert tracker.count_market_quotes() == 0

    def test_lotto_vuoto_e_un_no_op(self, db):
        assert tracker.save_market_quotes([]) == {
            "saved": 0, "skipped": 0, "fixtures": 0, "by_reason": {}, "error": None}


# ---------------------------------------------------------------------------
# 4. Valori persistiti e copertura del contratto
# ---------------------------------------------------------------------------

class TestValoriPersistiti:
    def test_quote_reale_del_contratto_attraversa_il_ledger(self, db):
        q = ou_quote(2.5, "over", odds=1.95, main_line="true")
        assert tracker.save_market_quotes([q])["saved"] == 1
        stored = tracker.market_quote(FIXTURE, "OU", "over", "2.5")
        expected = q.as_row()
        assert stored["price"] == expected["odds"]
        assert stored["liquidity"] == expected["depth_usdc"]
        assert stored["ledger_esito"] == expected["ledger_esito"] == "Over 2.5"
        assert stored["derived_from"] == list(expected["derived_from"])
        assert stored["extra"] == expected["extra"]
        assert stored["observed_at"] == expected["observed_at"]
        assert stored["kickoff"] == expected["kickoff"]
        assert stored["identity_key"] == expected["identity_key"]

    def test_handicap_e_risultato_esatto_hanno_lesito_del_ledger(self, db):
        """`ledger_esito` e' il ponte verso `ml_audit`: deve arrivare scritto."""
        tracker.save_market_quotes([
            quote(market="AH", selection="1", line=-0.75, odds=1.9).as_row(),
            quote(market="CS", selection="3-1", odds=9.0).as_row(),
        ])
        stored = {q["market_type"]: q for q in tracker.get_market_quotes(FIXTURE)}
        assert stored["AH"]["ledger_esito"] == "Home -0.75"
        assert stored["AH"]["line_key"] == "-0.75"
        assert stored["CS"]["ledger_esito"] == "3-1"

    def test_implied_prob_calcolata_market_prob_non_inventata(self, db):
        tracker.save_market_quotes([row(odds=2.0), row(selection="under", odds=1.8,
                                                        market_prob=0.53)])
        over = tracker.market_quote(FIXTURE, "OU", "over", "2.5")
        under = tracker.market_quote(FIXTURE, "OU", "under", "2.5")
        assert over["implied_prob"] == pytest.approx(0.5)
        # Il devigging e' una scelta dell'engine: il ledger la scrive se gliela
        # danno, non la inventa.
        assert over["market_prob"] is None
        assert under["market_prob"] == pytest.approx(0.53)

    def test_json_rilette_e_main_line_tre_stati(self, db):
        tracker.save_market_quotes([
            row(main_line=True, derived_from=["1X2"], extra={"a": 1}),
            row(selection="under", main_line=False, derived_from=[]),
            row(fixture_id=OTHER, main_line=None, derived_from=["OU"], extra={"b": {1, 2}}),
        ])
        over = tracker.market_quote(FIXTURE, "OU", "over", "2.5")
        under = tracker.market_quote(FIXTURE, "OU", "under", "2.5")
        terzo = tracker.market_quote(OTHER, "OU", "over", "2.5")
        assert over["main_line"] is True and under["main_line"] is False
        assert terzo["main_line"] is None                     # non dichiarato NON e' False
        assert terzo["derived_from"] == ["OU"]
        assert terzo["extra"] == {"b": "{1, 2}"}              # `default=str`: mai un'eccezione

    def test_ogni_campo_del_contratto_finisce_in_una_colonna(self):
        """TRIPWIRE: un campo nuovo del contratto non puo' sparire in silenzio."""
        columns = set(tracker.MARKET_QUOTE_FIELDS)
        sources = set(tracker.MARKET_QUOTE_SOURCE.values())
        for name in MARKET_ROW_FIELDS:
            assert name in columns or name in sources, f"campo non persistito: {name}"
        for column, source in tracker.MARKET_QUOTE_SOURCE.items():
            assert column in columns, column
            assert source in MARKET_ROW_FIELDS, source
        assert not set(tracker.MARKET_QUOTE_DERIVED) - columns

    def test_letture_filtrate_e_limite(self, db):
        tracker.save_market_quotes([
            row(), row(selection="under"), row(line_key="3.5", line=3.5),
            row(fixture_id=OTHER, market_type="1X2", line_key="", line=None, selection="1"),
        ])
        assert len(tracker.get_market_quotes(FIXTURE)) == 3
        assert len(tracker.get_market_quotes(FIXTURE, market_type="OU")) == 3
        assert len(tracker.get_market_quotes(FIXTURE, line_key="2.5")) == 2
        assert len(tracker.get_market_quotes(FIXTURE, selection="over")) == 2
        assert len(tracker.get_market_quotes(market_type="1X2")) == 1
        assert len(tracker.get_market_quotes(FIXTURE, limit=1)) == 1
        assert tracker.market_quote(FIXTURE, "OU", "over", "9.5") is None

    def test_potatura_per_data(self, db):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        fresh = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        tracker.save_market_quotes([row(kickoff=old), row(selection="under", kickoff=fresh)])
        assert tracker.prune_market_quotes(days=7) == 1
        assert tracker.count_market_quotes() == 1
        assert tracker.market_quote(FIXTURE, "OU", "under", "2.5") is not None

    def test_potatura_non_tocca_le_righe_senza_data(self, db):
        """Senza kickoff ne' osservazione non si prova nulla: la riga resta."""
        tracker.save_market_quotes([row(kickoff=None, observed_at=None)])
        assert tracker.prune_market_quotes(days=1) == 0
        assert tracker.count_market_quotes() == 1


# ---------------------------------------------------------------------------
# 5. Il gateway di storage (Command pattern)
# ---------------------------------------------------------------------------

class TestMarketQuotesGateway:
    def test_comando_e_dedup_per_prezzo(self):
        from decision.commands import COMMAND_ORDER
        command = save_quotes_command([row()], fixture_id=FIXTURE)
        assert command.kind is CommandKind.SAVE_MARKET_QUOTES
        # Lo snapshot di mercato e' l'EVIDENZA del prezzo su cui si e' deciso:
        # apre l'ordine dichiarato, prima dell'audit e dell'effetto.
        assert COMMAND_ORDER[0] is CommandKind.SAVE_MARKET_QUOTES
        assert command.order == 0
        # Stessa quota -> stessa chiave; quota diversa -> chiave diversa.
        same = save_quotes_command([row()], fixture_id=FIXTURE)
        moved = save_quotes_command([row(odds=2.4)], fixture_id=FIXTURE)
        assert command.dedup_key == same.dedup_key
        assert command.dedup_key != moved.dedup_key

    def test_prima_evidenza_poi_effetto(self):
        """L'ordine dichiarato e' un'invariante, non un dettaglio.

        `persist_decision` prima di `place_order` e' la regola del 15/09
        ("un fallimento a valle non cancella l'audit"); lo snapshot di mercato
        e' l'evidenza del PREZZO su cui la decisione e' stata presa, quindi
        viene ancora prima. La misura CLV, che si puo' fare solo a cose fatte,
        chiude.
        """
        at = {kind: index for index, kind in enumerate(COMMAND_ORDER)}
        assert at[CommandKind.SAVE_MARKET_QUOTES] < at[CommandKind.PERSIST_DECISION]
        assert at[CommandKind.PERSIST_DECISION] < at[CommandKind.PLACE_ORDER]
        assert at[CommandKind.PLACE_ORDER] < at[CommandKind.NOTIFY_OPERATORS]
        assert at[CommandKind.NOTIFY_OPERATORS] < at[CommandKind.WRITE_CLV]
        assert COMMAND_ORDER[-1] is CommandKind.WRITE_CLV

    def test_comando_senza_righe_non_nasce(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            save_quotes_command([], fixture_id=FIXTURE)

    def test_gateway_scrive_sul_ledger(self, db):
        command = save_quotes_command([row(), row(selection="under", odds=1.8)],
                                      fixture_id=FIXTURE)
        result = MarketQuotesGateway().execute(command, ctx=None, obs=None)
        assert result.ok and result.status == "executed"
        assert result.data["saved"] == 2 and result.data["skipped"] == 0
        assert tracker.count_market_quotes(FIXTURE) == 2
        # Ripetere lo stesso comando aggiorna, non duplica.
        again = MarketQuotesGateway().execute(command, ctx=None, obs=None)
        assert again.ok and tracker.count_market_quotes(FIXTURE) == 2

    def test_gateway_scrive_anche_le_righe_scartate_nel_riepilogo(self, db):
        command = save_quotes_command([row(), row(fixture_id="", odds=0)])
        result = MarketQuotesGateway().execute(command, ctx=None, obs=None)
        assert result.ok and result.data["saved"] == 1 and result.data["skipped"] == 1
        assert result.data["by_reason"] == {"fixture_id_mancante": 1}

    def test_gateway_audit_only_e_mai_un_falso_successo(self, db):
        assert MarketQuotesGateway().audit_only is True
        # Nessuna riga accettata = ingestione FALLITA (non un ok silenzioso).
        solo_sporca = MarketQuotesGateway().execute(
            save_quotes_command([row(fixture_id="")]), ctx=None, obs=None)
        assert not solo_sporca.ok and solo_sporca.status == "error"
        assert "nessuna quota accettata" in solo_sporca.detail

    def test_writer_rotto_diventa_un_errore_non_un_eccezione(self):
        def rotto(rows):
            raise RuntimeError("disco pieno")

        command = save_quotes_command([row()])
        result = MarketQuotesGateway(writer=rotto).execute(command, ctx=None, obs=None)
        assert not result.ok and result.status == "error"
        assert "disco pieno" in result.detail

    def test_writer_iniettabile_evita_il_db(self):
        seen = {}

        def writer(rows):
            seen["rows"] = list(rows)
            return {"saved": len(rows), "skipped": 0, "fixtures": 1}

        command = save_quotes_command([row()], fixture_id=FIXTURE)
        result = MarketQuotesGateway(writer=writer).execute(command, ctx=None, obs=None)
        assert result.ok and len(seen["rows"]) == 1
        assert seen["rows"][0]["odds"] == 1.95

    def test_shadow_registra_senza_scrivere(self, db, tmp_path):
        """In shadow il comando si REGISTRA e il ledger resta vuoto."""
        from decision.gateways import ShadowGateway
        shadow = ShadowGateway(tmp_path / "shadow.jsonl")
        assert shadow.dry_run is True                  # dichiara di non eseguire
        result = shadow.execute(save_quotes_command([row()]), ctx=None, obs=None)
        assert result.ok and result.dry_run is True
        assert result.status == "recorded"
        assert tracker.count_market_quotes() == 0
        assert (tmp_path / "shadow.jsonl").exists()


# ---------------------------------------------------------------------------
# 6. Purezza
# ---------------------------------------------------------------------------

def test_import_decision_non_carica_la_produzione():
    code = ("import decision;"
            "from decision.gateways import MarketQuotesGateway;"
            "import sys;"
            "print([m for m in ('tracker', 'auto_bet', 'bot', 'odds_api', 'sx_signals')"
            " if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout
