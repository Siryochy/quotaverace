"""
surebet_scanner.py – Scanner automatico di opportunità di arbitraggio (surebet)

Confronta quote tra bookmaker per lo stesso evento/mercato e individua
opportunità matematicamente valide di arbitraggio.

Ipotesi:
- Quote decimale (europea)
- Mercati con esiti mutualmente esclusivi e completi
- Commissioni bookmaker trascurate per semplicità (da aggiungere in v2)

ATTENZIONE: Lo scanner non ha accesso a feed quote in tempo reale.
            I dati di test sono esplicitamente mock e non presentati come reali.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple

# Riutilizziamo il contratto di quote normalizzato da odds_ingest
from config import DATA_DIR
from odds_ingest import load_odds

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Costanti configurabili
# ---------------------------------------------------------------------------
DEFAULT_MIN_MARGIN = 0.001  # 0.1% margine minimo per segnalare (abbassato per test)
DEFAULT_STAKE_UNIT = 100.0  # unità base per calcolo allocazione

# Percorso database opportunità (per verifica ex-post)
SUREBET_DB = DATA_DIR / "surebet_log.jsonl"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SurebetOpportunity:
    """
    Rappresenta un'opportunità di arbitraggio individuata.

    ATTENZIONE: Non è una garanzia di profitto. Il rendimento è teorico
    e dipende dalla disponibilità delle quote al momento della puntata.
    """
    timestamp: str
    evento: str
    mercato: str
    esiti: Tuple[str, ...]
    bookmakers: Tuple[str, ...]
    quote: Tuple[float, ...]
    margin: float
    allocazioni: Tuple[float, ...]
    rendimento_atteso: float
    fonte_dati: str
    nota_limitazione: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "evento": self.evento,
            "mercato": self.mercato,
            "esiti": list(self.esiti),
            "bookmakers": list(self.bookmakers),
            "quote": list(self.quote),
            "margin": self.margin,
            "allocazioni": list(self.allocazioni),
            "rendimento_atteso": self.rendimento_atteso,
            "fonte_dati": self.fonte_dati,
            "nota_limitazione": self.nota_limitazione,
        }


# ---------------------------------------------------------------------------
# Logica core
# ---------------------------------------------------------------------------

def calculate_inverse_sum(quote: List[float]) -> float:
    """
    Calcola la somma degli inversi delle quote (bookmaker margin).

    Se sum < 1.0 → opportunità di arbitraggio.
    Se sum = 1.0 → mercato efficiente.
    Se sum > 1.0 → vantaggio bookmaker.
    """
    return sum(1.0 / q for q in quote if q > 1.0)


def calculate_stake_allocation(
    quote: List[float],
    total_stake: float = DEFAULT_STAKE_UNIT,
) -> List[float]:
    """
    Calcola la ripartizione della puntata per garantire profitto uguale
    su ogni esito (allocazione proporzionale agli inversi).

    Formula: stake_i = (1/quote_i) / sum(1/quote_j) × total_stake
    """
    inv_sum = calculate_inverse_sum(quote)
    if inv_sum <= 0:
        return [0.0] * len(quote)

    allocations = []
    for q in quote:
        weight = (1.0 / q) / inv_sum
        allocations.append(round(weight * total_stake, 2))
    return allocations


def detect_surebet(
    odds_group: List[dict],
    min_margin: float = DEFAULT_MIN_MARGIN,
) -> Optional[SurebetOpportunity]:
    """
    Analizza un gruppo di quote per lo stesso evento/mercato e verifica
    se esiste un'opportunità di arbitraggio.

    Parametri
    ----------
    odds_group : list[dict]
        Quote dal contratto normalizzato (odds_ingest) per stesso evento+mercato.
    min_margin : float
        Margine minimo assoluto per segnalare.

    Ritorna
    -------
    SurebetOpportunity | None
    """
    if len(odds_group) < 2:
        return None

    # Raggruppa per esito, prendi la quota migliore per ciascun esito
    best_per_outcome: Dict[str, Tuple[float, str]] = {}
    for o in odds_group:
        esito = o.get("esito", "").strip()
        quota = o.get("quota_decimale", 0.0)
        bookmaker = o.get("bookmaker", "unknown")

        if not esito or quota <= 1.0:
            continue

        if esito not in best_per_outcome or quota > best_per_outcome[esito][0]:
            best_per_outcome[esito] = (quota, bookmaker)

    if len(best_per_outcome) < 2:
        return None

    esiti = tuple(sorted(best_per_outcome.keys()))
    quote = [best_per_outcome[e][0] for e in esiti]
    bookmakers = [best_per_outcome[e][1] for e in esiti]

    inv_sum = calculate_inverse_sum(quote)

    # Surebet valida solo se inv_sum < 1.0 (margine negativo)
    margin = inv_sum - 1.0
    if margin >= -min_margin or margin >= 0:
        return None

    allocazioni = calculate_stake_allocation(quote)
    rendimento = abs(margin) * 100

    if set(esiti) == {"1", "2"}:
        mercato = "1X2 (senza pareggio)"
    elif set(esiti) == {"1", "X", "2"}:
        mercato = "1X2"
    elif any("Over" in e for e in esiti) and any("Under" in e for e in esiti):
        mercato = "Over/Under"
    else:
        mercato = "Altro"

    return SurebetOpportunity(
        timestamp=datetime.now(timezone.utc).isoformat(),
        evento=odds_group[0].get("evento", "Sconosciuto"),
        mercato=mercato,
        esiti=esiti,
        bookmakers=tuple(bookmakers),
        quote=tuple(quote),
        margin=round(margin, 4),
        allocazioni=tuple(allocazioni),
        rendimento_atteso=round(rendimento, 2),
        fonte_dati="mock",
        nota_limitazione=(
            "Dati di prova statici. Nessun feed quote in tempo reale disponibile. "
            "Le quote potrebbero non essere attualmente offerte dai bookmaker indicati."
        ),
    )


def scan_surebets(
    odds_df,
    min_margin: float = DEFAULT_MIN_MARGIN,
) -> List[SurebetOpportunity]:
    """
    Scansiona un DataFrame di quote e individua tutte le surebet.
    """
    import pandas as pd

    opportunities = []

    for evento, group in odds_df.groupby("evento"):
        mercati_1x2 = group[group["esito"].isin(["1", "X", "2"])]
        mercati_ou = group[
            group["esito"].str.contains("Over|Under", case=False, na=False)
        ]

        if len(mercati_1x2) >= 2:
            opp = detect_surebet(mercati_1x2.to_dict("records"), min_margin)
            if opp:
                opportunities.append(opp)

        if len(mercati_ou) >= 2:
            opp = detect_surebet(mercati_ou.to_dict("records"), min_margin)
            if opp:
                opportunities.append(opp)

    opportunities.sort(key=lambda x: x.rendimento_atteso, reverse=True)
    return opportunities


# ---------------------------------------------------------------------------
# Persistenza e notifica
# ---------------------------------------------------------------------------

def log_opportunity(opp: SurebetOpportunity) -> None:
    """Registra l'opportunità emessa per verifica ex-post."""
    with open(SUREBET_DB, "a", encoding="utf-8") as f:
        f.write(json.dumps(opp.to_dict(), ensure_ascii=False) + "\n")


