"""Retention config (.dashboard-config.json) and the automatic housekeeping it
drives via main(): sessions inactive past retention_days auto-trash (still
recoverable), and trash entries themselves age out past purge_days (hard
delete). Both dials default to off (None) — today's keep-forever behavior."""
import os
import tempfile
import unittest

from fixtures import cd, make_session, user_msg, write_jsonl, now_offset_iso


def _page(out: str, sid: str, ext: str = "html") -> str:
    return os.path.join(out, "sessions", f"{sid}.{ext}")


class TestConfigRoundTrip(unittest.TestCase):
    def test_defaults_are_off(self):
        with tempfile.TemporaryDirectory() as out:
            cfg = cd.load_config(out)
            self.assertIsNone(cfg["retention_days"])
            self.assertIsNone(cfg["purge_days"])

    def test_save_then_load_round_trips(self):
        with tempfile.TemporaryDirectory() as out:
            cd.save_config(out, {"retention_days": 90, "purge_days": 30})
            cfg = cd.load_config(out)
            self.assertEqual(cfg["retention_days"], 90)
            self.assertEqual(cfg["purge_days"], 30)

    def test_set_retention_off_clears_to_none(self):
        with tempfile.TemporaryDirectory() as out:
            cd.save_config(out, {"retention_days": 90, "purge_days": None})
            cfg = cd.load_config(out)
            cfg["retention_days"] = None
            cd.save_config(out, cfg)
            self.assertIsNone(cd.load_config(out)["retention_days"])

    def test_malformed_value_self_heals_to_default(self):
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, cd.CONFIG_FILE)
            with open(path, "w") as fh:
                cd.json.dump({"retention_days": "not-a-number", "purge_days": -5}, fh)
            cfg = cd.load_config(out)
            self.assertIsNone(cfg["retention_days"])
            self.assertIsNone(cfg["purge_days"], "negative purge_days must not be trusted")

    def test_days_or_off_parses(self):
        self.assertIsNone(cd._days_or_off("off"))
        self.assertIsNone(cd._days_or_off("OFF"))
        self.assertEqual(cd._days_or_off("90"), 90.0)
        with self.assertRaises(cd.argparse.ArgumentTypeError):
            cd._days_or_off("-5")
        with self.assertRaises(cd.argparse.ArgumentTypeError):
            cd._days_or_off("banana")


