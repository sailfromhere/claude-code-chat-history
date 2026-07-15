"""Incremental write_site: unchanged session pages must be skipped (not
re-rendered/rewritten), any relevant change must force a rewrite, a missing
file must self-heal even when the fingerprint says 'unchanged', deleted
sessions' pages are ARCHIVED (kept AND still listed, flagged) by default, and
pruned only when prune_orphans=True is passed explicitly."""
import os
import re
import tempfile
import unittest

from fixtures import cd, make_session, user_msg, assistant_text, assistant_usage

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

    def test_orphans_kept_by_default_archive(self):
        # A session this generator previously wrote (it's in the manifest) whose
        # jsonl later disappears → its pages are KEPT (archived) by default, and
        # it stays on the manifest ledger AND remains listed in index.html,
        # flagged as archived (not silently dropped from the UI).
        gone = make_session([user_msg("bye")], session_id="gone1", ai_title="Farewell Chat")
        kept = make_session([user_msg("hello")], session_id="inc5")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([gone, kept], out)
            self.assertTrue(os.path.isfile(_page(out, "gone1")))
            written, skipped, pruned, archived = cd.write_site([kept], out)
            self.assertTrue(os.path.isfile(_page(out, "gone1")), "archived page deleted")
            self.assertTrue(os.path.isfile(_page(out, "gone1", "md")))
            self.assertTrue(os.path.isfile(_page(out, "inc5")))
            self.assertEqual(pruned, 0)
            self.assertEqual(archived, 1)
            manifest_path = os.path.join(out, cd.RENDER_MANIFEST)
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = cd.json.load(fh)
            self.assertIn("gone1", manifest, "orphan dropped from manifest ledger")
            cards_path = os.path.join(out, cd.ARCHIVE_CARDS)
            with open(cards_path, encoding="utf-8") as fh:
                cards = cd.json.load(fh)
            self.assertIn("gone1", cards, "orphan dropped from card-metadata sidecar")
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                index_html = fh.read()
            self.assertIn("Farewell Chat", index_html,
                          "archived session's card missing from index — not listed")
            self.assertIn("archived-pill", index_html, "archived flag/badge missing")
            self.assertIn('href="sessions/gone1.html"', index_html,
                          "archived card must still link to its frozen page")

    def test_archived_session_stays_listed_across_multiple_orphan_runs(self):
        # The stub must survive more than one run after the source disappears —
        # not just the first "just went orphan" run.
        gone = make_session([user_msg("bye")], session_id="gone3", ai_title="Old Chat")
        kept = make_session([user_msg("hello")], session_id="inc5c")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([gone, kept], out)
            cd.write_site([kept], out)  # gone3 goes orphan here
            written, skipped, pruned, archived = cd.write_site([kept], out)  # second orphan run
            self.assertEqual(archived, 1, "orphan lost after a second run")
            self.assertTrue(os.path.isfile(_page(out, "gone3")))
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                self.assertIn("Old Chat", fh.read())

    def test_archived_sessions_included_in_sidebar_cost_rollup(self):
        # User-confirmed choice: sidebar totals include archived sessions, not
        # just live ones — the numbers reflect true all-time history.
        gone = make_session([assistant_usage("claude-sonnet-5", input=1000, output=1000)],
                            session_id="gone4")
        kept = make_session([user_msg("hello")], session_id="inc5d")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([gone, kept], out)
            gone_cost = gone.cost_usd
            self.assertGreater(gone_cost, 0)
            cd.write_site([kept], out)  # gone4 goes orphan
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                index_html = fh.read()
            # "All projects" tally is the first .pcost span rendered — check THAT
            # span specifically (not just some card's own tally) to prove the sum
            # actually folds the archived session's cost in.
            m = re.search(r'<span class="pcost">(.*?)</span>', index_html)
            self.assertIsNotNone(m, "sidebar pcost span not found")
            self.assertIn(f"{gone_cost:,.2f}", m.group(1),
                          "archived session's cost missing from sidebar rollup")

    def test_archived_count_reflects_only_actually_listed_cards(self):
        # An orphan whose card metadata isn't available (e.g. the very first run
        # after upgrading, when .archive-cards.json doesn't exist yet) is still
        # kept on disk and carried in the manifest, but produces no card — the
        # `archived` return value (and the CLI's "still listed" message) must NOT
        # count it, or it's a lie: nothing was actually listed for it.
        gone = make_session([user_msg("bye")], session_id="gone5")
        kept = make_session([user_msg("hello")], session_id="inc5e")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([gone, kept], out)
            # Simulate the upgrade-path gap: no card metadata was ever captured
            # for "gone5" (as if this were the first run after adding the
            # sidecar, or the sidecar was reset).
            cards_path = os.path.join(out, cd.ARCHIVE_CARDS)
            os.remove(cards_path)
            written, skipped, pruned, archived = cd.write_site([kept], out)
            self.assertEqual(archived, 0, "counted as archived/listed with no card to show")
            self.assertTrue(os.path.isfile(_page(out, "gone5")), "page should still be kept")
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                self.assertNotIn('href="sessions/gone5.html"', fh.read(),
                                 "card listed despite having no metadata to render it from")

    def test_corrupt_archive_card_row_is_dropped_not_crashed(self):
        # .archive-cards.json is generator-owned and atomically written, but a
        # hand-edit, cross-version file, or partial corruption could put a
        # wrong-typed value in a row (e.g. cost_usd as a string). The dataclass
        # constructor doesn't type-check, so an unvalidated row would flow into
        # unguarded downstream code (sort by last_ts, cost formatting, token
        # arithmetic) and crash the WHOLE index render, not just that card.
        gone = make_session([user_msg("bye")], session_id="gone6")
        kept = make_session([user_msg("hello")], session_id="inc5f")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([gone, kept], out)
            cards_path = os.path.join(out, cd.ARCHIVE_CARDS)
            with open(cards_path, encoding="utf-8") as fh:
                cards = cd.json.load(fh)
            cards["gone6"]["cost_usd"] = "not-a-number"  # corrupt the row
            cards["gone6"]["last_ts"] = None
            with open(cards_path, "w", encoding="utf-8") as fh:
                cd.json.dump(cards, fh)
            # Must not raise, and must not list the corrupt card.
            written, skipped, pruned, archived = cd.write_site([kept], out)
            self.assertEqual(archived, 0)
            self.assertTrue(os.path.isfile(_page(out, "gone6")), "page should still be kept")
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                self.assertNotIn('href="sessions/gone6.html"', fh.read())

    def test_orphan_pages_of_deleted_sessions_are_pruned_when_opted_in(self):
        # Same setup, but with prune_orphans=True the old mirror behavior applies:
        # pages are removed on the next run.
        gone = make_session([user_msg("bye")], session_id="gone2")
        kept = make_session([user_msg("hello")], session_id="inc5b")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([gone, kept], out)
            self.assertTrue(os.path.isfile(_page(out, "gone2")))
            cd.write_site([kept], out, prune_orphans=True)
            self.assertFalse(os.path.exists(_page(out, "gone2")), "orphan not pruned")
            self.assertFalse(os.path.exists(_page(out, "gone2", "md")))
            self.assertTrue(os.path.isfile(_page(out, "inc5b")))

    def test_prune_never_touches_files_it_did_not_create(self):
        # --output-dir can point anywhere; pre-existing .html/.md in sessions/
        # that this generator never wrote (absent from the manifest) must survive,
        # even under prune_orphans=True.
        s = make_session([user_msg("hello")], session_id="inc7")
        with tempfile.TemporaryDirectory() as out:
            os.makedirs(os.path.join(out, "sessions"))
            for ext in ("html", "md"):
                with open(_page(out, "unrelated-doc", ext), "w") as fh:
                    fh.write("precious user file")
            cd.write_site([s], out, prune_orphans=True)
            cd.write_site([s], out, prune_orphans=True)  # second run: manifest exists, still no claim
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
