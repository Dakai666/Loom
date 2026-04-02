"""
PromptStack — three-layer prompt composition system.

Injection order: SOUL (global identity) → Agent (project-specific) → Personality (session-scoped).
Later layers supplement earlier principles; they do not override them.

Layers
------
SOUL.md       Global, permanent identity. Always loaded first. Never grows.
Agent.md      Project/environment-specific context. Can be self-updated by the agent.
Personality   A cognitive lens for the session. Swappable at runtime.

Usage
-----
    stack = PromptStack.from_config(toml_config, base_dir=Path.cwd())
    system_prompt = stack.load()            # compose all layers
    stack.switch_personality("adversarial") # swap lens
    stack.clear_personality()               # remove lens
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class PromptLayer:
    """A single layer in the prompt stack."""
    name: str           # "soul" | "agent" | "personality"
    content: str
    path: Path | None = None


class PromptStack:
    """
    Composes the agent system prompt from up to three ordered layers.

    Parameters
    ----------
    soul_path:          Path to SOUL.md (global identity).
    agent_path:         Path to Agent.md (project-specific, optional).
    personality_path:   Path to a personality file (session-scoped, optional).
    personalities_dir:  Directory containing named personality files.
    """

    LAYER_SEPARATOR = "\n\n---\n\n"

    def __init__(
        self,
        soul_path: str | Path | None = None,
        agent_path: str | Path | None = None,
        personality_path: str | Path | None = None,
        personalities_dir: str | Path | None = None,
    ) -> None:
        self._soul_path = Path(soul_path) if soul_path else None
        self._agent_path = Path(agent_path) if agent_path else None
        self._personality_path = Path(personality_path) if personality_path else None
        self._personalities_dir: Path = (
            Path(personalities_dir) if personalities_dir else Path("personalities")
        )
        self._layers: list[PromptLayer] = []

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> str:
        """
        Read all configured layer files and build the composed prompt.
        Missing files are silently skipped.
        Returns the composed system prompt string.
        """
        self._layers = []

        # Layer 1 — SOUL
        if self._soul_path and self._soul_path.exists():
            self._layers.append(PromptLayer(
                "soul",
                self._soul_path.read_text(encoding="utf-8"),
                self._soul_path,
            ))

        # Layer 2 — Agent
        if self._agent_path and self._agent_path.exists():
            self._layers.append(PromptLayer(
                "agent",
                self._agent_path.read_text(encoding="utf-8"),
                self._agent_path,
            ))

        # Layer 3 — Personality
        if self._personality_path and self._personality_path.exists():
            self._layers.append(PromptLayer(
                "personality",
                self._personality_path.read_text(encoding="utf-8"),
                self._personality_path,
            ))

        return self.composed_prompt

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def composed_prompt(self) -> str:
        """The system prompt produced by joining all loaded layers."""
        return self.LAYER_SEPARATOR.join(layer.content for layer in self._layers)

    @property
    def layer_names(self) -> list[str]:
        """Names of currently loaded layers, in injection order."""
        return [layer.name for layer in self._layers]

    @property
    def current_personality(self) -> str | None:
        """Stem name of the active personality file, or None."""
        for layer in self._layers:
            if layer.name == "personality" and layer.path:
                return layer.path.stem
        return None

    # ------------------------------------------------------------------
    # Runtime switching
    # ------------------------------------------------------------------

    def switch_personality(self, name: str) -> bool:
        """
        Load a personality by name from the personalities directory.

        Parameters
        ----------
        name:   Stem name of the personality file (without .md extension).

        Returns True if the file was found and loaded; False otherwise.
        """
        candidate = self._personalities_dir / f"{name}.md"
        if not candidate.exists():
            return False

        content = candidate.read_text(encoding="utf-8")
        new_layer = PromptLayer("personality", content, candidate)

        for i, layer in enumerate(self._layers):
            if layer.name == "personality":
                self._layers[i] = new_layer
                return True

        self._layers.append(new_layer)
        return True

    def clear_personality(self) -> None:
        """Remove the personality layer, keeping SOUL and Agent layers."""
        self._layers = [l for l in self._layers if l.name != "personality"]
        self._personality_path = None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def available_personalities(self) -> list[str]:
        """List personality stem names available in the personalities directory."""
        if not self._personalities_dir.exists():
            return []
        return sorted(p.stem for p in self._personalities_dir.glob("*.md"))

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: dict, base_dir: Path | None = None) -> "PromptStack":
        """
        Build a PromptStack from a parsed loom.toml config dict.

        Reads the ``[identity]`` section:
            soul             = "SOUL.md"               # default
            agent            = "Agent.md"              # optional
            personality      = "personalities/foo.md"  # optional
            personalities_dir = "personalities"        # default

        Paths are resolved relative to *base_dir* (defaults to cwd).
        """
        base = base_dir or Path.cwd()
        identity = config.get("identity", {})

        def _resolve(val: str | None, default: str | None = None) -> Path | None:
            v = val if val is not None else default
            if not v:           # None or empty string → skip
                return None
            p = Path(v)
            return p if p.is_absolute() else base / p

        return cls(
            soul_path=_resolve(identity.get("soul"), "SOUL.md"),
            agent_path=_resolve(identity.get("agent")),
            personality_path=_resolve(identity.get("personality")),
            personalities_dir=_resolve(identity.get("personalities_dir"), "personalities"),
        )