class TestAutoRetentionAndPurgeViaMain(unittest.TestCase):
    def test_set_retention_persists_and_main_reports_it(self):
        with tempfile.TemporaryDirectory() as projects, tempfile.TemporaryDirectory() as out:
            os.makedirs(os.path.join(projects, "proj"))
            rc = cd.main(["--no-titles", "--projects-dir", projects, "--output-dir", out,
                         "--set-retention", "90", "--set-purge", "30"])
            self.assertEqual(rc, 0)
            cfg = cd.load_config(out)
            self.assertEqual(cfg["retention_days"], 90)
            self.assertEqual(cfg["purge_days"], 30)

    def test_auto_retention_trashes_old_sessions_on_normal_build(self):
        with tempfile.TemporaryDirectory() as projects, tempfile.TemporaryDirectory() as out:
            old_ts = now_offset_iso(days=100)
            recent_ts = now_offset_iso(days=1)
            write_jsonl(projects, "old.jsonl", [
                {"type": "user", "uuid": "u1", "timestamp": old_ts,
                 "message": {"role": "user", "content": "old one"}},
            ], session_id="ret-old")
            write_jsonl(projects, "recent.jsonl", [
                {"type": "user", "uuid": "u2", "timestamp": recent_ts,
                 "message": {"role": "user", "content": "recent one"}},
            ], session_id="ret-recent")

            # Render once with retention off, so "ret-old" has an existing page —
            # otherwise a session auto-trashed on its very first-ever render never
            # had a page to begin with, which is a different (also-handled) case.
            cd.main(["--no-titles", "--projects-dir", projects, "--output-dir", out])
            self.assertTrue(os.path.isfile(_page(out, "ret-old")))

            cd.main(["--no-titles", "--projects-dir", projects, "--output-dir", out,
                    "--set-retention", "90"])
            trash = cd.load_trash(out)
            self.assertIn("ret-old", trash)
            self.assertNotIn("ret-recent", trash)
            self.assertEqual(trash["ret-old"]["reason"], "retention")
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                index_html = fh.read()
            self.assertNotIn("sessions/ret-old.html", index_html)
            self.assertIn("sessions/ret-recent.html", index_html)
            # Soft-delete contract still holds for automatic trashing too.
            self.assertTrue(os.path.isfile(_page(out, "ret-old")))

    def test_dry_run_never_triggers_auto_retention(self):
        with tempfile.TemporaryDirectory() as projects, tempfile.TemporaryDirectory() as out:
            old_ts = now_offset_iso(days=100)
            write_jsonl(projects, "old.jsonl", [
                {"type": "user", "uuid": "u1", "timestamp": old_ts,
                 "message": {"role": "user", "content": "old one"}},
            ], session_id="ret-dry")
            cd.main(["--no-titles", "--projects-dir", projects, "--output-dir", out])
            # Turn retention on directly (bypassing --set-retention's own immediate
            # sweep, which is a separate, intentional behavior tested elsewhere) so
            # this test isolates ONLY "does --dry-run suppress the auto-sweep".
            cd.save_config(out, {"retention_days": 90, "purge_days": None})
            cd.main(["--no-titles", "--projects-dir", projects, "--output-dir", out, "--dry-run"])
            trash = cd.load_trash(out)
            self.assertNotIn("ret-dry", trash, "auto-retention must not run under --dry-run")

    def test_auto_purge_hard_deletes_stale_trash_entries(self):
        s = make_session([user_msg("hi")], session_id="purge1")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            trash = cd.load_trash(out)
            cd._delete_sessions(["purge1"], [s], out, trash)
            # Backdate the trash entry so it's already past the purge threshold.
            trash["purge1"]["deleted_at"] = now_offset_iso(days=40)
            cd.save_trash(out, trash)
            cd.save_config(out, {"retention_days": None, "purge_days": 30})

            cd.write_site([s], out, deleted_sids=frozenset(cd.load_trash(out)))
            # Simulate what main()'s auto-purge sweep does directly (no source dir
            # needed for this unit-level check).
            trash2 = cd.load_trash(out)
            config = cd.load_config(out)
            import datetime as dtmod
            now = dtmod.datetime.now().astimezone()
            stale = {sid for sid, row in trash2.items()
                    if (now - dtmod.datetime.fromisoformat(row["deleted_at"])).total_seconds() / 86400
                    >= config["purge_days"]}
            self.assertEqual(stale, {"purge1"})
            n = cd._empty_trash(trash2, out, only=stale)
            self.assertEqual(n, 1)
            self.assertFalse(os.path.exists(_page(out, "purge1")))

    def test_auto_purge_via_main_end_to_end_for_a_gone_source(self):
        # The coherent "permanently gone" case: the source .jsonl has actually
        # disappeared (simulating Claude Code's own cleanupPeriodDays prune) by
        # the time purge runs, so there's no live session left to re-render it.
        with tempfile.TemporaryDirectory() as projects, tempfile.TemporaryDirectory() as out:
            ts = now_offset_iso(days=1)
            write_jsonl(projects, "s.jsonl", [
                {"type": "user", "uuid": "u1", "timestamp": ts,
                 "message": {"role": "user", "content": "hi"}},
            ], session_id="purge2")
            cd.main(["--no-titles", "--projects-dir", projects, "--output-dir", out])
            trash = cd.load_trash(out)
            cd._delete_sessions(["purge2"], cd.load_sessions(projects), out, trash)
            trash["purge2"]["deleted_at"] = now_offset_iso(days=40)
            cd.save_trash(out, trash)

            with tempfile.TemporaryDirectory() as empty_projects:
                cd.main(["--no-titles", "--projects-dir", empty_projects, "--output-dir", out,
                        "--set-purge", "30"])
            self.assertFalse(os.path.exists(_page(out, "purge2")),
                            "auto-purge must hard-delete a trash entry older than purge_days")
            trash_after = cd.load_trash(out)
            self.assertNotIn("purge2", trash_after)

    def test_empty_trash_on_a_still_live_session_reappears_same_run(self):
        # A session whose source .jsonl still exists can't really be forgotten
        # by this generator — it mirrors the source. --empty-trash frees disk
        # and forgets the bookkeeping row right now, but since the session is
        # still fed in as live, this SAME run's write_site() re-renders it
        # fresh. This is intentional (see _warn_reappearing) — pin it so it
        # isn't mistaken for a bug later.
        with tempfile.TemporaryDirectory() as projects, tempfile.TemporaryDirectory() as out:
            write_jsonl(projects, "s.jsonl", [
                {"type": "user", "uuid": "u1", "timestamp": now_offset_iso(days=1),
                 "message": {"role": "user", "content": "hi"}},
            ], session_id="live1")
            cd.main(["--no-titles", "--projects-dir", projects, "--output-dir", out])
            trash = cd.load_trash(out)
            cd._delete_sessions(["live1"], cd.load_sessions(projects), out, trash)
            cd.save_trash(out, trash)

            cd.main(["--no-titles", "--projects-dir", projects, "--output-dir", out,
                    "--empty-trash", "--yes"])
            self.assertTrue(os.path.isfile(_page(out, "live1")),
                           "still-live session must be rendered fresh in the same run")
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                self.assertIn("sessions/live1.html", fh.read())

    def test_restore_of_an_old_session_is_not_reversed_in_the_same_run(self):
        # HIGH bug found by adversarial review: --restore popped a sid from
        # trash, but the auto-retention sweep ran right after in the SAME
        # main() call and immediately re-selected it (still old) and re-trashed
        # it — silently undoing the user's own explicit action before the
        # command even finished. A session named in THIS run's --restore must
        # be exempt from THIS run's auto-retention sweep.
        with tempfile.TemporaryDirectory() as projects, tempfile.TemporaryDirectory() as out:
            old_ts = now_offset_iso(days=100)
            write_jsonl(projects, "old.jsonl", [
                {"type": "user", "uuid": "u1", "timestamp": old_ts,
                 "message": {"role": "user", "content": "old one"}},
            ], session_id="rest-old")
            cd.main(["--no-titles", "--projects-dir", projects, "--output-dir", out])
            cd.save_config(out, {"retention_days": 90, "purge_days": None})
            trash = cd.load_trash(out)
            cd._delete_sessions(["rest-old"], cd.load_sessions(projects), out, trash)
            cd.save_trash(out, trash)
            self.assertIn("rest-old", cd.load_trash(out))

            cd.main(["--no-titles", "--projects-dir", projects, "--output-dir", out,
                    "--restore", "rest-old"])
            trash_after = cd.load_trash(out)
            self.assertNotIn("rest-old", trash_after,
                            "restore must not be reversed by the same run's auto-retention sweep")
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                self.assertIn("sessions/rest-old.html", fh.read())

    def test_trashed_orphan_survives_prune_orphans(self):
        # deleted_sids (trash) must take priority over --prune-orphans: once a
        # source-gone session is trashed, --prune-orphans must not be able to
        # hard-delete it out from under the soft-delete contract — only
        # --empty-trash/purge may do that.
        gone = make_session([user_msg("bye")], session_id="orph1", ai_title="Orphan Chat")
        kept = make_session([user_msg("hi")], session_id="orph1-kept")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([gone, kept], out)
            trash = cd.load_trash(out)
            cd._delete_sessions(["orph1"], [gone, kept], out, trash)
            cd.save_trash(out, trash)
            # "gone" now disappears from the source; run with --prune-orphans=True.
            cd.write_site([kept], out, prune_orphans=True, deleted_sids=frozenset(trash))
            self.assertTrue(os.path.isfile(_page(out, "orph1")),
                           "trashed orphan must survive --prune-orphans (soft-delete wins)")

    def test_list_trash_does_not_crash_on_malformed_deleted_at(self):
        # MEDIUM bug found by adversarial review: sorted() on a mixed-type
        # deleted_at (str vs non-str) would raise and abort --list-trash.
        # load_trash's own validation already drops such rows, but _print_trash
        # must also tolerate a hand-built dict that bypasses load_trash.
        trash = {
            "a": {"deleted_at": "2026-01-01T00:00:00", "reason": "manual", "fields": {}},
            "b": {"deleted_at": 12345, "reason": "manual", "fields": {}},  # malformed
        }
        cd._print_trash(trash)  # must not raise

    def test_load_trash_drops_row_with_non_string_deleted_at(self):
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, cd.TRASH_FILE)
            with open(path, "w") as fh:
                cd.json.dump({
                    "good": {"deleted_at": "2026-01-01T00:00:00", "reason": "manual"},
                    "bad": {"deleted_at": 12345, "reason": "manual"},
                }, fh)
            trash = cd.load_trash(out)
            self.assertIn("good", trash)
            self.assertNotIn("bad", trash, "non-string deleted_at must be dropped, not crash later")

    def test_auto_retention_trashes_an_archived_stub_not_just_live_sessions(self):
        # _known_cards (used for retention selection) must include orphans
        # (source already gone) rebuilt from .archive-cards.json, not just
        # live sessions — otherwise retention would silently never touch the
        # dashboard's own permanent-archive sessions no matter how old.
        gone = make_session([user_msg("bye")], session_id="stub-old")
        gone.last_ts = now_offset_iso(days=200)
        kept = make_session([user_msg("hi")], session_id="stub-old-kept")
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([gone, kept], out)
            cd.write_site([kept], out)  # "gone" becomes an archived stub

            known = cd._known_cards([kept], out)
            stub = next(c for c in known if c.session_id == "stub-old")
            self.assertTrue(getattr(stub, "is_archived", False))

            trash = cd.load_trash(out)
            n = cd._delete_sessions(["stub-old"], [kept], out, trash, reason="retention")
            self.assertEqual(n, 1, "retention selection must reach archived stubs, not just live sessions")


if __name__ == "__main__":
    unittest.main()
