import asyncio
from io import StringIO

from rich.console import Console
from agent.readiness import report_endpoint


async def test_announced_listener_and_exit():
    output = StringIO()
    console = Console(file=output, width=120)
    announced = set()

    async def accept(reader, writer):
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    async with server:
        await report_endpoint(console, "api", "INFO started", announced)
        await report_endpoint(console, "api", f"GET {url}", announced)
        assert not output.getvalue()
        for _ in range(2):
            await report_endpoint(console, "api", f"Listening at {url}", announced)
        assert output.getvalue().count("TCP listener confirmed") == 1
    await report_endpoint(console, "exited", f"Listening at {url}", set())
    assert "exited" not in output.getvalue()
