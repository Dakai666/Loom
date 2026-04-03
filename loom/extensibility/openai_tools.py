"""
OpenAIToolsLens — imports OpenAI-compatible tool definitions into Loom.

This lens understands the standard OpenAI function-calling schema, which is
also used by many third-party agent frameworks (LangChain, AutoGen, etc.).
It converts tool definitions into Loom AdapterRegistry entries so they can be
installed into a session without touching Loom's core harness.

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
    "middleware": [                         (optional, informational only)
        {
            "name":        "RateLimitMiddleware",
            "description": "Throttles outbound API calls to 10 req/s."
        }
    ]
}

``parameters`` maps directly to Loom's ``input_schema`` field.
``trust`` is Loom-specific; if absent, defaults to "safe".

Middleware entries are recorded in ``LensResult.middleware_patterns`` for
documentation purposes but are never auto-imported — adding middleware to
Loom's harness is a deliberate, code-level act.

Extracted adapters are ready for ``AdapterRegistry.from_lens_result()``.
"""

from __future__ import annotations

from loom.extensibility.lens import BaseLens, LensResult


class OpenAIToolsLens(BaseLens):
    """
    Lens for OpenAI-compatible tool definition format.

    Extracts platform adapters (tool definitions) and records any middleware
    patterns as informational annotations.
    """

    name = "openai_tools"
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
        """Extract adapter dicts and middleware patterns from the source."""
        parsed = self._parse(source)

        if not isinstance(parsed, dict):
            return LensResult(
                source="openai_tools",
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
                "input_schema": raw.get(
                    "parameters",
                    raw.get("input_schema", {"type": "object", "properties": {}}),
                ),
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
            source="openai_tools",
            platform_adapters=adapters,
            middleware_patterns=middleware_patterns,
            warnings=warnings,
        )
