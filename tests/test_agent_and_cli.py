import asyncio
import json
import sys
import types

import httpx
import pytest

import agent.gemini_client as gc
from agent.agent import GeminiAgent, _parse_finding
from agent.chat import ChatAgent
from buffer.parser import parse

REAL_ASYNC_CLIENT = httpx.AsyncClient


def install_mock(monkeypatch, handler):
    """Route every httpx.AsyncClient created by the app through `handler`."""
    seen = []

    def factory(*args, **kwargs):
        def wrapped(request):
            seen.append(request)
            return handler(request)

        kwargs["transport"] = httpx.MockTransport(wrapped)
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(gc.httpx, "AsyncClient", factory)

    async def no_sleep(_):
        return None

    # Skip retry back-off, but only inside the Gemini client (not the whole event loop)
    monkeypatch.setattr(gc, "asyncio", types.SimpleNamespace(sleep=no_sleep))
    return seen


def gemini_reply(text):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def trigger_event():
    obj = parse("2026-04-08T10:00:00Z ERROR db down", source="api")
    return {"rule": "NewErrorTypeRule", "log_obj": obj, "buffer_window": [obj], "stats": {}}


async def test_api_key_goes_in_header_not_url(monkeypatch):
    seen = install_mock(monkeypatch, lambda r: gemini_reply('{"root_signal": "x", "confidence": "HIGH"}'))
    finding = await GeminiAgent(api_key="SECRET-KEY").analyze(trigger_event())
    assert "SECRET-KEY" not in str(seen[0].url)
    assert seen[0].headers["x-goog-api-key"] == "SECRET-KEY"
    assert finding["confidence"] == "high"


async def test_error_findings_never_contain_the_key(monkeypatch):
    install_mock(monkeypatch, lambda r: httpx.Response(403, json={"error": {"message": "API key not valid"}}))
    finding = await GeminiAgent(api_key="SECRET-KEY").analyze(trigger_event())
    assert "SECRET-KEY" not in json.dumps(finding)
    assert "403" in finding["caused_by"]


async def test_client_errors_are_not_retried(monkeypatch):
    seen = install_mock(monkeypatch, lambda r: httpx.Response(400, json={"error": {"message": "bad"}}))
    await GeminiAgent(api_key="k").analyze(trigger_event())
    assert len(seen) == 1


async def test_rate_limit_is_retried_then_succeeds(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429) if len(calls) < 3 else gemini_reply("{}")

    install_mock(monkeypatch, handler)
    finding = await GeminiAgent(api_key="k").analyze(trigger_event())
    assert len(calls) == 3 and finding["trigger"] == "NewErrorTypeRule"


async def test_timeout_gives_timeout_finding(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    install_mock(monkeypatch, handler)
    finding = await GeminiAgent(api_key="k", timeout=5).analyze(trigger_event())
    assert "timed out after 5s" in finding["caused_by"]


async def test_blocked_or_empty_response_is_reported(monkeypatch):
    install_mock(monkeypatch, lambda r: httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]}))
    finding = await GeminiAgent(api_key="k").analyze(trigger_event())
    assert "SAFETY" in finding["caused_by"]


async def test_missing_key_is_a_clear_error():
    finding = await GeminiAgent(api_key="").analyze(trigger_event())
    assert "no API key" in finding["caused_by"]


def test_flash_model_disables_thinking_and_requests_json():
    payload = GeminiAgent(api_key="k", model="gemini-2.5-flash")._build_payload("hi")
    cfg = payload["generationConfig"]
    assert cfg["thinkingConfig"] == {"thinkingBudget": 0}
    assert cfg["responseMimeType"] == "application/json"
    pro = GeminiAgent(api_key="k", model="gemini-2.5-pro")._build_payload("hi")["generationConfig"]
    assert "thinkingConfig" not in pro  # 2.5-pro rejects a zero budget


