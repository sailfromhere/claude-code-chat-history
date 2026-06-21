"""The documented attribution traps — injected/non-human content must never be
rendered as if the human said it, and human text that *quotes* a tag must stay
visible. These are the regressions that actually bit this renderer.
"""
import unittest

from fixtures import (cd, make_session, all_turn_html, turns_by_role,
                      user_msg, user_blocks, slash_command, expansion, skill_body,
                      bash_io, tool_result_entry, assistant_text, assistant_blocks,
                      text_block, tool_use_block, sdk_submitted_msg, typed_msg)


class TestAttribution(unittest.TestCase):
    def test_slash_command_expansion_folds_under_command(self):
        s = make_session([
            slash_command("/init"),
            expansion("You are initializing a new CLAUDE.md. Analyze the codebase…"),
            assistant_text("Done."),
        ])
        # The expansion must not appear as a human ("You") turn.
        self.assertEqual(turns_by_role(s, "user"), [])
        cmd = turns_by_role(s, "command")
        self.assertTrue(cmd, "expected a command turn")
        joined = "\n".join(t.html for t in cmd)
        self.assertIn("⌘ /init", joined)
        # The boilerplate is folded inside the command's expansion, not loose text.
        self.assertIn("initializing a new CLAUDE.md", joined)

    def test_tool_result_nests_under_call_not_a_user_turn(self):
        s = make_session([
            user_msg("read the file"),
            assistant_blocks([tool_use_block("Read", {"file_path": "/x"}, "t1")]),
            tool_result_entry("t1", "FILE_CONTENTS_HERE"),
        ])
        # Exactly one human turn (the request); the result is not its own user turn.
        users = turns_by_role(s, "user")
        self.assertEqual(len(users), 1)
        self.assertNotIn("FILE_CONTENTS_HERE", users[0].html)
        # The result renders under Claude's tool_use.
        asst = "\n".join(t.html for t in turns_by_role(s, "assistant"))
        self.assertIn("FILE_CONTENTS_HERE", asst)

    def test_human_quoting_a_tag_stays_visible(self):
        # A bug report that pastes a tag alongside prose must NOT be hidden.
        text = "here's a bug: <bash-input>ls</bash-input> leaked into my message"
        s = make_session([user_msg(text)])
        users = turns_by_role(s, "user")
        self.assertEqual(len(users), 1, "human message wrongly suppressed")
        self.assertIn("here&#x27;s a bug", users[0].html)
        self.assertIn("leaked into my message", users[0].html)

    def test_skill_body_renders_as_chip_not_prose(self):
        s = make_session([skill_body("myskill")])
        self.assertEqual(turns_by_role(s, "user"), [])
        cmd = turns_by_role(s, "command")
        self.assertEqual(len(cmd), 1)
        self.assertIn("loaded skill", cmd[0].html)
        self.assertIn("myskill", cmd[0].html)
        # The example tag inside the skill body must not become its own sys chip.
        self.assertNotIn('class="sys-event"><summary><span class="sys-icon">⚙</span> search_first',
                         cmd[0].html)

    def test_no_response_requested_sentinel_filtered(self):
        s = make_session([
            user_msg("hello"),
            assistant_text("No response requested."),
        ])
        self.assertEqual(turns_by_role(s, "assistant"), [])
        self.assertEqual(s.n_asst, 0)
        self.assertNotIn("No response requested.", all_turn_html(s))

    def test_ansi_codes_stripped(self):
        s = make_session([user_msg("\x1b[1mBold\x1b[22m and plain")])
        html = turns_by_role(s, "user")[0].html
        self.assertNotIn("\x1b", html)
        self.assertNotIn("[1m", html)
        self.assertIn("Bold", html)
        self.assertIn("and plain", html)

    def test_bash_output_not_double_encoded(self):
        # Source stdout is already entity-encoded ("a &gt; b"); the renderer must
        # unescape then re-escape exactly once — not produce "&amp;gt;".
        s = make_session([bash_io("echo a", stdout="a &gt; b")])
        html = "\n".join(t.html for t in turns_by_role(s, "command"))
        self.assertIn("a &gt; b", html)
        self.assertNotIn("&amp;gt;", html)

    def test_orphan_tool_result_renders_as_neutral_block_not_prose(self):
        # A tool_result with no matching tool_use renders as a neutral, tools-only
        # block (the "↳ tool result" row) — never as human prose. (It still lands in
        # a user-role Turn, but tools_only=True hides it when tools are off, and the
        # content is a tool block, not a paragraph the human "said".)
        s = make_session([tool_result_entry("missing", "ORPHANED")])
        user_turns = turns_by_role(s, "user")
        self.assertEqual(len(user_turns), 1)
        t = user_turns[0]
        self.assertTrue(t.tools_only)
        self.assertIn("↳ tool result", t.html)
        self.assertIn('<pre class="code">ORPHANED</pre>', t.html)
        self.assertNotIn("<p>ORPHANED</p>", t.html)


    def test_sdk_submitted_prompt_flagged_not_attributed_to_human(self):
        # A prompt a tool sent via headless `claude -p` must not read as "You".
        s = make_session([
            sdk_submitted_msg("You are transcribing one page of a comic…"),
            assistant_text("Here is the transcription."),
        ])
        users = turns_by_role(s, "user")
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0].submitted, "sdk")
        page = cd._session_page(s)
        self.assertIn("Claude (SDK)", page)
        self.assertIn("⚙ submitted", page)
        self.assertNotIn(">You<", page)  # the human label must not appear for this turn

    def test_entrypoint_only_marks_sdk_on_older_cli(self):
        # Older CLIs (≤2.1.159) omit promptSource; entrypoint:"sdk-cli" must suffice.
        s = make_session([sdk_submitted_msg("tool-sent prompt", source="entrypoint")])
        self.assertEqual(turns_by_role(s, "user")[0].submitted, "sdk")

    def test_typed_prompt_is_not_flagged(self):
        s = make_session([typed_msg("how do I do X?"), assistant_text("Like this.")])
        users = turns_by_role(s, "user")
        self.assertEqual(users[0].submitted, "")
        page = cd._session_page(s)
        self.assertNotIn("⚙ submitted", page)
        self.assertNotIn("Claude (SDK)", page)

    def test_plain_user_msg_without_provenance_fields_not_flagged(self):
        # Bare fixtures (no entrypoint/promptSource) must default to human, unflagged.
        s = make_session([user_msg("plain message")])
        self.assertEqual(turns_by_role(s, "user")[0].submitted, "")


if __name__ == "__main__":
    unittest.main()
