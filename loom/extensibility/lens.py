"""
Lens System — abstract base and registry for importing foreign framework artifacts.

A Lens reads the output of another agent framework (YAML skills, JSON tool defs,
Python modules) and converts it into Loom-compatible data structures that the
SkillImportPipeline and AdapterRegistry can consume.

Design
------
- Lenses are stateless. ``extract()`` takes a source and returns a ``LensResult``.
- ``supports()`` lets the registry auto-detect the right lens for a given source.
- ``LensRegistry`` can detect, route, and invoke lenses by name or auto-discovery.

Adding a new lens
-----------------
1. Subclass ``BaseLens``, set ``name`` and ``version`` as class attributes.
2. Implement ``extract()`` and ``supports()``.
3. Register with ``LensRegistry.register(MyLens())``.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# LensResult
# ---------------------------------------------------------------------------

@dataclass
class LensResult:
    """
    Artifacts extracted by a lens from a foreign framework.

    Fields
    ------
    source:              Human-readable label for the extraction origin.
    skills:              Raw skill dicts ready for ``SkillImportPipeline``.
    middleware_patterns: Informational only — describe patterns observed in the
                         foreign harness (not auto-imported).
    platform_adapters:   Tool definitions ready for ``AdapterRegistry``.
    warnings:            Non-fatal issues encountered during extraction.
    """
    source: str
    skills: list[dict] = field(default_factory=list)
    middleware_patterns: list[dict] = field(default_factory=list)
    platform_adapters: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.skills and not self.middleware_patterns and not self.platform_adapters


# ---------------------------------------------------------------------------
# BaseLens
# ---------------------------------------------------------------------------

class BaseLens(ABC):
    """
    Abstract base for all Loom lenses.

    Subclasses must:
    - Set ``name`` (class attribute) to a unique identifier string.
    - Implement ``extract(source)`` and ``supports(source)``.

    The ``_parse()`` helper converts str/dict/list sources into Python objects.
    """

    name: str = ""
    version: str = "1.0"

    @abstractmethod
    def extract(self, source: str | dict) -> LensResult:
        """
        Extract Loom-compatible data from a foreign framework artifact.

        Parameters
        ----------
        source: A JSON string, a pre-parsed dict, or a file path string.
        """

    @abstractmethod
    def supports(self, source: str | dict) -> bool:
        """Return True if this lens can process the given source."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse(self, source: str | dict | list) -> dict | list | None:
        """Parse *source* to a Python object. Returns None if unparseable."""
        if isinstance(source, (dict, list)):
            return source
        if isinstance(source, str):
            try:
                return json.loads(source)
            except (json.JSONDecodeError, ValueError):
                return None
        return None


# ---------------------------------------------------------------------------
# LensRegistry
# ---------------------------------------------------------------------------

class LensRegistry:
    """
    Registry of all available lenses.

    Usage
    -----
        registry = LensRegistry()
        registry.register(HermesLens())
        registry.register(ClawCodeLens())

        result = registry.extract(source_dict)          # auto-detect
        result = registry.extract(source_dict, lens_name="hermes")  # explicit
    """

    def __init__(self) -> None:
        self._lenses: dict[str, BaseLens] = {}

    def register(self, lens: BaseLens) -> None:
        """Register a lens instance under its ``name``."""
        self._lenses[lens.name] = lens

    def get(self, name: str) -> BaseLens | None:
        """Return the lens with the given name, or None."""
        return self._lenses.get(name)

    def detect(self, source: str | dict) -> list[BaseLens]:
        """Return all lenses that claim to support this source."""
        return [lens for lens in self._lenses.values() if lens.supports(source)]

    def extract(
        self,
        source: str | dict,
        *,
        lens_name: str | None = None,
    ) -> LensResult | None:
        """
        Run extraction using a specific lens or the first auto-detected one.

        Returns None if no matching lens is found.
        """
        if lens_name:
            lens = self.get(lens_name)
            return lens.extract(source) if lens else None
        for lens in self.detect(source):
            return lens.extract(source)
        return None

    @property
    def registered_names(self) -> list[str]:
        """Names of all registered lenses."""
        return list(self._lenses.keys())
