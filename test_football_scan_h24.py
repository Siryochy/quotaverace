"""Tripwire scansione calcio 1X2 H24/7 (nessun gating weekend).

La scansione del calcio deve operare in modalita' continua: i job
mattutino/pomeridiano/serale girano OGNI giorno (run_daily di
python-telegram-bot, senza filtro giorni) e la pipeline
(fixture_engine/odds_api) non applica alcuna logica basata sul giorno
della settimana. Questo test blocca chiunque introduca una limitazione
\"solo weekend\" nel percorso di scansione.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_scan_jobs_registrati_daily_senza_giorni():
    """I 3 job di scansione sono run_daily: girano tutti i 7 giorni."""
    src = _read("bot.py")
    for job in ("morning_job", "afternoon_job", "evening_job"):
        assert f"run_daily({job}" in src, f"{job} non registrato come daily"
    # Nessuna chiamata run_daily con argomento `days=` (filtro weekend)
    import re
    daily_calls = re.findall(r"run_daily\([^)]*\)", src)
    for call in daily_calls:
        assert "days=" not in call, f"gating giorni in run_daily: {call}"


def test_nessun_gating_settimanale_nella_pipeline():
    """fetch_and_analyze_today e la rotazione quote non dipendono dal
    giorno della settimana (assenza di isoweekday/weekday/%7/strftime)."""
    for name in ("fixture_engine.py", "odds_api.py"):
        src = _read(name)
        for banned in ("isoweekday", ".weekday(", "strftime(\"%A\"",
                       "strftime('%A')", "%w"):
            assert banned not in src, f"{name}: gating {banned} trovato"
        # il gating implicito (day % 7) non deve esistere nel codice di
        # scansione/rotazione (intervalli in giorni SI', non in giorni
        # della settimana)
        assert "day % 7" not in src


def test_rotazione_intervalli_non_settimanale():
    """SPORTS_INTERVAL_DAYS usa intervalli in giorni (rotazione cache),
    NON una programmazione per giorno della settimana."""
    src = _read("odds_api.py")
    # gli intervalli sono definiti come numeri di giorni
    assert "SPORTS_INTERVAL_DAYS" in src
    for banned in ("monday", "tuesday", "wednesday", "thursday",
                   "friday", "saturday", "sunday"):
        assert banned not in src.lower(), f"gating {banned} in odds_api.py"


def test_auto_bet_24_7_registrato_repeating():
    """Il giro puntate e' 24/7: run_repeating OGNI MINUTO, non daily."""
    import re
    src = _read("bot.py")
    assert "run_repeating(auto_bet_job" in src
    # Frequenza continua (09/09): 60s, mai piu' intervalli orari/3h.
    assert re.search(r"run_repeating\(auto_bet_job[^)]*interval=60\b", src), \
        "auto_bet_job deve girare ogni minuto (interval=60)"
    assert "days=" not in src.split("run_repeating(auto_bet_job")[1][:300]