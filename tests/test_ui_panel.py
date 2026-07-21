"""The browser-side Cleanup/Retention/Trash command-builder UI added to
index.html: per-card data-sid + delete affordance, and the settings panel
embedding the current trash list / retention config read-only at generation
time (this stays a static site — no fetch(), so the panel can only reflect
state as of the last build)."""
import os
import re
import tempfile
import unittest
from html.parser import HTMLParser

from fixtures import cd, make_session, user_msg


class _Parse(HTMLParser):
    pass


def _index_html_for(sessions, trash=None, config=None):
    return cd._index_page(sessions, trash=trash, config=config)


class TestCardDeleteAffordance(unittest.TestCase):
    def test_card_carries_data_sid(self):
        s = make_session([user_msg("hi")], session_id="ui1")
        html = _index_html_for([s])
        self.assertIn('data-sid="ui1"', html)

    def test_card_has_delete_control_wired_to_its_own_sid(self):
        s = make_session([user_msg("hi")], session_id="ui2")
        html = _index_html_for([s])
        self.assertIn("card-del", html)
        self.assertIn("cmdDelete('ui2')", html)

    def test_delete_control_stops_propagation_and_prevents_navigation(self):
        # Otherwise clicking 🗑 would both navigate the card's <a href> AND
        # select the card, instead of just opening the command dialog.
        s = make_session([user_msg("hi")], session_id="ui3")
        html = _index_html_for([s])
        m = re.search(r'<span class="card-del"[^>]*onclick="([^"]*)"', html)
        self.assertIsNotNone(m, "card-del span not found")
        self.assertIn("event.preventDefault()", m.group(1))
        self.assertIn("event.stopPropagation()", m.group(1))


class TestSettingsPanel(unittest.TestCase):
    def test_empty_trash_shows_placeholder(self):
        s = make_session([user_msg("hi")], session_id="ui4")
        html = _index_html_for([s], trash={})
        self.assertIn("Trash is empty.", html)

    def test_trash_rows_render_project_title_and_restore_command(self):
        s = make_session([user_msg("hi")], session_id="ui5")
        trash = {
            "gone1": {
                "deleted_at": "2026-01-01T00:00:00-07:00",
                "reason": "manual",
                "fields": {"title": "My Old Chat", "project_name": "myproj"},
            }
        }
        html = _index_html_for([s], trash=trash)
        self.assertIn("My Old Chat", html)
        self.assertIn("myproj", html)
        self.assertIn("cmdRestore('gone1')", html)
        self.assertIn("Trash (1)", html)

    def test_empty_trash_button_only_shown_when_trash_nonempty(self):
        # Note: "cmdEmptyTrash" the JS function is always defined in <script> —
        # check for the actual BUTTON markup, not the identifier substring.
        s = make_session([user_msg("hi")], session_id="ui6")
        empty_html = _index_html_for([s], trash={})
        self.assertNotIn("Empty trash (permanent)", empty_html)
        trash = {"gone2": {"deleted_at": "2026-01-01T00:00:00", "reason": "manual", "fields": {}}}
        full_html = _index_html_for([s], trash=trash)
        self.assertIn("Empty trash (permanent)", full_html)
        self.assertIn('onclick="cmdEmptyTrash()"', full_html)

    def test_retention_config_displayed(self):
        s = make_session([user_msg("hi")], session_id="ui7")
        html_off = _index_html_for([s], config={"retention_days": None, "purge_days": None})
        self.assertIn("auto-trash off", html_off)
        self.assertIn("auto-purge off", html_off)
        html_on = _index_html_for([s], config={"retention_days": 90, "purge_days": 30})
        self.assertIn("auto-trash 90d", html_on)
        self.assertIn("auto-purge 30d", html_on)

    def test_project_options_include_known_projects(self):
        s = make_session([user_msg("hi")], session_id="ui8", project="/Users/x/myproj")
        html = _index_html_for([s])
        self.assertIn('<option value="myproj">myproj</option>', html)

    def test_retention_inputs_prefill_with_current_config(self):
        # HIGH bug found by adversarial review: the retention/purge inputs
        # started blank while only a separate read-only line showed the
        # current values. Building a command after touching only ONE field
        # silently emitted "--set-purge off" (or vice versa) for the field the
        # user never meant to change, wiping it. Fix: pre-fill each input with
        # its OWN current value, so submitting an untouched field reproduces
        # it instead of coercing it to off.
        s = make_session([user_msg("hi")], session_id="ui11")
        html = _index_html_for([s], config={"retention_days": 90, "purge_days": 30})
        self.assertIn('id="retDays" type="number" min="0" placeholder="off"\n          value="90"',
                     html)
        self.assertIn('id="purgeDays" type="number" min="0" placeholder="off"\n          value="30"',
                     html)

    def test_retention_inputs_blank_when_off(self):
        s = make_session([user_msg("hi")], session_id="ui12")
        html = _index_html_for([s], config={"retention_days": None, "purge_days": None})
        self.assertIn('id="retDays" type="number" min="0" placeholder="off"\n          value=""', html)
        self.assertIn('id="purgeDays" type="number" min="0" placeholder="off"\n          value=""', html)

    def test_defaults_to_no_trash_no_config_without_crashing(self):
        # write_site's default (trash=None, config=None) path — most tests in
        # other files call write_site without ever mentioning trash/config.
        s = make_session([user_msg("hi")], session_id="ui9")
        html = _index_html_for([s])
        self.assertIn("Trash is empty.", html)
        self.assertIn("auto-trash off", html)


class TestPanelHtmlStaysWellFormed(unittest.TestCase):
    def test_page_with_trash_and_settings_parses(self):
        s = make_session([user_msg("hi")], session_id="ui10")
        trash = {"g": {"deleted_at": "2026-01-01T00:00:00", "reason": "manual",
                       "fields": {"title": "T", "project_name": "P"}}}
        with tempfile.TemporaryDirectory() as out:
            cd.write_site([s], out, trash=trash, config={"retention_days": 90, "purge_days": 30})
            with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                html = fh.read()
            _Parse().feed(html)  # raises on gross malformed markup


class TestModalKeyboardAndTapTargetGuards(unittest.TestCase):
    # Two LOW findings from adversarial review, pinned so they can't regress.

    def test_keydown_handler_is_modal_aware(self):
        js = cd._INDEX_JS
        m = re.search(r"document\.addEventListener\('keydown', e=>\{(.*?)\n\}\);", js, re.S)
        self.assertIsNotNone(m, "keydown handler not found")
        body = m.group(1)
        self.assertIn("cmdDialog", body,
                     "keydown handler must check whether the cmd dialog is open")
        self.assertIn("settingsPanel", body,
                     "keydown handler must check whether the settings panel is open")
        self.assertIn("Escape", body)

    def test_card_del_is_not_a_live_tap_target_when_invisible(self):
        css = cd._CSS
        rule = re.search(r"\.card-del\{([^}]*)\}", css)
        self.assertIsNotNone(rule, ".card-del base rule not found")
        self.assertIn("opacity:0", rule.group(1))
        self.assertIn("pointer-events:none", rule.group(1),
                     "an opacity:0 control must also disable pointer events, or it's a hidden "
                     "but still-clickable tap target on touch/no-hover")
        hover_rule = re.search(r"\.card:hover \.card-del,\.card:focus-within \.card-del\{([^}]*)\}", css)
        self.assertIsNotNone(hover_rule, "hover/focus-within reveal rule not found")
        self.assertIn("pointer-events:auto", hover_rule.group(1))


if __name__ == "__main__":
    unittest.main()
