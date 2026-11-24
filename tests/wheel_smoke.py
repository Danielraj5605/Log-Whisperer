"""Build a wheel and exercise its installed console script outside the source tree."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]
MARKER = "synthetic-wheel-marker"
INCIDENT = f"FATAL smoke incident password={MARKER}\n"


def exercise(cli, work, env):
    def run(*args, stdin=""):
        result = subprocess.run(
            [cli, *args], cwd=work, env=env, input=stdin, text=True,
            encoding="utf-8", capture_output=True, timeout=45,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert MARKER not in output, f"Unredacted output from {args[0]}"
        return output

    watch = run("watch", "--stdin", "--local", "--no-notification", stdin=INCIDENT)
    summary = json.loads(watch.split("Session summary: ")[-1])
    assert summary["pending"] == 0 and summary["delivered"]["jsonl"] == 1, summary

    log = work / "sample.log"
    log.write_text(INCIDENT, encoding="utf-8")
    replay = run("analyze", "--local", "--file", log.name)
    assert "Scanned 1 lines" in replay, replay
    preview = run("chat", "--preview", "--file", log.name)
    assert "smoke incident" in preview and "[redacted]" in preview, preview

    records = (work / ".logwhisper/alerts.jsonl").read_text(encoding="utf-8")
    assert MARKER not in records
    findings = [json.loads(line) for line in records.splitlines()]
    assert [f["source"] for f in findings] == ["stdin", log.name]
    assert all("[redacted]" in f["root_signal"] for f in findings)


def main():
    with tempfile.TemporaryDirectory(prefix="logwhisper-wheel-") as directory:
        work = Path(directory).resolve()
        assert not work.is_relative_to(ROOT), "Use a temporary directory outside the checkout"
        venv_dir = work / "venv"
        venv.create(venv_dir, with_pip=True)
        scripts = venv_dir / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        cli = scripts / ("logwhisper.exe" if os.name == "nt" else "logwhisper")
        env = os.environ.copy()
        for name in ("PYTHONPATH", "PYTHONHOME", "GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            env.pop(name, None)
        env.update(PYTHONNOUSERSITE="1", PYTHONUTF8="1")

        def pip(*args):
            subprocess.run([python, "-m", "pip", "--disable-pip-version-check", *args],
                           cwd=work, env=env, timeout=360, check=True)

        pip("wheel", "--no-deps", "--wheel-dir", str(work), str(ROOT))
        wheel, = work.glob("logwhisper-*.whl")
        pip("install", str(wheel))
        pip("check")
        env.update(APPDATA=str(work), XDG_CONFIG_HOME=str(work))
        exercise(cli, work, env)
        print(f"{wheel.name}: watch, replay, preview and redaction passed")


if __name__ == "__main__":
    main()
