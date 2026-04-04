"""Loom Discord Bot platform."""

__all__ = ["LoomDiscordBot"]


def __getattr__(name: str):
    if name == "LoomDiscordBot":
        from .bot import LoomDiscordBot
        return LoomDiscordBot
    raise AttributeError(name)
