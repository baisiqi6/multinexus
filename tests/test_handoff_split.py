import unittest

from multinexus.handoff import split_handoff_lines


class TestSplitHandoffLines(unittest.TestCase):
    def test_plain_text_no_handoff(self):
        handoffs, text = split_handoff_lines("Hello, this is a normal response.")
        self.assertEqual(handoffs, [])
        self.assertEqual(text, "Hello, this is a normal response.")

    def test_single_handoff(self):
        response = "Done with task.\n[handoff] <@12345> please review"
        handoffs, text = split_handoff_lines(response)
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0], "[handoff] <@12345> please review")
        self.assertEqual(text, "Done with task.")

    def test_multiple_handoffs(self):
        response = (
            "Results:\n"
            "[handoff] <@111> task A\n"
            "[handoff] <@222> task B\n"
            "That's all."
        )
        handoffs, text = split_handoff_lines(response)
        self.assertEqual(len(handoffs), 2)
        self.assertEqual(handoffs[0], "[handoff] <@111> task A")
        self.assertEqual(handoffs[1], "[handoff] <@222> task B")
        self.assertIn("Results:", text)
        self.assertIn("That's all.", text)
        self.assertNotIn("[handoff]", text)

    def test_handoff_with_leading_spaces_stripped(self):
        response = "  [handoff] <@123> do something"
        handoffs, text = split_handoff_lines(response)
        self.assertEqual(handoffs[0], "[handoff] <@123> do something")
        self.assertFalse(handoffs[0].startswith(" "))

    def test_only_handoff_no_display_text(self):
        response = "[handoff] <@123> go"
        handoffs, text = split_handoff_lines(response)
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(text, "")

    def test_long_handoff_truncated(self):
        long_task = "x" * 3000
        response = f"[handoff] <@123> {long_task}"
        handoffs, _ = split_handoff_lines(response)
        self.assertLessEqual(len(handoffs[0]), 1900)

    def test_display_text_no_handoff_content(self):
        response = (
            "Analysis complete.\n"
            "[handoff] <@999> continue\n"
            "Summary: all good."
        )
        handoffs, text = split_handoff_lines(response)
        self.assertNotIn("[handoff]", text)
        self.assertIn("Analysis complete.", text)
        self.assertIn("Summary: all good.", text)

    def test_handoff_example_inside_code_fence_is_not_split(self):
        response = (
            "转交格式:\n"
            "```text\n"
            "[handoff] <@123> 任务描述\n"
            "```"
        )
        handoffs, text = split_handoff_lines(response)
        self.assertEqual(handoffs, [])
        self.assertIn("[handoff] <@123> 任务描述", text)

    def test_text_mention_handoff_example_is_not_split(self):
        response = "[handoff] @AgentName 任务描述"
        handoffs, text = split_handoff_lines(response)
        self.assertEqual(handoffs, [])
        self.assertIn(response, text)
        self.assertIn("handoff 未发送", text)

    def test_missing_space_after_handoff_is_not_split(self):
        response = "[handoff]<@123> 任务描述"
        handoffs, text = split_handoff_lines(response)
        self.assertEqual(handoffs, [])
        self.assertIn(response, text)
        self.assertIn("handoff 未发送", text)

    def test_multiple_malformed_candidates_emit_one_diagnostic(self):
        response = (
            "[handoff] @Unknown first\n"
            "[handoff]<@123> second"
        )
        handoffs, text = split_handoff_lines(response)
        self.assertEqual(handoffs, [])
        self.assertIn("[handoff] @Unknown first", text)
        self.assertIn("[handoff]<@123> second", text)
        self.assertEqual(text.count("handoff 未发送"), 1)

    def test_mixed_valid_and_malformed_candidates(self):
        response = (
            "[handoff] <@123> valid\n"
            "[handoff] @Unknown malformed"
        )
        handoffs, text = split_handoff_lines(response)
        self.assertEqual(handoffs, ["[handoff] <@123> valid"])
        self.assertNotIn("[handoff] <@123> valid", text)
        self.assertIn("[handoff] @Unknown malformed", text)
        self.assertIn("handoff 未发送", text)

    def test_markdown_wrapped_handoff_example_is_plain_text(self):
        response = "> [handoff] @AgentName 任务描述"
        handoffs, text = split_handoff_lines(response)
        self.assertEqual(handoffs, [])
        self.assertEqual(text, response)

    def test_repeated_valid_candidates_preserve_existing_behavior(self):
        response = (
            "[handoff] <@123> same\n"
            "[handoff] <@123> same"
        )
        handoffs, text = split_handoff_lines(response)
        self.assertEqual(
            handoffs,
            ["[handoff] <@123> same", "[handoff] <@123> same"],
        )
        self.assertEqual(text, "")

    def test_diagnostic_can_be_deferred_until_alias_resolution(self):
        response = "[handoff] @KnownAgent 任务描述"
        handoffs, text = split_handoff_lines(response, emit_diagnostic=False)
        self.assertEqual(handoffs, [])
        self.assertEqual(text, response)


if __name__ == "__main__":
    unittest.main()
