"""Tool-chip summaries and plan-card outcome badges."""
import unittest

from fixtures import (cd, make_session, all_turn_html, user_msg,
                      assistant_blocks, tool_use_block, tool_result_entry)

PLAN_PATH = cd._PLANS_DIR + "/2026-07-09-example.md"


def plan_write(tid: str = "w1") -> dict:
    return tool_use_block("Write", {"file_path": PLAN_PATH,
                                    "content": "# Plan\n- step one"}, tid)


class TestPlanOutcome(unittest.TestCase):
    """The plan card should show whether the user approved or rejected the plan
    (the outcome lives on the ExitPlanMode call's tool_result, verified shapes
    2026-07-09: 96× "User has approved your plan…", 3× is_error rejection)."""

    def test_approved_badge_on_plan_card(self):
        s = make_session([
            user_msg("plan this"),
            assistant_blocks([plan_write(), tool_use_block("ExitPlanMode", {}, "e1")]),
            tool_result_entry("e1", "User has approved your plan. You can now start coding."),
        ])
        html = all_turn_html(s)
        self.assertIn("plan-card", html)
        self.assertIn("approved", html)
        self.assertNotIn("rejected", html)

    def test_rejected_badge_on_plan_card(self):
        s = make_session([
            user_msg("plan this"),
            assistant_blocks([plan_write(), tool_use_block("ExitPlanMode", {}, "e1")]),
            tool_result_entry("e1", "The user doesn't want to proceed with this tool use.",
                              is_error=True),
        ])
        self.assertIn("rejected", all_turn_html(s))

    def test_no_exitplanmode_means_no_badge(self):
        s = make_session([user_msg("plan this"), assistant_blocks([plan_write()])])
        html = all_turn_html(s)
        self.assertIn("plan-card", html)
        self.assertNotIn("plan-outcome", html)

    def test_epm_in_later_turn_still_pairs(self):
        # Write and ExitPlanMode are usually the same turn but can be adjacent ones.
        s = make_session([
            user_msg("plan this"),
            assistant_blocks([plan_write()]),
            assistant_blocks([tool_use_block("ExitPlanMode", {}, "e1")]),
            tool_result_entry("e1", "User has approved your plan."),
        ])
        self.assertIn("approved", all_turn_html(s))

    def test_two_plans_pair_with_their_own_epm(self):
        # Plan 1 rejected, plan 2 approved — badges must not cross-pair.
        s = make_session([
            user_msg("plan this"),
            assistant_blocks([plan_write("w1"), tool_use_block("ExitPlanMode", {}, "e1")]),
            tool_result_entry("e1", "The user doesn't want to proceed.", is_error=True),
            assistant_blocks([plan_write("w2"), tool_use_block("ExitPlanMode", {}, "e2")]),
            tool_result_entry("e2", "User has approved your plan."),
        ])
        html = all_turn_html(s)
        self.assertIn("rejected", html)
        self.assertIn("approved", html)
        # The first card must carry the rejection, the second the approval.
        self.assertLess(html.index("rejected"), html.index("approved"))


class TestToolSummary(unittest.TestCase):
    """Collapsed-chip one-liners for newer tools (shapes verified 2026-07-09)."""

    def test_skill_shows_skill_name(self):
        self.assertIn("claude-api",
                      cd._tool_summary("Skill", {"skill": "claude-api", "args": "pricing"}))

    def test_agent_prefers_description_over_prompt(self):
        out = cd._tool_summary("Agent", {
            "description": "Explore plan mode rendering",
            "subagent_type": "Explore",
            "prompt": "A very long agent brief that should not be the summary…",
        })
        self.assertIn("Explore plan mode rendering", out)
        self.assertNotIn("very long agent brief", out)

    def test_taskcreate_shows_subject(self):
        out = cd._tool_summary("TaskCreate", {
            "subject": "Fix time cell", "description": "long details…"})
        self.assertIn("Fix time cell", out)
        self.assertNotIn("long details", out)

    def test_taskupdate_shows_id_and_status(self):
        out = cd._tool_summary("TaskUpdate", {"taskId": "3", "status": "completed"})
        self.assertIn("3", out)
        self.assertIn("completed", out)

    def test_bash_still_shows_command_not_description(self):
        out = cd._tool_summary("Bash", {"command": "ls -la", "description": "List files"})
        self.assertEqual(out, "ls -la")


if __name__ == "__main__":
    unittest.main()
