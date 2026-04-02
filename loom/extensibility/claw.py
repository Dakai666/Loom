"""
ClawCodeLens — converts instructkr/claw-code tool definitions to Loom adapter dicts.

Expected source format (dict or JSON string)
--------------------------------------------
{
    "tools": [
        {
            "name":        "search_web",
            "description": "Search the web and return top results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            },
            "trust": "safe",
            "tags":  ["web", "search"]
        }
    ],
    "middleware": [                        (optional)
        {
            "name":        "RateLimitMiddleware",
            "description": "Throttles outbound API calls to 10 req/s."
        }
    ]
}

Extracted adapters are ready for AdapterRegistry.from_lens_result().
Middleware patterns are informational only — they describe harness behaviors
observed in claw-code but are not auto-imported.
"""

from __future__ import annotations

from loom.extensibility.lens import BaseLens, LensResult


class ClawCodeLens(BaseLens):
    """
    Lens for claw-code (instructkr) tool-definition format.

    Extracts platform adapters (tool definitions) and middleware patterns.
    """

    name = "claw"
    version = "1.0"

    _VALID_TRUST = frozenset({"safe", "guarded", "critical"})

    def supports(self, source: str | dict) -> bool:
        """Return True if the source contains a top-level 'tools' key."""
        parsed = self._parse(source)
        if isinstance(parsed, dict):
            return "tools" in parsed
        if isinstance(source, str):
            return '"tools"' in source
        return False

    def extract(self, source: str | dict) -> LensResult:
        """Extract adapter dicts and middleware patterns from a claw-code source."""
        parsed = self._parse(source)

        if not isinstance(parsed, dict):
            return LensResult(
                source="claw",
                warnings=["Could not parse source as a dict"],
            )

        raw_tools = parsed.get("tools", [])
        raw_middleware = parsed.get("middleware", [])

        adapters: list[dict] = []
        middleware_patterns: list[dict] = []
        warnings: list[str] = []

        for i, raw in enumerate(raw_tools if isinstance(raw_tools, list) else []):
            if not isinstance(raw, dict):
                warnings.append(f"Tool[{i}] is not a dict — skipped")
                continue

            name = (raw.get("name") or "").strip()
            if not name:
                warnings.append(f"Tool[{i}] has no 'name' — skipped")
                continue

            trust = (raw.get("trust") or "safe").lower()
            if trust not in self._VALID_TRUST:
                warnings.append(
                    f"Tool '{name}' has unknown trust '{trust}' — defaulting to 'safe'"
                )
                trust = "safe"

            adapters.append({
                "name":         name,
                "description":  (raw.get("description") or "").strip(),
                "input_schema": raw.get("parameters", {"type": "object", "properties": {}}),
                "trust_level":  trust,
                "tags":         list(raw.get("tags", [])),
            })

        for raw in (raw_middleware if isinstance(raw_middleware, list) else []):
            if isinstance(raw, dict) and (raw.get("name") or "").strip():
                middleware_patterns.append({
                    "name":        raw["name"].strip(),
                    "description": (raw.get("description") or "").strip(),
                })

        return LensResult(
            source="claw",
            platform_adapters=adapters,
            middleware_patterns=middleware_patterns,
            warnings=warnings,
        )
