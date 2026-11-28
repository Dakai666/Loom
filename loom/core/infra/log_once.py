"""Process-local, keyed diagnostics shared by repeatedly loaded config."""

import logging
from threading import Lock
from typing import Hashable

_seen: set[tuple[str, Hashable]] = set()
_lock = Lock()


def log_once(logger: logging.Logger, level: int, key: Hashable,
             message: str, *args: object) -> None:
    """Emit once per logger/key in this process, even across threads."""
    with _lock:
        identity = (logger.name, key)
        if identity in _seen:
            return
        _seen.add(identity)
    logger.log(level, message, *args)
