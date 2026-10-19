import asyncio

import buffer.ring_buffer as rb
from buffer.parser import parse
from buffer.ring_buffer import RingBuffer
from trigger.cooldown import CooldownTracker
from trigger.engine import TriggerEngine
from trigger.rules import (
    ClusterFormationRule,
    CriticalKeywordRule,
    HttpErrorRule,
    MetricThresholdRule,
    NewErrorTypeRule,
    RateSpikeRule,
    SilenceAnomalyRule,
    build_rules,
)


def make_engine(rules):
    q = asyncio.Queue(maxsize=50)
    return TriggerEngine(rules, CooldownTracker(), q), q, RingBuffer()


def test_new_error_fires_once_per_distinct_error():
    engine, q, buf = make_engine([NewErrorTypeRule(cooldown=0)])
    for i in range(5):
        engine.process(parse(f"2026-04-08 10:00:0{i} ERROR db timeout id={i}", source="s"), buf)
    assert q.qsize() == 1  # same signature each time -> only the first is "new"
    engine.process(parse("ERROR a completely different failure", source="s"), buf)
    assert q.qsize() == 2


def test_multiple_rules_on_one_line_make_one_event():
    engine, q, buf = make_engine([NewErrorTypeRule(), CriticalKeywordRule()])
    engine.process(parse("FATAL out of memory", source="a"), buf)
    assert q.qsize() == 1  # one LLM call, not two
    event = q.get_nowait()
    assert event["rules"] == ["NewErrorTypeRule", "CriticalKeywordRule"]
    assert event["rule"] == "NewErrorTypeRule + CriticalKeywordRule"
    # both rules are now cooling for this source
    engine.process(parse("FATAL out of memory again", source="a"), buf)
    assert q.qsize() == 0 and engine.suppression_count >= 1


def test_evaluate_before_push_order():
    engine, q, buf = make_engine([NewErrorTypeRule()])
    engine.process(parse("ERROR first failure", source="a"), buf)
    assert q.qsize() == 1
    assert len(buf) == 1


def test_trigger_window_contains_triggering_line():
    engine, q, buf = make_engine([CriticalKeywordRule()])
    engine.process(parse("FATAL out of memory", source="a"), buf)
    event = q.get_nowait()
    assert event["buffer_window"][-1]["raw"] == "FATAL out of memory"


def test_critical_keywords_respect_word_boundaries():
    rule = CriticalKeywordRule()
    buf = RingBuffer()
    assert not rule.evaluate(parse("INFO booked the room for the zoom call"), buf)
    assert rule.evaluate(parse("kernel: Out of memory: oom-killer invoked"), buf)
    assert rule.evaluate(parse("process crashed: segmentation fault"), buf)


def test_empty_extra_keyword_does_not_match_everything():
    rule = CriticalKeywordRule(extra_keywords=["", "  ", "meltdown"])
    buf = RingBuffer()
    assert not rule.evaluate(parse("INFO all good"), buf)
    assert rule.evaluate(parse("INFO reactor meltdown"), buf)


def test_rate_spike_counts_current_line():
    rule = RateSpikeRule(threshold=3, window_seconds=10)
    buf = RingBuffer()
    for _ in range(2):
        line = parse("ERROR boom", source="a")
        assert not rule.evaluate(line, buf)
        buf.push(line)
    assert rule.evaluate(parse("ERROR boom", source="a"), buf)  # 2 buffered + this one = 3


def test_cluster_includes_current_source():
    rule = ClusterFormationRule(min_sources=3, window_seconds=30)
    buf = RingBuffer()
    for s in ("a", "b"):
        buf.push(parse("ERROR x", source=s))
    assert rule.evaluate(parse("ERROR x", source="c"), buf)


def test_http_error_rule_fires_for_new_status_only():
    rule = HttpErrorRule()
    buf = RingBuffer()
    line = parse('POST /predict HTTP/1.1" 500 -')
    assert rule.evaluate(line, buf)
    buf.push(line)
    assert not rule.evaluate(parse('POST /predict HTTP/1.1" 500 -'), buf)
    assert rule.evaluate(parse('POST /predict HTTP/1.1" 404 -'), buf)