def test_parse_finding_normalizes_types():
    f = _parse_finding('```json\n{"evidence": "one line", "confidence": "Certain", "action": null}\n```')
    assert f["evidence"] == ["one line"] and f["confidence"] == "low" and f["action"] == ""
    assert _parse_finding("not json at all")["caused_by"] == "Failed to parse LLM response"
    assert _parse_finding("[1, 2, 3]")["confidence"] == "low"  # JSON but not an object


async def test_chat_keeps_history_and_reports_errors(monkeypatch):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return gemini_reply("an answer")

    install_mock(monkeypatch, handler)
    agent = ChatAgent(api_key="k")
    await agent.ask("first?", project_context={"project_type": "unknown", "files": {}, "package_info": {}})
    await agent.ask("second?", project_context={"project_type": "unknown", "files": {}, "package_info": {}})
    assert len(bodies[1]["contents"]) == 3  # first Q, first A, second Q
    install_mock(monkeypatch, lambda r: httpx.Response(401, json={"error": {"message": "nope"}}))
    reply = await ChatAgent(api_key="k").ask("x", project_context={"project_type": "unknown", "files": {}, "package_info": {}})
    assert reply.startswith("Error:") and "401" in reply


# ── Subprocess helpers ───────────────────────────────────────────────────────


async def test_spawn_streams_lines_and_terminate_kills_tree():
    from agent.procs import iter_lines, spawn, terminate

    proc = await spawn(f'"{sys.executable}" -c "print(\'hello\', flush=True); import time; time.sleep(60)"')
    lines = []
    async for line in iter_lines(proc.stdout):
        lines.append(line)
        break
    assert lines == ["hello"]
    await terminate(proc)
    assert proc.returncode is not None


async def test_over_long_line_does_not_kill_the_reader():
    from agent.procs import iter_lines, spawn

    code = "import sys; sys.stdout.write('x' * 2_000_000 + '\\n'); print('after', flush=True)"
    proc = await spawn(f'"{sys.executable}" -c "{code}"')
    lines = [line async for line in iter_lines(proc.stdout)]
    assert lines[-1] == "after"
    await proc.wait()


# ── deps / detect ────────────────────────────────────────────────────────────


def test_node_deps_check_reads_package_json(tmp_path):
    from agent.deps import check_service_deps

    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"left-pad": "1"}}))
    assert check_service_deps("web", tmp_path).missing  # no node_modules
    (tmp_path / "node_modules").mkdir()
    assert check_service_deps("web", tmp_path).missing  # node_modules exists, package doesn't
    (tmp_path / "node_modules" / "left-pad").mkdir()
    result = check_service_deps("web", tmp_path)
    assert not result.missing and result.manager == "npm"  # no lockfile still counts as npm


def test_python_deps_check(tmp_path):
    from agent.deps import check_service_deps

    (tmp_path / "requirements.txt").write_text("# c\n-r other.txt\nrich>=13\n")
    assert not check_service_deps("api", tmp_path).missing
    (tmp_path / "requirements.txt").write_text("definitely-not-installed-pkg==1.0\n")
    assert check_service_deps("api", tmp_path).missing


def test_install_commands_need_no_shell(tmp_path):
    from agent.deps import check_service_deps

    (tmp_path / "requirements.txt").write_text("rich\n")
    cmd = check_service_deps("api", tmp_path).install_command()
    assert cmd[0] == sys.executable and "pip" in cmd


def test_detect_node_without_dev_script(tmp_path):
    from agent.detect import detect_dev_command

    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "tsc"}}))
    assert detect_dev_command("node", tmp_path) is None
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"start": "node ."}}))
    assert "start" in detect_dev_command("node", tmp_path)


# ── CLI ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return CliRunner()


def test_cli_commands_register(cli):
    from agent import app

    result = cli.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("run", "shell", "watch", "analyze", "setup", "telegram", "chat", "project"):
        assert name in result.output


