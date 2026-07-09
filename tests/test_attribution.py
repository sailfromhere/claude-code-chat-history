"""The documented attribution traps — injected/non-human content must never be
rendered as if the human said it, and human text that *quotes* a tag must stay
visible. These are the regressions that actually bit this renderer.
"""
import unittest

from fixtures import (cd, make_session, all_turn_html, turns_by_role,
                      user_msg, user_blocks, slash_command, expansion, skill_body,
                      bash_io, tool_result_entry, assistant_text, assistant_blocks,
                      text_block, tool_use_block, sdk_submitted_msg, typed_msg,
                      interrupt_entry, compact_summary_entry, image_block)


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

    # --- Esc interrupts (2026-07-09) ---------------------------------------- #
    def test_interrupt_renders_as_marker_not_user_speech(self):
        s = make_session([
            user_msg("do the thing"),
            assistant_text("working on it"),
            interrupt_entry(),
        ])
        users = turns_by_role(s, "user")
        self.assertEqual(len(users), 1, "interrupt wrongly became a user turn")
        self.assertNotIn("Request interrupted", users[0].html)
        self.assertEqual(s.n_user, 1)  # not counted as a prompt
        marker = "\n".join(t.html for t in turns_by_role(s, "command"))
        self.assertIn("Interrupted by user", marker)
        # And the titler must not see it as something the human said.
        self.assertNotIn("Request interrupted", cd._condense_transcript(s))

    def test_interrupt_tool_use_variant(self):
        s = make_session([interrupt_entry(tool_use=True)])
        self.assertEqual(turns_by_role(s, "user"), [])
        marker = "\n".join(t.html for t in turns_by_role(s, "command"))
        self.assertIn("Interrupted by user", marker)
        self.assertIn("tool use", marker)

    def test_human_quoting_interrupt_sentinel_stays_visible(self):
        # Only an EXACT sentinel-only message is an interrupt; prose quoting it isn't.
        s = make_session([user_msg(
            "why did the CLI print [Request interrupted by user] yesterday?")])
        users = turns_by_role(s, "user")
        self.assertEqual(len(users), 1, "human message wrongly suppressed")
        self.assertIn("why did the CLI print", users[0].html)

    def test_interrupt_right_after_command_not_folded_as_expansion(self):
        # Esc immediately after a slash command: the interrupt must not be claimed
        # as the command's "expanded prompt" (same flaw class as the /compact fold).
        s = make_session([slash_command("/foo"), interrupt_entry()])
        chips = "\n".join(t.html for t in turns_by_role(s, "command"))
        self.assertIn("Interrupted by user", chips)
        self.assertNotIn("expanded command prompt", chips)

    # --- compact summaries (2026-07-09) -------------------------------------- #
    def test_compact_summary_renders_as_chip_not_user(self):
        s = make_session([
            user_msg("first real prompt"),
            compact_summary_entry(),
            user_msg("continue please"),
        ])
        users = turns_by_role(s, "user")
        self.assertEqual(len(users), 2, "compact summary wrongly became a user turn")
        for t in users:
            self.assertNotIn("session is being continued", t.html)
        self.assertEqual(s.n_user, 2)
        chip = "\n".join(t.html for t in turns_by_role(s, "command"))
        self.assertIn("compacted", chip)
        self.assertIn("session is being continued", chip)  # body available, collapsed
        self.assertNotIn("session is being continued", cd._condense_transcript(s))

    def test_compact_summary_after_slash_compact_not_folded_as_expansion(self):
        # Real shape of a MANUAL /compact: the summary entry directly follows the
        # <command-name>/compact</command-name> entry, so the expansion-folding rule
        # would claim it as the command's "expanded prompt". It must render as the
        # compacted-chip instead (found on real data 2026-07-09).
        s = make_session([
            slash_command("/compact"),
            compact_summary_entry(),
            user_msg("carry on"),
        ])
        chips = "\n".join(t.html for t in turns_by_role(s, "command"))
        self.assertIn("conversation compacted", chips)
        self.assertNotIn("expanded command prompt", chips)
        self.assertEqual(len(turns_by_role(s, "user")), 1)

    def test_compact_summary_never_becomes_snippet_or_title(self):
        # A post-compact continuation where the summary is the FIRST user entry.
        s = make_session([compact_summary_entry(), user_msg("keep going")])
        self.assertEqual(s.snippet, "keep going")
        self.assertNotIn("continued", s.title)

    # --- pasted images (2026-07-09) ------------------------------------------ #
    def test_pasted_image_renders_inline(self):
        s = make_session([user_blocks([text_block("look at this"), image_block()])])
        users = turns_by_role(s, "user")
        self.assertEqual(len(users), 1)
        self.assertIn("<img", users[0].html)
        self.assertIn("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgFAKE", users[0].html)
        self.assertFalse(users[0].tools_only, "image turn must survive the Tools toggle")

    def test_image_only_prompt_still_renders(self):
        s = make_session([user_blocks([image_block()])])
        users = turns_by_role(s, "user")
        self.assertEqual(len(users), 1, "image-only prompt dropped")
        self.assertIn("<img", users[0].html)
        self.assertFalse(users[0].tools_only)

    def test_malformed_image_block_gets_placeholder_not_crash(self):
        # Untrusted jsonl: non-dict source / non-string data must not abort the
        # whole site generation (every other block handler isinstance-guards).
        for bad in ({"type": "image", "source": "not-a-dict"},
                    {"type": "image", "source": {"type": "base64", "data": 123}},
                    {"type": "image"}):
            s = make_session([user_blocks([bad])])
            html = "\n".join(t.html for t in cd._render_turns(s))
            self.assertNotIn("<img", html)
            self.assertIn("unsupported source", html)

    def test_oversized_image_gets_placeholder_not_embed(self):
        big = image_block(data="A" * (cd.MAX_IMAGE_B64 + 1))
        s = make_session([user_blocks([big])])
        html = turns_by_role(s, "user")[0].html
        self.assertNotIn("<img", html)
        self.assertIn("too large", html)


if __name__ == "__main__":
    unittest.main()