def format_telegram_notification(opp: SurebetOpportunity) -> str:
    """
    Formatta la notifica Telegram per una surebet.

    ATTENZIONE: Non descrive l'opportunità come garantita o priva di rischi.
    """
    lines = [
        f"⚡ *OPPORTUNITÀ ARBITRAGGIO – {opp.evento}*",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"📅 *Data:* {opp.timestamp[:19]} UTC",
        f"🏟 *Evento:* {opp.evento}",
        f"🎯 *Mercato:* {opp.mercato}",
        f"",
        f"📊 *Quote migliori per esito:*",
    ]

    for esito, bookmaker, quota, alloc in zip(
        opp.esiti, opp.bookmakers, opp.quote, opp.allocazioni
    ):
        lines.append(f"   • {esito}: {quota:.2f} ({bookmaker}) → puntata {alloc:.2f}u")

    lines.extend([
        f"",
        f"📈 *Margine arbitraggio:* {opp.margin * 100:.2f}%",
        f"💰 *Rendimento atteso:* {opp.rendimento_atteso:.2f}%",
        f"",
        f"⚠️ *NOTA IMPORTANTE*",
        f"Questa è un'opportunità teorica basata su dati di prova.",
        f"Non è garantita: le quote possono variare prima della puntata.",
        f"Verifica sempre la disponibilità effettiva sui bookmaker.",
        f"",
        f"🎲 *Gioca responsabilmente*",
        f"Le scommesse sono un gioco d'azzardo. Se hai bisogno di aiuto:",
        f"[www.adm.gov.it](https://www.adm.gov.it)",
    ])

    return "\n".join(lines)


