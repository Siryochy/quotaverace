"""Rating dinamici time-decay dagli ultimi risultati (tabella match_results)."""
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "quotaverace.db"
HALF_LIFE_DAYS = 100.0   # mezza vita (ricerca: 100-150 giorni)
PRIOR_MATCHES = 6.0      # forza del prior (shrinkage verso la media)
MIN_MATCHES = 1          # sotto questa soglia si usa il rating statico

GLOBAL_H = 1.52          # gol medi casa
GLOBAL_A = 1.28          # gol medi trasferta


def _weight(days_ago):
    return math.exp(-math.log(2) * days_ago / HALF_LIFE_DAYS)


def _parse_ts(ts):
    try:
        d = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        return d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def compute_ratings():
    """Calcola attack/defense (casa/trasferta) da match_results e li salva."""
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS team_ratings (
        team TEXT PRIMARY KEY, league TEXT,
        attack_home REAL, defense_home REAL,
        attack_away REAL, defense_away REAL,
        n_home INTEGER, n_away INTEGER, updated_at TEXT)""")
    c.execute("SELECT league, home_team, away_team, score_home, score_away, settled_at FROM match_results")
    rows = c.fetchall()

    acc = {}
    now = datetime.now(timezone.utc)
    for league, home, away, sh, sa, ts in rows:
        if sh is None or sa is None or not home or not away:
            continue
        ts_d = _parse_ts(ts)
        if ts_d:
            days_ago = (now - ts_d).total_seconds() / 86400.0
        else:
            days_ago = 0.0
        w = _weight(days_ago)
        for team, gf, ga, side in ((home, sh, sa, "h"), (away, sa, sh, "a")):
            a = acc.setdefault(team, {"league": set(), "h_gf": [], "h_ga": [], "a_gf": [], "a_ga": []})
            a["league"].add(league)
            (a["h_gf"] if side == "h" else a["a_gf"]).append((w, gf))
            (a["h_ga"] if side == "h" else a["a_ga"]).append((w, ga))

    def _rate(pairs, avg):
        # il conteggio n e' il numero REALE di partite, non la somma dei pesi,
        # cosi' il check MIN_MATCHES in get_rating riflette le partite giocate
        # e non viene azzerato dal time-decay (halflife ~100gg).
        if not pairs:
            return None, 0.0
        wsum = sum(w for w, _ in pairs)
        if wsum <= 0:
            return None, 0.0
        obs = sum(w * g for w, g in pairs) / wsum
        n = len(pairs)
        # coeff = obs / avg: scala i gol osservati in un COEFFICIENTE attorno a
        # 1 (molto prima del prior). expected_goals moltiplica: lam = avg*att*def,
        # quindi un coeff 1.0 = forza media, >1 attacca di piu', <1 difende di piu'.
        coeff = obs / avg if avg > 0 else 1.0
        return (coeff * wsum + 1.0 * PRIOR_MATCHES) / (wsum + PRIOR_MATCHES), n

    c.execute("DELETE FROM team_ratings")
    for team, a in acc.items():
        league = next(iter(a["league"]), "")
        atk_h, n_h = _rate(a["h_gf"], GLOBAL_H)
        def_h, _ = _rate(a["h_ga"], GLOBAL_H)
        atk_a, n_a = _rate(a["a_gf"], GLOBAL_A)
        def_a, _ = _rate(a["a_ga"], GLOBAL_A)
        if atk_h is None or atk_a is None:
            continue
        c.execute("""INSERT OR REPLACE INTO team_ratings VALUES (?,?,?,?,?,?,?,?,?)""",
                  (team, league, atk_h, def_h, atk_a, def_a,
                   n_h, n_a, datetime.now().isoformat()))
    conn.commit(); conn.close()
    return len(acc)


def get_rating(team):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("SELECT attack_home, defense_home, attack_away, defense_away, n_home, n_away FROM team_ratings WHERE team=?", (team,))
    row = c.fetchone(); conn.close()
    if not row:
        return None
    atk_h, def_h, atk_a, def_a, n_h, n_a = row
    if (n_h + n_a) < MIN_MATCHES:
        return None
    return {"attack_home": atk_h, "defense_home": def_h,
            "attack_away": atk_a, "defense_away": def_a}
