"""Bulk selection (_select_by_age/_select_by_project) and the destructive
--empty-trash path (_empty_trash) — the only code that permanently removes a
trashed session's pages and its manifest/archive-cards bookkeeping rows."""
import os
import tempfile
import unittest

from fixtures import cd, make_session, user_msg, now_offset_iso


def _page(out: str, sid: str, ext: str = "html") -> str:
    return os.path.join(out, "sessions", f"{sid}.{ext}")


def _aged_session(session_id: str, days: float, **kw) -> "cd.Session":
    s = make_session([user_msg("hi")], session_id=session_id, **kw)
    s.last_ts = now_offset_iso(days=days)
    return s


class TestSelectByAge(unittest.TestCase):
    def test_selects_only_sessions_older_than_threshold(self):
        old = _aged_session("old1", 100)
        recent = _aged_session("recent1", 5)
        sids = cd._select_by_age([old, recent], 90)
        self.assertEqual(sids, ["old1"])

    def test_boundary_is_inclusive(self):
        exactly = _aged_session("exact1", 90)
        sids = cd._select_by_age([exactly], 90)
        self.assertEqual(sids, ["exact1"])

    def test_unparseable_last_ts_is_never_selected(self):
        s = _aged_session("bad1", 200)
        s.last_ts = ""  # simulate a session with no usable timestamp
        sids = cd._select_by_age([s], 1)
        self.assertEqual(sids, [], "a card whose age can't be confirmed must not be selected")


class TestSelectByProject(unittest.TestCase):
    def test_selects_matching_project_only(self):
        a = make_session([user_msg("hi")], session_id="p1", project="/Users/x/projA")
        b = make_session([user_msg("hi")], session_id="p2", project="/Users/x/projB")
        sids = cd._select_by_project([a, b], "projA")
        self.assertEqual(sids, ["p1"])


class TestEmptyTrash(unittest.TestCase):
    def test_empty_trash_removes_pages_and_bookkeeping(self):
        s = make_session([user_msg("hi")], session_id="et1")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            trash = cd.load_trash(out)
            cd._delete_sessions(["et1"], [s], out, trash)
            cd.write_site([s], out, deleted_sids=frozenset(trash))
            self.assertTrue(os.path.isfile(_page(out, "et1")), "page should survive soft-delete")

            n = cd._empty_trash(trash, out)
            self.assertEqual(n, 1)
            self.assertEqual(trash, {}, "trash dict must be cleared in place")
            self.assertFalse(os.path.exists(_page(out, "et1")), "page must be hard-deleted")
            self.assertFalse(os.path.exists(_page(out, "et1", "md")))

            manifest_path = os.path.join(out, cd.RENDER_MANIFEST)
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = cd.json.load(fh)
            self.assertNotIn("et1", manifest, "manifest entry must be dropped on purge")

            cards_path = os.path.join(out, cd.ARCHIVE_CARDS)
            with open(cards_path, encoding="utf-8") as fh:
                cards = cd.json.load(fh)
            self.assertNotIn("et1", cards, "archive-cards entry must be dropped on purge")

    def test_empty_trash_never_touches_other_sessions(self):
        keep = make_session([user_msg("hi")], session_id="et2")
        gone = make_session([user_msg("bye")], session_id="et3")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([keep, gone], out)
            trash = cd.load_trash(out)
            cd._delete_sessions(["et3"], [keep, gone], out, trash)
            cd.write_site([keep, gone], out, deleted_sids=frozenset(trash))

            cd._empty_trash(trash, out)
            self.assertTrue(os.path.isfile(_page(out, "et2")), "untouched session must survive")

    def test_empty_trash_traversal_guard(self):
        # A poisoned trash entry whose "id" contains path separators must not
        # let os.remove escape the sessions/ directory. One ".." exactly cancels
        # out sessions/, landing on out/escaped.html — verified this actually
        # targets that file (not some nonexistent path that "survives" for the
        # wrong reason) by mutation-testing this case with the guard removed.
        with tempfile.TemporaryDirectory() as out:
            os.makedirs(os.path.join(out, "sessions"))
            outside = os.path.join(out, "escaped.html")
            with open(outside, "w") as fh:
                fh.write("precious")
            trash = {"../escaped": {"deleted_at": "x", "reason": "manual"}}
            cd._empty_trash(trash, out)
            self.assertTrue(os.path.isfile(outside), "traversal guard bypassed — file outside sessions/ deleted")
            self.assertEqual(trash, {}, "poisoned entry should still be evicted from the trash dict")


if __name__ == "__main__":
    unittest.main()
