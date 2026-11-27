"""R2 nits: fake socket/clock + fake child; no API or life CLI execution."""
import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import loom.weather as weather
from loom.core.harness.middleware import ToolCall

NOW = datetime(2026, 11, 27, 10, tzinfo=timezone.utc)
DATA = json.dumps({"hourly": {"time": ["2026-11-27T10:00"], "weather_code": [61],
                              "temperature_2m": [17], "precipitation_probability": [80]}}).encode()


@pytest.mark.parametrize("case", ["drip", "oversize", "exact_limit", "invalid_json"])
def test_http_read_has_remaining_deadline_and_exact_size_boundary(monkeypatch, case):
    import http.client
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(weather, "TIMEOUT", 2)
    data = DATA if case == "drip" else DATA + b" " * (weather.MAX_BYTES - len(DATA) + (case == "oversize"))
    if case == "invalid_json":
        data = b"<html>private error body</html>"
    class Response:
        status = 200
        offset = 0
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def close(self): pass
        def isclosed(self): return False
        def read(self, size):  # old transport: one read can exceed the total deadline
            if case == "drip": clock[0] += 3
            return data[:size]
        def read1(self, size):
            assert 0 < size <= 4096
            if case == "drip": clock[0] += 1
            block = data[self.offset:self.offset + (1 if case == "drip" else size)]
            self.offset += len(block)
            return block
    response = Response()
    timeouts = []
    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = SimpleNamespace(settimeout=lambda value: timeouts.append(value))
        def connect(self): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return response
        def close(self): pass
    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(weather.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=lambda *a, **kw: response))
    result = weather.query_weather(25, 121, NOW)
    assert result["status"] == ("ok" if case == "exact_limit" else "unavailable")
    assert all(0 < t <= 2 for t in timeouts)


@pytest.mark.parametrize("cancel", [False, True])
async def test_native_tool_deadline_or_cancel_kills_and_reaps_worker(monkeypatch, cancel):
    monkeypatch.setattr(weather, "TIMEOUT", 0.01)
    # Old in-process path remains bounded for this RED probe, never real IO.
    def old_transport(url):
        time.sleep(0.03)
        return json.loads(DATA)
    monkeypatch.setattr(weather, "_http_json", old_transport)
    started = asyncio.Event()
    class Process:
        returncode = None
        killed = False
        reaped = False
        async def communicate(self):
            started.set()
            await asyncio.Event().wait()
        def kill(self): self.killed = True; self.returncode = -9
        async def wait(self): self.reaped = True; return self.returncode
    process = Process()
    async def spawn(*args, **kwargs): return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    tool = weather.make_weather_tool()
    call = ToolCall(tool_name="weather", args={"latitude": 25, "longitude": 121},
                    trust_level=tool.trust_level, session_id="r2")
    task = asyncio.create_task(tool.executor(call))
    if cancel:
        # Let either old thread or new child start, then cancel the caller.
        await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    else:
        result = await task
        assert json.loads(result.output) == {"status": "unavailable"}
    assert process.killed and process.reaped
