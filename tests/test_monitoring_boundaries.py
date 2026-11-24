import asyncio
import json
from xml.etree import ElementTree as ET

import pytest
from typer.testing import CliRunner

from agent import app
from agent.agent import _format_context
from agent.pipeline import Monitor
from agent.privacy import redact
from agent.storage import append_record
from buffer.parser import parse
from config import load
from output.windows_notification import WindowsNotifier


def test_redaction_and_source_scope():
    incident = parse("ERROR password=private-123", source="api")
    neighbor = parse("INFO unrelated customer record", source="billing")
    prompt = _format_context({"rule": "test", "log_obj": incident, "stats": {},
                              "buffer_window": [neighbor, incident]})
    assert "private-123" not in prompt
    assert "customer record" not in prompt
    assert "[redacted]" in prompt
    assert "abc-token" not in redact("Authorization: Bearer abc-token")
    assert "abc-token" not in redact('{"api_key": "abc-token"}')


def test_native_toast_treats_punctuation_as_literal():
    marker = "ordinary <xml> & punctuation ' and Unicode ?"
    xml = WindowsNotifier()._xml({"root_signal": marker})
    assert marker in ET.fromstring(xml).findall(".//text")[1].text


def test_nested_config_defaults_and_diagnostic(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("llm: disabled\nbuffer: {max_lines: -4}\n")
    config = load(path)
    assert config["buffer"]["max_lines"] == 500
    assert config["llm"]["model"]
    assert "buffer.max_lines" in caplog.text


def test_log_rotation_preserves_whole_records(tmp_path):
    path = tmp_path / "service.log"
    for line in ["one", "two", "six"]:
        append_record(path, line, {"max_bytes": 5, "backups": 1})
    assert path.read_text() == "six\n"
    assert (tmp_path / "service.log.1").read_text() == "two\n"
    assert not (tmp_path / "service.log.2").exists()


def test_local_cli_limits_and_persistence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "events"
    path.write_text("2020-01-01T10:00:00Z FATAL password=private-123\n"
                    "2020-01-02T10:00:00Z FATAL server panic\n")
    runner = CliRunner()
    args = ["analyze", "--file", str(path), "--local", "--max-alerts"]
    assert "2 anomaly trigger" in runner.invoke(app, args + ["0"]).output
    assert not (tmp_path / ".logwhisper").exists()
    result = runner.invoke(app, args + ["1"])
    assert result.exit_code == 0, result.exception
    data = (tmp_path / ".logwhisper/alerts.jsonl").read_text()
    assert len(data.splitlines()) == 1
    assert "private-123" not in data
    assert "Local rule matched" in json.loads(data)["caused_by"]


def test_failed_sole_source_returns_failure(tmp_path):
    result = CliRunner().invoke(app, ["watch", "--file", str(tmp_path), "--local"])
    assert result.exit_code == 1


async def test_shutdown_reports_inflight_work(monkeypatch):
    monitor = Monitor("", windows_notifications=False)
    async def slow_analysis(event):
        await asyncio.Event().wait()
    monkeypatch.setattr(monitor.agent, "analyze", slow_analysis)
    monitor.start_worker()
    monitor.ingest(parse("FATAL synthetic failure"))
    await monitor.finish(timeout=0.01)
    assert monitor.status()["pending"] == 1
    assert monitor.worker is None


async def test_repl_file_watch_emits_finding(monkeypatch, tmp_path):
    from agent.repl import REPLSession
    from ingest.file_adapter import FileAdapter

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (tmp_path / ".logwhisper.yaml").write_text("output: {windows_notification: false}")
    source = tmp_path / "app.log"
    source.touch()

    async def finite_stream(self):
        yield parse("FATAL synthetic incident", source=str(source))
    monkeypatch.setattr(FileAdapter, "stream", finite_stream)
    session = REPLSession()
    await session._cmd_watch(str(source))
    assert session._monitor.status()["delivered"]["jsonl"] == 1
    assert session._monitor.status()["pending"] == 0


def test_chat_preview_keeps_project_private_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "requirements.txt").touch()
    (tmp_path / "main.py").write_text("# project-marker")
    log = tmp_path / "app.log"
    log.write_text("password=private-123")
    args = ["chat", "--preview", "--file", str(log)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.exception
    assert "private-123" not in result.output
    assert "project-marker" not in result.output
    (tmp_path / ".logwhisper.yaml").write_text("privacy: {project_context: true}")
    assert "project-marker" in CliRunner().invoke(app, args).output


@pytest.mark.asyncio
async def test_managed_child_excludes_monitor_keys(monkeypatch):
    import sys
    from agent.procs import spawn
    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    proc = await spawn(f'"{sys.executable}" -c "import os; print(123 if os.getenv(\'GEMINI_API_KEY\') else 456)"')
    output, _ = await proc.communicate()
    assert output.strip() == b"456"

async def test_stdin_preserves_order_across_backpressure(monkeypatch):
    import io
    import sys
    from ingest.stream_adapters import StdinAdapter
    lines = [f"INFO event-{i}" for i in range(1000)]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines)))
    seen = []
    async for event in StdinAdapter().stream():
        seen.append(event["raw"])
        if len(seen) % 50 == 0:
            await asyncio.sleep(0.001)
    assert seen == lines


def test_static_launcher_uses_public_root(tmp_path):
    from agent.detect import detect_dev_command
    public = tmp_path / "public"
    public.mkdir()
    (public / "index.html").touch()
    command = detect_dev_command("static", tmp_path)
    assert "static_server.py" in command
    assert str(public) in command