def test_analyze_replays_log_and_reports_findings(cli, monkeypatch, tmp_path):
    from agent import app

    monkeypatch.setattr(
        GeminiAgent,
        "analyze",
        lambda self, event: asyncio.sleep(0, {
            "alert_id": "LW-1", "timestamp": event["log_obj"]["ts"], "source": event["log_obj"]["source"],
            "trigger": event["rule"], "confidence": "high", "root_signal": event["log_obj"]["raw"],
            "caused_by": "because", "action": "fix it", "evidence": [], "blast_radius": "all",
        }),
    )
    log = tmp_path / "incident.log"
    log.write_text(
        "2020-01-01T10:00:00Z INFO started\n"
        "2020-01-01T10:00:01Z ERROR db connection refused\n"
        "2020-01-01T10:00:02Z ERROR db connection refused\n"
        "2020-01-01T10:00:03Z INFO latency=5000ms done\n",
        encoding="utf-8",
    )
    result = cli.invoke(app, ["analyze", "--file", str(log)])
    assert result.exit_code == 0, result.output
    assert "Scanned 4 lines" in result.output
    assert "ANOMALY DETECTED" in result.output
    assert (tmp_path / ".logwhisper" / "alerts.jsonl").exists()


def test_analyze_since_filter_and_bad_input(cli, tmp_path):
    from agent import app

    log = tmp_path / "x.log"
    log.write_text("2020-01-01T10:00:00Z INFO a\n")
    bad = cli.invoke(app, ["analyze", "--file", str(log), "--since", "yesterday-ish"])
    assert bad.exit_code == 1
    none = cli.invoke(app, ["analyze", "--file", str(log), "--since", "2030-01-01T00:00:00+00:00"])
    assert none.exit_code == 0 and "Scanned 0 lines" in none.output
    missing = cli.invoke(app, ["analyze", "--file", str(tmp_path / "nope.log")])
    assert missing.exit_code == 1


def test_watch_requires_a_source_and_a_key(cli, monkeypatch, tmp_path):
    from agent import app

    result = cli.invoke(app, ["watch"])
    assert result.exit_code == 1
    monkeypatch.delenv("GEMINI_API_KEY")
    result = cli.invoke(app, ["watch", "--file", "x.log"])
    assert result.exit_code == 1


def test_telegram_cli_validates_action_first(cli):
    from agent import app

    assert cli.invoke(app, ["telegram", "bogus"]).exit_code == 1
    result = cli.invoke(app, ["telegram", "status"])
    assert result.exit_code == 1 and "not configured" in result.output.lower()


# ── end-to-end pipeline ───────────────────────────────────────────────────────


async def test_monitor_pipeline_end_to_end(monkeypatch, tmp_path):
    """File -> adapter -> engine -> queue -> (mock) LLM -> alert, with each line handled once."""
    from agent.pipeline import Monitor
    from ingest.file_adapter import FileAdapter

    install_mock(monkeypatch, lambda r: gemini_reply('{"root_signal": "r", "confidence": "high"}'))
    monkeypatch.chdir(tmp_path)

    got = []
    monitor = Monitor("k", windows_notifications=False, telegram=False, print_alerts=False, on_finding=got.append)
    path = tmp_path / "app.log"
    path.write_text("")
    worker = monitor.start_worker()
    reader = asyncio.create_task(monitor.consume(FileAdapter(path, poll_ms=10)))
    await asyncio.sleep(0.1)
    with open(path, "a") as f:
        f.write("2026-04-08 10:00:00 ERROR db timeout id=1\n2026-04-08 10:00:01 ERROR db timeout id=2\n")
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    worker.cancel()
    reader.cancel()
    await asyncio.gather(worker, reader, return_exceptions=True)

    assert len(monitor.buffer) == 2  # each line ingested exactly once
    assert len(got) == 1  # same error twice -> one alert (second is a repeat / in cooldown)
    assert (tmp_path / ".logwhisper" / "alerts.jsonl").read_text().count("\n") == 1
