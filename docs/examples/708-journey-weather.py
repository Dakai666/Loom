"""Replace journey/weather.py with this whole file, after installing S3.

Compatibility callable for the existing journey.py/engine.now contract.
Journey still owns its existing JsonCache and state; no CLI/state changes.
"""
from loom.weather import query_weather, validate_snapshot, format_weather


def make_weather(cache, fetch=None):
    def weather(lat, lng, dt, day=False):
        # New schema: never interpret the old raw API cache as a snapshot.
        key = f"weather:v2:{lat:.4f},{lng:.4f}:{dt.isoformat()[:13]}:{dt.utcoffset()}:{day}"
        cached = cache.get(key, 3600)  # read only
        snapshot = validate_snapshot(cached)
        if snapshot["status"] != "ok":
            snapshot = query_weather(lat, lng, dt, fetch=fetch, day=day)  # query only
            if snapshot["status"] == "ok":
                cache.set(key, snapshot)  # explicit journey-owned cache write
        return format_weather(snapshot)
    return weather
