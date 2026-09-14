"""Test della guardia anti-drift (`railway_drift_check.py`) — tutti OFFLINE.

Nessuna chiamata alla CLI `railway`: il runner e' iniettato, quindi si testano
classificazione, codici di uscita e fail-safe senza rete.
"""

import json

import pytest

import railway_drift_check as rdc


class FakeResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def plan(**change_set):
    """Piano minimo nella forma reale di `railway config plan --json`."""
    return {
        "ok": True,
        "command": "plan",
        "currentEnvironment": {"environmentName": "production"},
        "changeSet": change_set if change_set else {"changes": []},
        "diff": "",
    }


def change(summary="Update x", severity="safe", kind="resource.update", details=None):
    out = {"summary": summary, "severity": severity, "kind": kind}
    if details:
        out["details"] = details
    return out


def fake_runner(payload, returncode=0, stderr=""):
    """Runner finto con la firma `(argv, **kwargs)` di `subprocess.run`."""
    captured = {}

    def runner(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return FakeResult(stdout=body, stderr=stderr, returncode=returncode)

    runner.captured = captured
    return runner


# ---------------------------------------------------------------------------
# 1. Il comando lanciato (tripwire sul falso errore del wrapper)
# ---------------------------------------------------------------------------

class TestComando:
    def test_forma_esatta_del_comando(self):
        assert rdc.PLAN_COMMAND == ["railway", "config", "plan", "--json"]

    def test_nessun_wrapper_nel_comando(self):
        """`timeout`/`nice` cambiano `$process.env._` e rompono il check di
        versione di railway/iac (falso "requires Railway CLI 5.42.1")."""
        for wrapper in ("timeout", "nice", "xargs", "env", "sh", "bash"):
            assert wrapper not in rdc.PLAN_COMMAND

    def test_nessuna_shell(self):
        source = open(rdc.__file__, encoding="utf-8").read()
        assert "shell=True" not in source

    def test_child_env_imposta_l_eseguibile_vero(self, monkeypatch):
        """`$_` deve puntare alla CLI, non all'interprete che lancia la guardia
        (altrimenti railway/iac confronta "Python 3.12.1" con 5.42.1 e fallisce)."""
        monkeypatch.setattr(rdc.shutil, "which", lambda name: "/usr/bin/railway-finto")
        env = rdc.child_env({"PATH": "/usr/bin", "_": "/usr/bin/python"})
        assert env["_"] == "/usr/bin/railway-finto"
        assert env["PATH"] == "/usr/bin"          # il resto dell'ambiente resta

    def test_child_env_senza_cli_non_inventa_nulla(self, monkeypatch):
        monkeypatch.setattr(rdc.shutil, "which", lambda name: None)
        env = rdc.child_env({"_": "/usr/bin/python"})
        assert env["_"] == "/usr/bin/python"

    def test_runner_di_default_usa_child_env(self, monkeypatch):
        seen = {}

        class R:
            stdout = json.dumps(plan(changes=[]))
            stderr = ""
            returncode = 0

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return R()

        monkeypatch.setattr(rdc.subprocess, "run", fake_run)
        monkeypatch.setattr(rdc.shutil, "which", lambda name: "/usr/bin/railway-finto")
        rdc.run_plan()
        assert seen["env"]["_"] == "/usr/bin/railway-finto"
        assert seen["capture_output"] is True and "shell" not in seen


# ---------------------------------------------------------------------------
# 2. Classificazione delle modifiche
# ---------------------------------------------------------------------------

class TestIsDestructive:
    def test_severity_destructive(self):
        assert rdc.is_destructive(change(severity="destructive", kind="variable.delete"))

    def test_severity_safe_vince_sul_kind(self):
        # la severity esplicita e' autorevole: non tiriamo a indovinare dal nome
        assert not rdc.is_destructive(change(severity="safe", kind="resource.remove"))

    def test_senza_severity_si_guarda_il_kind(self):
        for kind in ("variable.delete", "resource.destroy", "service.remove"):
            assert rdc.is_destructive({"kind": kind})

    def test_kind_safe_senza_severity(self):
        assert not rdc.is_destructive({"kind": "resource.update"})

    def test_input_non_dict(self):
        assert not rdc.is_destructive(None)
        assert not rdc.is_destructive("boh")


class TestAnalyze:
    def test_piano_pulito(self):
        analysis = rdc.analyze(plan(changes=[change()]))
        assert analysis["ok"] is True
        assert analysis["n_changes"] == 1
        assert analysis["destructive"] == []
        assert analysis["environment"] == "production"

    def test_piano_vuoto_e_ok(self):
        analysis = rdc.analyze(plan())
        assert analysis["ok"] is True and analysis["n_changes"] == 0

    def test_piano_con_cancellazione_e_drift(self):
        analysis = rdc.analyze(plan(changes=[
            change(summary="Update api-volume config.isCreated"),
            change(summary="Delete variable api.TENNIS_SANDBOX_ENABLED",
                   severity="destructive", kind="variable.delete",
                   details=["TENNIS_SANDBOX_ENABLED"]),
        ]))
        assert analysis["ok"] is False
        assert analysis["n_changes"] == 2
        assert len(analysis["destructive"]) == 1
        assert "TENNIS_SANDBOX_ENABLED" in analysis["destructive"][0]

    def test_resource_update_safe_non_e_drift(self):
        """`config.isCreated` ricompare sempre nel piano: non deve bloccare."""
        assert rdc.analyze(plan(changes=[
            change(summary="Update api-volume config.isCreated", severity="safe",
                   kind="resource.update", details=["config.isCreated (null → true)"])]))["ok"] is True

    def test_piano_senza_changeset_e_errore(self):
        with pytest.raises(rdc.PlanError):
            rdc.analyze({"ok": True})

    def test_piano_ok_false_e_errore(self):
        with pytest.raises(rdc.PlanError):
            rdc.analyze({"ok": False, "error": "boom"})

    def test_report_leggibile(self):
        good = rdc.format_report(rdc.analyze(plan(changes=[change()])))
        assert "✅" in good and "apply sicuro" in good
        bad = rdc.format_report(rdc.analyze(plan(changes=[
            change(severity="destructive", kind="variable.delete",
                   summary="Delete variable api.TENNIS_SANDBOX_ENABLED")])))
        assert "❌" in bad and "preserve()" in bad


# ---------------------------------------------------------------------------
# 3. Esecuzione del piano (runner iniettato) e fail-safe
# ---------------------------------------------------------------------------

class TestRunPlan:
    def test_runner_riceve_il_comando_giusto(self):
        runner = fake_runner(plan(changes=[]))
        rdc.run_plan(runner=runner)
        assert runner.captured["argv"] == rdc.PLAN_COMMAND

    def test_stdout_vuoto_e_errore(self):
        with pytest.raises(rdc.PlanError, match="piano vuoto"):
            rdc.run_plan(runner=fake_runner("", returncode=1, stderr="not linked"))

    def test_stdout_non_json_e_errore(self):
        with pytest.raises(rdc.PlanError, match="non JSON"):
            rdc.run_plan(runner=fake_runner("errore: qualcosa e' andato storto"))

    def test_ok_false_e_errore(self):
        with pytest.raises(rdc.PlanError):
            rdc.run_plan(runner=fake_runner({"ok": False, "error": "render fallito"}))

    def test_cli_assente_e_errore(self):
        def runner(argv, **kwargs):
            raise FileNotFoundError("railway non trovato")

        with pytest.raises(rdc.PlanError, match="non trovata"):
            rdc.run_plan(runner=runner)

    def test_eccezione_generica_diventa_planerror(self):
        def runner(argv, **kwargs):
            raise TimeoutError("scaduto")

        with pytest.raises(rdc.PlanError, match="TimeoutError"):
            rdc.run_plan(runner=runner)


# ---------------------------------------------------------------------------
# 4. Codici di uscita della CLI
# ---------------------------------------------------------------------------

class TestMain:
    def test_piano_pulito_exit_0(self, capsys):
        assert rdc.main([], runner=fake_runner(plan(changes=[change()]))) == 0
        assert "✅" in capsys.readouterr().out

    def test_drift_exit_1(self, capsys):
        code = rdc.main([], runner=fake_runner(plan(changes=[
            change(severity="destructive", kind="variable.delete",
                   summary="Delete variable api.TENNIS_SANDBOX_ENABLED")])))
        assert code == 1
        assert "DISTRUTTIVA" in capsys.readouterr().out

    def test_piano_non_valutabile_exit_2(self, capsys):
        """MAI un falso ok: un piano illeggibile esce 2, non 0."""
        assert rdc.main([], runner=fake_runner("")) == 2
        err = capsys.readouterr().err
        assert "non valutabile" in err and "exit 2" in err

    def test_json_output(self, capsys):
        code = rdc.main(["--json"], runner=fake_runner(plan(changes=[change()])))
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["ok"] is True

    def test_verbose_mostra_i_dettagli(self, capsys):
        rdc.main(["--verbose"], runner=fake_runner(plan(changes=[
            change(severity="destructive", kind="variable.delete",
                   summary="Delete variable x", details=["x"])])))
        assert "dettaglio" in capsys.readouterr().out
