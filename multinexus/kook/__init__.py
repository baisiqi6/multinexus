"""KOOK bridge for MultiNexus.

Handles KOOK Gateway (WebSocket) and HTTP polling, mention routing,
and message filtering. Submits AgentRequests to the local agentd
instead of calling adapters directly.
"""


def __getattr__(name):
    if name == "KookBridge":
        from .bot import KookBridge
        return KookBridge
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["KookBridge"]
