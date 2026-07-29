"""Agent daemon: local HTTP server that processes AgentRequests via existing adapters.

One agentd process per agent identity. Bridges submit requests via HTTP POST to localhost.
Agentd manages adapter call/resume, session persistence, timeouts, and health checks.
"""

__all__ = ["AgentDaemon"]


def __getattr__(name: str):
    if name == "AgentDaemon":
        from multinexus.agentd.server import AgentDaemon

        return AgentDaemon
    raise AttributeError(name)
