"""Shared Open-Meteo query, with no filesystem/cache/state writes.

Only finite numeric fields and WMO enums cross the API boundary. The caller
owns cache writes explicitly; a query (including window peek) never owns one.
API contract: https://open-meteo.com/en/docs (hourly, Celsius, WMO codes).
"""
from __future__ import annotations

import asyncio
import json
import math
import http.client
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

WMO = {
    0: "晴", 1: "晴時多雲", 2: "多雲", 3: "陰", 45: "霧", 48: "霧",
    **dict.fromkeys((51, 53, 55, 56, 57), "毛毛雨"),
    **dict.fromkeys((61, 63, 65, 66, 67, 80, 81, 82), "雨"),
    **dict.fromkeys((71, 73, 75, 77, 85, 86), "雪"),
    **dict.fromkeys((95, 96, 99), "雷雨"),
}
RAIN = frozenset((51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99))
TIMEOUT = 10
MAX_BYTES = 256 * 1024


class WeatherUnavailable(OSError):
    """Transport/deadline/size failure, never an external error body."""


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WeatherUnavailable("deadline exceeded")
    return remaining


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def validate_snapshot(raw):
    """Return a new bounded schema, never strings copied from the provider."""
    if not isinstance(raw, dict) or raw.get("status") != "ok":
        return {"status": "unknown"}
    try:
        stamp = datetime.fromisoformat(raw["at"])
        if stamp.tzinfo is None:
            raise ValueError
        code, temp, rain = raw["code"], raw["temperature_c"], raw["rain_probability"]
        if type(code) is not int or code not in WMO or not _number(temp, -100, 70) or not _number(rain, 0, 100):
            raise ValueError
        lat, lng = raw["latitude"], raw["longitude"]
        if not _number(lat, -90, 90) or not _number(lng, -180, 180):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"status": "unknown"}
    result = {"status": "ok", "at": stamp.isoformat(), "code": code,
            "temperature_c": temp, "rain_probability": rain,
            "latitude": lat, "longitude": lng}
    summary = raw.get("day")
    if isinstance(summary, dict) and all(_number(summary.get(k), low, high) for k, low, high in (
            ("min_c", -100, 70), ("max_c", -100, 70), ("rain_max", 0, 100))):
        if summary["min_c"] <= summary["max_c"]:
            result["day"] = {k: summary[k] for k in ("min_c", "max_c", "rain_max")}
    return result


def _http_json(url):
    # No redirects/proxies. The native caller additionally owns a killable
    # worker deadline, including DNS/connect, headers, reads and JSON parse.
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "api.open-meteo.com"
            or parsed.path != "/v1/forecast" or parsed.fragment):
        raise WeatherUnavailable("unsupported origin")
    deadline = time.monotonic() + TIMEOUT
    connection = http.client.HTTPSConnection(parsed.netloc, timeout=_remaining(deadline))
    response = None
    try:
        connection.connect()
        sock = connection.sock
        sock.settimeout(_remaining(deadline))
        connection.request("GET", parsed.path + "?" + parsed.query)
        sock.settimeout(_remaining(deadline))
        response = connection.getresponse()
        if response.status != 200:
            raise WeatherUnavailable("HTTP request unavailable")
        data = bytearray()
        while True:
            sock.settimeout(_remaining(deadline))
            block = response.read1(min(4096, MAX_BYTES + 1 - len(data)))
            _remaining(deadline)
            data.extend(block)
            if len(data) > MAX_BYTES:
                raise WeatherUnavailable("oversized response")
            if not block:
                break
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            raise WeatherUnavailable("invalid response") from None
        _remaining(deadline)
        return value
    finally:
        if response is not None:
            response.close()
        connection.close()


