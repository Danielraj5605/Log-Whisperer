from config import DEFAULT_CONFIG, expand_env, load


def test_load_does_not_mutate_defaults(tmp_path):
    cfg_file = tmp_path / ".logwhisper.yaml"
    cfg_file.write_text("triggers:\n  rate_spike:\n    threshold: 99\n")
    cfg = load(cfg_file)
    assert cfg["triggers"]["rate_spike"]["threshold"] == 99
    assert cfg["triggers"]["rate_spike"]["window_seconds"] == 10  # deep-merged, not replaced
    assert DEFAULT_CONFIG["triggers"]["rate_spike"]["threshold"] == 5
    assert load(tmp_path / "missing.yaml")["triggers"]["rate_spike"]["threshold"] == 5


def test_load_survives_bad_yaml(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("llm: [unclosed")
    assert load(f)["llm"]["model"] == "gemini-2.5-flash"
    f.write_text("- just\n- a list\n")
    assert load(f)["llm"]["model"] == "gemini-2.5-flash"


def test_env_expansion(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_KEY", "abc123")
    assert expand_env({"a": ["${MY_KEY}", "x"]}) == {"a": ["abc123", "x"]}
    f = tmp_path / "c.yaml"
    f.write_text("llm:\n  api_key: ${MY_KEY}\n")
    assert load(f)["llm"]["api_key"] == "abc123"


def test_credentials_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no project .logwhisper.yaml here
    from agent import session

    session.save_global_api_key("secret-key-1234", "gemini")
    session.save_telegram_config(" tok ", " 42 ")
    assert session.get_api_key() == "secret-key-1234"
    assert session.get_telegram_config() == {"bot_token": "tok", "chat_id": "42"}
    session.clear_telegram_config()
    assert session.get_telegram_config() == {"bot_token": "", "chat_id": ""}
    assert session.get_api_key() == "secret-key-1234"  # preserved

    session.get_global_credentials_path().write_text("[1, 2]")  # malformed shape
    assert session.get_api_key() is None
    assert session.get_telegram_config()["bot_token"] == ""


def test_mask_secret():
    from agent.session import mask_secret

    assert mask_secret("abcdefghijklmnop").endswith("mnop")
    assert "abcd" not in mask_secret("abcdefghijklmnop")
    assert mask_secret("short") == "•" * 5


def test_telegram_message_escapes_html_and_truncates():
    from output.telegram_notification import TelegramNotifier

    n = TelegramNotifier("tok", "1")
    msg = n._format_message(
        {
            "root_signal": "<b>bad</b> & worse",
            "evidence": ["a <script>", None],
            "confidence": None,
            "caused_by": "x" * 10000,
        },
        project="p<1>",
    )
    assert "<b>bad</b>" not in msg and "&lt;b&gt;bad&lt;/b&gt; &amp; worse" in msg
    assert "<script>" not in msg
    assert len(msg) <= 4096


def test_telegram_redacts_token():
    from output.telegram_notification import TelegramNotifier

    n = TelegramNotifier("123:SECRET", "1")
    assert "SECRET" not in n._redact("error for url https://api.telegram.org/bot123:SECRET/sendMessage")


def test_render_alert_survives_hostile_and_malformed_fields():
    from rich.console import Console

    from output.terminal import render_alert

    finding = {
        "root_signal": "[/not-a-tag] oops [bold",
        "caused_by": None,
        "confidence": None,
        "evidence": "just a string",
        "source": "svc[1]",
        "timestamp": "2026-04-08T[x]",
    }
    Console(record=True, width=100).print(render_alert(finding, 3))


def test_service_log_prefix_is_literal():
    from rich.console import Console

    from output.terminal import service_prefix

    c = Console(record=True, width=60)
    c.print(service_prefix("backend"))
    assert "[backend]" in c.export_text()


def test_dashboard_renders_all_panels_and_hostile_alerts():
    from rich.console import Console

    from output.dashboard import Dashboard

    d = Dashboard(["backend", "frontend"])
    d.append_log("backend", "[/oops] line")
    d.add_alert({"source": "backend", "trigger": "T", "root_signal": "[x", "confidence": None})
    c = Console(record=True, width=120, height=40)
    c.print(d._render())
    out = c.export_text()
    assert "Services" in out and "Live Logs" in out and "Alert History" in out
    assert "[backend]" in out