def test_metric_rule_ignores_non_numeric():
    rule = MetricThresholdRule()
    obj = parse("INFO ok")
    obj["fields"] = {"latency_ms": "slow"}
    assert not rule.evaluate(obj, RingBuffer())
    assert rule.evaluate(parse("INFO latency=2500ms"), RingBuffer())


def test_silence_rule_detects_gap_before_new_line():
    now = [1000.0]
    buf = RingBuffer(clock=lambda: now[0])
    rule = SilenceAnomalyRule(silence_threshold_seconds=60)
    first = parse("INFO hello", source="a")
    first["epoch"] = 1000.0
    buf.push(first)
    now[0] = 1030.0
    assert not rule.evaluate(parse("INFO again", source="a"), buf)
    now[0] = 1100.0
    assert rule.evaluate(parse("INFO back", source="a"), buf)


def test_cooldown_suppresses_and_counts():
    t = [0.0]
    q = asyncio.Queue()
    engine = TriggerEngine([CriticalKeywordRule(cooldown=30)], CooldownTracker(clock=lambda: t[0]), q)
    buf = RingBuffer()
    engine.process(parse("FATAL one", source="a"), buf)
    engine.process(parse("FATAL two", source="a"), buf)
    assert q.qsize() == 1 and engine.suppression_count == 1
    t[0] = 31
    engine.process(parse("FATAL three", source="a"), buf)
    assert q.qsize() == 2


def test_queue_full_does_not_start_cooldown():
    q = asyncio.Queue(maxsize=1)
    engine = TriggerEngine([CriticalKeywordRule(cooldown=30)], CooldownTracker(), q)
    buf = RingBuffer()
    engine.process(parse("FATAL one", source="a"), buf)
    engine.process(parse("FATAL two", source="b"), buf)  # queue full -> dropped
    q.get_nowait()
    engine.process(parse("FATAL three", source="b"), buf)  # must not be in cooldown
    assert q.qsize() == 1


def test_a_crashing_rule_does_not_stop_others():
    class Boom(NewErrorTypeRule):
        def evaluate(self, log_obj, buffer):
            raise RuntimeError("bad rule")

    engine, q, buf = make_engine([Boom(), CriticalKeywordRule()])
    engine.process(parse("FATAL x", source="a"), buf)
    assert q.qsize() == 1
    assert len(buf) == 1


def test_ring_buffer_since_survives_wraparound():
    buf = RingBuffer(maxlen=3)
    seq = 0
    seen = []
    for i in range(10):
        buf.push(parse(f"INFO line {i}"))
        entries, seq = buf.since(seq)
        seen += [e["raw"] for e in entries]
    assert seen == [f"INFO line {i}" for i in range(10)]


def test_signature_pruning_keeps_newest():
    buf = RingBuffer()
    for i in range(rb._SIGNATURE_CAP + 5):
        obj = parse("ERROR x")
        obj["signature"] = f"sig{i}"
        buf.push(obj)
    assert len(buf._signatures) <= rb._SIGNATURE_PRUNE_TO + 5
    assert not buf.is_new_signature(f"sig{rb._SIGNATURE_CAP + 4}")
    assert buf.is_new_signature("sig0")


def test_batch_clock_windows_old_logs():
    now = [0.0]
    buf = RingBuffer(clock=lambda: now[0])
    old = parse("ERROR old", source="a")
    old["epoch"] = 5000.0
    now[0] = 5000.0
    buf.push(old)
    assert buf.error_rate("a", 10) == 1
    assert len(buf.window(60)) == 1


def test_build_rules_respects_config():
    rules = build_rules({
        "rate_spike": {"enabled": False},
        "critical_keyword": {"cooldown_seconds": 7, "extra_keywords": ["meltdown"]},
        "silence_anomaly": {"enabled": False},
    })
    names = [r.name for r in rules]
    assert "RateSpikeRule" not in names and "SilenceAnomalyRule" not in names
    crit = next(r for r in rules if r.name == "CriticalKeywordRule")
    assert crit.cooldown == 7