async def _query_with_deadline(lat, lng):
    """A hard native-tool deadline without orphan background IO threads.

    A disposable copy of the same read-only window command owns the blocking
    network stack. Timeout/cancellation kills and reaps it; DNS/header trickle
    cannot leave an unbounded thread running in the session process.
    """
    if not _number(lat, -90, 90) or not _number(lng, -180, 180):
        return {"status": "unknown"}
    process = None
    deadline = time.monotonic() + TIMEOUT
    try:
        async with asyncio.timeout(TIMEOUT):
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "loom.weather", "--latitude", str(lat), "--longitude", str(lng),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await process.communicate()
            if process.returncode != 0 or len(out) > MAX_BYTES:
                return {"status": "unavailable"}
            raw = json.loads(out).get("weather")
            _remaining(deadline)
            if isinstance(raw, dict) and raw.get("status") in ("unknown", "unavailable"):
                return {"status": raw["status"]}
            return validate_snapshot(raw)
    except (TimeoutError, OSError, ValueError, TypeError, AttributeError, RecursionError):
        return {"status": "unavailable"}
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def query_weather(lat, lng, at, *, fetch=None, day=False):
    """Read one hour. Failure/unknown are data, with no raw error body.

    Deliberately uncached. Journey's explicit cache owner may store a result;
    the room stores only the previous observation, never an API response.
    """
    if not _number(lat, -90, 90) or not _number(lng, -180, 180):
        return {"status": "unknown"}
    if not isinstance(at, datetime) or at.tzinfo is None:
        return {"status": "unknown"}
    # Window uses UTC. Journey keeps its IANA local-day summary; fixed
    # offsets without an IANA name use UTC. No timezone string from API.
    zone = getattr(at.tzinfo, "key", "UTC")
    hour = (at if zone != "UTC" else at.astimezone(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": f"{lat:.4f}", "longitude": f"{lng:.4f}", "timezone": zone,
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "start_date": hour.date().isoformat(), "end_date": hour.date().isoformat(),
    })
    try:
        raw = (fetch or _http_json)(url)
        hourly = raw["hourly"]
        times = hourly["time"]
        if not isinstance(times, list) or len(times) > 25:
            raise ValueError
        i = times.index(hour.strftime("%Y-%m-%dT%H:00"))
        snapshot = {"status": "ok", "at": hour.isoformat(), "latitude": lat,
            "longitude": lng, "code": hourly["weather_code"][i],
            "temperature_c": hourly["temperature_2m"][i],
            "rain_probability": hourly["precipitation_probability"][i]}
        if day:
            temps = hourly["temperature_2m"]
            probs = hourly["precipitation_probability"]
            if (isinstance(temps, list) and isinstance(probs, list) and len(temps) == len(probs) == len(times)
                    and all(_number(t, -100, 70) for t in temps)
                    and all(_number(p, 0, 100) for p in probs)):
                daytime = [p for t, p in zip(times, probs)
                           if isinstance(t, str) and len(t) == 16 and 8 <= datetime.fromisoformat(t).hour <= 22]
                if daytime:
                    snapshot["day"] = {"min_c": min(temps), "max_c": max(temps), "rain_max": max(daytime)}
        return validate_snapshot(snapshot)
    except (KeyError, IndexError, TypeError, ValueError):
        return {"status": "unknown"}
    except Exception:  # network/transport failures must not relay error text
        return {"status": "unavailable"}


def format_weather(snapshot):
    s = validate_snapshot(snapshot)
    if s["status"] != "ok":
        return "天氣未知（資料不可用）"
    line = f"{WMO[s['code']]} {s['temperature_c']:.0f}°C 降雨 {s['rain_probability']:.0f}%"
    if d := s.get("day"):
        line += f"（今天 {d['min_c']:.0f}–{d['max_c']:.0f}°C，白天最高降雨 {d['rain_max']:.0f}%）"
    return line


def window_changes(snapshot, previous):
    """First/repeated/older/unknown observations are quiet; no writes."""
    s, p = validate_snapshot(snapshot), validate_snapshot(previous)
    if s["status"] != "ok" or p["status"] != "ok":
        return []
    if (s["latitude"], s["longitude"]) != (p["latitude"], p["longitude"]):
        return []
    if datetime.fromisoformat(s["at"]) <= datetime.fromisoformat(p["at"]):
        return []
    changes = []
    if s["code"] in RAIN and p["code"] not in RAIN:
        changes.append(("rain_started", "窗外開始下雨。"))
    if s["temperature_c"] <= p["temperature_c"] - 3:
        changes.append(("cooled", f"窗外降溫至 {s['temperature_c']:.0f}°C。"))
    return [{"id": sid, "key": s["at"], "text": text} for sid, text in changes]


def make_weather_tool():
    from loom.core.harness.middleware import ToolResult
    from loom.core.harness.permissions import ToolCapability, TrustLevel
    from loom.core.harness.registry import ToolDefinition

    async def weather(call):
        s = await _query_with_deadline(call.args.get("latitude"), call.args.get("longitude"))
        return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True,
                          output=json.dumps(s, allow_nan=False))

    return ToolDefinition(name="weather", description="查指定經緯度的結構化天氣；未知會明示，不寫快取。",
        trust_level=TrustLevel.SAFE, capabilities=ToolCapability.NETWORK | ToolCapability.READ_PROBE,
        input_schema={"type": "object", "properties": {
            "latitude": {"type": "number", "minimum": -90, "maximum": 90},
            "longitude": {"type": "number", "minimum": -180, "maximum": 180}},
            "required": ["latitude", "longitude"], "additionalProperties": False}, executor=weather)


def main():
    """Read-only window command. The daemon, not this process, saves baseline."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    args = parser.parse_args()
    print(json.dumps({"weather": query_weather(args.latitude, args.longitude,
                                              datetime.now(timezone.utc))}, allow_nan=False))


if __name__ == "__main__":
    main()
