"""End-to-end smoke: write_site must generate every page without raising (this
catches the f-string / broken-markup class that has shipped before) and the
output must be parseable HTML containing the expected anchors."""
import os
import re
import tempfile
import unittest
from html.parser import HTMLParser

from fixtures import (cd, make_session, user_msg, slash_command, expansion,
                      skill_body, bash_io, assistant_blocks, assistant_text,
                      tool_use_block, text_block, tool_result_entry, assistant_usage)


def _varied_session():
    """One session exercising the full render path."""
    return make_session([
        user_msg("Help me with **markdown** and `code` and a [link](https://x.com)."),
        slash_command("/init"),
        expansion("You are initializing CLAUDE.md…"),
        skill_body("verify"),
        bash_io("ls -la", stdout="total 0\nfile &gt; out.txt"),
        assistant_blocks([
            text_block("Here's a fenced block:\n```python\nprint('hi')\n```"),
            tool_use_block("Read", {"file_path": "/etc/hosts"}, "t1"),
        ]),
        tool_result_entry("t1", "127.0.0.1 localhost"),
        assistant_blocks([tool_use_block("Edit", {
            "file_path": "/x.py", "old_string": "a = 1", "new_string": "a = 2"}, "t2")]),
        tool_result_entry("t2", "edited"),
        assistant_usage("claude-opus-4-8", input=1000, output=500, cache_read=2000),
        assistant_text("All done."),
    ], session_id="smoke1", ai_title="Smoke Test Session")


class _Parse(HTMLParser):
    """Feeding output through HTMLParser surfaces gross structural breakage."""
    pass


class TestSmoke(unittest.TestCase):
    def test_write_site_generates_parseable_pages(self):
        sessions = [_varied_session()]
        with tempfile.TemporaryDirectory() as out:
            cd.write_site(sessions, out)   # must not raise

            index = os.path.join(out, "index.html")
            page = os.path.join(out, "sessions", "smoke1.html")
            md = os.path.join(out, "sessions", "smoke1.md")
            for p in (index, page, md):
                self.assertTrue(os.path.isfile(p), f"missing {p}")
                self.assertGreater(os.path.getsize(p), 0)

            for p in (index, page):
                with open(p, encoding="utf-8") as fh:
                    content = fh.read()
                self.assertTrue(content.lstrip().startswith("<!DOCTYPE html>"))
                _Parse().feed(content)   # raises on malformed markup

            with open(page, encoding="utf-8") as fh:
                page_html = fh.read()
            self.assertIn("Smoke Test Session", page_html)
            self.assertIn("⌘ /init", page_html)
            self.assertIn("loaded skill", page_html)
            with open(index, encoding="utf-8") as fh:
                index_html = fh.read()
            self.assertIn("sessions/smoke1.html", index_html)
            # Scoped cost tallies: per-project (left pane), per-date-group (JS-filled
            # span), and per-session card data attributes for the JS re-tally.
            self.assertIn('class="pcost"', index_html)
            self.assertIn('class="group-tally"', index_html)
            self.assertIn("data-cost=", index_html)
            self.assertIn("data-tok=", index_html)
            with open(md, encoding="utf-8") as fh:
                self.assertIn("Smoke Test Session", fh.read())

    def test_date_margin_auto_scoped_to_cards(self):
        """Regression: `.date` is shared by the session-card list and the transcript
        header (.hmeta). `margin-left:auto` right-pins the date in cards but, on the
        bare `.date` rule, leaks into the header and flings the date/count/usage to the
        right edge (the 'justified' header bug, 2026-06-13). It must live ONLY on the
        card-scoped rule so the header stays left-aligned."""
        css = cd._CSS
        # The card-scoped rule must exist and carry the auto-margin.
        card_rule = re.search(r"\.card-top\s+\.date\{([^}]*)\}", css)
        self.assertIsNotNone(card_rule, ".card-top .date rule is missing")
        self.assertIn("margin-left:auto", card_rule.group(1))
        # The bare `.date{…}` rule (start of line) must NOT — or the header re-breaks.
        base_rule = re.search(r"(?:^|\n)\.date\{([^}]*)\}", css)
        self.assertIsNotNone(base_rule, "base .date rule is missing")
        self.assertNotIn(
            "margin-left:auto", base_rule.group(1),
            "margin-left:auto leaked onto the bare .date rule → header .hmeta will justify")

    def test_no_summary_tooltips_and_no_project_tooltip(self):
        """The LLM summary must NOT appear as a hover popup anywhere — not on the
        index card title, not on the session-page <h1> (removed 2026-06-21, user
        doesn't read them). Project-selection buttons also carry no tooltip (cost is
        shown inline). The summary may still feed the inline card snippet."""
        marker = "ZZ_UNIQUE_SUMMARY_MARKER"
        s = make_session([user_msg("hi")], session_id="tip1", ai_title="Titled")
        s.summary = marker
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out)
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                index_html = fh.read()
            with open(os.path.join(out, "sessions", "tip1.html"), encoding="utf-8") as fh:
                page_html = fh.read()
        # The summary never shows up as a tooltip (data-tip) on either page.
        self.assertNotIn(f'data-tip="{marker}"', index_html)
        self.assertNotIn(f'data-tip="{marker}"', page_html)
        self.assertNotIn("has-summary", index_html)
        self.assertNotIn("has-summary", page_html)
        # No project-selection button has a tooltip.
        for m in re.finditer(r"<button class=\"proj[^>]*>", index_html):
            self.assertNotIn("data-tip", m.group(0),
                             "project button should not carry a hover tooltip")

    def test_empty_site(self):
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([], out)
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("No chat history found", content)


if __name__ == "__main__":
    unittest.main()
