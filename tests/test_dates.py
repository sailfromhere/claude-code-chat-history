"""Clock-dependent bucketing — tested with now-relative timestamps so the
assertions never drift as real time passes (no production clock injection)."""
import unittest

from fixtures import cd, now_offset_iso


class TestDateGroup(unittest.TestCase):
    def test_today(self):
        self.assertEqual(cd._date_group(now_offset_iso(seconds=60)), "Today")

    def test_yesterday(self):
        self.assertEqual(cd._date_group(now_offset_iso(days=1)), "Yesterday")

    def test_previous_7_days(self):
        self.assertEqual(cd._date_group(now_offset_iso(days=3)), "Previous 7 days")

    def test_previous_30_days(self):
        self.assertEqual(cd._date_group(now_offset_iso(days=15)), "Previous 30 days")

    def test_older(self):
        self.assertEqual(cd._date_group(now_offset_iso(days=90)), "Older")

    def test_empty_and_malformed(self):
        self.assertEqual(cd._date_group(""), "Older")
        self.assertEqual(cd._date_group("not-a-date"), "Older")


class TestActive(unittest.TestCase):
    def test_recent_is_active(self):
        self.assertTrue(cd._is_active(now_offset_iso(seconds=120)))

    def test_old_is_not_active(self):
        self.assertFalse(cd._is_active(now_offset_iso(seconds=60 * 60)))
        self.assertFalse(cd._is_active(""))
        self.assertFalse(cd._is_active("not-a-date"))

    def test_active_label_is_relative(self):
        self.assertEqual(cd._active_label(now_offset_iso(seconds=10)), "active now")
        self.assertEqual(cd._active_label(now_offset_iso(seconds=60 * 5)), "active 5m ago")


class TestTallyStr(unittest.TestCase):
    def test_complete(self):
        self.assertEqual(cd._tally_str(2_500_000, 3.5, True), "~$3.50 · 2.5M tok")

    def test_incomplete_marks_plus(self):
        self.assertEqual(cd._tally_str(500, 1.0, False), "~$1.00+ · 500 tok")


class TestFmt(unittest.TestCase):
    def test_fmt_date_passthrough_on_bad_input(self):
        self.assertEqual(cd._fmt_date("garbage"), "garbage")

    def test_fmt_tokens(self):
        self.assertEqual(cd._fmt_tokens(500), "500")
        self.assertEqual(cd._fmt_tokens(1500), "2K")
        self.assertEqual(cd._fmt_tokens(2_500_000), "2.5M")


if __name__ == "__main__":
    unittest.main()
