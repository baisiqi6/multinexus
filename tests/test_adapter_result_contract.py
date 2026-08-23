import unittest

from multinexus.adapters.base import (
    DIAGNOSTIC_MAX_BYTES,
    DIAGNOSTIC_TRUNCATION_MARKER,
    ERROR_PREFIXES,
    FAILURE_CATEGORIES,
    NO_RESPONSE_SENTINEL,
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    OUTCOME_TIMED_OUT,
    AdapterResult,
    bounded_diagnostic,
    failed_result,
    is_error_text,
    safe_exception_diagnostic,
    timed_out_result,
)


class EffectiveOutcomeTests(unittest.TestCase):
    """Priority: explicit outcome > metadata.timeout > ``Claude timeout:``
    text > shared error prefixes > success."""

    def test_explicit_outcome_wins_over_all_legacy_signals(self):
        cases = (
            (
                OUTCOME_SUCCESS,
                {"timeout": True},
                "Claude timeout: 60s",
                OUTCOME_SUCCESS,
            ),
            (
                OUTCOME_FAILED,
                {"timeout": True},
                "Claude timeout: 60s",
                OUTCOME_FAILED,
            ),
            (
                OUTCOME_TIMED_OUT,
                {},
                "Agent error: boom",
                OUTCOME_TIMED_OUT,
            ),
        )
        for outcome, metadata, text, expected in cases:
            with self.subTest(outcome=outcome):
                result = AdapterResult(
                    text=text,
                    metadata=metadata,
                    outcome=outcome,
                    error_category=(
                        "provider_error" if outcome == OUTCOME_FAILED else None
                    ),
                )
                self.assertEqual(result.effective_outcome(), expected)

    def test_legacy_metadata_timeout(self):
        result = AdapterResult(text="everything fine", metadata={"timeout": True})
        self.assertEqual(result.effective_outcome(), OUTCOME_TIMED_OUT)

    def test_legacy_claude_timeout_colon_text(self):
        result = AdapterResult(text="Claude timeout: no reply in 60s")
        self.assertEqual(result.effective_outcome(), OUTCOME_TIMED_OUT)

    def test_claude_timeout_without_colon_is_failed_not_timed_out(self):
        result = AdapterResult(text="Claude timeout after 60s")
        self.assertEqual(result.effective_outcome(), OUTCOME_FAILED)

    def test_legacy_error_prefix_texts(self):
        for text in (
            "Agent error: boom",
            "OpenCode CLI failed: spawn error",
            "Codex stopped responding",
            "omp timed out",
            "  (no response)  ",
        ):
            with self.subTest(text=text):
                result = AdapterResult(text=text)
                self.assertEqual(result.effective_outcome(), OUTCOME_FAILED)

    def test_plain_text_is_success(self):
        result = AdapterResult(text="task completed")
        self.assertEqual(result.effective_outcome(), OUTCOME_SUCCESS)


class ValidationTests(unittest.TestCase):
    """Fail-closed validation of outcome/error_category combinations."""

    def test_invalid_outcome_rejected(self):
        with self.assertRaises(ValueError):
            AdapterResult(text="x", outcome="bogus")

    def test_invalid_category_rejected(self):
        for outcome, category in (
            (OUTCOME_FAILED, "bogus"),
            (OUTCOME_TIMED_OUT, "bogus"),
        ):
            with self.subTest(outcome=outcome, category=category):
                with self.assertRaises(ValueError):
                    AdapterResult(
                        text="x", outcome=outcome, error_category=category
                    )

    def test_category_without_outcome_rejected(self):
        with self.assertRaises(ValueError):
            AdapterResult(text="x", error_category="process_error")

    def test_success_with_category_rejected(self):
        with self.assertRaises(ValueError):
            AdapterResult(
                text="x", outcome=OUTCOME_SUCCESS, error_category="process_error"
            )

    def test_failed_without_category_rejected(self):
        with self.assertRaises(ValueError):
            AdapterResult(text="x", outcome=OUTCOME_FAILED)

    def test_timed_out_with_non_timeout_category_rejected(self):
        with self.assertRaises(ValueError):
            AdapterResult(
                text="x", outcome=OUTCOME_TIMED_OUT, error_category="provider_error"
            )

    def test_every_failure_category_accepted_for_failed(self):
        for category in FAILURE_CATEGORIES:
            with self.subTest(category=category):
                result = AdapterResult(
                    text="x", outcome=OUTCOME_FAILED, error_category=category
                )
                self.assertEqual(result.error_category, category)

    def test_timed_out_auto_fixes_category(self):
        auto = AdapterResult(text="x", outcome=OUTCOME_TIMED_OUT)
        explicit = AdapterResult(
            text="x", outcome=OUTCOME_TIMED_OUT, error_category="timeout"
        )
        self.assertEqual(auto.error_category, "timeout")
        self.assertEqual(explicit.error_category, "timeout")


