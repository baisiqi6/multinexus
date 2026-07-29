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
        self.assertEqual(text, "[handoff] @AgentName 任务描述")

    def test_missing_space_after_handoff_is_not_split(self):
        response = "[handoff]<@123> 任务描述"
        handoffs, text = split_handoff_lines(response)
        self.assertEqual(handoffs, [])
        self.assertEqual(text, "[handoff]<@123> 任务描述")


if __name__ == "__main__":
    unittest.main()
