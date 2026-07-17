"""Tool-chip summaries, plan-card outcome badges, model-switch dividers,
and turn durations."""
import tempfile
import unittest

from fixtures import (cd, make_session, all_turn_html, user_msg, assistant_text,
                      assistant_blocks, tool_use_block, tool_result_entry,
                      turn_duration_line, write_jsonl)

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


class TestModelDivider(unittest.TestCase):
    """User choice 2026-07-09 (mockup C): a divider row when the model switches
    mid-session; turns themselves stay clean. No divider for the starting model."""

    def test_divider_on_model_switch(self):
        s = make_session([
            user_msg("do it"),
            assistant_text("on opus", model="claude-opus-4-8"),
            assistant_text("now on sonnet", model="claude-sonnet-5"),
        ])
        html = all_turn_html(s)
        self.assertEqual(html.count("model-divider"), 1)
        self.assertIn("model → sonnet-5", html)   # trimmed, no "claude-" prefix

    def test_no_divider_without_switch(self):
        s = make_session([
            user_msg("do it"),
            assistant_text("a", model="claude-opus-4-8"),
            assistant_text("b", model="claude-opus-4-8"),
        ])
        self.assertNotIn("model-divider", all_turn_html(s))

    def test_divider_waits_for_a_visible_turn(self):
        # A model switch on an INVISIBLE assistant entry (e.g. empty thinking
        # block) must not strand a divider next to nothing — it attaches to the
        # next visible assistant turn instead.
        s = make_session([
            user_msg("go"),
            assistant_text("on opus", model="claude-opus-4-8"),
            assistant_blocks([{"type": "thinking", "thinking": "", "signature": "x"}],
                             model="claude-sonnet-5"),
        ])
        self.assertNotIn("model-divider", all_turn_html(s),
                         "divider emitted with no visible turn after it")
        s2 = make_session([
            user_msg("go"),
            assistant_text("on opus", model="claude-opus-4-8"),
            assistant_blocks([{"type": "thinking", "thinking": "", "signature": "x"}],
                             model="claude-sonnet-5"),
            assistant_text("visible reply", model="claude-sonnet-5"),
        ])
        html = all_turn_html(s2)
        self.assertEqual(html.count("model-divider"), 1)
        self.assertIn("model → sonnet-5", html)

    def test_synthetic_and_missing_models_do_not_divide(self):
        s = make_session([
            user_msg("do it"),
            assistant_text("a", model="claude-opus-4-8"),
            assistant_text("placeholder", model="<synthetic>"),
            assistant_text("b"),  # no model field at all
            assistant_text("c", model="claude-opus-4-8"),
        ])
        self.assertNotIn("model-divider", all_turn_html(s))


class TestTurnDuration(unittest.TestCase):
    """User choice 2026-07-09 (mockup D, every turn): durations from
    system/turn_duration lines, attached via parentUuid to the assistant turn."""

    def test_duration_attaches_to_parent_assistant_turn(self):
        asst = assistant_text("done after a long think")
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "a.jsonl", [
                user_msg("go"),
                asst,
                turn_duration_line(asst["uuid"], 308628),  # 5m 9s
            ])
            s = cd.load_sessions(d)[0]
            page = cd._session_page(s)
            self.assertIn('class="dur"', page)
            self.assertIn("5m 9s", page)

    def test_no_duration_lines_means_no_dur_spans(self):
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "a.jsonl", [user_msg("go"), assistant_text("quick")])
            page = cd._session_page(cd.load_sessions(d)[0])
            self.assertNotIn('class="dur"', page)

    def test_fmt_dur(self):
        self.assertEqual(cd._fmt_dur(500), "<1s")
        self.assertEqual(cd._fmt_dur(45_000), "45s")
        self.assertEqual(cd._fmt_dur(161_000), "2m 41s")
        self.assertEqual(cd._fmt_dur(3_900_000), "1h 5m")
        # Untrusted jsonl values must never raise (json.loads accepts Infinity/NaN).
        for bad in ("garbage", None, float("inf"), float("nan"), [1]):
            self.assertEqual(cd._fmt_dur(bad), "")

    def test_non_string_model_never_divides_or_crashes(self):
        s = make_session([
            user_msg("go"),
            assistant_text("a", model=123),
            assistant_text("b", model="claude-opus-4-8"),
        ])
        self.assertNotIn("model-divider", all_turn_html(s))


