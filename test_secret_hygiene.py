"""Igiene dei segreti: nessuna credenziale in chiaro nel codice sorgente.

Il progetto legge TUTTE le chiavi API e le password SOLO da variabili
d'ambiente: il file .env locale (gitignored) viene caricato da
``config.load_dotenv()`` e la cartella protetta ``secrets/`` (vault cifrato
Fernet, gitignored) riempie solo le variabili ancora mancanti via
``secrets_store.load_secrets_dir()``. Su Railway i segreti vivono nelle env
vars del progetto. Nessun valore segreto deve MAI comparire in chiaro nei
sorgenti.

Questo test è la tripwire che rende la regola un contratto permanente: se
qualcuno incolla un token, una chiave o una password in un file .py del
repository, il test fallisce indicando file e riga incriminati.

Controlli:
1. Formati di credenziale reali nei sorgenti .py: bot token Telegram,
   GitHub PAT, Google API key, AWS access key, Slack token, Stripe,
   chiavi private PEM, "Bearer <segreto>".
2. Assegnazioni hardcoded a variabili dal nome credenziale
   (es. ``TOKEN = "..."``, ``os.environ["API_KEY"] = "..."``).
3. URL con credenziali incorporate (://user:password@host).
4. File .env* e cartella secrets/ MAI tracciati da git.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Cartelle escluse dallo scan (dipendenze, dati, segreti, altro repo).
_EXCLUDED_DIRS = {
    ".git", "venv", "data", "webapp", "secrets", "__pycache__",
    ".pytest_cache", ".vercel", ".railway", "node_modules", "_archivio",
}

# Parole che rendono un valore un segnaposto/fake voluto (mai un segreto reale).
_PLACEHOLDER = re.compile(
    r"(changeme|your[_-]|example|demo|fake|dummy|tripwire|placeholder|"
    r"xxxx+|REPLACE|TBD|testtoken|test[_-]?key|<[^>]+>|^test$|^password$|"
    r"^secret$|^token$)",
    re.IGNORECASE,
)

# Formati di credenziale riconoscibili (alta affidabilità anche fuori contesto).
_CREDENTIAL_FORMATS = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),                # bot token Telegram
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),                 # GitHub PAT
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),                     # Google API key
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                  # AWS access key
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),               # Slack token
    re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{10,}\b"),   # Stripe
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),             # chiave privata PEM
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}\b"),            # Bearer token
]

# Nomi di variabile "credential-like" per il controllo 2.
_NAME_HINT = re.compile(
    r"(TOKEN|API_?KEY|PASSWORD|PASSWD|SECRET|CLIENT_?SECRET|APP_?KEY|"
    r"ACCESS_?KEY|PRIVATE_?KEY|AUTH|BOT_?TOKEN|GITHUB_?TOKEN|MASTER_?KEY|"
    r"CREDENTIAL)",
    re.IGNORECASE,
)

_URL_USERINFO = re.compile(r"://[^/\s:@]+:[^/\s@]+@")

# Assegnazione in cima a riga:  NOME = "valore"
_ASSIGN = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[\"']([^\"']{6,})[\"']\s*(?:#.*)?$"
)
# Assegnazione a os.environ["NOME"] = "valore"
_ENVIRON_ASSIGN = re.compile(
    r'os\.environ\[["\'][A-Za-z0-9_]+["\']\]\s*=\s*["\']([^"\']{6,})["\']'
)


def _suspicious_value(value: str) -> bool:
    """True se il valore letterale sembra una credenziale vera."""
    for rx in _CREDENTIAL_FORMATS:
        mm = rx.search(value)
        # I valori marcati fake/dummy/test (token di prova nei test) sono
        # voluti: la guardia riguarda le credenziali vere.
        if mm and not _PLACEHOLDER.search(mm.group(0)):
            return True
    if _PLACEHOLDER.search(value):
        return False
    classes = sum(bool(p.search(value)) for p in (
        re.compile(r"[a-z]"), re.compile(r"[A-Z]"),
        re.compile(r"[0-9]"), re.compile(r"[^A-Za-z0-9]"),
    ))
    return (len(value) >= 10 and classes >= 2) or len(value) >= 24


def _source_files():
    self_path = Path(__file__).resolve()
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if any(part in _EXCLUDED_DIRS for part in rel.parts):
            continue
        if p.resolve() == self_path:
            continue
        yield p, rel


def _scan_sources() -> list[str]:
    """Ritorna le violazioni trovate: 'file:riga — motivo'."""
    violations: list[str] = []
    for path, rel in _source_files():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            where = f"{rel}:{lineno}"
            for rx in _CREDENTIAL_FORMATS:
                mm = rx.search(line)
                # I valori marcati fake/dummy/test (es. token di prova nei
                # test) sono voluti: la guardia riguarda le credenziali vere.
                if mm and not _PLACEHOLDER.search(mm.group(0)):
                    violations.append(f"{where} — formato credenziale rilevato")
                    break
            m = _ASSIGN.match(line)
            if m and _NAME_HINT.search(m.group(1)) and _suspicious_value(m.group(2)):
                violations.append(
                    f"{where} — credenziale hardcoded: {m.group(1)} = \"...\"")
            m = _ENVIRON_ASSIGN.search(line)
            if m and _suspicious_value(m.group(1)):
                violations.append(f"{where} — os.environ con valore hardcoded")
            if _URL_USERINFO.search(line):
                violations.append(f"{where} — URL con credenziali incorporate")
    return violations


def test_nessuna_credenziale_hardcoded_nei_sorgenti():
    violations = _scan_sources()
    assert not violations, (
        "Trovate credenziali in chiaro nei sorgenti! Tutte le chiavi/password "
        "devono arrivare da variabili d'ambiente (.env gitignored + vault "
        "secrets/vault.bin):\n" + "\n".join(violations)
    )


def _git_ls_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files"],
            capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return out.stdout.splitlines()


def test_file_segreti_mai_tracciati_in_git():
    """.env*, secrets/ e file chiave/certificato non devono essere nel tree."""
    tracked = _git_ls_files()
    bad = [f for f in tracked if re.search(
        r"(^|/)(\.env.*|secrets/.*|\.askpass_github\.sh)$"
        r"|\.(key|pem|p12|pfx)$", f)]
    assert not bad, (
        "File contenenti segreti tracciati da git (vanno in .gitignore e "
        "rimossi dall'indice):\n" + "\n".join(bad)
    )
