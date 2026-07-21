"""Trash (soft-delete): a trashed session must be excluded from the index card
list and sidebar rollups without touching its rendered page or the underlying
manifest/archive-cards entries. Restore must bring it back losslessly, whether
its source .jsonl is still live or already gone (archived-stub case). Deleting
never resurrects on a later regen — that's the whole point of the feature."""
import os
import tempfile
import unittest

from fixtures import cd, make_session, user_msg, assistant_usage


def _page(out: str, sid: str, ext: str = "html") -> str:
    return os.path.join(out, "sessions", f"{sid}.{ext}")


class TestTrashRoundTrip(unittest.TestCase):
    def test_deleted_session_excluded_from_index_but_page_kept(self):
        s = make_session([user_msg("hello")], session_id="tr1", ai_title="Trash Me")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            self.assertTrue(os.path.isfile(_page(out, "tr1")))

            trash = cd.load_trash(out)
            n = cd._delete_sessions(["tr1"], [s], out, trash, reason="manual")
            self.assertEqual(n, 1)
            cd.save_trash(out, trash)

            cd.write_site([s], out, deleted_sids=frozenset(trash))
            self.assertTrue(os.path.isfile(_page(out, "tr1")), "soft-delete must keep the page")
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                index_html = fh.read()
            self.assertNotIn('href="sessions/tr1.html"', index_html,
                             "trashed session must not be listed")
            self.assertNotIn("Trash Me", index_html)

    def test_deleted_session_stays_gone_on_a_later_regen(self):
        # The core requirement: a rerun of the generator must NOT pull a deleted
        # session back in.
        s = make_session([user_msg("hello")], session_id="tr2")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            trash = cd.load_trash(out)
            cd._delete_sessions(["tr2"], [s], out, trash)
            cd.save_trash(out, trash)
            cd.write_site([s], out, deleted_sids=frozenset(trash))

            # Simulate a completely fresh invocation: reload trash from disk.
            trash2 = cd.load_trash(out)
            self.assertIn("tr2", trash2)
            cd.write_site([s], out, deleted_sids=frozenset(trash2))
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                self.assertNotIn('href="sessions/tr2.html"', fh.read())

    def test_restore_brings_a_live_session_back(self):
        s = make_session([user_msg("hello")], session_id="tr3", ai_title="Come Back")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            trash = cd.load_trash(out)
            cd._delete_sessions(["tr3"], [s], out, trash)
            cd.write_site([s], out, deleted_sids=frozenset(trash))
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                self.assertNotIn("Come Back", fh.read())

            n = cd._restore_sessions(["tr3"], trash)
            self.assertEqual(n, 1)
            self.assertNotIn("tr3", trash)
            cd.write_site([s], out, deleted_sids=frozenset(trash))
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                index_html = fh.read()
            self.assertIn("Come Back", index_html)
            self.assertIn('href="sessions/tr3.html"', index_html)

    def test_trashed_source_gone_session_stays_hidden_and_restores_to_a_stub(self):
        # A session that's both an orphan (source .jsonl gone) AND trashed must
        # stay hidden — and restoring it must rebuild its archived-stub card,
        # since its manifest/card-metadata rows were carried forward while
        # trashed.
        gone = make_session([user_msg("bye")], session_id="tr4", ai_title="Ghost Chat")
        kept = make_session([user_msg("hi")], session_id="tr4-kept")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([gone, kept], out)  # both live and rendered

            trash = cd.load_trash(out)
            cd._delete_sessions(["tr4"], [gone, kept], out, trash)
            cd.save_trash(out, trash)
            # "gone" now disappears from the source (simulating Claude Code's
            # cleanupPeriodDays prune) — only "kept" is passed in from here on.
            cd.write_site([kept], out, deleted_sids=frozenset(trash))
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                self.assertNotIn("Ghost Chat", fh.read())
            self.assertTrue(os.path.isfile(_page(out, "tr4")), "page must survive soft-delete")

            # Restore: dropped from trash, next run rebuilds the archived stub.
            trash2 = cd.load_trash(out)
            cd._restore_sessions(["tr4"], trash2)
            cd.save_trash(out, trash2)
            cd.write_site([kept], out, deleted_sids=frozenset(trash2))
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                index_html = fh.read()
            self.assertIn("Ghost Chat", index_html, "restored orphan must reappear as a stub")
            self.assertIn("archived-pill", index_html)

    def test_trashed_sessions_excluded_from_sidebar_cost_rollup(self):
        s = make_session([assistant_usage("claude-sonnet-5", input=1000, output=1000)],
                         session_id="tr5")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            cost = s.cost_usd
            self.assertGreater(cost, 0)
            trash = cd.load_trash(out)
            cd._delete_sessions(["tr5"], [s], out, trash)
            cd.write_site([s], out, deleted_sids=frozenset(trash))
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                index_html = fh.read()
            self.assertNotIn(f"{cost:,.2f}", index_html,
                             "trashed session's cost must drop out of the sidebar rollup")

    def test_delete_unknown_id_is_skipped_not_fabricated(self):
        s = make_session([user_msg("hi")], session_id="tr6")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            trash = cd.load_trash(out)
            n = cd._delete_sessions(["does-not-exist"], [s], out, trash)
            self.assertEqual(n, 0)
            self.assertNotIn("does-not-exist", trash)

    def test_restore_unknown_id_is_a_noop(self):
        trash = {}
        n = cd._restore_sessions(["never-trashed"], trash)
        self.assertEqual(n, 0)

    def test_delete_is_idempotent(self):
        s = make_session([user_msg("hi")], session_id="tr7")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            trash = cd.load_trash(out)
            cd._delete_sessions(["tr7"], [s], out, trash)
            first_deleted_at = trash["tr7"]["deleted_at"]
            cd._delete_sessions(["tr7"], [s], out, trash)  # delete again
            self.assertEqual(trash["tr7"]["deleted_at"], first_deleted_at,
                            "re-deleting an already-trashed session must not disturb its entry")

    def test_load_trash_self_heals_on_corrupt_file(self):
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, cd.TRASH_FILE)
            with open(path, "w") as fh:
                fh.write("not json{{{")
            trash = cd.load_trash(out)
            self.assertEqual(trash, {})

    def test_load_trash_drops_malformed_rows(self):
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, cd.TRASH_FILE)
            with open(path, "w") as fh:
                cd.json.dump({"good": {"deleted_at": "x", "reason": "manual"},
                             "bad": "not-a-dict"}, fh)
            trash = cd.load_trash(out)
            self.assertIn("good", trash)
            self.assertNotIn("bad", trash)


if __name__ == "__main__":
    unittest.main()
