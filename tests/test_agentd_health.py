from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from multinexus.agentd.health import AgentdHealthProjection


class AgentdHealthProjectionTests(unittest.TestCase):
    def test_writes_bounded_atomic_json_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "agentd.json"
            projection = AgentdHealthProjection(path, agent_id="mac-claude")

            projection.write("latched", "claim_authority_uncertain")

            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["schema_version"], 1)
            self.assertEqual(document["agent_id"], "mac-claude")
            self.assertEqual(document["state"], "latched")
            self.assertEqual(document["reason_code"], "claim_authority_uncertain")
            self.assertTrue(document["updated_at"].endswith("Z"))
            self.assertEqual(list(path.parent.glob(".*.agentd.json.*")), [])

    def test_disabled_projection_keeps_state_without_filesystem_write(self) -> None:
        projection = AgentdHealthProjection(None, agent_id="test-agent")
        projection.write("degraded", "contract_probe_unavailable")
        self.assertEqual(projection.state, "degraded")
        self.assertEqual(projection.reason_code, "contract_probe_unavailable")

    def test_environment_selects_projection_path(self) -> None:
        with patch.dict(
            "os.environ",
            {"MULTINEXUS_AGENTD_HEALTH_FILE": "/tmp/multinexus-agentd.json"},
            clear=False,
        ):
            projection = AgentdHealthProjection.from_environment("test-agent")
        self.assertEqual(projection.path, Path("/tmp/multinexus-agentd.json"))


if __name__ == "__main__":
    unittest.main()
