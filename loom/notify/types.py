"""
Notification data types — the canonical contract between the
Action Planner and all notification adapters.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Any


class NotificationType(Enum):
    INFO    = "info"     # pure information, no reply needed
    CONFIRM = "confirm"  # yes/no required; timeout → degraded action
    INPUT   = "input"    # freeform user input needed before continuing
    ALERT   = "alert"    # urgent, needs immediate attention
    REPORT  = "report"   # periodic summary


class ConfirmResult(Enum):
    APPROVED = "approved"
    DENIED   = "denied"
    TIMEOUT  = "timeout"


@dataclass
class Notification:
    """
    A single notification produced by the Action Planner or Autonomy Engine.

    `timeout_seconds` only applies to CONFIRM / INPUT types.
    When it elapses without a reply, the ConfirmFlow returns TIMEOUT.
    """
    type: NotificationType
    title: str
    body: str
    trigger_name: str = ""
    timeout_seconds: int = 60
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
