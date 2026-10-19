from datetime import datetime, timezone

from buffer.parser import is_http_error, parse


def test_true_false_fields_do_not_crash():
    # Used to raise IndexError (m.group(4) on a 3-group regex)
    obj = parse("INFO cache=true retry=false latency=120ms", source="s")
    assert obj["fields"]["latency_ms"] == 120


def test_iso_timestamp_variants_are_parsed():
    assert parse("2026-04-08T10:00:00Z ERROR boom")["ts"].startswith("2026-04-08T10:00:00")
    assert parse("2026-04-08T10:00:00,250+00:00 ERROR boom")["ts"].startswith("2026-04-08T10:00:00.25")
    # naive timestamp without fraction: used to fall through to now()
    obj = parse("2026-04-08 10:00:00 ERROR boom")
    assert obj["ts"][:4] == "2026"
    assert obj["ts"][:10] in ("2026-04-07", "2026-04-08", "2026-04-09")  # local tz -> utc shift


def test_epoch_needs_boundaries_and_plausible_range():
    now = datetime.now(timezone.utc).timestamp()
    obj = parse("ERROR id=12345678901234567890 failed")  # 20-digit id is not a timestamp
    assert abs(obj["epoch"] - now) < 5
    assert int(parse("1700000000 ERROR boom")["epoch"]) == 1700000000


def test_common_log_format():
    obj = parse('127.0.0.1 - - [11/Apr/2026:10:00:00 +0000] "GET / HTTP/1.1" 500 12')
    assert obj["ts"].startswith("2026-04-11T10:00:00")


def test_signature_ignores_timestamps_and_ids():
    a = parse("2026-04-08 10:00:00 ERROR db timeout request=1234 pid=88")
    b = parse("2026-04-08 10:00:07 ERROR db timeout request=9999 pid=91")
    c = parse("2026-04-08 10:00:07 ERROR something else entirely")
    assert a["signature"] == b["signature"]
    assert a["signature"] != c["signature"]


def test_signature_keeps_http_status():
    a = parse('POST /x HTTP/1.1" 500 -')
    b = parse('POST /x HTTP/1.1" 404 -')
    assert a["signature"] != b["signature"]


def test_http_error_detection():
    assert is_http_error('127.0.0.1 - - [x] "POST /predict HTTP/1.1" 500 -')
    assert is_http_error("POST /api/submit 401 50ms")
    assert not is_http_error("worker pid 503 started")  # no HTTP context
    assert not is_http_error("GET /x 200 12ms")
