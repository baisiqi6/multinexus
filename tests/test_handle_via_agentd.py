"""Tests for the bridge-side display-text extraction from a coord-runtime
terminal-job dict.

Background: the bridge submits a request via coord's
\`runtime request submit\` CLI, polls for the job, and posts the
adapter's reply back to the originating Discord channel. The shape
of the polled job dict has changed over time:

* current \`coord job list\` (used by \`wait_for_job_result\`) deserializes
  the SQLite row's \`result_json\` column into a \`result\` dict
* older \`coord job get\` / direct-SQL callers exposed the raw
  JSON string as \`result_json\`
* an empty / error path returns \`{"status": "done", "result": null}\`
  or \`{"status": "failed"}\` with no \`result\`

In all cases the bridge must produce a meaningful display string
rather than the placeholder \`"Job done"\`. These tests pin the
extraction contract for \`DiscordClient._extract_completed_display_text\`.
"""

import unittest

from multinexus.client import DiscordClient


class ExtractCompletedDisplayTextTests(unittest.TestCase):
    def test_prefers_result_response_text(self):
        """The new coord CLI shape: result is a deserialized dict."""
        completed = {
            "id": "request:abc",
            "status": "done",
            "result": {
                "response_text": "Hello from Claude",
                "session_id": "sess-1",
                "duration_ms": 12345,
            },
        }
        self.assertEqual(
            DiscordClient._extract_completed_display_text(completed),
            "Hello from Claude",
        )

    def test_falls_back_to_result_text_field(self):
        completed = {
            "status": "done",
            "result": {"text": "alt text field"},
        }
        self.assertEqual(
            DiscordClient._extract_completed_display_text(completed),
            "alt text field",
        )

    def test_accepts_legacy_result_json_string(self):
        """The older coord CLI shape: result_json is a JSON string."""
        completed = {
            "status": "done",
            "result_json": '{"response_text": "legacy reply"}',
        }
        self.assertEqual(
            DiscordClient._extract_completed_display_text(completed),
            "legacy reply",
        )

    def test_legacy_result_json_text_fallback(self):
        completed = {
            "status": "done",
            "result_json": '{"text": "legacy alt text"}',
        }
        self.assertEqual(
            DiscordClient._extract_completed_display_text(completed),
            "legacy alt text",
        )

    def test_result_wins_over_result_json(self):
        """If both shapes are present (drift between coord versions), prefer
        the deserialized dict; it is the one produced by current coord."""
        completed = {
            "status": "done",
            "result": {"response_text": "current shape"},
            "result_json": '{"response_text": "older shape"}',
        }
        self.assertEqual(
            DiscordClient._extract_completed_display_text(completed),
            "current shape",
        )

    def test_empty_response_text_falls_back_to_text(self):
        completed = {
            "status": "done",
            "result": {"response_text": "", "text": "non-empty alt"},
        }
        self.assertEqual(
            DiscordClient._extract_completed_display_text(completed),
            "non-empty alt",
        )

    def test_both_empty_returns_placeholder(self):
        completed = {
            "status": "done",
            "result": {"response_text": "", "text": ""},
        }
        self.assertEqual(
            DiscordClient._extract_completed_display_text(completed),
            "(empty response)",
        )

    def test_missing_result_returns_job_done_fallback(self):
        """No result payload at all → fall back to 'Job <status>'.
        This is the path that previously produced 'Job done' for every
        real reply, and is the regression this commit guards against
        for the specific shape mismatch; the placeholder is still
        correct when the payload really is empty."""
        completed = {"status": "done"}
        self.assertEqual(
            DiscordClient._extract_completed_display_text(completed),
            "Job done",
        )

    def test_missing_result_failed_returns_job_failed(self):
        completed = {"status": "failed"}
        self.assertEqual(
            DiscordClient._extract_completed_display_text(completed),
            "Job failed",
        )

    def test_invalid_legacy_json_string_falls_back_to_placeholder(self):
        completed = {
            "status": "done",
            "result_json": "not-json",
        }
        # json.loads raises, we fall back to the Job <status> placeholder
        self.assertEqual(
            DiscordClient._extract_completed_display_text(completed),
            "Job done",
        )


if __name__ == "__main__":
    unittest.main()
