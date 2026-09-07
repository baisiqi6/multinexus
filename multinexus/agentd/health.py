"""Best-effort local health projection for a managed agentd process.

The projection is an operator read model. Coordinate remains authoritative for
jobs, leases and liveness; this file only answers whether this local process
reached readiness or entered a fail-closed state.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

HEALTH_SCHEMA_VERSION = 1
HEALTH_STATES = frozenset(
    {"starting", "ready", "processing", "degraded", "latched", "stopped"}
)
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class AgentdHealthProjection:
    """Write a bounded, atomic, local-only agentd status document."""

    def __init__(self, path: Path | None, *, agent_id: str):
        self.path = path
        self.agent_id = agent_id
        self.state = "stopped"
        self.reason_code = ""

    @classmethod
    def from_environment(cls, agent_id: str) -> "AgentdHealthProjection":
        raw_path = os.environ.get("MULTINEXUS_AGENTD_HEALTH_FILE", "").strip()
        return cls(Path(raw_path).expanduser() if raw_path else None, agent_id=agent_id)

    def write(self, state: str, reason_code: str = "") -> None:
        """Publish a state without allowing projection failures to stop agentd."""
        if state not in HEALTH_STATES:
            raise ValueError(f"unsupported agentd health state: {state!r}")
        if reason_code and not _REASON_CODE.fullmatch(reason_code):
            raise ValueError(f"invalid agentd health reason code: {reason_code!r}")

        self.state = state
        self.reason_code = reason_code
        if self.path is None:
            return

        document = {
            "schema_version": HEALTH_SCHEMA_VERSION,
            "agent_id": self.agent_id,
            "state": state,
            "reason_code": reason_code,
            "updated_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                json.dump(document, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                temporary = Path(handle.name)
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                if "temporary" in locals():
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass
            log.warning(
                "Unable to write agentd health projection %s: %s",
                self.path,
                type(exc).__name__,
            )
