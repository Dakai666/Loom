"""
Vision input — canonical shape and provider converters.

The Loom harness used to assume ``{"role": "user", "content": "string"}`` only.
M3 (and any other native multimodal model we wire in the future) needs to be
able to receive image inputs alongside text. This module defines:

  * :class:`VisionInput` — provider-neutral descriptor for an image reference
  * :func:`validate_vision_input` — magic-byte MIME detection, size limits
  * :func:`to_anthropic_image_block` — canonical → Anthropic ``image`` block
  * :func:`to_openai_image_block`   — canonical → OpenAI-style ``image_url`` /
                                       ``input_image`` blocks
  * :func:`load_vision_input`       — read a local file path, build a
                                       ``VisionInput`` with a fresh digest
  * :func:`extract_image_paths`     — helper used by the CLI to detect
                                       workspace-relative image references
                                       in free-form user input

Security/transport rules (see issue #505):

  * Magic-byte MIME detection — never trust the file extension alone.
  * Hard size cap (:data:`MAX_SIZE_BYTES`) before we even build a base64 blob.
  * No EXIF passthrough — the validator only exposes ``media_type`` and
    ``digest``; callers must not read other bytes from the source after
    :func:`load_vision_input` returns.
  * No base64 ever enters semantic memory or session history in raw form.
    Use the ``digest`` for cross-session references; the agent's
    text observation is the durable record.

Provider-neutral canonical block (used inside ``content`` list):

    {"type": "image", "source": {
        "kind": "file" | "url" | "raw",
        "ref":  <Path> | <str URL> | <bytes>,
        "media_type": "image/png" | ...,
        "digest": "sha256:<hex>",
    }}
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

SUPPORTED_MIME: Final[set[str]] = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}
"""Image MIME types the harness will accept. Add to this set as the
supported-model matrix grows — but only after verifying the model's
provider actually accepts the new type."""

MAX_SIZE_BYTES: Final[int] = 10 * 1024 * 1024
"""Hard cap on image size after magic-byte validation. 10 MB is a
deliberate, conservative ceiling for MVP; raise via ``loom.toml`` only
after we have a per-model budget table."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VisionInputError(ValueError):
    """Raised when an image reference fails validation.

    The harness treats this as a user-facing error (CLI prints a clear
    message, Discord bot replies in-thread). Providers should not catch
    this silently — if the model rejected the input we want the user
    to know.
    """


# ---------------------------------------------------------------------------
# Canonical shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VisionSource:
    """Reference to where the image bytes live.

    The validator normalizes everything to a single shape so the
    provider adapters never need to know whether the upstream came
    from a workspace path, a remote URL, or raw bytes (e.g. an MCP
    tool result).
    """

    kind: str  # "file" | "url" | "raw"
    ref: Any
    """Path for ``file``, ``str`` URL for ``url``, ``bytes`` for ``raw``."""


@dataclass(frozen=True)
class VisionInput:
    """Provider-neutral image input descriptor.

    Construct via :func:`load_vision_input` (preferred for the CLI/Discord
    flow) or by hand when the bytes are already in memory (e.g. an MCP tool
    that just produced an image artifact).
    """

    source: VisionSource
    caption: str = ""
    media_type: str = ""   # filled by validate
    size_bytes: int = 0    # filled by validate
    digest: str = ""       # sha256:<hex> filled by validate
    origin: str = "user"   # "user" | "discord" | "tool" | "agent"


# ---------------------------------------------------------------------------
# Magic-byte MIME detection
# ---------------------------------------------------------------------------

# Mirrors the standard magic-byte patterns. We only care about the four
# formats we accept in SUPPORTED_MIME — anything else raises.
_MAGIC_BYTES: Final[dict[bytes, str]] = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",  # RIFF....WEBP — checked more strictly below
}
_WEBP_OFFSET: Final[int] = 8  # "WEBP" sits at byte 8 of a RIFF container


def _detect_mime(data: bytes) -> str:
    """Best-effort MIME detection from the first ~12 bytes.

    Falls back to ``application/octet-stream`` if nothing matches; the
    caller is expected to reject anything outside :data:`SUPPORTED_MIME`.
    """
    for sig, mime in _MAGIC_BYTES.items():
        if data.startswith(sig):
            if mime == "image/webp":
                # RIFF alone is too permissive — confirm the WEBP marker.
                if len(data) >= _WEBP_OFFSET + 4 and data[_WEBP_OFFSET:_WEBP_OFFSET + 4] == b"WEBP":
                    return mime
                continue
            return mime
    return "application/octet-stream"


