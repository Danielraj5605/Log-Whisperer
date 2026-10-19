import asyncio

from ingest.file_adapter import FileAdapter


async def collect(adapter, n, timeout=5.0):
    out = []

    async def run():
        async for obj in adapter.stream():
            out.append(obj["raw"])
            if len(out) >= n:
                adapter.stop()
                return

    await asyncio.wait_for(run(), timeout)
    return out


async def test_tails_appended_lines(tmp_path):
    p = tmp_path / "app.log"
    p.write_text("old line\n")
    ad = FileAdapter(p, poll_ms=10)
    task = asyncio.create_task(collect(ad, 2))
    await asyncio.sleep(0.1)
    with open(p, "a") as f:
        f.write("new 1\nnew 2\n")
    assert await task == ["new 1", "new 2"]  # history skipped, no tell() crash


async def test_file_created_later_is_read_from_start(tmp_path):
    p = tmp_path / "later.log"
    ad = FileAdapter(p, poll_ms=10)
    task = asyncio.create_task(collect(ad, 2))
    await asyncio.sleep(0.1)
    p.write_text("first\nsecond\n")
    assert await task == ["first", "second"]


async def test_empty_file_at_start_does_not_lose_lines(tmp_path):
    p = tmp_path / "empty.log"
    p.write_text("")
    ad = FileAdapter(p, poll_ms=10)
    task = asyncio.create_task(collect(ad, 3))
    await asyncio.sleep(0.1)
    for i in range(3):
        with open(p, "a") as f:
            f.write(f"line {i}\n")
        await asyncio.sleep(0.05)
    assert await task == ["line 0", "line 1", "line 2"]


async def test_partial_line_waits_for_newline(tmp_path):
    p = tmp_path / "partial.log"
    p.write_text("")
    ad = FileAdapter(p, poll_ms=10)
    task = asyncio.create_task(collect(ad, 1))
    await asyncio.sleep(0.1)
    with open(p, "a") as f:
        f.write("half a li")
    await asyncio.sleep(0.1)
    assert not task.done()
    with open(p, "a") as f:
        f.write("ne\n")
    assert await task == ["half a line"]


async def test_truncation_is_detected(tmp_path):
    p = tmp_path / "trunc.log"
    p.write_text("")
    ad = FileAdapter(p, poll_ms=10)
    task = asyncio.create_task(collect(ad, 2))
    await asyncio.sleep(0.1)
    with open(p, "a") as f:
        f.write("a long first line before truncation\n")
    await asyncio.sleep(0.15)
    p.write_text("after\n")  # truncate + rewrite (shorter than before)
    assert await task == ["a long first line before truncation", "after"]


async def test_crlf_and_blank_lines(tmp_path):
    p = tmp_path / "crlf.log"
    p.write_text("")
    ad = FileAdapter(p, poll_ms=10)
    task = asyncio.create_task(collect(ad, 2))
    await asyncio.sleep(0.1)
    with open(p, "ab") as f:
        f.write(b"one\r\n\r\ntwo\r\n")
    assert await task == ["one", "two"]
