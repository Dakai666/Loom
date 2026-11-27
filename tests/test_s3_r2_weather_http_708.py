"""Synthetic local HTTP transport, never Open-Meteo or the weather CLI."""
import http.client
import http.server
import json
import threading
from datetime import datetime, timezone

from loom.weather import query_weather


def test_http10_closes_socket_after_last_body_chunk(monkeypatch):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    body = json.dumps({"hourly": {"time": [stamp], "weather_code": [61],
        "temperature_2m": [17], "precipitation_probability": [80]}}).encode()
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(http.client, "HTTPSConnection", lambda host, timeout:
            http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout))
        result = query_weather(25, 121, datetime.now(timezone.utc))
        assert result["status"] == "ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