def send_telegram_notification(
    opp: SurebetOpportunity,
    bot_token: str,
    chat_id: str,
) -> bool:
    """
    Invia notifica Telegram dell'opportunità.

    ATTENZIONE: Richiede token e chat_id validi.
    """
    try:
        from telegram import Bot
        import asyncio

        bot = Bot(token=bot_token)
        text = format_telegram_notification(opp)

        asyncio.run(bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
        ))
        return True
    except Exception as e:
        logger.error(f"Errore invio Telegram: {e}")
        return False


# ---------------------------------------------------------------------------
# Dati di prova espliciti (mock, NON presentati come real-time)
# ---------------------------------------------------------------------------

def get_mock_odds_for_testing() -> List[dict]:
    """
    Restituisce dati di prova statici per testare lo scanner.

    ATTENZIONE: Questi sono dati fittizi esplicitamente mock.
    Non rappresentano quote reali di alcun bookmaker.
    """
    return [
        # ==== EVENTO 1: Roma vs Empoli (NO surebet) ====
        {
            "bookmaker": "Bet365",
            "evento": "Serie A – Roma vs Empoli",
            "sport": "calcio",
            "esito": "1",
            "quota_decimale": 1.35,
            "timestamp": "2024-09-17T15:00:00Z",
        },
        {
            "bookmaker": "Snai",
            "evento": "Serie A – Roma vs Empoli",
            "sport": "calcio",
            "esito": "1",
            "quota_decimale": 1.33,
            "timestamp": "2024-09-17T15:00:00Z",
        },
        {
            "bookmaker": "Bet365",
            "evento": "Serie A – Roma vs Empoli",
            "sport": "calcio",
            "esito": "X",
            "quota_decimale": 5.00,
            "timestamp": "2024-09-17T15:00:00Z",
        },
        {
            "bookmaker": "Snai",
            "evento": "Serie A – Roma vs Empoli",
            "sport": "calcio",
            "esito": "X",
            "quota_decimale": 4.80,
            "timestamp": "2024-09-17T15:00:00Z",
        },
        {
            "bookmaker": "Bet365",
            "evento": "Serie A – Roma vs Empoli",
            "sport": "calcio",
            "esito": "2",
            "quota_decimale": 8.50,
            "timestamp": "2024-09-17T15:00:00Z",
        },
        {
            "bookmaker": "Snai",
            "evento": "Serie A – Roma vs Empoli",
            "sport": "calcio",
            "esito": "2",
            "quota_decimale": 8.00,
            "timestamp": "2024-09-17T15:00:00Z",
        },
        # ==== EVENTO 2: Surebet Test (SÌ surebet!) ====
        # 1/2.20 + 1/3.60 + 1/5.00 = 0.4545 + 0.2778 + 0.2000 = 0.9323 < 1.0
        # Margine = -6.77%, rendimento = +7.26%
        {
            "bookmaker": "Bet365",
            "evento": "Serie A – Surebet Test",
            "sport": "calcio",
            "esito": "1",
            "quota_decimale": 2.20,
            "timestamp": "2024-09-17T15:00:00Z",
        },
        {
            "bookmaker": "Pinnacle",
            "evento": "Serie A – Surebet Test",
            "sport": "calcio",
            "esito": "X",
            "quota_decimale": 3.60,
            "timestamp": "2024-09-17T15:00:00Z",
        },
        {
            "bookmaker": "Betfair",
            "evento": "Serie A – Surebet Test",
            "sport": "calcio",
            "esito": "2",
            "quota_decimale": 5.00,
            "timestamp": "2024-09-17T15:00:00Z",
        },
        # ==== EVENTO 3: Over/Under Test (NO surebet) ====
        {
            "bookmaker": "Bet365",
            "evento": "Serie A – Over Under Test",
            "sport": "calcio",
            "esito": "Over 2.5",
            "quota_decimale": 1.85,
            "timestamp": "2024-09-17T15:00:00Z",
        },
        {
            "bookmaker": "Snai",
            "evento": "Serie A – Over Under Test",
            "sport": "calcio",
            "esito": "Over 2.5",
            "quota_decimale": 1.80,
            "timestamp": "2024-09-17T15:00:00Z",
        },
        {
            "bookmaker": "Bet365",
            "evento": "Serie A – Over Under Test",
            "sport": "calcio",
            "esito": "Under 2.5",
            "quota_decimale": 2.00,
            "timestamp": "2024-09-17T15:00:00Z",
        },
        {
            "bookmaker": "Snai",
            "evento": "Serie A – Over Under Test",
            "sport": "calcio",
            "esito": "Under 2.5",
            "quota_decimale": 1.95,
            "timestamp": "2024-09-17T15:00:00Z",
        },
    ]


