"""Tests for the separately versioned capacity authority projection."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from multinexus.executor_capacity_authority import (
    CapacityAuthorityError,
    CapacityCatalog,
    CapacityPolicy,
    compute_capacity_catalog_hash,
    compute_capacity_policy_id,
    parse_capacity_catalog,
)


class CapacityCatalogTests(TestCase):
    def _write_toml(self, content: str) -> Path:
        path = Path(tempfile.mkdtemp()) / "capacity.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def _valid_base(self) -> str:
        return """\
[capacity_registry]
id = "multinexus.discord.capacity"
version = 1

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 1
"""

    def test_fixture_hash_matches_computed(self):
        fixture_path = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "capacity_catalog_v1.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        expected_hash = hashlib.sha256(
            json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        policies = [CapacityPolicy(agent_id=p["agent_id"], max_concurrent_jobs=p["max_concurrent_jobs"]) for p in fixture["policies"]]
        catalog = CapacityCatalog(
            source_id=fixture["source_id"],
            source_version=fixture["source_version"],
            catalog_hash="",
            policies=tuple(policies),
        )
        self.assertEqual(compute_capacity_catalog_hash(catalog), expected_hash)

    def test_policy_id_matches_cross_repository_fixture(self):
        fixture_path = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "capacity_catalog_v1.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        catalog_hash = hashlib.sha256(
            json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        policy_id = compute_capacity_policy_id(
            agent_id="mac-claude",
            catalog_hash=catalog_hash,
            max_concurrent_jobs=1,
            source_id="multinexus.discord.capacity",
            source_version=1,
        )
        expected = "sha256:2bb3f41503e8eaf997269a2ee950f87a16d56cd2c2966f72c4207ab764355765"
        self.assertEqual(policy_id, expected)

    def test_valid_catalog_parses(self):
        catalog = parse_capacity_catalog(self._write_toml(self._valid_base()))
        self.assertEqual(catalog.source_id, "multinexus.discord.capacity")
        self.assertEqual(catalog.source_version, 1)
        self.assertEqual(len(catalog.policies), 1)
        self.assertEqual(catalog.policies[0].agent_id, "mac-claude")
        self.assertEqual(catalog.policies[0].max_concurrent_jobs, 1)

    def test_missing_registry_rejected(self):
        path = self._write_toml("""\