class BoundedDiagnosticTests(unittest.TestCase):
    """At most 4096 UTF-8 bytes; truncation marker reserved at the tail;
    multi-byte characters never split."""

    def test_fits_unchanged(self):
        self.assertEqual(bounded_diagnostic("a" * DIAGNOSTIC_MAX_BYTES), "a" * 4096)
        self.assertEqual(bounded_diagnostic("中"), "中")

    def test_ascii_over_budget_keeps_marker_within_budget(self):
        out = bounded_diagnostic("a" * 5000)
        self.assertLessEqual(len(out.encode("utf-8")), DIAGNOSTIC_MAX_BYTES)
        self.assertTrue(out.endswith(DIAGNOSTIC_TRUNCATION_MARKER))

    def test_ascii_exactly_one_over_budget_uses_full_budget(self):
        out = bounded_diagnostic("a" * (DIAGNOSTIC_MAX_BYTES + 1))
        self.assertEqual(len(out.encode("utf-8")), DIAGNOSTIC_MAX_BYTES)
        self.assertTrue(out.endswith(DIAGNOSTIC_TRUNCATION_MARKER))

    def test_cjk_never_split_and_always_valid_utf8(self):
        # Each char is 3 UTF-8 bytes; 2000 chars overflow the budget.
        for text in ("中" * 2000, "🚀" * 2000):
            with self.subTest(chars=len(text)):
                out = bounded_diagnostic(text)
                encoded = out.encode("utf-8")
                self.assertLessEqual(len(encoded), DIAGNOSTIC_MAX_BYTES)
                self.assertTrue(out.endswith(DIAGNOSTIC_TRUNCATION_MARKER))
                # No replacement chars, no partial multi-byte sequences.
                self.assertNotIn("\ufffd", out)
                self.assertEqual(out, encoded.decode("utf-8"))

    def test_marker_rejected_when_budget_smaller_than_marker(self):
        out = bounded_diagnostic("中" * 100, max_bytes=10)
        self.assertLessEqual(len(out.encode("utf-8")), 10)
        self.assertNotIn(DIAGNOSTIC_TRUNCATION_MARKER, out)
        self.assertEqual(out, out.encode("utf-8").decode("utf-8"))

    def test_post_init_bounds_diagnostic(self):
        result = AdapterResult(
            text="x",
            outcome=OUTCOME_FAILED,
            error_category="process_error",
            diagnostic="中" * 2000,
        )
        self.assertLessEqual(len(result.diagnostic.encode("utf-8")), 4096)
        self.assertTrue(result.diagnostic.endswith(DIAGNOSTIC_TRUNCATION_MARKER))

    def test_exception_diagnostic_does_not_copy_exception_message(self):
        diagnostic = safe_exception_diagnostic(
            RuntimeError("token=secret-provider-payload")
        )
        self.assertEqual(diagnostic, "RuntimeError")
        self.assertNotIn("secret", diagnostic)


class ErrorTextTests(unittest.TestCase):
    def test_every_error_prefix_matches(self):
        for prefix in ERROR_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertTrue(is_error_text(prefix + " detail"))

    def test_sentinel_matches_exact_trim_only(self):
        self.assertTrue(is_error_text(NO_RESPONSE_SENTINEL))
        self.assertTrue(is_error_text("  (no response)\n"))
        self.assertFalse(is_error_text("(no response) but work continued"))
        self.assertFalse(is_error_text("no response"))

    def test_plain_text_does_not_match(self):
        self.assertFalse(is_error_text("task completed"))
        self.assertFalse(is_error_text(""))


class HelperAndLegacyConstructionTests(unittest.TestCase):
    def test_failed_result(self):
        result = failed_result(
            "provider crashed", category="process_error", session_id="s1"
        )
        self.assertEqual(result.text, "provider crashed")
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "process_error")
        self.assertEqual(result.session_id, "s1")
        self.assertEqual(result.effective_outcome(), OUTCOME_FAILED)

    def test_timed_out_result(self):
        result = timed_out_result("no reply", session_id="s2")
        self.assertEqual(result.text, "no reply")
        self.assertEqual(result.outcome, OUTCOME_TIMED_OUT)
        self.assertEqual(result.error_category, "timeout")
        self.assertEqual(result.effective_outcome(), OUTCOME_TIMED_OUT)

    def test_legacy_construction_still_works(self):
        result = AdapterResult(text="done")
        self.assertIsNone(result.outcome)
        self.assertIsNone(result.error_category)
        self.assertEqual(result.diagnostic, "")
        self.assertEqual(result.effective_outcome(), OUTCOME_SUCCESS)

    def test_helpers_return_same_class(self):
        self.assertIsInstance(failed_result("x", category="timeout"), AdapterResult)
        self.assertIsInstance(timed_out_result("x"), AdapterResult)


if __name__ == "__main__":
    unittest.main()
