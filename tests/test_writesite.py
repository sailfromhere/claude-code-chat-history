"""Incremental write_site: unchanged session pages must be skipped (not
re-rendered/rewritten), any relevant change must force a rewrite, deleted
sessions' pages must be pruned, and a missing file must self-heal even when
the fingerprint says 'unchanged'."""
import os
import tempfile
import unittest

from fixtures import cd, make_session, user_msg, assistant_text

OLD = (1000000000, 1000000000)  # backdate marker mtime


def _page(out: str, sid: str, ext: str = "html") -> str:
    return os.path.join(out, "sessions", f"{sid}.{ext}")


def _backdate(out: str, sid: str) -> None:
    for ext in ("html", "md"):
        os.utime(_page(out, sid, ext), OLD)


def _was_rewritten(out: str, sid: str, ext: str = "html") -> bool:
    return os.path.getmtime(_page(out, sid, ext)) != OLD[1]


class TestIncrementalWriteSite(unittest.TestCase):
    def test_unchanged_session_is_skipped_but_index_rewrites(self):
        s = make_session([user_msg("hello"), assistant_text("hi")], session_id="inc1")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            _backdate(out, "inc1")
            idx = os.path.join(out, "index.html")
            os.utime(idx, OLD)
            cd.write_site([s], out)
            self.assertFalse(_was_rewritten(out, "inc1"), "unchanged page rewritten")
            self.assertFalse(_was_rewritten(out, "inc1", "md"))
            self.assertNotEqual(os.path.getmtime(idx), OLD[1], "index must always rewrite")

    def test_title_change_forces_rewrite(self):
        s = make_session([user_msg("hello")], session_id="inc2")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            _backdate(out, "inc2")
            s.title = "A Fresh LLM Title"
            cd.write_site([s], out)
            self.assertTrue(_was_rewritten(out, "inc2"))
            with open(_page(out, "inc2"), encoding="utf-8") as fh:
                self.assertIn("A Fresh LLM Title", fh.read())

    def test_new_entries_force_rewrite(self):
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([make_session([user_msg("hello")], session_id="inc3")], out)
            _backdate(out, "inc3")
            grown = make_session([user_msg("hello"), assistant_text("world")],
                                 session_id="inc3")
            cd.write_site([grown], out)
            self.assertTrue(_was_rewritten(out, "inc3"))

    def test_missing_file_self_heals_despite_matching_fingerprint(self):
        s = make_session([user_msg("hello")], session_id="inc4")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            os.remove(_page(out, "inc4"))
            cd.write_site([s], out)
            self.assertTrue(os.path.isfile(_page(out, "inc4")))

    def test_orphan_pages_of_deleted_sessions_are_pruned(self):
        # A session this generator previously wrote (it's in the manifest) whose
        # jsonl later disappears → its pages are removed on the next run.
        gone = make_session([user_msg("bye")], session_id="gone1")
        kept = make_session([user_msg("hello")], session_id="inc5")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([gone, kept], out)
            self.assertTrue(os.path.isfile(_page(out, "gone1")))
            cd.write_site([kept], out)
            self.assertFalse(os.path.exists(_page(out, "gone1")), "orphan not pruned")
            self.assertFalse(os.path.exists(_page(out, "gone1", "md")))
            self.assertTrue(os.path.isfile(_page(out, "inc5")))

    def test_prune_never_touches_files_it_did_not_create(self):
        # --output-dir can point anywhere; pre-existing .html/.md in sessions/
        # that this generator never wrote (absent from the manifest) must survive.
        s = make_session([user_msg("hello")], session_id="inc7")
        with tempfile.TemporaryDirectory() as out:
            os.makedirs(os.path.join(out, "sessions"))
            for ext in ("html", "md"):
                with open(_page(out, "unrelated-doc", ext), "w") as fh:
                    fh.write("precious user file")
            cd.write_site([s], out)
            cd.write_site([s], out)  # second run: manifest exists now, still no claim
            self.assertTrue(os.path.isfile(_page(out, "unrelated-doc")),
                            "deleted a file the generator never created")
            self.assertTrue(os.path.isfile(_page(out, "unrelated-doc", "md")))

    def test_generator_change_invalidates_everything(self):
        s = make_session([user_msg("hello")], session_id="inc6")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            _backdate(out, "inc6")
            saved = cd._GEN_HASH
            try:
                cd._GEN_HASH = "simulated-new-generator-version"
                cd.write_site([s], out)
            finally:
                cd._GEN_HASH = saved
            self.assertTrue(_was_rewritten(out, "inc6"),
                            "generator change must rewrite all pages")


if __name__ == "__main__":
    unittest.main()