[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 1
""")
        with self.assertRaisesRegex(CapacityAuthorityError, "missing \\[capacity_registry\\] metadata"):
            parse_capacity_catalog(path)

    def test_unknown_registry_key_rejected(self):
        path = self._write_toml("""\
[capacity_registry]
id = "multinexus.discord.capacity"
version = 1
extra = "bad"

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 1
""")
        with self.assertRaisesRegex(CapacityAuthorityError, "unknown \\[capacity_registry\\] keys"):
            parse_capacity_catalog(path)

    def test_unknown_policy_key_rejected(self):
        path = self._write_toml("""\
[capacity_registry]
id = "multinexus.discord.capacity"
version = 1

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 1
extra = "bad"
""")
        with self.assertRaisesRegex(CapacityAuthorityError, "unknown keys in executor_capacities entry"):
            parse_capacity_catalog(path)

    def test_duplicate_agent_id_rejected(self):
        path = self._write_toml("""\
[capacity_registry]
id = "multinexus.discord.capacity"
version = 1

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 1

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 2
""")
        with self.assertRaisesRegex(CapacityAuthorityError, "duplicate executor_capacity agent_id"):
            parse_capacity_catalog(path)

    def test_max_concurrent_jobs_must_be_integer(self):
        path = self._write_toml("""\
[capacity_registry]
id = "multinexus.discord.capacity"
version = 1

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = true
""")
        with self.assertRaisesRegex(CapacityAuthorityError, "must be an integer"):
            parse_capacity_catalog(path)

    def test_max_concurrent_jobs_out_of_range_rejected(self):
        path = self._write_toml("""\
[capacity_registry]
id = "multinexus.discord.capacity"
version = 1

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 33
""")
        with self.assertRaisesRegex(CapacityAuthorityError, "between 1 and 32"):
            parse_capacity_catalog(path)

    def test_unsafe_agent_id_rejected(self):
        path = self._write_toml("""\
[capacity_registry]
id = "multinexus.discord.capacity"
version = 1

[[executor_capacities]]
agent_id = "mac/claude"
max_concurrent_jobs = 1
""")
        with self.assertRaisesRegex(CapacityAuthorityError, "unsafe characters"):
            parse_capacity_catalog(path)

    def test_version_must_be_non_negative_integer(self):
        path = self._write_toml("""\
[capacity_registry]
id = "multinexus.discord.capacity"
version = -1

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 1
""")
        with self.assertRaisesRegex(CapacityAuthorityError, "non-negative integer"):
            parse_capacity_catalog(path)

    def test_policies_sorted_by_agent_id_in_hash(self):
        path = self._write_toml("""\
[capacity_registry]
id = "multinexus.discord.capacity"
version = 1

[[executor_capacities]]
agent_id = "zebra"
max_concurrent_jobs = 1

[[executor_capacities]]
agent_id = "alpha"
max_concurrent_jobs = 1
""")
        catalog = parse_capacity_catalog(path)
        self.assertEqual(catalog.policies[0].agent_id, "zebra")  # parse order preserved
        canonical = json.dumps({
            "contract_version": 1,
            "source_id": "multinexus.discord.capacity",
            "source_version": 1,
            "policies": [{"agent_id": "alpha", "max_concurrent_jobs": 1}, {"agent_id": "zebra", "max_concurrent_jobs": 1}],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertEqual(catalog.catalog_hash, expected_hash)

    def test_roster_unchanged_after_capacity_roots(self):
        from multinexus.registry_authority import load_authority, canonical_hash, canonical_executor_catalog_hash

        path = self._write_toml("""\
[registry]
id = "multinexus.discord"
version = 2

[[executor_definitions]]
id = "claude-code"
provider = "anthropic-claude"
adapter = "claude"
capabilities = ["coding"]

[[agents]]
id = "mac-claude"
display_name = "Mac Claude"
discord_user_id = "100000000000000001"
executor_definition_id = "claude-code"
runner_profile_id = "mac-claude"

[[external_agents]]
id = "server-hermes"
display_name = "Hermes"
discord_user_id = "100000000000000002"

[capacity_registry]
id = "multinexus.discord.capacity"
version = 1

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 1
""")
        authority_with_capacity = load_authority(path)
        authority_without_capacity = load_authority(self._write_toml("""\
[registry]
id = "multinexus.discord"
version = 2

[[executor_definitions]]
id = "claude-code"
provider = "anthropic-claude"
adapter = "claude"
capabilities = ["coding"]

[[agents]]
id = "mac-claude"
display_name = "Mac Claude"
discord_user_id = "100000000000000001"
executor_definition_id = "claude-code"
runner_profile_id = "mac-claude"

[[external_agents]]
id = "server-hermes"
display_name = "Hermes"
discord_user_id = "100000000000000002"
"""))
        self.assertEqual(authority_with_capacity.source_hash, authority_without_capacity.source_hash)
        self.assertEqual(
            authority_with_capacity.executor_catalog_hash,
            canonical_executor_catalog_hash(
                authority_without_capacity.source_id,
                authority_without_capacity.source_version,
                authority_without_capacity.executor_definitions,
                authority_without_capacity.executor_bindings,
            ),
        )
        self.assertEqual(len(authority_with_capacity.entries), 2)
        self.assertEqual(len(authority_with_capacity.executor_bindings), 1)

    def test_parse_accepts_full_shared_registry(self):
        path = self._write_toml("""\
[registry]
id = "multinexus.discord"
version = 2

[[executor_definitions]]
id = "claude-code"
provider = "anthropic-claude"
adapter = "claude"
capabilities = ["coding"]

[[agents]]
id = "mac-claude"
display_name = "Mac Claude"
discord_user_id = "100000000000000001"
executor_definition_id = "claude-code"
runner_profile_id = "mac-claude"

[[external_agents]]
id = "server-hermes"
display_name = "Hermes"
discord_user_id = "100000000000000002"

[capacity_registry]
id = "multinexus.discord.capacity"
version = 1

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 1
""")
        catalog = parse_capacity_catalog(path)
        self.assertEqual(catalog.source_id, "multinexus.discord.capacity")
        self.assertEqual(catalog.source_version, 1)
        self.assertEqual(len(catalog.policies), 1)
        self.assertEqual(catalog.policies[0].agent_id, "mac-claude")
        self.assertEqual(catalog.policies[0].max_concurrent_jobs, 1)

    def test_parse_rejects_unknown_capacity_root_keys(self):
        path = self._write_toml("""\
[capacity_registry]
id = "multinexus.discord.capacity"
version = 1

[unknown_root]
key = "value"

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 1
""")
        with self.assertRaisesRegex(CapacityAuthorityError, "unknown root keys"):
            parse_capacity_catalog(path)

    def test_parse_rejects_secret_bearing_root_keys(self):
        path = self._write_toml("""\
[capacity_registry]
id = "multinexus.discord.capacity"
version = 1

[secrets]
token = "leaked"

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 1
""")
        with self.assertRaisesRegex(CapacityAuthorityError, "unknown root keys"):
            parse_capacity_catalog(path)

    def test_parse_rejects_whitespace_source_id(self):
        path = self._write_toml("""\
[capacity_registry]
id = "  multinexus.discord.capacity  "
version = 1

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 1
""")
        with self.assertRaisesRegex(CapacityAuthorityError, "must not have surrounding whitespace"):
            parse_capacity_catalog(path)

    def test_source_id_is_not_coerced_from_non_string(self):
        path = self._write_toml("""\
[capacity_registry]
id = 123
version = 1

[[executor_capacities]]
agent_id = "mac-claude"
max_concurrent_jobs = 1
""")
        with self.assertRaisesRegex(CapacityAuthorityError, "must be a string"):
            parse_capacity_catalog(path)


if __name__ == "__main__":
    import unittest

    unittest.main()
