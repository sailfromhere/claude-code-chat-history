"""load_sessions: multi-file merge, aiTitle resolution, title fallback, snippet."""
import tempfile
import unittest

from fixtures import (cd, write_jsonl, user_msg, slash_command, assistant_text,
                      ai_title_line, bash_io, sdk_submitted_msg, typed_msg,
                      make_session)


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

    def test_malformed_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_jsonl(d, "a.jsonl", [user_msg("valid line")])
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("{ not valid json\n\n")
            sessions = cd.load_sessions(d)
            self.assertEqual(len(sessions), 1)


if __name__ == "__main__":
    unittest.main()
