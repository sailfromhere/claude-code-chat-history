"""load_sessions: multi-file merge, aiTitle resolution, title fallback, snippet."""
import tempfile
import unittest

from fixtures import (cd, write_jsonl, user_msg, slash_command, assistant_text,
                      ai_title_line, agent_name_line, bash_io, sdk_submitted_msg,
                      typed_msg, make_session)


class TestLoadSessions(unittest.TestCase):
    def test_merges_files_sharing_session_id(self):
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "a.jsonl", [user_msg("first half")], session_id="same")
            write_jsonl(d, "b.jsonl", [assistant_text("second half")], session_id="same")
            sessions = cd.load_sessions(d)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(len(sessions[0].entries), 2)

    def test_ai_title_resolves_across_files(self):
        # aiTitle lives in a *different* file of a resumed session.
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "a.jsonl", [user_msg("do the thing")], session_id="s9")
            write_jsonl(d, "b.jsonl", [ai_title_line("s9", "Resumed Session Title")],
                        session_id="s9")
            sessions = cd.load_sessions(d)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].ai_title, "Resumed Session Title")
            self.assertEqual(sessions[0].title, "Resumed Session Title")

    def test_title_falls_back_to_human_snippet(self):
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "a.jsonl", [user_msg("Refactor the parser module")])
            s = cd.load_sessions(d)[0]
            self.assertIn("Refactor the parser", s.title)

    def test_title_falls_back_to_command_then_untitled(self):
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "a.jsonl", [slash_command("/init")])
            s = cd.load_sessions(d)[0]
            self.assertEqual(s.title, "/init")
        with tempfile.TemporaryDirectory() as d:
            # Only a tool-result-style entry; nothing human, no command.
            write_jsonl(d, "a.jsonl", [assistant_text("hi")])
            s = cd.load_sessions(d)[0]
            self.assertEqual(s.title, "(untitled session)")

    def test_snippet_skips_noise_uses_human_text(self):
        # A wrapper-only noise entry (bash I/O) must not become the snippet — the
        # human text does. (A slash command isn't used here: the entry right after
        # one is folded as its expansion by design, which is its own test.)
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "a.jsonl",
                        [bash_io("ls", stdout="file.txt"), user_msg("the real human prompt")])
            s = cd.load_sessions(d)[0]
            self.assertIn("the real human prompt", s.snippet)
            self.assertNotIn("bash-input", s.snippet)
            self.assertNotIn("file.txt", s.snippet)

    def test_project_alias_folds_renamed_path(self):
        # A session whose cwd predates a project rename should report the
        # canonical (post-rename) project name via PROJECT_ALIASES.
        old, new = "/x/proj_old", "/x/proj_new"
        saved = dict(cd.PROJECT_ALIASES)
        cd.PROJECT_ALIASES.clear()
        cd.PROJECT_ALIASES[old] = new
        try:
            s = cd.Session(session_id="s", project_path=old)
            self.assertEqual(s.project_name, "proj_new")
            # Trailing slash on the recorded cwd still matches.
            self.assertEqual(
                cd.Session(session_id="s", project_path=old + "/").project_name,
                "proj_new")
            # An unaliased path is left untouched.
            self.assertEqual(
                cd.Session(session_id="s", project_path="/x/other").project_name,
                "other")
        finally:
            cd.PROJECT_ALIASES.clear()
            cd.PROJECT_ALIASES.update(saved)

    def test_session_flagged_sdk_initiated_when_first_prompt_is_sdk(self):
        s = make_session([
            sdk_submitted_msg("You are transcribing a comic page…"),
            assistant_text("Done."),
        ])
        self.assertTrue(s.initiated_by_sdk)

    def test_session_not_sdk_when_human_typed_first(self):
        s = make_session([typed_msg("do the thing"), assistant_text("ok")])
        self.assertFalse(s.initiated_by_sdk)
        # A bare (no-provenance) human message also must not be flagged.
        self.assertFalse(make_session([user_msg("plain")]).initiated_by_sdk)

    def test_leading_slash_command_means_human_initiated(self):
        # A session opened with a typed slash command is human-initiated, even if a
        # later turn were sdk — the *initiator* is what counts.
        s = make_session([slash_command("/init"), assistant_text("ok")])
        self.assertFalse(s.initiated_by_sdk)

    def test_sdk_pill_appears_in_card_only_for_sdk_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "sdk.jsonl", [sdk_submitted_msg("comic transcribe")],
                        session_id="sdk1")
            write_jsonl(d, "human.jsonl", [typed_msg("hello there")], session_id="hum1")
            sessions = cd.load_sessions(d)
            html = cd._index_page(sessions)
            self.assertIn("⚙ SDK", html)
            self.assertEqual(html.count('class="sdk-pill"'), 1)  # only the sdk session

    def test_agent_name_used_as_title_fallback(self):
        # The harness-assigned session name beats the snippet-truncation fallback
        # (it's the name Claude Code's own UI shows) but never the AI title.
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "a.jsonl",
                        [agent_name_line("s1", "fix-sticky-header-gap"),
                         user_msg("a long human prompt about the sticky header bug")],
                        session_id="s1")
            s = cd.load_sessions(d)[0]
            self.assertEqual(s.title, "fix-sticky-header-gap")
            self.assertIn("long human prompt", s.snippet)  # snippet unaffected

    def test_ai_title_beats_agent_name(self):
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "a.jsonl",
                        [agent_name_line("s1", "kebab-name"),
                         ai_title_line("s1", "Proper AI Title"),
                         user_msg("hello")],
                        session_id="s1")
            s = cd.load_sessions(d)[0]
            self.assertEqual(s.title, "Proper AI Title")

    def test_last_agent_name_wins(self):
        # Sessions get renamed as work progresses — the latest name is current.
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "a.jsonl",
                        [agent_name_line("s1", "old-name"),
                         agent_name_line("s1", "new-name"),
                         user_msg("hi")],
                        session_id="s1")
            self.assertEqual(cd.load_sessions(d)[0].title, "new-name")

    def test_agent_name_last_wins_across_files_by_mtime(self):
        # agent-name lines carry no timestamp, so "last wins" must follow file
        # mtime (the current name lives in the most recently written file), not
        # glob order — a stale rename in an older resumed-session file must lose.
        import os
        import time
        with tempfile.TemporaryDirectory() as d:
            # "a.jsonl" globs first but is NEWER; "z.jsonl" globs last but is older.
            newer = write_jsonl(d, "a.jsonl",
                                [agent_name_line("s1", "current-name"), user_msg("hi")],
                                session_id="s1")
            older = write_jsonl(d, "z.jsonl",
                                [agent_name_line("s1", "stale-name")], session_id="s1")
            now = time.time()
            os.utime(older, (now - 100, now - 100))
            os.utime(newer, (now, now))
            self.assertEqual(cd.load_sessions(d)[0].title, "current-name")

    def test_agent_name_title_is_length_capped(self):
        # agentName comes from untrusted jsonl; as a title it must be truncated
        # like every other title source.
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "a.jsonl",
                        [agent_name_line("s1", "x" * 500), user_msg("hi")],
                        session_id="s1")
            s = cd.load_sessions(d)[0]
            self.assertLessEqual(len(s.title), cd.TITLE_FALLBACK_CHARS)

    def test_agent_name_searchable_even_when_ai_title_shown(self):
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(d, "a.jsonl",
                        [agent_name_line("s1", "kebab-search-target"),
                         ai_title_line("s1", "Shown Title"),
                         user_msg("hello")],
                        session_id="s1")
            html = cd._index_page(cd.load_sessions(d))
            self.assertIn("kebab-search-target", html)

    def test_malformed_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_jsonl(d, "a.jsonl", [user_msg("valid line")])
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("{ not valid json\n\n")
            sessions = cd.load_sessions(d)
            self.assertEqual(len(sessions), 1)


if __name__ == "__main__":
    unittest.main()
