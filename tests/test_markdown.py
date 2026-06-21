"""Markdown rendering — correctness for the detail cases, plus a TERMINATION
guard. The parser has hung before (the `#`-without-space infinite loop), and a
pure-Python infinite loop can't be interrupted by a watchdog thread, so the
pathological corpus runs in a subprocess with a hard timeout: a real hang fails
one test cleanly instead of freezing the suite.
"""
import subprocess
import sys
import unittest

from fixtures import cd, ROOT

R = cd._render_text_body


class TestMarkdownCorrectness(unittest.TestCase):
    def test_headings(self):
        self.assertIn("<h1>Title</h1>", R("# Title"))
        self.assertIn("<h3>Sub</h3>", R("### Sub"))

    def test_hash_without_space_is_not_a_heading(self):
        # The exact shape that caused the infinite loop — now a paragraph.
        out = R("#NoSpace")
        self.assertNotIn("<h1>", out)
        self.assertIn("#NoSpace", out)

    def test_inline_emphasis_and_code(self):
        out = R("**bold** and *italic* and `code`")
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn("<em>italic</em>", out)
        self.assertIn("<code>code</code>", out)

    def test_link(self):
        out = R("see [docs](https://example.com)")
        self.assertIn('<a href="https://example.com"', out)
        self.assertIn(">docs</a>", out)

    def test_unordered_list(self):
        out = R("- one\n- two")
        self.assertIn("<ul>", out)
        self.assertEqual(out.count("<li>"), 2)

    def test_ordered_list(self):
        out = R("1. one\n2. two")
        self.assertIn("<ol>", out)

    def test_table(self):
        out = R("| a | b |\n| - | - |\n| 1 | 2 |")
        self.assertIn("<table>", out)
        self.assertIn("<th>a</th>", out)
        self.assertIn("<td>1</td>", out)

    def test_blockquote(self):
        out = R("> quoted line")
        self.assertIn("<blockquote>", out)
        self.assertIn("quoted line", out)

    def test_fenced_code_block(self):
        out = R("```python\nprint(1)\n```")
        self.assertIn('<pre class="code"><code class="language-python">', out)
        self.assertIn("print(1)", out)

    def test_html_is_escaped(self):
        out = R("a <script>alert(1)</script> tag")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_diff_lines_red_green(self):
        out = cd._diff_lines("alpha\nbeta", "alpha\ngamma")
        self.assertIn('class="diff"', out)
        self.assertIn("d-del", out)
        self.assertIn("d-add", out)


# Inputs that have hung or could hang a naive parser. Each must terminate and
# return a string.
PATHOLOGICAL = [
    "",
    "#",
    "#NoSpace",
    "######still no space",
    "```",                       # unterminated fence
    "```python\nno close",
    ">",
    ">>>nested\n>>still",
    "> > > deep\n> > deep\n> shallow",
    "|",
    "|a|b|\n|-|-|",              # header + separator, no body rows
    "| a | b |\n| - | - |",
    "> ```\n> code in quote\n> ```",
    "- " * 2000,
    "-\n" * 1000,
    "\n" * 1000,
    "*" * 5000,
    "_" * 5000,
]


class TestMarkdownTermination(unittest.TestCase):
    def _render_in_subprocess(self, text: str) -> None:
        prog = (
            "import sys;"
            f"sys.path.insert(0, {ROOT!r});"
            "import chats_dashboard as cd;"
            "r = cd._render_text_body(sys.stdin.read());"
            "assert isinstance(r, str)"
        )
        try:
            subprocess.run([sys.executable, "-c", prog], input=text, text=True,
                           capture_output=True, timeout=10, check=True)
        except subprocess.TimeoutExpired:
            self.fail(f"_render_text_body hung on input: {text!r:.80}")
        except subprocess.CalledProcessError as exc:
            self.fail(f"_render_text_body errored on {text!r:.80}: {exc.stderr}")

    def test_pathological_inputs_terminate(self):
        for text in PATHOLOGICAL:
            with self.subTest(text=text[:40]):
                self._render_in_subprocess(text)


if __name__ == "__main__":
    unittest.main()