# ---------------------------------------------------------------------------
# Entry point per test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 70)
    print("QUOTVERACE – Surebet Scanner (MODALITÀ TEST)")
    print("=" * 70)
    print()
    print("⚠️  ATTENZIONE: Nessun feed quote in tempo reale disponibile.")
    print("    I dati seguenti sono esplicitamente mock di prova.")
    print()

    mock_data = get_mock_odds_for_testing()

    import pandas as pd
    df = pd.DataFrame(mock_data)

    # Debug: mostra somme inverse per ogni evento
    print("--- Verifica pre-scan ---")
    for evento, group in df.groupby("evento"):
        for subset_name, subset in [
            ("1X2", group[group["esito"].isin(["1", "X", "2"])]),
            ("O/U", group[group["esito"].str.contains("Over|Under", case=False, na=False)]),
        ]:
            if len(subset) > 0:
                best = subset.loc[subset.groupby("esito")["quota_decimale"].idxmax()]
                quote = best["quota_decimale"].tolist()
                inv_sum = calculate_inverse_sum(quote)
                margin = inv_sum - 1.0
                status = "✅ SUREBET" if margin < 0 else "❌ NO"
                print(f"  {evento} [{subset_name}]: quote={quote}, inv_sum={inv_sum:.4f}, margin={margin:.4f} {status}")
    print()

    opportunities = scan_surebets(df, min_margin=0.001)

    print(f"Opportunità individuate: {len(opportunities)}")
    print()

    for i, opp in enumerate(opportunities, 1):
        print(f"--- Opportunità #{i} ---")
        print(f"Evento: {opp.evento}")
        print(f"Mercato: {opp.mercato}")
        print(f"Esiti: {opp.esiti}")
        print(f"Quote: {opp.quote}")
        print(f"Bookmaker: {opp.bookmakers}")
        print(f"Margine: {opp.margin * 100:.2f}%")
        print(f"Allocazioni: {opp.allocazioni}")
        print(f"Rendimento atteso: {opp.rendimento_atteso:.2f}%")
        print(f"Fonte: {opp.fonte_dati}")
        print(f"Nota: {opp.nota_limitazione}")
        print()

        log_opportunity(opp)

    if not opportunities:
        print("Nessuna opportunità di arbitraggio individuata nei dati di prova.")
        print("(Prova ad abbassare min_margin o aggiungere quote più divergenti)")

    print()
    print(f"Log salvato in: {SUREBET_DB}")
    print("=" * 70)