# ---------------------------------------------------------------------------
# SHA-256 digest helper
# ---------------------------------------------------------------------------


def _digest_bytes(data: bytes) -> str:
    """Stable, prefixed digest used for cross-session references.

    The ``sha256:`` prefix matches the convention used by the broader
    Loom ledger so vision artifacts are searchable alongside other
    content-addressed entries.
    """
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _read_source(source: VisionSource) -> bytes:
    """Read bytes from a :class:`VisionSource`.

    For ``raw`` the bytes are returned unchanged. For ``file`` we trust
    the path (caller must already have passed scope/security checks).
    For ``url`` we only accept ``http(s)`` schemes to avoid surprising
    side effects from ``file://`` or ``data:`` URLs.
    """
    if source.kind == "raw":
        if not isinstance(source.ref, (bytes, bytearray)):
            raise VisionInputError(f"raw source expects bytes, got {type(source.ref).__name__}")
        return bytes(source.ref)

    if source.kind == "file":
        if not isinstance(source.ref, (str, os.PathLike)):
            raise VisionInputError(f"file source expects path, got {type(source.ref).__name__}")
        path = Path(source.ref)
        if not path.is_file():
            raise VisionInputError(f"file not found: {path}")
        return path.read_bytes()

    if source.kind == "url":
        if not isinstance(source.ref, str):
            raise VisionInputError(f"url source expects str, got {type(source.ref).__name__}")
        parsed = urllib.parse.urlparse(source.ref)
        if parsed.scheme not in {"http", "https"}:
            raise VisionInputError(
                f"only http(s) URLs are accepted (got scheme {parsed.scheme!r})"
            )
        # Network IO is intentionally not done here. URL-based image
        # references are passed through to providers that support them
        # (Anthropic, OpenAI, xAI) and let the model fetch. If we ever
        # need eager prefetch we can wire it via the ``fetch_url`` tool
        # and the resulting ``raw`` source.
        return b""

    raise VisionInputError(f"unknown source kind: {source.kind!r}")


def validate_vision_input(v: VisionInput) -> VisionInput:
    """Populate ``media_type``/``size_bytes``/``digest`` and enforce limits.

    Returns a new :class:`VisionInput` with the validation fields filled
    in. URL sources skip the byte-level checks because we don't read
    them eagerly — providers do the fetch.
    """
    if v.media_type and v.size_bytes and v.digest:
        # Already validated.
        return v

    if v.source.kind == "url":
        if v.media_type and v.media_type not in SUPPORTED_MIME:
            raise VisionInputError(f"unsupported URL media_type: {v.media_type}")
        # We can't measure size from a URL without fetching; defer to
        # the provider's own size guard.
        return VisionInput(
            source=v.source,
            caption=v.caption,
            media_type=v.media_type or "image/unknown",
            size_bytes=v.size_bytes,
            digest=v.digest,
            origin=v.origin,
        )

    data = _read_source(v.source)
    if not data:
        raise VisionInputError("empty image data")
    if len(data) > MAX_SIZE_BYTES:
        raise VisionInputError(
            f"image too large: {len(data)} bytes (cap {MAX_SIZE_BYTES})"
        )

    mime = _detect_mime(data)
    if mime not in SUPPORTED_MIME:
        raise VisionInputError(
            f"unsupported image format: {mime} "
            f"(supported: {', '.join(sorted(SUPPORTED_MIME))})"
        )

    return VisionInput(
        source=v.source,
        caption=v.caption,
        media_type=mime,
        size_bytes=len(data),
        digest=_digest_bytes(data),
        origin=v.origin,
    )


# ---------------------------------------------------------------------------
# Loading from local paths
# ---------------------------------------------------------------------------


def load_vision_input(
    path: str | os.PathLike,
    *,
    caption: str = "",
    origin: str = "user",
) -> VisionInput:
    """Convenience: read a local file and validate it.

    The caller is responsible for security/scope checks — this function
    assumes the path has already been authorised (e.g. inside the session
    workspace).
    """
    src = VisionSource(kind="file", ref=path)
    return validate_vision_input(
        VisionInput(source=src, caption=caption, origin=origin)
    )


# ---------------------------------------------------------------------------
# Free-form input parsing (CLI use)
# ---------------------------------------------------------------------------

