"""railway_drift_check.py — guardia anti-drift dell'infrastruttura as code.

Perche' esiste. Il 14/09/2026 `railway config plan` ha rivelato che un
`config apply` avrebbe **cancellato** due variabili di produzione presenti su
Railway ma NON dichiarate in `.railway/railway.ts`:
`api.TENNIS_SANDBOX_ENABLED` (avrebbe spento scan+settle del sandbox tennis) e
`surebet.SUREBET_CRON_HOLD_SECONDS`. Una variabile impostata da dashboard e non
dichiarata nel file viene distrutta da apply: il file protegge con `preserve()`
tutto cio' che e' gestito a mano.

Uso:
    venv/bin/python railway_drift_check.py [--json] [--verbose]

Codici di uscita:
    0 = nessuna modifica distruttiva (apply sicuro)
    1 = drift distruttiva: NON applicare, dichiarare prima la variabile/risorsa
    2 = piano non valutabile (CLI assente, errore di rete/render): MAI un
        falso "ok" — un piano che non si legge non e' un piano pulito.

⚠️ `railway/iac` verifica la versione dell'eseguibile con `$process.env._
--version`: quando la CLI viene lanciata da un INTERPRETE o da un wrapper, `$_`
punta a quello (`python --version` -> "3.12.1" < 5.42.1, `timeout --version` ->
"8.32") e si ottiene il falso errore "This version of railway/iac requires
Railway CLI 5.42.1 or newer". La guardia lo evita da sola: `_child_env()`
imposta `_` al percorso reale della CLI (`shutil.which`) nell'ambiente del
figlio, quindi funziona sia da shell sia da Python. Tripwire:
`test_railway_drift_check.py` (forma esatta del comando, nessun wrapper, `_`
impostato sull'eseguibile vero; `subprocess.run(timeout=...)` NON e' un wrapper,
e' un parametro Python).

Nota: `api-volume config.isCreated` ricompare SEMPRE nel piano (e' un marker di
autorizzazione lato file, non un campo che Railway memorizza). Per questo la
guardia controlla **"0 to destroy"**, non "0 modifiche": un piano con solo
`resource.update` safe e' accettabile.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Callable, Optional, Sequence

# Forma ESATTA del comando (vedi l'avvertenza sui wrapper nel docstring).
PLAN_COMMAND = ["railway", "config", "plan", "--json"]
PLAN_TIMEOUT_SECONDS = 180
DESTRUCTIVE_SEVERITIES = {"destructive", "danger", "critical"}
DESTRUCTIVE_KINDS = ("delete", "destroy", "remove")


class PlanError(RuntimeError):
    """Il piano non e' stato prodotto o non e' leggibile."""


def child_env(base: Optional[dict] = None) -> dict:
    """Ambiente per il processo figlio con `_` = percorso REALE della CLI.

    Il check di compatibilita' di `railway/iac` esegue `$process.env._ --version
    ` e pretende una terna x.y.z >= 5.42.1: se `_` eredita l'interprete che ha
    lanciato la guardia (`python railway_drift_check.py` -> `$_` = il python
    della venv) il piano fallisce con un falso errore di versione. Lo forziamo
    al binario `railway` risolto con `shutil.which`; se non e' risolvibile non
    tocchiamo nulla (meglio l'errore originale che un ambiente inventato).
    """
    env = dict(os.environ if base is None else base)
    executable = shutil.which(PLAN_COMMAND[0])
    if executable:
        env["_"] = executable
    return env


def is_destructive(change: Any) -> bool:
    """True se la singola modifica distrugge qualcosa.

    La `severity` esplicita vince sul `kind`: se Railway la marca `safe` non
    facciamo supposizioni dal nome. Senza severity ci affidiamo ai verbi
    distruttivi del `kind` (`variable.delete`, `resource.remove`, ...).
    """
    item = change if isinstance(change, dict) else {}
    severity = str(item.get("severity") or "").strip().lower()
    if severity:
        return severity in DESTRUCTIVE_SEVERITIES
    kind = str(item.get("kind") or "").strip().lower()
    return any(token in kind for token in DESTRUCTIVE_KINDS)


def _change_line(change: Any, verbose: bool = False) -> str:
    item = change if isinstance(change, dict) else {}
    summary = str(item.get("summary") or item.get("kind") or "modifica sconosciuta")
    severity = str(item.get("severity") or "?")
    line = f"[{severity}] {summary}"
    if verbose:
        details = item.get("details")
        if isinstance(details, (list, tuple)) and details:
            line += " — " + "; ".join(str(d) for d in details)
    return line


def analyze(plan: Any) -> dict:
    """Classifica un piano gia' parsato (funzione PURA: nessuna rete)."""
    data = plan if isinstance(plan, dict) else {}
    if data.get("ok") is False:
        raise PlanError(str(data.get("error") or "il piano riporta ok=false"))
    change_set = data.get("changeSet")
    if not isinstance(change_set, dict):
        raise PlanError("piano senza 'changeSet': formato inatteso")
    raw = change_set.get("changes")
    changes = [c for c in raw] if isinstance(raw, (list, tuple)) else []
    destructive = [c for c in changes if is_destructive(c)]
    return {
        "ok": not destructive,
        "n_changes": len(changes),
        "changes": [_change_line(c) for c in changes],
        "destructive": [_change_line(c, verbose=True) for c in destructive],
        "diff": data.get("diff") or "",
        "environment": (data.get("currentEnvironment") or {}).get("environmentName"),
    }


def run_plan(runner: Optional[Callable[..., Any]] = None) -> dict:
    """Esegue `railway config plan --json` e ritorna il piano parsato."""
    if runner is None:
        def runner(argv: Sequence[str], **kwargs: Any):   # noqa: ANN001
            return subprocess.run(list(argv), capture_output=True, text=True,
                                  env=child_env(),
                                  timeout=kwargs.get("timeout", PLAN_TIMEOUT_SECONDS))
    try:
        result = runner(PLAN_COMMAND, timeout=PLAN_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise PlanError(f"CLI `railway` non trovata: {exc}") from exc
    except Exception as exc:  # timeout, permessi, ...
        raise PlanError(f"impossibile eseguire `railway config plan`: {type(exc).__name__}: {exc}") from exc

    stdout = getattr(result, "stdout", "") or ""
    if not stdout.strip():
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise PlanError(f"piano vuoto (exit {getattr(result, 'returncode', '?')})"
                        + (f": {stderr[:300]}" if stderr else ""))
    try:
        plan = json.loads(stdout)
    except ValueError as exc:
        raise PlanError(f"output del piano non JSON: {exc}") from exc
    plan = plan if isinstance(plan, dict) else {}
    if plan.get("ok") is not True:
        raise PlanError(f"piano non valido: {plan.get('error') or plan}")
    return plan


def format_report(analysis: dict) -> str:
    """Report leggibile (Telegram-friendly)."""
    lines = ["🧱 Drift infrastruttura (railway config plan)"]
    if analysis.get("environment"):
        lines.append(f"  ambiente   : {analysis['environment']}")
    lines.append(f"  modifiche  : {analysis['n_changes']}")
    if analysis["ok"]:
        lines.append("  esito      : ✅ nessuna modifica distruttiva (apply sicuro)")
        for change in analysis["changes"]:
            lines.append(f"    · {change}")
    else:
        lines.append(f"  esito      : ❌ {len(analysis['destructive'])} modifica/e DISTRUTTIVA/E — non applicare")
        for change in analysis["destructive"]:
            lines.append(f"    · {change}")
        lines.append("  fix        : dichiara la variabile/risorsa in .railway/railway.ts "
                     "(preserve() per le env gestite da dashboard) e riprova")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None,
         runner: Optional[Callable[..., Any]] = None) -> int:
    parser = argparse.ArgumentParser(description="Guardia anti-drift di .railway/railway.ts")
    parser.add_argument("--json", action="store_true", help="output JSON")
    parser.add_argument("--verbose", action="store_true", help="mostra i dettagli delle modifiche")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        analysis = analyze(run_plan(runner=runner))
    except PlanError as exc:
        print(f"⚠️  piano non valutabile: {exc}", file=sys.stderr)
        print("    (exit 2: nessuna garanzia, NON considerarlo un via libera)", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(analysis, ensure_ascii=False, indent=2))
    else:
        print(format_report(analysis))
        if args.verbose and analysis["changes"]:
            print("  dettaglio  :")
            for change in analysis["changes"]:
                print(f"    · {change}")
    return 0 if analysis["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
