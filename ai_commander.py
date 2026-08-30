"""
QuotaVerace AI Commander
========================

"Comandante" AI berbasis Gemini yang mengorkestrasi seluruh mesin QuotaVerace:
 - Poisson engine (expected goals, probabilitas 1x2 / Over-Under / BTTS)
 - Value filter (EV, Kelly stake, sanity filter)
 - Surebet scanner
 - Rating engine & tracker (riwayat sinyal, performa)
 - Fixture engine (kalender hari ini, schedina, multipla)

Desain:
 - Agent meminta LLM memilih tool (function calling), lalu agent mengeksekusi
   fungsi Python lokal. Tidak ada SQL/akses file dari LLM — hanya tool binding.
 - Semua perhitungan kuantitatif tetap di mesin deterministik; LLM hanya
   memilih tool, merangkai urutan, dan menjelaskan hasil.
 - Fail-closed: error tool dikembalikan ke LLM sebagai teks, tidak pernah
   melempar exception keluar dari loop agent.

Pakai:
    from ai_commander import AICommander
    cmd = AICommander()
    print(cmd.run("/schedina"))
    print(cmd.run("analisa Inter vs Napoli dan beri stake untuk bankroll 500"))
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

logger = logging.getLogger("ai_commander")

MODEL_NAME = "gemini-3.6-flash"

SYSTEM_INSTRUCTION = (
    "Kamu adalah 'Comandante', otak AI di balik bot sampingan QuotaVerace. "
    "Tugasmu menjawab pertanyaan pengguna tentang taruhan sepak bola dengan "
    "MEMANGGIL tool yang tersedia, lalu menjelaskan hasilnya dengan ringkas.\n"
    "Aturan:\n"
    "1. Selalu gunakan tool untuk data (jangan pernah mengarang angka, "
    "probabilitas, atau quota).\n"
    "2. Boleh memanggil beberapa tool berurutan (mis. kalender dulu, lalu "
    "analisa pertandingan, lalu stake).\n"
    "3. Setelah tool kembali, rangkum hasilnya dalam bahasa pengguna, "
    "sebutkan stake Kelly Pro bila relevan, dan sertakan disclaimer singkat.\n"
    "4. Jika pertanyaan tidak butuh tool (mis. menjelaskan konsep EV), "
    "jawab langsung dan tetap sertakan disclaimer.\n"
    "5. Jangan pernah menjanjikan profit. Ini alat bantu keputusan, bukan "
    "jaminan hasil."
)

DISCLAIMER = (
    "\n\n🎲 *Gioca responsabilmente* — alat bantu keputusan, bukan jaminan profit."
)


# ---------------------------------------------------------------------------
# Tool bindings — tipai tipis di atas mesin QuotaVerace yang sudah ada.
# ---------------------------------------------------------------------------

def _tool_analyze_match(home: str, away: str) -> dict:
    """Analisis pertandingan: expected goals, probabilitas 1X2, Over 2.5."""
    from poisson_engine import expected_goals, prob_1x2, prob_over_under
    lam_h, lam_a = expected_goals(home, away)
    p1, px, p2 = prob_1x2(lam_h, lam_a)
    p_over, p_under = prob_over_under(lam_h, lam_a)
    return {
        "home": home, "away": away,
        "expected_goals": {"home": round(lam_h, 2), "away": round(lam_a, 2)},
        "probabilitas": {
            "1": round(p1, 4), "X": round(px, 4), "2": round(p2, 4),
            "over_2_5": round(p_over, 4), "under_2_5": round(p_under, 4),
        },
    }


def _tool_value_filter(probability: float, quota: float) -> dict:
    """EV + rekomendasi stake Kelly Pro untuk satu peluang."""
    from value_filter import compute_ev, get_pro_stake, is_sane
    ev = compute_ev(probability, quota)
    sane, reason = is_sane(probability, quota, ev)
    pro = get_pro_stake(_get_bankroll(), probability, quota)
    return {
        "ev": round(ev, 4),
        "sane": sane,
        "filter_reason": None if sane else reason,
        "stake_eur": pro["stake"],
        "stake_pct_of_bankroll": pro["stake_pct_of_bankroll"],
    }


def _tool_surebets() -> dict:
    """Scan surebet (arbitrag) dari odds yang ada."""
    from surebet_scanner import scan_surebets
    from odds_ingest import load_odds
    sures = scan_surebets(load_odds())
    return {"count": len(sures), "surebets": sures[:5]}


def _tool_performance(days: int = 30) -> dict:
    """Ringkasan performa sinyal (chiusi, won, lost, ROI, hit rate)."""
    from tracker import get_performance_summary
    return get_performance_summary(days=max(1, min(days, 90)))


def _tool_recent_signals(limit: int = 10) -> dict:
    """N sinyal terakhir dari tracker."""
    from tracker import get_signals
    signals = get_signals(limit=max(1, min(limit, 30)))
    return {"count": len(signals), "signals": [
        {"evento": s.evento, "esito": s.esito, "quota": s.quota,
         "ev": s.ev, "esito_finale": s.esito_finale} for s in signals
    ]}


def _tool_calendar() -> dict:
    """Kalender pertama hari ini (dari DB, tanpa panggil API eksternal)."""
    from fixture_engine import get_value_picks_for_schedina
    picks = get_value_picks_for_schedina()
    return {"count": len(picks), "matches": [
        {"league": p["league"], "home": p["home"], "away": p["away"],
         "esito": p["esito"], "quota": p["quota"], "ev": p["ev"]}
        for p in picks
    ]}


_bankroll_state = {"value": 100.0}


def _get_bankroll() -> float:
    return _bankroll_state["value"]


def _tool_set_bankroll(amount: float) -> dict:
    """Set bankroll referensi untuk perhitungan stake."""
    _bankroll_state["value"] = max(10.0, float(amount))
    return {"bankroll": _bankroll_state["value"]}


TOOLS: dict[str, Callable[..., dict]] = {
    "analyze_match": _tool_analyze_match,
    "value_filter": _tool_value_filter,
    "surebets": _tool_surebets,
    "performance": _tool_performance,
    "recent_signals": _tool_recent_signals,
    "calendar": _tool_calendar,
    "set_bankroll": _tool_set_bankroll,
}

TOOL_DECLARATIONS = [
    {"name": "analyze_match",
     "description": "Analisis pertandingan: expected goals dan probabilitas 1X2, Over/Under 2.5.",
     "parameters": {"type": "OBJECT", "properties": {
         "home": {"type": "STRING"}, "away": {"type": "STRING"}},
         "required": ["home", "away"]}},
    {"name": "value_filter",
     "description": "EV dan stake Kelly Pro untuk satu peluang (probabilitas 0-1, quota desimal).",
     "parameters": {"type": "OBJECT", "properties": {
         "probability": {"type": "NUMBER"}, "quota": {"type": "NUMBER"}},
         "required": ["probability", "quota"]}},
    {"name": "surebets",
     "description": "Scan surebet (arbitrag) dari odds yang tersedia.",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "performance",
     "description": "Ringkasan performa sinyal: chiusi, won, lost, ROI, hit rate.",
     "parameters": {"type": "OBJECT", "properties": {
         "days": {"type": "INTEGER"}}}},
    {"name": "recent_signals",
     "description": "N sinyal terakhir dari tracker.",
     "parameters": {"type": "OBJECT", "properties": {
         "limit": {"type": "INTEGER"}}}},
    {"name": "calendar",
     "description": "Kalender/value picks hari ini dari database lokal.",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "set_bankroll",
     "description": "Set bankroll referensi (EUR) untuk perhitungan stake.",
     "parameters": {"type": "OBJECT", "properties": {
         "amount": {"type": "NUMBER"}}, "required": ["amount"]}},
]


class AICommander:
    """Agent loop Gemini dengan function calling ke mesin QuotaVerace."""

    def __init__(self, model_name: str = MODEL_NAME, api_key: str | None = None,
                 max_steps: int = 6):
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(
            api_key=api_key or os.environ["GOOGLE_API_KEY"])
        self._model_name = model_name
        self._max_steps = max_steps
        self._config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            temperature=0.2,
        )

    # -- tool dispatch ------------------------------------------------------
    def _dispatch(self, name: str, args: dict[str, Any]) -> dict:
        fn = TOOLS.get(name)
        if fn is None:
            return {"error": f"tool tidak dikenal: {name}"}
        try:
            return fn(**args)
        except Exception as e:  # fail-closed: error jadi teks untuk LLM
            logger.exception("tool %s gagal", name)
            return {"error": f"{type(e).__name__}: {e}"}

    # -- agent loop ---------------------------------------------------------
    def run(self, prompt: str) -> str:
        from google.genai import types

        # Daftar turn eksplisit: user -> (model+function calls) -> user(function responses)...
        turns: list[Any] = [types.Content(role="user", parts=[types.Part(text=prompt)])]
        for _ in range(self._max_steps):
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=turns,
                config=self._config,
            )
            calls = list(response.function_calls or [])
            if not calls:
                return response.text or ""

            # simpan turn model (berisi function calls) apa adanya
            model_turn = response.candidates[0].content
            turns.append(model_turn)

            # eksekusi semua function calls, kirim hasil sebagai turn user
            response_parts: list[Any] = []
            for call in calls:
                result = self._dispatch(call.name, dict(call.args or {}))
                response_parts.append(types.Part.from_function_response(
                    name=call.name, response={"result": json.dumps(result)}))
            turns.append(types.Content(role="user", parts=response_parts))
        return "Batas langkah agent tercapai sebelum jawaban final."

    # -- helper kenyamanan ---------------------------------------------------
    def ask(self, prompt: str) -> str:
        """Alias run() — jawaban final sebagai teks."""
        return self.run(prompt)


def main() -> None:
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    prompt = " ".join(sys.argv[1:]) or "/schedina"
    commander = AICommander()
    print(commander.run(prompt))


if __name__ == "__main__":
    main()