# Heuristic: any whitespace-bounded token ending in a known image
# extension, OR a path that exists on disk and starts with the magic
# bytes of a supported image. Kept conservative — the issue's "natural
# input" goal is about *not* requiring ``/see``, not about auto-attaching
# every plausible path.
_IMAGE_EXTS: Final[tuple[str, ...]] = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_PATH_TOKEN: Final[re.Pattern[str]] = re.compile(r"[\s\"'`]([^\s\"'`]+\.[A-Za-z0-9]{2,5})")


def extract_image_paths(text: str, base: Path | None = None) -> list[Path]:
    """Return absolute paths to image files referenced in ``text``.

    A token is accepted if:
      * its extension is in :data:`_IMAGE_EXTS`, AND
      * it resolves to an existing file (relative to ``base`` or
        absolute).

    The function never raises — unresolvable or non-image tokens are
    silently ignored. The caller is expected to feed the returned
    paths through :func:`load_vision_input` so that magic-byte detection
    gets a final say.
    """
    base = base or Path.cwd()
    found: list[Path] = []
    seen: set[Path] = set()
    for match in _PATH_TOKEN.finditer(text):
        token = match.group(1)
        if not token.lower().endswith(_IMAGE_EXTS):
            continue
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = (base / candidate).resolve()
        if not candidate.is_file():
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        found.append(candidate)
    return found


# ---------------------------------------------------------------------------
# Provider converters
# ---------------------------------------------------------------------------


def _read_for_block(v: VisionInput) -> bytes:
    """Read bytes for inline base64 embedding. URL sources return empty."""
    if v.source.kind == "url":
        return b""
    return _read_source(v.source)


def to_anthropic_image_block(v: VisionInput) -> dict[str, Any]:
    """Convert a :class:`VisionInput` to an Anthropic ``image`` block.

    Uses ``base64`` source for file/raw inputs and ``url`` source for
    URL references — the Anthropic API accepts both and we let the
    provider decide which to optimise for.
    """
    v = validate_vision_input(v)
    if v.source.kind == "url":
        return {
            "type": "image",
            "source": {
                "type": "url",
                "url": v.source.ref,
            },
        }
    data = _read_for_block(v)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": v.media_type,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def to_openai_image_block(v: VisionInput) -> dict[str, Any]:
    """Convert a :class:`VisionInput` to an OpenAI-style ``image_url`` block.

    Compatible with ``/v1/chat/completions`` (OpenAI, xAI, OpenRouter,
    LMStudio) and the Responses API ``input_image`` shape via the
    :func:`to_responses_image_block` adapter.
    """
    v = validate_vision_input(v)
    if v.source.kind == "url":
        return {
            "type": "image_url",
            "image_url": {"url": v.source.ref},
        }
    data = _read_for_block(v)
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{v.media_type};base64,{encoded}"},
    }


def to_responses_image_block(v: VisionInput) -> dict[str, Any]:
    """Convert a :class:`VisionInput` to a Responses API ``input_image`` block.

    The Responses API uses ``input_image`` instead of ``image_url`` but
    otherwise accepts the same payload. Kept as a separate function so
    call-sites read clearly.
    """
    block = to_openai_image_block(v)
    return {"type": "input_image", "image_url": block["image_url"]["url"]}


def build_user_content(
    text: str,
    images: list[VisionInput] | None = None,
) -> str | list[dict[str, Any]]:
    """Build a canonical user ``content`` field.

    Returns the original ``text`` when no images are provided so we don't
    perturb providers that handle list content poorly. With images, returns
    a list of blocks in the order ``[text, image, image, ...]``.
    """
    if not images:
        return text
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for img in images:
        validated = validate_vision_input(img)
        blocks.append({
            "type": "image",
            "source": {
                "kind": validated.source.kind,
                # ``file`` and ``url`` refs are both JSON-serialisable.
                # File refs are coerced to str so Path objects (which
                # callers may pass in) become a plain string. Only
                # ``raw`` (bytes) is dropped at this stage — the
                # session_log strip handles persistence-time filtering,
                # so we do not need to over-eagerly null out file refs.
                "ref": (
                    str(validated.source.ref)
                    if validated.source.kind in {"url", "file"} else None
                ),
                "media_type": validated.media_type,
                "digest": validated.digest,
            },
        })
    return blocks


__all__ = [
    "MAX_SIZE_BYTES",
    "SUPPORTED_MIME",
    "VisionInput",
    "VisionInputError",
    "VisionSource",
    "build_user_content",
    "extract_image_paths",
    "load_vision_input",
    "to_anthropic_image_block",
    "to_openai_image_block",
    "to_responses_image_block",
    "validate_vision_input",
]