def askq_call(questions: list[dict], tid: str = "q1") -> dict:
    return tool_use_block("AskUserQuestion", {"questions": questions}, tid)


class TestAskUserQuestion(unittest.TestCase):
    """The AskUserQuestion card must reflect the real answer, which lives in the entry's
    top-level toolUseResult (answers/annotations keyed by question text) — not the tool_result
    string, which the old regex-only renderer scraped. Real-data shapes verified 2026-07-14/15
    (custom-typed "Other" answers and notes are absent from the string entirely)."""

    QUESTION = {
        "question": "Which approach?",
        "header": "Approach",
        "multiSelect": False,
        "options": [
            {"label": "Option A", "description": "the first way"},
            {"label": "Option B", "description": "the second way"},
        ],
    }

    def test_picked_option_marked_chosen(self):
        s = make_session([
            user_msg("go"),
            assistant_blocks([askq_call([self.QUESTION])]),
            tool_result_entry("q1", 'Your questions have been answered: "Which approach?"="Option A"',
                              tool_use_result={"questions": [self.QUESTION],
                                              "answers": {"Which approach?": "Option A"},
                                              "annotations": {}}),
        ])
        html = all_turn_html(s)
        self.assertIn('askq-opt chosen', html)
        self.assertIn("Option A", html)
        self.assertNotIn("typed answer", html)

    def test_custom_typed_answer_shown_not_dropped(self):
        typed = "do neither, use a hybrid of both instead"
        s = make_session([
            user_msg("go"),
            assistant_blocks([askq_call([self.QUESTION])]),
            tool_result_entry("q1", f'Your questions have been answered: "Which approach?"="{typed}"',
                              tool_use_result={"questions": [self.QUESTION],
                                              "answers": {"Which approach?": typed},
                                              "annotations": {}}),
        ])
        html = all_turn_html(s)
        self.assertIn(typed, html)
        self.assertIn("typed answer", html)
        self.assertNotIn('askq-opt chosen', html)  # neither listed option was picked

    def test_note_rendered(self):
        note = "also, why did this only break after the merge yesterday?"
        s = make_session([
            user_msg("go"),
            assistant_blocks([askq_call([self.QUESTION])]),
            tool_result_entry("q1", 'Your questions have been answered: "Which approach?"="Option A"',
                              tool_use_result={"questions": [self.QUESTION],
                                              "answers": {"Which approach?": "Option A"},
                                              "annotations": {"Which approach?": {"notes": note}}}),
        ])
        html = all_turn_html(s)
        self.assertIn("askq-note", html)
        self.assertIn(note, html)

    def test_multiselect_marks_every_chosen_label(self):
        q = {**self.QUESTION, "multiSelect": True,
            "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"},
                       {"label": "C", "description": "c"}]}
        s = make_session([
            user_msg("go"),
            assistant_blocks([askq_call([q])]),
            tool_result_entry("q1", 'Your questions have been answered: "Which approach?"="A, C"',
                              tool_use_result={"questions": [q],
                                              "answers": {"Which approach?": "A, C"},
                                              "annotations": {}}),
        ])
        html = all_turn_html(s)
        self.assertEqual(html.count("askq-opt chosen"), 2)

    def test_timeout_shows_no_answer_status(self):
        s = make_session([
            user_msg("go"),
            assistant_blocks([askq_call([self.QUESTION])]),
            tool_result_entry("q1",
                              "No response after 60s — the user may be away from keyboard.",
                              tool_use_result={"questions": [self.QUESTION],
                                              "answers": {}, "annotations": {},
                                              "afkTimeoutMs": 60000}),
        ])
        html = all_turn_html(s)
        self.assertIn("no answer", html)
        self.assertNotIn("askq-opt chosen", html)

    def test_rejected_clarify_shows_status_not_string_tooluseresult(self):
        # A rejected/clarify call's toolUseResult is a plain string, not the answers dict —
        # must not crash and must not be mistaken for a structured answer.
        s = make_session([
            user_msg("go"),
            assistant_blocks([askq_call([self.QUESTION])]),
            tool_result_entry("q1", "The user doesn't want to proceed with this tool use.",
                              is_error=True,
                              tool_use_result="Error: The user doesn't want to proceed..."),
        ])
        html = all_turn_html(s)
        self.assertIn("sent back to clarify", html)

    def test_legacy_capture_without_structured_result_still_marks_chosen(self):
        # Older captures (or synthetic data) with no top-level toolUseResult at all — falls
        # back to the old string-scrape so this doesn't regress.
        s = make_session([
            user_msg("go"),
            assistant_blocks([askq_call([self.QUESTION])]),
            tool_result_entry("q1", 'Your questions have been answered: "Which approach?"="Option B"'),
        ])
        html = all_turn_html(s)
        self.assertIn('askq-opt chosen', html)
        self.assertIn("Option B", html)

    def test_markdown_export_includes_typed_answer_and_note(self):
        typed = "a hybrid approach"
        note = "leave the door open for later"
        s = make_session([
            user_msg("go"),
            assistant_blocks([askq_call([self.QUESTION])]),
            tool_result_entry("q1", f'Your questions have been answered: "Which approach?"="{typed}"',
                              tool_use_result={"questions": [self.QUESTION],
                                              "answers": {"Which approach?": typed},
                                              "annotations": {"Which approach?": {"notes": note}}}),
        ])
        md = cd._session_markdown(s)
        self.assertIn(typed, md)
        self.assertIn(note, md)

    def test_markdown_export_marks_chosen_for_legacy_capture(self):
        # A legacy capture (no toolUseResult) must mark the chosen option in markdown too,
        # not just HTML — this regressed before the two render paths shared one extractor.
        s = make_session([
            user_msg("go"),
            assistant_blocks([askq_call([self.QUESTION])]),
            tool_result_entry("q1", 'Your questions have been answered: "Which approach?"="Option B"'),
        ])
        md = cd._session_markdown(s)
        self.assertIn("✓ Option B", md)

    def test_multiselect_label_containing_comma_not_split(self):
        # A label that itself contains a comma must not be mis-parsed into leftover
        # "typed" text when peeled out of the comma-joined multi-select answer.
        q = {**self.QUESTION, "multiSelect": True,
            "options": [{"label": "Rename, then delete", "description": "a"},
                       {"label": "Keep as-is", "description": "b"}]}
        s = make_session([
            user_msg("go"),
            assistant_blocks([askq_call([q])]),
            tool_result_entry("q1", 'answered',
                              tool_use_result={"questions": [q],
                                              "answers": {"Which approach?":
                                                          "Rename, then delete, Keep as-is"},
                                              "annotations": {}}),
        ])
        html = all_turn_html(s)
        self.assertEqual(html.count("askq-opt chosen"), 2)
        self.assertNotIn("typed answer", html)

    def test_malformed_tool_use_result_never_crashes(self):
        # toolUseResult is harness-controlled JSONL — malformed/adversarial shapes must
        # degrade gracefully, not raise and take the whole dashboard build down.
        bad_shapes = [
            {"questions": [self.QUESTION], "answers": {}, "annotations": {"Which approach?": "not a dict"}},
            {"questions": [self.QUESTION], "answers": ["not", "a", "dict"], "annotations": {}},
            {"questions": [self.QUESTION], "answers": {"Which approach?": ["A", "B"]}, "annotations": {}},
            {"questions": [self.QUESTION], "answers": {"Which approach?": 7}, "annotations": {}},
            {"questions": ["not a dict"], "answers": {}, "annotations": {}},
            ["not", "a", "dict"],
            42,
        ]
        for tur in bad_shapes:
            with self.subTest(tur=tur):
                s = make_session([
                    user_msg("go"),
                    assistant_blocks([askq_call([self.QUESTION])]),
                    tool_result_entry("q1", "some result text", tool_use_result=tur),
                ])
                all_turn_html(s)          # must not raise
                cd._session_markdown(s)   # must not raise

    def test_javascript_link_in_typed_answer_neutralized(self):
        # Untrusted answer/note text can contain markdown-link syntax; a javascript: href
        # must not become a clickable anchor in the rendered card.
        s = make_session([
            user_msg("go"),
            assistant_blocks([askq_call([self.QUESTION])]),
            tool_result_entry("q1", "result",
                              tool_use_result={"questions": [self.QUESTION],
                                              "answers": {"Which approach?":
                                                          "[click](javascript:alert(1))"},
                                              "annotations": {}}),
        ])
        html = all_turn_html(s)
        self.assertNotIn('href="javascript:', html)
        self.assertIn("click", html)


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
