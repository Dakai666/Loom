from __future__ import annotations


RESPONSES_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
RESPONSES_REASONING_DEFAULT_EFFORT = "high"


def normalize_reasoning_effort(value: str | None) -> str:
    if not value:
        return RESPONSES_REASONING_DEFAULT_EFFORT
    candidate = str(value).strip().lower()
    if candidate in RESPONSES_REASONING_EFFORTS:
        return candidate
    return RESPONSES_REASONING_DEFAULT_EFFORT
