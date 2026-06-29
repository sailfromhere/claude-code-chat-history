#!/usr/bin/env python3
"""
chats_dashboard.py — build a static HTML dashboard to browse Claude Code chat history.

Scans ~/.claude/projects/*/*.jsonl, groups sessions (merging resumed sessions that
share a sessionId), and renders a self-contained static site to
~/.claude/history-dashboard/:

    index.html            project sidebar + searchable session list
    sessions/<id>.html    full transcript for one session

Navigation is plain <a href> links (no fetch/XHR) so it works when opened directly
over the file:// protocol — browsers block fetch() over file:// for CORS reasons.

Usage:
    python3 chats_dashboard.py [--open] [--projects-dir DIR] [--output-dir DIR]

stdlib-only; no third-party dependencies.
"""
from __future__ import annotations

import argparse
import difflib
import glob
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

HOME = os.path.expanduser("~")
DEFAULT_PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
DEFAULT_OUTPUT_DIR = os.path.join(HOME, ".claude", "history-dashboard")

# Fold a renamed/moved project's old cwd into its current one so sessions that
# ran before a rename don't show up as a separate project. Maps an old absolute
# cwd (as recorded in the .jsonl `cwd` field) → the canonical current cwd.
# Trailing slashes are ignored. Add a line here whenever you rename a project.
PROJECT_ALIASES = {
    "/Users/kevinyu/claude/ticket_snagger": "/Users/kevinyu/claude/ticket_snagger_recreation_gov",
}

# Cap per-block content so a 2 MB session doesn't produce an enormous DOM.
# Full content always remains in the original .jsonl.
MAX_BLOCK_CHARS = 8000
MAX_RESULT_LINES = 40   # cap tool-result height (even when tools are shown)
MAX_DIFF_LINES = 60     # cap Edit/Write diff height
SNIPPET_CHARS = 200
TITLE_FALLBACK_CHARS = 70
ACTIVE_WINDOW_MIN = 15  # a session is "active" if its last activity is within this many minutes

# Public API list prices, USD per million tokens: (input, output).
# Cache read ≈ 0.1× input; cache write (5-min) ≈ 1.25× input.
PRICING = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_READ_MULT = 0.10
CACHE_WRITE_MULT = 1.25

# LLM titling via the local `claude` CLI.
TITLE_MODEL = "haiku"
TITLE_CACHE = ".title-cache.json"
TITLE_PROMPT_VERSION = 2  # bump to invalidate the cache when the prompt changes
TITLE_CONDENSE_HEAD = 6000
TITLE_CONDENSE_TAIL = 2000
TITLE_TIMEOUT = 90
TITLE_WORKERS = 4
TITLE_INSTRUCTIONS = (
    "You label a Claude Code session transcript for a history dashboard. "
    "Read it and reply with EXACTLY one line, no preamble, no quotes:\n"
    "<concise title, max 8 words> || <one-sentence summary, max 20 words>\n"
    "The title should capture what the whole session was about (the goal/outcome), "
    "not just the first message. Transcript follows:\n\n"
)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Turn:
    """One rendered conversation entry (a user or assistant message)."""
    role: str          # "user" | "assistant" | "command"
    ts: str            # ISO timestamp
    html: str          # rendered inner HTML
    tools_only: bool = False  # turn has only tool I/O → hide whole row when tools hidden
    submitted: str = ""  # provenance of a "user" turn: "sdk" if Claude/a tool sent it (not typed)


@dataclass
class Session:
    session_id: str
    project_path: str = ""
    entries: list[dict] = field(default_factory=list)  # raw user/assistant entries
    ai_title: str | None = None

    # computed
    first_ts: str = ""
    last_ts: str = ""
    n_user: int = 0
    n_asst: int = 0
    snippet: str = ""
    title: str = ""
    summary: str = ""  # one-line LLM summary, when titling is enabled
    initiated_by_sdk: bool = False  # session's first prompt was SDK/tool-submitted, not typed

    # usage rollup
    tok_in: int = 0
    tok_out: int = 0
    tok_cache_read: int = 0
    tok_cache_write: int = 0
    cost_usd: float = 0.0
    cost_complete: bool = True  # False if any model's price is unknown

    @property
    def project_name(self) -> str:
        path = self.project_path.rstrip("/")
        path = PROJECT_ALIASES.get(path, path)
        return os.path.basename(path) or "(unknown)"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def load_sessions(projects_dir: str) -> list[Session]:
    """Read every .jsonl under projects_dir, grouping entries by sessionId."""
    sessions: dict[str, Session] = {}
    titles: dict[str, str] = {}

    for path in glob.glob(os.path.join(projects_dir, "*", "*.jsonl")):
        for line in _iter_jsonl(path):
            etype = line.get("type")
            if etype == "ai-title":
                sid = line.get("sessionId")
                t = line.get("aiTitle")
                if sid and t:
                    titles[sid] = t
                continue
            if etype not in ("user", "assistant"):
                continue
            sid = line.get("sessionId")
            if not sid:
                continue
            s = sessions.get(sid)
            if s is None:
                s = sessions[sid] = Session(session_id=sid)
            if not s.project_path and line.get("cwd"):
                s.project_path = line["cwd"]
            s.entries.append(line)

    result: list[Session] = []
    for sid, s in sessions.items():
        if not s.entries:
            continue
        s.entries.sort(key=lambda e: e.get("timestamp") or "")
        s.ai_title = titles.get(sid)
        _compute_metadata(s)
        result.append(s)

    # Most recently active sessions first.
    result.sort(key=lambda s: s.last_ts, reverse=True)
    return result


def _iter_jsonl(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        print(f"  warning: could not read {path}: {exc}", file=sys.stderr)


def _compute_metadata(s: Session) -> None:
    timestamps = [e.get("timestamp") for e in s.entries if e.get("timestamp")]
    if timestamps:
        s.first_ts, s.last_ts = timestamps[0], timestamps[-1]

    expansions = _expansion_uuids(s.entries)
    first_command: str | None = None

    for e in s.entries:
        role = (e.get("message") or {}).get("role")
        if role != "user":
            if role == "assistant" and not _is_sentinel_assistant(e):
                s.n_asst += 1
            continue
        if _is_noise(e):
            if _is_slash_command(e):
                s.n_user += 1  # the command is the user's "prompt"
                if first_command is None:
                    first_command = _command_label(e)
            continue
        if _is_meta(e) or _is_tool_result_only(e) or e.get("uuid") in expansions:
            continue
        if s.n_user == 0:  # the first real prompt = what initiated the session
            s.initiated_by_sdk = _prompt_submitted_by(e) == "sdk"
        s.n_user += 1
        if not s.snippet:
            text = _plain_user_text(e).strip()
            if text:
                s.snippet = _truncate(text, SNIPPET_CHARS)

    _compute_usage(s)

    # Prefer the AI title; then a human snippet; then the command issued; else untitled.
    if s.ai_title:
        s.title = s.ai_title
    elif s.snippet:
        s.title = _truncate(s.snippet, TITLE_FALLBACK_CHARS)
    elif first_command:
        s.title = first_command
        s.snippet = s.snippet or first_command
    else:
        s.title = "(untitled session)"


def _compute_usage(s: Session) -> None:
    """Sum token usage across assistant turns and estimate API-rate cost."""
    for e in s.entries:
        msg = e.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        u = msg.get("usage")
        if not isinstance(u, dict):
            continue
        i = int(u.get("input_tokens") or 0)
        o = int(u.get("output_tokens") or 0)
        cr = int(u.get("cache_read_input_tokens") or 0)
        cw = int(u.get("cache_creation_input_tokens") or 0)
        s.tok_in += i
        s.tok_out += o
        s.tok_cache_read += cr
        s.tok_cache_write += cw

        price = PRICING.get(msg.get("model"))
        if price is None:
            if msg.get("model") not in (None, "<synthetic>"):
                s.cost_complete = False
            continue
        in_rate, out_rate = price
        s.cost_usd += (
            i * in_rate
            + o * out_rate
            + cr * in_rate * CACHE_READ_MULT
            + cw * in_rate * CACHE_WRITE_MULT
        ) / 1_000_000


# --------------------------------------------------------------------------- #
# Content extraction helpers
# --------------------------------------------------------------------------- #
def _content(entry: dict):
    return (entry.get("message") or {}).get("content")


_OPEN_TAG_RE = re.compile(r"<([a-z][a-z0-9-]*)>")


def _is_wrapper_only(text: str) -> bool:
    """True if the message is composed ENTIRELY of <tag>…</tag> blocks (with only
    whitespace between/around them). Harness-injected entries (slash-command
    machinery, bash I/O, task notifications, system reminders, and any future
    wrapper tag) are always wrapper-only; a human message that merely quotes a
    tag has prose outside it and is not. O(n), no backtracking."""
    t = text.strip()
    if not t.startswith("<"):
        return False
    pos, n = 0, len(t)
    while pos < n:
        while pos < n and t[pos].isspace():
            pos += 1
        if pos >= n:
            break
        m = _OPEN_TAG_RE.match(t, pos)
        if not m:
            return False
        close = f"</{m.group(1)}>"
        idx = t.find(close, m.end())
        if idx == -1:
            return False
        pos = idx + len(close)
    return True


def _is_noise(entry: dict) -> bool:
    """A user entry that is harness-injected wrapper-tag content, not human text."""
    return _is_wrapper_only(_plain_user_text(entry))


def _is_meta(entry: dict) -> bool:
    """Harness-injected meta content (skill bodies, command expansions, caveats).
    `isMeta: True` reliably marks non-human user entries."""
    return entry.get("isMeta") is True


def _prompt_submitted_by(entry: dict) -> str:
    """Provenance of a user prompt: "" if the human typed it, "sdk" if Claude or a
    tool submitted it programmatically (a headless `claude -p` subprocess).
    `entrypoint:"sdk-cli"` is the robust cross-version flag; `promptSource:"sdk"`
    corroborates it on newer CLIs. (`promptSource:"typed"` = human; absent on older
    CLIs ≤2.1.159, which predate SDK subprocesses anyway — so entrypoint suffices.)"""
    if entry.get("entrypoint") == "sdk-cli" or entry.get("promptSource") == "sdk":
        return "sdk"
    return ""


# Synthetic assistant turns the harness records when no model reply is needed
# (e.g. right after /exit or at a resume seam). Not real content.
_ASSISTANT_SENTINELS = {"No response requested."}


def _is_sentinel_assistant(entry: dict) -> bool:
    c = _content(entry)
    if not isinstance(c, list):
        return False
    if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in c):
        return False
    texts = [b.get("text", "").strip() for b in c
             if isinstance(b, dict) and b.get("type") == "text"]
    texts = [t for t in texts if t]
    return bool(texts) and all(t in _ASSISTANT_SENTINELS for t in texts)


def _is_slash_command(entry: dict) -> bool:
    """A user entry that issued a slash command (has a <command-name> marker)."""
    return "<command-name>" in _plain_user_text(entry)


def _command_label(entry: dict) -> str | None:
    """The slash-command name (e.g. '/init'), or None if this isn't one."""
    m = re.search(r"<command-name>([^<]*)</command-name>", _plain_user_text(entry))
    return m.group(1).strip() if (m and m.group(1).strip()) else None


def _command_stdout(entry: dict) -> str | None:
    """Captured output of a local `!`/slash command, full text (not truncated)."""
    m = re.search(r"<local-command-stdout>(.*?)</local-command-stdout>",
                  _plain_user_text(entry), re.S)
    return m.group(1).strip() if (m and m.group(1).strip()) else None


def _bash_input(entry: dict) -> str | None:
    """A shell command the user ran via the `!` prefix."""
    m = re.search(r"<bash-input>(.*?)</bash-input>", _plain_user_text(entry), re.S)
    return m.group(1).strip() if (m and m.group(1).strip()) else None


def _bash_output(entry: dict) -> tuple[str, str]:
    """(stdout, stderr) for a `!` command. Source is HTML-entity-encoded; un-escape it."""
    txt = _plain_user_text(entry)
    out = re.search(r"<bash-stdout>(.*?)</bash-stdout>", txt, re.S)
    err = re.search(r"<bash-stderr>(.*?)</bash-stderr>", txt, re.S)
    o = html.unescape(out.group(1)).strip() if out else ""
    e = html.unescape(err.group(1)).strip() if err else ""
    return o, e


# Wrapper tags that already have dedicated renderers (command/bash/local-command).
_HANDLED_SYS_TAGS = {
    "command-name", "command-message", "command-args", "command-stdout",
    "local-command-caveat", "local-command-stdout",
    "bash-input", "bash-stdout", "bash-stderr",
}


def _tagval(body: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", body, re.S)
    return html.unescape(m.group(1).strip()) if m else ""


def _render_system_extra(text: str) -> str:
    """Render harness-injected wrapper tags that lack a dedicated renderer:
    task-notification (status chip), system-reminder (muted note), and any
    unknown tag (generic muted block). Keeps future tags from leaking as text."""
    out: list[str] = []
    for m in re.finditer(r"<task-notification>(.*?)</task-notification>", text, re.S):
        body = m.group(1)
        status, summ = _tagval(body, "status"), _tagval(body, "summary")
        err = status in ("failed", "error", "timeout")
        label = f"background task {status}" if status else "background task"
        detail = f" — {_esc(summ)}" if summ else ""
        out.append(f'<div class="sys-event{" error" if err else ""}">'
                   f'<span class="sys-icon">⚙</span> <b>{_esc(label)}</b>{detail}</div>')
    for m in re.finditer(r"<system-reminder>(.*?)</system-reminder>", text, re.S):
        body = _esc(_clip(html.unescape(m.group(1).strip())))
        out.append('<details class="sys-event"><summary><span class="sys-icon">⚙</span> '
                   f'system reminder</summary><div class="sys-body">{body}</div></details>')
    for m in re.finditer(r"<([a-z][a-z0-9-]*)>(.*?)</\1>", text, re.S):
        tag = m.group(1)
        if tag in _HANDLED_SYS_TAGS or tag in ("task-notification", "system-reminder"):
            continue
        inner = _clip(html.unescape(m.group(2).strip()))
        head = f'<span class="sys-icon">⚙</span> {_esc(tag)}'
        if inner:
            out.append(f'<details class="sys-event"><summary>{head}</summary>'
                       f'<div class="sys-body">{_esc(inner)}</div></details>')
        else:
            out.append(f'<div class="sys-event">{head}</div>')
    return "".join(out)


def _render_meta(entry: dict) -> str:
    """Collapsed muted block for injected meta content (skill bodies, etc.)."""
    txt = _plain_user_text(entry).strip()
    m = re.match(r"Base directory for this skill:\s*(\S+)", txt)
    if m:
        name = m.group(1).rstrip("/").split("/")[-1]
        head = f'<span class="sys-icon">⚙</span> loaded skill: <b>{_esc(name)}</b>'
    else:
        head = f'<span class="sys-icon">⚙</span> injected context'
    body = _esc(_clip(_cap_lines(txt)))
    return (f'<details class="sys-event"><summary>{head}</summary>'
            f'<div class="sys-body">{body}</div></details>')


def _expansion_uuids(entries: list[dict]) -> set[str]:
    """
    A slash command stores two entries: the <command-name> marker, then a
    separate user-text entry holding the prompt that command expands to.
    Return the uuids of those expansion entries so they aren't mistaken for
    something the human typed.
    """
    out: set[str] = set()
    for i, e in enumerate(entries):
        if (e.get("message") or {}).get("role") != "user" or not _is_slash_command(e):
            continue
        nxt = entries[i + 1] if i + 1 < len(entries) else None
        if (nxt and (nxt.get("message") or {}).get("role") == "user"
                and not _is_noise(nxt) and not _is_tool_result_only(nxt)
                and _plain_user_text(nxt).strip()):
            uid = nxt.get("uuid")
            if uid:
                out.add(uid)
    return out


def _result_map(entries: list[dict]) -> dict[str, dict]:
    """Map tool_use_id → tool_result block, so results pair with their call."""
    out: dict[str, dict] = {}
    for e in entries:
        c = _content(e)
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                    out[b["tool_use_id"]] = b
    return out


def _is_tool_result_only(entry: dict) -> bool:
    c = _content(entry)
    if not isinstance(c, list):
        return False
    kinds = {b.get("type") for b in c if isinstance(b, dict)}
    return bool(kinds) and kinds <= {"tool_result"}


# Terminal control sequences (ANSI/CSI) leak into captured command stdout; the
# ESC byte is non-printing, so without stripping you see junk like "[1m…[22m".
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _plain_user_text(entry: dict) -> str:
    """Human-typed text from a user entry (string content or text blocks)."""
    c = _content(entry)
    if isinstance(c, str):
        return _strip_ansi(c)
    if isinstance(c, list):
        parts = [b.get("text", "") for b in c
                 if isinstance(b, dict) and b.get("type") == "text"]
        return _strip_ansi("\n".join(p for p in parts if p))
    return ""


def _tool_result_text(content) -> str:
    """tool_result.content can be a string or a list of blocks."""
    if isinstance(content, str):
        return _strip_ansi(content)
    if isinstance(content, list):
        out = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                out.append(b.get("text", ""))
            elif b.get("type") == "tool_reference":
                out.append(f"[tool_reference: {b.get('name', '')}]")
            else:
                out.append(f"[{b.get('type')}]")
        return _strip_ansi("\n".join(out))
    return ""


def _truncate(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _esc(s: str) -> str:
    return html.escape(s or "")


def _hue(name: str) -> int:
    """Deterministic 0–359 hue from a project name — stable, no manual color picking."""
    return int(hashlib.sha1(name.encode("utf-8")).hexdigest(), 16) % 360


def _inline(text: str) -> str:
    """Inline markdown on a single line: code, bold, italic, links (HTML-escaped)."""
    text = _esc(text)
    codes: list[str] = []

    def stash(m):
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)                       # inline code (protected)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                  r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)  # bold
    text = re.sub(r"(?<!\w)__([^_]+)__(?!\w)", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*\s][^*]*?)\*", r"<em>\1</em>", text)       # italic
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<em>\1</em>", text)   # italic (word-bounded)
    text = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _md_table(rows: list[str]) -> str:
    def cells(line):
        line = line.strip().strip("|")
        return [c.strip() for c in line.split("|")]
    head = cells(rows[0])
    body = [cells(r) for r in rows[2:]]
    out = ["<table><thead><tr>"]
    out += [f"<th>{_inline(c)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _render_text_body(text: str) -> str:
    """Render markdown (headings, lists, tables, blockquotes, fenced code, inline) to HTML."""
    text = _strip_ansi(text)
    lines = text.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        s = line.strip()

        if s.startswith("```"):                                    # fenced code
            lang = s[3:].strip().split()[0] if s[3:].strip() else ""
            buf = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1
            cls = f' class="language-{_esc(lang)}"' if lang else ""
            out.append(f'<pre class="code"><code{cls}>{_esc(chr(10).join(buf))}</code></pre>')
            continue

        if not s:                                                  # blank
            i += 1; continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):                 # hr
            out.append("<hr>"); i += 1; continue

        m = re.match(r"^(#{1,6})\s+(.*)$", s)                      # heading
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>"); i += 1; continue

        if "|" in line and i + 1 < n and re.match(r"^\s*\|?[\s:|-]*-[\s:|-]*$", lines[i + 1]):
            tbl = [line, lines[i + 1]]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                tbl.append(lines[i]); i += 1
            out.append(_md_table(tbl)); continue

        if s.startswith(">"):                                      # blockquote
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i])); i += 1
            out.append(f"<blockquote>{_render_text_body(chr(10).join(buf))}</blockquote>")
            continue

        lm = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)        # list
        if lm:
            ordered = lm.group(2).endswith(".")
            tag = "ol" if ordered else "ul"
            items = []
            while i < n:
                im = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", lines[i])
                if not im:
                    break
                items.append(f"<li>{_inline(im.group(3))}</li>"); i += 1
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue

        para = [s]                                                 # paragraph (consume ≥1 line)
        i += 1
        while i < n and lines[i].strip() and not _para_breaks(lines[i], lines[i + 1] if i + 1 < n else ""):
            para.append(lines[i].strip()); i += 1
        out.append(f'<p>{"<br>".join(_inline(p) for p in para)}</p>')
    return "\n".join(out)


def _para_breaks(line: str, nxt: str) -> bool:
    """True if `line` starts a new block (so the current paragraph should end)."""
    s = line.strip()
    return bool(
        s.startswith(("```", ">", "#"))
        or re.match(r"^(\s*)([-*+]|\d+\.)\s+", line)
        or re.match(r"^(-{3,}|\*{3,}|_{3,})$", s)
        or ("|" in line and re.match(r"^\s*\|?[\s:|-]*-[\s:|-]*$", nxt))
    )


def _diff_row(cls: str, old: str, new: str, code: str) -> str:
    return (f'<div class="d-line {cls}"><span class="d-old">{old}</span>'
            f'<span class="d-new">{new}</span><span class="d-code">{_esc(code)}</span></div>')


def _cap_rows(rows: list[str], n: int = MAX_DIFF_LINES) -> str:
    if len(rows) <= n:
        return "".join(rows)
    note = _diff_row("d-ctx", "", "", f"[+{len(rows) - n} more lines]")
    return "".join(rows[:n]) + note


def _diff_lines(old: str, new: str) -> str:
    """A red/green unified diff with an old/new line-number gutter."""
    rows = []
    oldn = newn = 0
    for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2):
        if ln.startswith(("---", "+++")):
            continue
        if ln.startswith("@@"):
            m = re.search(r"-(\d+)(?:,\d+)? \+(\d+)", ln)
            if m:
                oldn, newn = int(m.group(1)), int(m.group(2))
            rows.append(_diff_row("d-hunk", "", "", ln))
        elif ln.startswith("+"):
            rows.append(_diff_row("d-add", "", str(newn), ln[1:])); newn += 1
        elif ln.startswith("-"):
            rows.append(_diff_row("d-del", str(oldn), "", ln[1:])); oldn += 1
        else:
            rows.append(_diff_row("d-ctx", str(oldn), str(newn), ln[1:] if ln[:1] == " " else ln))
            oldn += 1; newn += 1
    return f'<div class="diff">{_cap_rows(rows)}</div>'


_PLANS_DIR = os.path.join(HOME, ".claude", "plans")


def _is_plan_write(name: str, inp: dict) -> bool:
    """True when this Write call is writing a plan file to ~/.claude/plans/."""
    if name != "Write" or not isinstance(inp, dict):
        return False
    fp = inp.get("file_path", "")
    return os.path.normpath(fp).startswith(os.path.normpath(_PLANS_DIR))


def _render_plan_card(content: str, file_path: str = "", allowed_prompts: list | None = None) -> str:
    """Render a plan as an expanded markdown card (not a collapsed tool chip)."""
    path_html = (
        f'<div class="plan-path">{_esc(file_path)}</div>'
        if file_path else ""
    )
    body_html = _render_text_body(content) if content.strip() else "<em>(empty)</em>"
    approved_html = ""
    if allowed_prompts:
        items = "".join(
            f'<li>{_esc(p.get("tool",""))}: {_esc(p.get("prompt",""))}</li>'
            for p in allowed_prompts if isinstance(p, dict)
        )
        approved_html = f'<div class="plan-approved"><b>Approved actions:</b><ul>{items}</ul></div>'
    return (
        f'<div class="plan-card">'
        f'<div class="plan-header">📋 Plan</div>'
        f'{path_html}'
        f'{body_html}'
        f'{approved_html}'
        f'</div>'
    )


def _render_edit_diff(name: str, inp: dict) -> str:
    """Red/green diff view for Edit/MultiEdit/Write tool calls."""
    path = inp.get("file_path", "")
    head = f'<div class="tlabel">edit · {_esc(path)}</div>' if path else ""
    if name == "Write":
        rows = [
            _diff_row("d-add", "", str(i), l)
            for i, l in enumerate(str(inp.get("content", "")).splitlines(), 1)
        ]
        return head + f'<div class="diff">{_cap_rows(rows)}</div>'
    if name == "MultiEdit":
        edits = inp.get("edits", []) or []
        return head + "".join(
            _diff_lines(str(e.get("old_string", "")), str(e.get("new_string", "")))
            for e in edits if isinstance(e, dict)
        )
    # Edit
    return head + _diff_lines(str(inp.get("old_string", "")), str(inp.get("new_string", "")))


def _render_askq(inp: dict, result: dict | None) -> str:
    """Readable Q&A card for an AskUserQuestion call, with the chosen option marked."""
    answer = _tool_result_text(result.get("content")) if result else ""
    chosen = set(re.findall(r'="([^"]+)"', answer))
    out = ['<div class="askq">']
    for q in inp.get("questions", []) if isinstance(inp, dict) else []:
        out.append(f'<div class="askq-q">❓ {_inline(q.get("question", ""))}</div>')
        out.append('<ul class="askq-opts">')
        for opt in q.get("options", []):
            lbl = opt.get("label", "")
            sel = " chosen" if lbl in chosen else ""
            mark = "✓ " if lbl in chosen else ""
            prev = opt.get("preview")
            pv = (f'<details class="askq-prev"><summary>preview</summary>'
                  f'<pre class="code">{_esc(prev)}</pre></details>') if prev else ""
            out.append(f'<li class="askq-opt{sel}">{mark}<b>{_esc(lbl)}</b>'
                       f' — {_inline(opt.get("description", ""))}{pv}</li>')
        out.append("</ul>")
    out.append("</div>")
    return "".join(out)


def _render_tool_use(block: dict, result: dict | None) -> str:
    """One collapsible for a tool call: input plus its paired result (Claude's action)."""
    name = block.get("name", "tool")
    inp = block.get("input", {})
    summary = _tool_summary(name, inp)

    # AskUserQuestion is conversation, not tool noise → render as a visible Q&A card.
    if name == "AskUserQuestion":
        return _render_askq(inp, result)

    # Write to ~/.claude/plans/ → render as an expanded plan card.
    # ExitPlanMode also carries input.plan (old format) but we ignore it here because
    # the Write call already rendered the same content — don't show it twice.
    if _is_plan_write(name, inp):
        return _render_plan_card(
            str(inp.get("content", "")),
            file_path=inp.get("file_path", ""),
        )

    err = bool(result and result.get("is_error"))
    cls = "tool tool-use error" if err else "tool tool-use"
    flag = ' <span class="errflag">error</span>' if err else ""

    if name in ("Edit", "MultiEdit", "Write") and isinstance(inp, dict):
        request_html = _render_edit_diff(name, inp)
    else:
        body = _esc(_clip(json.dumps(inp, indent=2, ensure_ascii=False)))
        request_html = f'<div class="tlabel">request</div><pre class="code">{body}</pre>'

    parts = [
        f'<details class="{cls}">',
        f'<summary><span class="badge">▸ {_esc(name)}</span> {_esc(summary)}{flag}</summary>',
        request_html,
    ]
    if result is not None:
        out_text = _esc(_clip(_cap_lines(_tool_result_text(result.get("content")))))
        parts.append(f'<div class="tlabel">{"error" if err else "result"}</div>')
        parts.append(f'<pre class="code">{out_text}</pre>')
    parts.append("</details>")
    return "".join(parts)


def _render_orphan_result(block: dict) -> str:
    """A tool_result with no matching tool_use — render as a neutral exchange row."""
    text = _clip(_cap_lines(_tool_result_text(block.get("content"))))
    err = block.get("is_error")
    cls = "tool tool-use error" if err else "tool tool-use"
    label = "⚠ tool error" if err else "↳ tool result"
    return (
        f'<details class="{cls}">'
        f'<summary><span class="badge">{label}</span></summary>'
        f'<pre class="code">{_esc(text)}</pre></details>'
    )


def _tool_summary(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "query", "url", "prompt"):
        if inp.get(key):
            return _truncate(str(inp[key]), 100)
    return ""


def _clip(text: str) -> str:
    if len(text) > MAX_BLOCK_CHARS:
        return text[:MAX_BLOCK_CHARS] + f"\n… (truncated, {len(text)} chars total)"
    return text


def _cap_lines(text: str, n: int = MAX_RESULT_LINES) -> str:
    lines = text.split("\n")
    if len(lines) <= n:
        return text
    return "\n".join(lines[:n]) + f"\n[+{len(lines) - n} more lines]"


def _render_turns(s: Session) -> list[Turn]:
    results = _result_map(s.entries)
    consumed: set[str] = set()  # tool_use_ids already shown under their call
    expansions = _expansion_uuids(s.entries)
    entries = s.entries

    turns: list[Turn] = []
    i, n = 0, len(entries)
    while i < n:
        e = entries[i]
        role = (e.get("message") or {}).get("role")
        ts = e.get("timestamp", "")
        if role == "user":
            if e.get("uuid") in expansions:
                i += 1
                continue  # boilerplate already folded under its command
            if _is_meta(e) and not _is_noise(e):
                # Freeform injected content (skill bodies, etc.) — one collapsed chip.
                # Skip the command/bash/tag pipeline; its body isn't wrapper machinery.
                turns.append(Turn("command", ts, _render_meta(e)))
                i += 1
                continue
            if _is_noise(e):
                html_parts = ""
                label = _command_label(e)
                if label:
                    expand = _peek_expansion(entries, i, expansions)
                    html_parts = _command_html(label, expand)
                stdout = _command_stdout(e)
                if stdout:
                    html_parts += f'<div class="cmd-out">{_esc(_clip(stdout))}</div>'
                binput = _bash_input(e)
                if binput is not None:
                    html_parts += (f'<div class="bash-in"><span class="bash-prompt">$</span> '
                                   f'<code>{_esc(binput)}</code></div>')
                bo, be = _bash_output(e)
                if bo:
                    html_parts += f'<pre class="bash-out">{_esc(_clip(_cap_lines(bo)))}</pre>'
                if be:
                    html_parts += f'<pre class="bash-out bash-err">{_esc(_clip(_cap_lines(be)))}</pre>'
                html_parts += _render_system_extra(_plain_user_text(e))
                if html_parts:
                    turns.append(Turn("command", ts, html_parts))
                i += 1
                continue
            inner = _render_user_blocks(e, results, consumed)
            if inner.strip():
                turns.append(Turn("user", ts, inner, tools_only=not _has_visible_nontool(e),
                                  submitted=_prompt_submitted_by(e)))
        elif role == "assistant":
            if _is_sentinel_assistant(e):
                i += 1
                continue
            inner = _render_assistant_blocks(e, results, consumed)
            if inner.strip():
                turns.append(Turn("assistant", ts, inner, tools_only=not _has_visible_nontool(e)))
        i += 1
    return turns


def _has_visible_nontool(entry: dict) -> bool:
    """Does the entry render anything that survives 'hide tool calls'? (text, an
    AskUserQuestion card, or a plan card — all stay visible; plain tool_use/result don't.)"""
    c = _content(entry)
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                return True
            if b.get("type") == "tool_use":
                name = b.get("name", "")
                inp = b.get("input", {})
                if name == "AskUserQuestion":
                    return True
                if _is_plan_write(name, inp):
                    return True
    return False


def _peek_expansion(entries: list[dict], i: int, expansions: set[str]) -> str:
    nxt = entries[i + 1] if i + 1 < len(entries) else None
    if nxt and nxt.get("uuid") in expansions:
        return _plain_user_text(nxt).strip()
    return ""


def _command_html(label: str, expansion: str) -> str:
    out = f'<span class="cmd">⌘ {_esc(label)}</span>'
    if expansion:
        out += (
            '<details class="tool cmd-expand"><summary>'
            '<span class="badge">prompt</span> expanded command prompt</summary>'
            f'{_render_text_body(expansion)}</details>'
        )
    return out


def _render_user_blocks(entry: dict, results: dict, consumed: set) -> str:
    c = _content(entry)
    if isinstance(c, str):
        return _render_text_body(c)
    out: list[str] = []
    if isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                out.append(_render_text_body(b.get("text", "")))
            elif t == "tool_result":
                # Orphan results only; matched ones render under their tool_use.
                tid = b.get("tool_use_id")
                if tid not in consumed:
                    out.append(_render_orphan_result(b))
    return "\n".join(out)


def _render_assistant_blocks(entry: dict, results: dict, consumed: set) -> str:
    c = _content(entry)
    out: list[str] = []
    if isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                out.append(_render_text_body(b.get("text", "")))
            elif t == "tool_use":
                res = results.get(b.get("id"))
                if res is not None:
                    consumed.add(b.get("id"))
                out.append(_render_tool_use(b, res))
            # thinking: skipped intentionally
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def _fmt(ts: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime(fmt)
    except ValueError:
        return ts


def _fmt_date(ts: str) -> str:
    return _fmt(ts, "%Y-%m-%d")


def _tally_str(tokens: int, cost: float, complete: bool) -> str:
    """Compact scoped tally: '~$1.23 · 4.5M tok' (+ when any model is unpriced)."""
    return f"~${cost:,.2f}{'' if complete else '+'} · {_fmt_tokens(int(tokens))} tok"


def _session_tokens(s: "Session") -> int:
    return s.tok_in + s.tok_out + s.tok_cache_read + s.tok_cache_write


def _minutes_since(ts: str) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None
    return (datetime.now().astimezone() - dt).total_seconds() / 60


def _is_active(ts: str) -> bool:
    """Recently active — its last activity is within ACTIVE_WINDOW_MIN of now."""
    m = _minutes_since(ts)
    return m is not None and -1 <= m <= ACTIVE_WINDOW_MIN


def _active_label(ts: str) -> str:
    """Relative label, used ONLY for the active session (dates stay absolute elsewhere)."""
    m = _minutes_since(ts)
    if m is None or m < 1:
        return "active now"
    return f"active {int(m)}m ago"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _usage_html(s: Session) -> str:
    if not (s.tok_in or s.tok_out or s.tok_cache_read or s.tok_cache_write):
        return ""
    cost = f"${s.cost_usd:,.2f}" + ("" if s.cost_complete else "+")
    tip = (f"input {s.tok_in:,} · output {s.tok_out:,} · "
           f"cache read {s.tok_cache_read:,} · cache write {s.tok_cache_write:,} tokens. "
           f"Estimated at public API list prices"
           + ("" if s.cost_complete else "; some models unpriced (+)") + ".")
    return (f'<span class="usage" data-tip="{_esc(tip)}">'
            f'{_fmt_tokens(s.tok_in)} in · {_fmt_tokens(s.tok_out)} out · '
            f'{_fmt_tokens(s.tok_cache_read)} cache · ~{cost}</span>')


def _date_group(ts: str) -> str:
    """Bucket a session by recency for the grouped session list."""
    if not ts:
        return "Older"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return "Older"
    d = (datetime.now().astimezone().date() - dt.date()).days
    if d <= 0:
        return "Today"
    if d == 1:
        return "Yesterday"
    if d <= 7:
        return "Previous 7 days"
    if d <= 30:
        return "Previous 30 days"
    return "Older"


# --------------------------------------------------------------------------- #
# Page generation
# --------------------------------------------------------------------------- #
def _atomic_write_text(path: str, content: str) -> None:
    """Write via a temp file + os.replace so a crash mid-write can't leave a
    half-written page (os.replace is atomic on the same filesystem)."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def write_site(sessions: list[Session], output_dir: str) -> None:
    sessions_dir = os.path.join(output_dir, "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    _write_assets(output_dir)

    for s in sessions:
        _atomic_write_text(os.path.join(sessions_dir, f"{s.session_id}.html"),
                           _session_page(s))
        _atomic_write_text(os.path.join(sessions_dir, f"{s.session_id}.md"),
                           _session_markdown(s))

    # Write index last (atomically) so it never references a half-written page.
    _atomic_write_text(os.path.join(output_dir, "index.html"), _index_page(sessions))


def _write_assets(output_dir: str) -> None:
    """Copy the vendored highlight.js + build a light/dark theme into <output>/assets/."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    dst = os.path.join(output_dir, "assets")
    os.makedirs(dst, exist_ok=True)
    js = os.path.join(src, "highlight.min.js")
    if os.path.isfile(js):
        shutil.copyfile(js, os.path.join(dst, "highlight.min.js"))
    try:
        with open(os.path.join(src, "hl-light.css"), encoding="utf-8") as fh:
            light = fh.read()
        with open(os.path.join(src, "hl-dark.css"), encoding="utf-8") as fh:
            dark = fh.read()
        combined = light + "\n@media (prefers-color-scheme:dark){\n" + dark + "\n}\n"
        with open(os.path.join(dst, "hl.css"), "w", encoding="utf-8") as fh:
            fh.write(combined)
    except OSError:
        pass


def _index_page(sessions: list[Session]) -> str:
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")

    # Project list with counts + token/cost rollup, most-recent first (already sorted).
    projects: list[str] = []
    counts: dict[str, int] = {}
    p_tok: dict[str, int] = {}
    p_cost: dict[str, float] = {}
    p_complete: dict[str, bool] = {}
    tot_tok = tot_cost = 0.0
    tot_complete = True
    for s in sessions:
        p = s.project_name
        toks = _session_tokens(s)
        counts[p] = counts.get(p, 0) + 1
        p_tok[p] = p_tok.get(p, 0) + toks
        p_cost[p] = p_cost.get(p, 0.0) + s.cost_usd
        p_complete[p] = p_complete.get(p, True) and s.cost_complete
        tot_tok += toks
        tot_cost += s.cost_usd
        tot_complete = tot_complete and s.cost_complete
        if p not in projects:
            projects.append(p)

    sidebar = [
        f'<button class="proj active" data-project="" onclick="filterProject(this,\'\')">'
        f'<span class="proj-name">All projects</span>'
        f'<span class="count">{len(sessions)}</span>'
        f'<span class="pcost">{_tally_str(int(tot_tok), tot_cost, tot_complete)}</span></button>'
    ]
    for p in projects:
        sidebar.append(
            f'<button class="proj" style="--phue:{_hue(p)}" data-project="{_esc(p)}" '
            f'onclick="filterProject(this,\'{_esc(p)}\')">'
            f'<span class="proj-dot"></span><span class="proj-name">{_esc(p)}</span>'
            f'<span class="count">{counts[p]}</span>'
            f'<span class="pcost">{_tally_str(p_tok[p], p_cost[p], p_complete[p])}</span></button>'
        )

    cards = []
    cur_group = None
    for idx, s in enumerate(sessions):
        group = _date_group(s.last_ts)
        if group != cur_group:
            cur_group = group
            cards.append(f'<div class="group-header"><span>{_esc(group)}</span>'
                         f'<span class="group-tally"></span></div>')
        blurb = s.summary or s.snippet
        haystack = _esc(f"{s.title} {s.project_name} {blurb}".lower())
        href = f"sessions/{_esc(s.session_id)}.html"
        active = _is_active(s.last_ts)
        badge = (f'<span class="active-badge" data-tip="{_esc(_active_label(s.last_ts))}">'
                 f'● live</span>') if active else ""
        toks = _session_tokens(s)
        tally = f' · {_tally_str(toks, s.cost_usd, s.cost_complete)}' if toks else ""
        sdk_pill = ('<span class="sdk-pill" data-tip="Submitted by Claude or a tool '
                    '(a headless claude -p run), not typed by you">⚙ SDK</span>'
                    if s.initiated_by_sdk else "")
        cards.append(
            f'<a class="card{" active" if idx == 0 else ""}" href="{href}" target="content" '
            f'onclick="selectCard(this)" style="--phue:{_hue(s.project_name)}" '
            f'data-project="{_esc(s.project_name)}" data-search="{haystack}" '
            f'data-cost="{s.cost_usd:.4f}" data-tok="{toks}" '
            f'data-inc="{0 if s.cost_complete else 1}">'
            f'<div class="card-top"><span class="proj-badge">{_esc(s.project_name)}</span>'
            f'{sdk_pill}'
            f'<span class="date">{badge}{_esc(_fmt_date(s.last_ts))}</span></div>'
            f'<div class="title">{_esc(s.title)}</div>'
            f'<div class="snippet">{_esc(blurb)}</div>'
            f'<div class="meta">{s.n_user} prompts · {s.n_asst} replies{tally}</div>'
            f'</a>'
        )

    if sessions:
        first_src = f"sessions/{_esc(sessions[0].session_id)}.html"
        right = f'<iframe class="content" name="content" src="{first_src}"></iframe>'
        empty = ""
    else:
        right = '<div class="content placeholder">No sessions yet.</div>'
        empty = ('<div class="empty">No chat history found under '
                 '<code>~/.claude/projects</code>.</div>')

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code — chat history</title>
<style>{_CSS}</style>
</head><body>
<div class="layout" id="layout">
  <nav class="projcol">
    <div class="proj-head">
      <h1>Claude Code History</h1>
      <button id="searchBtn" class="search-btn" onclick="toggleSearch()" title="Search (/)">🔍</button>
    </div>
    <input id="search" type="search" placeholder="Search…" oninput="runSearch()" hidden>
    <div class="proj-list">{''.join(sidebar)}</div>
    <div class="gen">generated {generated}</div>
  </nav>
  <div class="resizer" data-pane="proj" title="Drag to resize"></div>
  <div class="sessioncol">
    <div class="cardlist" id="cards">{''.join(cards)}{empty}
      <div id="noresults" class="empty" hidden>No sessions match your search.</div>
    </div>
  </div>
  <div class="resizer" data-pane="sess" title="Drag to resize"></div>
  {right}
</div>
<script>{_INDEX_JS}</script>
</body></html>"""


def _session_page(s: Session) -> str:
    turns = _render_turns(s)
    rows = []
    for t in turns:
        if t.role == "command":
            rows.append(f'<div class="row command">{t.html}</div>')
            continue
        who = "You" if t.role == "user" else "Claude"
        extra = " tools-only" if t.tools_only else ""
        badge = ""
        if t.submitted == "sdk":
            who = "Claude (SDK)"
            badge = ('<span class="who-badge" data-tip="Submitted programmatically by '
                     'Claude or a tool (a headless claude -p subprocess), not typed by you">'
                     '⚙ submitted</span>')
        rows.append(
            f'<div class="row {t.role}{extra}">'
            f'<div class="who">{who}{badge}<span class="ts">{_esc(_fmt(t.ts))}</span></div>'
            f'<div class="bubble">{t.html}</div></div>'
        )

    md_name = f"{s.session_id}.md"
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(s.title)} — Claude Code</title>
<link rel="stylesheet" href="../assets/hl.css">
<style>{_CSS}</style>
</head><body>
<header class="session-header" style="--phue:{_hue(s.project_name)}">
  <div class="hdr-title">
    <a class="back" href="../index.html" target="_top" id="backlink" hidden>←</a>
    <h1>{_esc(s.title)}</h1>
    <div class="hdr-actions">
      <label class="tool-toggle"><input type="checkbox" id="toolToggle" onchange="toggleTools(this.checked)"> Tools</label>
      <a class="export" href="{md_name}" download="{md_name}" target="_blank">⬇ .md</a>
    </div>
  </div>
  <div class="hmeta">
    <span class="proj-badge">{_esc(s.project_name)}</span>
    <span class="date">{_esc(_fmt_date(s.first_ts))} → {_esc(_fmt_date(s.last_ts))}</span>
    <span class="date">{s.n_user}p · {s.n_asst}r</span>
    {_usage_html(s)}
  </div>
</header>
<main class="transcript">{''.join(rows)}</main>
<script>
if (window.self === window.top) document.getElementById('backlink').hidden = false;
function _pref(){{ try {{ return localStorage.getItem('showTools') === '1'; }} catch (e) {{ return false; }} }}
function toggleTools(on){{
  document.body.classList.toggle('hide-tools', !on);
  try {{ localStorage.setItem('showTools', on ? '1' : '0'); }} catch (e) {{}}
  if (window.highlightVisible) highlightVisible();
}}
(function(){{
  const show = _pref();
  document.body.classList.toggle('hide-tools', !show);
  document.getElementById('toolToggle').checked = show;
}})();
</script>
<script src="../assets/highlight.min.js"></script>
<script>
// Lazy highlight: only colorize code as it scrolls into the transcript — avoids
// a synchronous pass over every block on load (the main open-time cost on big chats).
(function(){{
  if (!window.hljs) return;
  const root = document.querySelector('.transcript');
  const io = new IntersectionObserver((ents, o) => {{
    for (const e of ents) if (e.isIntersecting) {{ hljs.highlightElement(e.target); o.unobserve(e.target); }}
  }}, {{root: root, rootMargin: '300px'}});
  window.highlightVisible = () =>
    document.querySelectorAll('pre code:not(.hljs)').forEach(el => io.observe(el));
  highlightVisible();
}})();
// Custom tooltip (native title= is unreliable and gets clipped by overflow:hidden).
(function(){{
  const tip = document.createElement('div');
  tip.id = 'tip'; tip.hidden = true; document.body.appendChild(tip);
  document.addEventListener('mouseover', e => {{
    const t = e.target.closest('[data-tip]');
    if (!t) return;
    tip.textContent = t.getAttribute('data-tip'); tip.hidden = false;
    const r = t.getBoundingClientRect();
    tip.style.left = Math.max(6, Math.min(r.left, innerWidth - tip.offsetWidth - 6)) + 'px';
    tip.style.top = Math.min(r.bottom + 6, innerHeight - tip.offsetHeight - 6) + 'px';
  }});
  document.addEventListener('mouseout', e => {{ if (e.target.closest('[data-tip]')) tip.hidden = true; }});
}})();
</script>
</body></html>"""


def _session_markdown(s: Session) -> str:
    """Plain-markdown export bundled into the page for the Export button."""
    results = _result_map(s.entries)
    expansions = _expansion_uuids(s.entries)
    lines = [f"# {s.title}", "", f"_{s.project_name} · {_fmt(s.first_ts)} → {_fmt(s.last_ts)}_", ""]
    for e in s.entries:
        role = (e.get("message") or {}).get("role")
        if role == "user":
            if e.get("uuid") in expansions:
                continue  # command boilerplate, not human text
            if _is_meta(e) and not _is_noise(e):
                m = re.match(r"Base directory for this skill:\s*(\S+)", _plain_user_text(e).strip())
                if m:
                    lines.append(f"`⚙ loaded skill: {m.group(1).rstrip('/').split('/')[-1]}`\n")
                continue
            if _is_noise(e):
                label = _command_label(e)
                if label:
                    lines.append(f"`⌘ {label}`\n")
                stdout = _command_stdout(e)
                if stdout:
                    lines.append(f"> {stdout}\n")
                binput = _bash_input(e)
                if binput is not None:
                    lines.append(f"```sh\n$ {binput}\n```\n")
                bo, be = _bash_output(e)
                if bo:
                    lines.append(f"```\n{_cap_lines(bo)}\n```\n")
                if be:
                    lines.append(f"```\n{_cap_lines(be)}\n```\n")
                txt = _plain_user_text(e)
                for tn in re.finditer(r"<task-notification>(.*?)</task-notification>", txt, re.S):
                    st, sm = _tagval(tn.group(1), "status"), _tagval(tn.group(1), "summary")
                    lines.append(f"> ⚙ background task {st}: {sm}\n")
                for sr in re.finditer(r"<system-reminder>(.*?)</system-reminder>", txt, re.S):
                    lines.append(f"> ⚙ system reminder: {_truncate(html.unescape(sr.group(1)), 200)}\n")
                continue
            if _is_tool_result_only(e):
                continue  # tool results belong with Claude's call, below
            txt = _plain_user_text(e).strip()
            if txt:
                lines.append(f"**You:** {txt}\n")
        elif role == "assistant":
            c = _content(e)
            if not isinstance(c, list) or _is_sentinel_assistant(e):
                continue
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    lines.append(f"**Claude:** {_strip_ansi(b['text']).strip()}\n")
                elif b.get("type") == "tool_use":
                    summ = _tool_summary(b.get("name", ""), b.get("input", {}))
                    lines.append(f"> ▸ {b.get('name')}: {summ}")
                    res = results.get(b.get("id"))
                    if res is not None:
                        tag = "error" if res.get("is_error") else "result"
                        rt = _truncate(_tool_result_text(res.get("content")), 200)
                        lines.append(f"> ↳ {tag}: {rt}")
                    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Static assets
# --------------------------------------------------------------------------- #
_CSS = """
/* CONVENTION: one _CSS string is shared by index.html AND every sessions/<id>.html.
   Put LAYOUT (margin/flex/position) on CONTAINER-SCOPED selectors (e.g. `.card-top .date`),
   never on a bare shared element class — a layout prop on a class used in two contexts leaks
   from one into the other (the 2026-06-16 `.date{margin-left:auto}` header-justify bug). Only
   VISUAL styling (color/font/padding) is safe on bare shared classes. See IMPROVEMENTS.md. */
:root{--bg:#fafafa;--panel:#fff;--ink:#1a1a1a;--muted:#6b7280;--line:#e5e7eb;
--accent:#7c5cff;--user:#eef2ff;--code:#f3f4f6;--badge:#ede9fe;--err:#fee2e2;--sel:#e6e0ff;}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--panel:#171a21;--ink:#e6e6e6;
--muted:#9aa1ad;--line:#262b36;--accent:#a78bfa;--user:#1e2233;--code:#1b1f27;
--badge:#2a2342;--err:#3b1d22;--sel:#322c52;}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--ink);display:flex;flex-direction:column}
/* session-page header (transcript pages still use a top <header>) */
header{flex:none;background:var(--panel);border-bottom:1px solid var(--line);
padding:8px 16px;display:flex;align-items:center;gap:12px}
header h1{font-size:15px;margin:0;flex:none}
/* index: title + search live in the left pane so the right panes get full height */
.proj-head{display:flex;align-items:flex-start;gap:8px;padding:2px 4px 8px;border-bottom:1px solid var(--line)}
.proj-head h1{font-size:14px;line-height:1.25;margin:0;flex:1;min-width:0;overflow-wrap:anywhere}
.search-btn{background:none;border:1px solid var(--line);border-radius:8px;cursor:pointer;
font-size:13px;padding:3px 8px;color:var(--ink);flex:none}
.search-btn:hover,.search-btn.active{border-color:var(--accent)}
#search{width:100%;flex:none;margin:8px 0 4px;padding:6px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--ink);font-size:13px}
.gen{color:var(--muted);font-size:11px;flex:none;padding:8px 4px 2px;border-top:1px solid var(--line)}
/* three-pane: projects | sessions | transcript, full viewport height.
   Pane widths are drag-adjustable (persisted); transcript takes the remainder. */
.layout{flex:1;display:flex;min-height:0;--projw:200px;--sessw:330px}
.projcol{width:var(--projw);flex:none;display:flex;flex-direction:column;padding:8px;
overflow:hidden;border-right:1px solid var(--line)}
.proj-list{flex:1;overflow:auto;display:flex;flex-direction:column;gap:2px;margin-top:6px}
.resizer{flex:none;width:6px;margin:0 -3px;z-index:5;cursor:col-resize;background:transparent;position:relative}
.resizer:hover,.resizer.active{background:var(--accent)}
.layout.dragging{cursor:col-resize}
.layout.dragging .content{pointer-events:none}
.proj{text-align:left;background:none;border:none;color:var(--ink);padding:7px 10px;border-radius:8px;
cursor:pointer;font-size:13px;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.proj:hover{background:var(--panel)}
.proj.active{background:var(--badge);font-weight:600}
.proj-dot{width:9px;height:9px;border-radius:50%;flex:none;background:hsl(var(--phue,265) 65% 55%)}
.proj-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.count{color:var(--muted);font-size:11px}
.pcost{flex-basis:100%;color:var(--muted);font-size:10.5px;font-weight:400;padding-left:16px}
.sessioncol{width:var(--sessw);flex:none;display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--line)}
.cardlist{flex:1;overflow:auto;padding:10px;display:flex;flex-direction:column;gap:8px}
.group-header{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;
color:var(--muted);padding:6px 4px 2px;position:sticky;top:0;background:var(--bg);
display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.group-tally{font-weight:400;text-transform:none;letter-spacing:0;font-size:11px;white-space:nowrap}
.card{display:block;text-decoration:none;color:inherit;background:var(--panel);
border:1px solid var(--line);border-left:3px solid hsl(var(--phue,265) 60% 60%);
border-radius:10px;padding:11px 12px;transition:.1s}
.card:hover{border-color:var(--accent);border-left-color:hsl(var(--phue,265) 60% 60%)}
/* Selection = soft accent fill only; borders (incl. the project-hue left stripe) stay put
   so the selection cue and the project-color cue never compete on the same edge. */
.card.active{background:var(--sel)}
.card.active:hover{border-left-color:hsl(var(--phue,265) 60% 60%)}
.card-top{display:flex;align-items:center;gap:6px;margin-bottom:5px}
/* INTENTIONALLY shared by card-top and the session header (.hmeta) — same pill in both.
   Visual-only (no layout), so the share is safe; keep it that way (see CONVENTION above). */
.proj-badge{background:hsl(var(--phue,265) 70% 91%);color:hsl(var(--phue,265) 55% 32%);
font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px}
@media (prefers-color-scheme:dark){.proj-badge{background:hsl(var(--phue,265) 38% 26%);color:hsl(var(--phue,265) 70% 80%)}}
.sdk-pill{font-size:10.5px;font-weight:600;color:var(--accent);background:var(--badge);
  padding:2px 7px;border-radius:20px;cursor:help;white-space:nowrap}
.date{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:6px}
.card-top .date{margin-left:auto}  /* right-pin the date in cards only; header .hmeta stays left-aligned */
.active-badge{color:#16a34a;font-weight:700;font-size:10.5px;letter-spacing:.02em}
@media (prefers-color-scheme:dark){.active-badge{color:#4ade80}}
.title{font-weight:600;margin-bottom:4px;font-size:14px}
.snippet{color:var(--muted);font-size:12.5px;display:-webkit-box;-webkit-line-clamp:2;
-webkit-box-orient:vertical;overflow:hidden}
.meta{color:var(--muted);font-size:11.5px;margin-top:6px}
.content{flex:1;border:none;height:100%;background:var(--bg)}
.placeholder{display:flex;align-items:center;justify-content:center;color:var(--muted)}
.empty{padding:30px;text-align:center;color:var(--muted)}
/* transcript */
.transcript{flex:1;min-height:0;overflow-y:auto;width:100%;max-width:880px;margin:0 auto;padding:22px}
.session-header{flex-direction:column;align-items:stretch;gap:5px;padding:8px 16px;z-index:20}
.hdr-title{display:flex;align-items:center;gap:10px}
.session-header h1{font-size:16px;margin:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.back{color:var(--accent);text-decoration:none;font-size:15px;flex:none}
.hmeta{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:11.5px}
.usage{font-size:11.5px;color:var(--accent);background:var(--badge);padding:1px 8px;border-radius:20px;cursor:help}
.export{background:var(--accent);color:#fff;border:none;text-decoration:none;padding:5px 11px;border-radius:7px;
cursor:pointer;font-size:12.5px;white-space:nowrap}
.hdr-actions{display:flex;gap:12px;align-items:center;flex:none}
.tool-toggle{display:flex;gap:5px;align-items:center;color:var(--muted);font-size:12.5px;cursor:pointer;user-select:none;white-space:nowrap}
body.hide-tools .tool-use{display:none}
body.hide-tools .row.tools-only{display:none}
.row{margin:18px 0}
.row .who{font-size:12px;font-weight:600;color:var(--muted);margin-bottom:5px}
.row .who .ts{font-weight:400;margin-left:8px}
.who-badge{font-weight:600;font-size:10px;color:var(--accent);background:var(--badge);
  border-radius:6px;padding:1px 6px;margin-left:8px;cursor:help;vertical-align:middle}
.bubble{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;overflow:auto}
.row.user .bubble{background:var(--user)}
.row.command{margin:10px 0}
/* harness-injected system events (task notifications, reminders, unknown tags) */
.sys-event{font-size:12.5px;color:var(--muted);background:var(--code);border-radius:6px;
padding:6px 10px;margin:4px 0;border-left:2px solid var(--line)}
.sys-event.error{border-left-color:#cf222e;color:#cf222e}
@media (prefers-color-scheme:dark){.sys-event.error{color:#f85149}}
.sys-event summary{cursor:pointer}
.sys-icon{opacity:.7}
#tip{position:fixed;z-index:1000;max-width:440px;background:var(--ink);color:var(--bg);
font-size:12px;line-height:1.45;padding:7px 10px;border-radius:7px;pointer-events:none;
box-shadow:0 4px 16px rgba(0,0,0,.3);white-space:normal}
#tip[hidden]{display:none}
.sys-body{white-space:pre-wrap;word-wrap:break-word;margin-top:6px;font-family:ui-monospace,monospace;font-size:12px}
.cmd{color:var(--muted);font-size:13px;font-family:ui-monospace,monospace}
.cmd-out{color:var(--muted);font-size:12.5px;font-family:ui-monospace,monospace;
white-space:pre-wrap;word-wrap:break-word;background:var(--code);border-radius:6px;
padding:7px 9px;margin-top:5px;border-left:2px solid var(--line)}
/* `!` bash commands: terminal-style prompt + output */
.bash-in{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;
background:var(--code);border-radius:6px 6px 0 0;padding:7px 10px;border-left:2px solid var(--accent)}
.bash-in .bash-prompt{color:var(--accent);font-weight:700;user-select:none}
.bash-in code{background:none;padding:0}
.bash-out{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;
white-space:pre-wrap;word-wrap:break-word;background:var(--code);margin:0 0 0 0;
padding:7px 10px;border-radius:0 0 6px 6px;border-left:2px solid var(--line);color:var(--muted)}
.bash-out.bash-err{color:#cf222e;border-left-color:#cf222e}
@media (prefers-color-scheme:dark){.bash-out.bash-err{color:#f85149}}
.prose{white-space:pre-wrap;word-wrap:break-word}
/* rendered markdown inside bubbles */
.bubble>:first-child{margin-top:0}
.bubble>:last-child{margin-bottom:0}
.bubble p{margin:8px 0}
.bubble h1,.bubble h2,.bubble h3,.bubble h4{margin:14px 0 6px;line-height:1.3}
.bubble h1{font-size:1.4em}.bubble h2{font-size:1.25em}.bubble h3{font-size:1.12em}.bubble h4{font-size:1em}
.bubble ul,.bubble ol{margin:8px 0;padding-left:22px}
.bubble li{margin:2px 0}
.bubble blockquote{margin:8px 0;padding:2px 12px;border-left:3px solid var(--line);color:var(--muted)}
.bubble a{color:var(--accent)}
.bubble hr{border:none;border-top:1px solid var(--line);margin:14px 0}
.bubble code{background:var(--code);border-radius:5px;padding:1px 5px;font-size:.9em;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.bubble table{border-collapse:collapse;margin:10px 0;font-size:13px;display:block;overflow:auto}
.bubble th,.bubble td{border:1px solid var(--line);padding:5px 10px;text-align:left}
.bubble th{background:var(--code)}
.code{background:var(--code);border-radius:8px;padding:10px;overflow:auto;font-size:12.5px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-wrap:break-word}
.bubble pre.code{margin:8px 0}
pre.code code{background:none;padding:0;font-size:1em}
code.hljs{background:transparent;padding:0}
/* red/green diffs for Edit/Write, with a line-number gutter */
.diff{background:var(--code);border-radius:8px;padding:6px 0;overflow:auto;margin:6px 0;
font-size:12.5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.d-line{display:grid;grid-template-columns:3.5ch 3.5ch 1fr;column-gap:6px}
.d-old,.d-new{text-align:right;color:var(--muted);opacity:.55;user-select:none}
.d-code{white-space:pre-wrap;word-break:break-word;padding-right:8px}
.d-add{background:rgba(46,160,67,.16)}.d-add .d-code{color:#1a7f37}
.d-del{background:rgba(248,81,73,.16)}.d-del .d-code{color:#cf222e}
.d-ctx .d-code{color:var(--muted)}
.d-hunk .d-code{color:var(--accent)}
@media (prefers-color-scheme:dark){.d-add .d-code{color:#3fb950}.d-del .d-code{color:#f85149}}
/* Plan card — ExitPlanMode / Write-to-plan rendered as expanded markdown */
.plan-card{border-left:3px solid #7c6af7;background:var(--bg2,var(--badge));border-radius:6px;padding:12px 16px;margin:8px 0}
.plan-header{font-weight:600;color:#7c6af7;font-size:.82em;letter-spacing:.04em;margin-bottom:6px}
.plan-path{font-size:.72em;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;margin-bottom:8px}
.plan-approved{font-size:.8em;color:var(--muted);margin-top:10px;border-top:1px solid var(--line);padding-top:6px}
.plan-approved ul{margin:4px 0 0;padding-left:1.2em}.plan-approved li{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;margin:2px 0}
/* AskUserQuestion shown as a Q&A card (not hidden by the tool toggle) */
.askq{border:1px solid var(--accent);border-radius:10px;padding:10px 12px;margin:10px 0;background:var(--badge)}
.askq-q{font-weight:600;margin-bottom:8px}
.askq-opts{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:5px}
.askq-opt{padding:6px 9px;border:1px solid var(--line);border-radius:7px;background:var(--panel);font-size:13.5px}
.askq-opt.chosen{border-color:var(--accent);box-shadow:inset 3px 0 0 var(--accent);font-weight:500}
.askq-prev{margin-top:5px}
.askq-prev summary{cursor:pointer;color:var(--muted);font-size:12px}
.tool{margin:8px 0;border:1px solid var(--line);border-radius:8px;padding:4px 8px}
.tool summary{cursor:pointer;color:var(--muted);font-size:13px}
.tool[open]{padding-bottom:8px}
.tool.error{background:var(--err)}
.tlabel{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:8px 2px 3px}
.errflag{color:#d33;font-size:11px;font-weight:600}
.cmd-expand{margin-top:6px}
.badge{background:var(--badge);color:var(--accent);font-size:11px;padding:1px 7px;border-radius:6px;margin-right:6px}
"""

_INDEX_JS = """
let curProject = '';
function selectCard(card){
  document.querySelectorAll('.card.active').forEach(c=>c.classList.remove('active'));
  card.classList.add('active');
}
function filterProject(btn, p){
  curProject = p;
  document.querySelectorAll('.proj').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  runSearch();
}
function fmtTok(n){ n=+n||0; if(n>=1e6) return (n/1e6).toFixed(1)+'M'; if(n>=1e3) return Math.round(n/1e3)+'K'; return ''+n; }
function fmtTally(tok,cost,inc){ return '~$'+cost.toFixed(2)+(inc?'+':'')+' · '+fmtTok(tok)+' tok'; }
function updateHeaders(){
  // Hide a date-group header when no card under it is visible, and tally the
  // cost/tokens of the *currently visible* cards in that group (so the tally
  // reflects the active project/search filter, not the all-time set).
  let header = null, anyVisible = false, cost = 0, tok = 0, inc = false;
  const finalize = () => {
    if(!header) return;
    header.style.display = anyVisible ? '' : 'none';
    const t = header.querySelector('.group-tally');
    if(t) t.textContent = anyVisible && (cost || tok) ? fmtTally(tok, cost, inc) : '';
  };
  for(const el of document.getElementById('cards').children){
    if(el.classList.contains('group-header')){
      finalize();
      header = el; anyVisible = false; cost = 0; tok = 0; inc = false;
    } else if(el.classList.contains('card') && el.style.display !== 'none'){
      anyVisible = true;
      cost += parseFloat(el.dataset.cost) || 0;
      tok += parseInt(el.dataset.tok) || 0;
      if(el.dataset.inc === '1') inc = true;
    }
  }
  finalize();
}
function runSearch(){
  const q = document.getElementById('search').value.trim().toLowerCase();
  let shown = 0;
  document.querySelectorAll('.card').forEach(c=>{
    const okP = !curProject || c.dataset.project === curProject;
    const okQ = !q || c.dataset.search.includes(q);
    const vis = okP && okQ;
    c.style.display = vis ? '' : 'none';
    if(vis) shown++;
  });
  document.getElementById('noresults').hidden = shown !== 0;
  updateHeaders();
}
function showSearch(){
  const s = document.getElementById('search');
  s.hidden = false;
  document.getElementById('searchBtn').classList.add('active');
  s.focus();
}
function toggleSearch(){
  const s = document.getElementById('search');
  if(s.hidden){ showSearch(); }
  else if(!s.value){ s.hidden = true; document.getElementById('searchBtn').classList.remove('active'); }
  else { s.focus(); }
}
// Keyboard navigation: ↑/↓ or j/k to move + open, / focuses search, Esc blurs it.
function visibleCards(){
  return [...document.querySelectorAll('.card')].filter(c=>c.style.display !== 'none');
}
function moveSel(delta){
  const vis = visibleCards();
  if(!vis.length) return;
  let idx = vis.findIndex(c=>c.classList.contains('active'));
  idx = idx < 0 ? 0 : Math.min(vis.length-1, Math.max(0, idx+delta));
  vis[idx].click();
  vis[idx].scrollIntoView({block:'nearest'});
}
document.addEventListener('keydown', e=>{
  const search = document.getElementById('search');
  const inSearch = document.activeElement === search;
  if(inSearch){ if(e.key === 'Escape'){ search.blur(); if(!search.value) toggleSearch(); } return; }
  if(e.key === '/'){ e.preventDefault(); showSearch(); return; }
  if(e.key === 'ArrowDown' || e.key === 'j'){ e.preventDefault(); moveSel(1); }
  else if(e.key === 'ArrowUp' || e.key === 'k'){ e.preventDefault(); moveSel(-1); }
});
// Drag-resizable panes (persisted). The transcript iframe takes the remaining width.
(function(){
  const layout = document.getElementById('layout');
  const VAR = {proj: '--projw', sess: '--sessw'};
  const KEY = {proj: 'projw', sess: 'sessw'};
  try {
    for (const k of ['proj','sess']){ const v = localStorage.getItem(KEY[k]); if(v) layout.style.setProperty(VAR[k], v); }
  } catch(e) {}
  let drag = null;
  document.querySelectorAll('.resizer').forEach(r => {
    r.addEventListener('mousedown', e => {
      const pane = r.dataset.pane;
      drag = {pane, r, startX: e.clientX, startW: parseInt(getComputedStyle(layout).getPropertyValue(VAR[pane]))};
      r.classList.add('active'); layout.classList.add('dragging'); e.preventDefault();
    });
  });
  window.addEventListener('mousemove', e => {
    if(!drag) return;
    const w = Math.max(130, Math.min(640, drag.startW + (e.clientX - drag.startX)));
    layout.style.setProperty(VAR[drag.pane], w + 'px');
  });
  window.addEventListener('mouseup', () => {
    if(!drag) return;
    try { localStorage.setItem(KEY[drag.pane], layout.style.getPropertyValue(VAR[drag.pane])); } catch(e) {}
    drag.r.classList.remove('active'); layout.classList.remove('dragging'); drag = null;
  });
})();
// Custom tooltip (shared with the session page) — drives [data-tip] on project
// rows, the totals strip, and the live badge.
(function(){
  const tip = document.createElement('div');
  tip.id = 'tip'; tip.hidden = true; document.body.appendChild(tip);
  document.addEventListener('mouseover', e => {
    const t = e.target.closest('[data-tip]');
    if (!t) return;
    tip.textContent = t.getAttribute('data-tip'); tip.hidden = false;
    const r = t.getBoundingClientRect();
    tip.style.left = Math.max(6, Math.min(r.left, innerWidth - tip.offsetWidth - 6)) + 'px';
    tip.style.top = Math.min(r.bottom + 6, innerHeight - tip.offsetHeight - 6) + 'px';
  });
  document.addEventListener('mouseout', e => { if (e.target.closest('[data-tip]')) tip.hidden = true; });
})();
updateHeaders();  // populate date-group tallies on first load (all cards visible)
"""


# --------------------------------------------------------------------------- #
# LLM titling (via the local, already-authenticated `claude` CLI)
# --------------------------------------------------------------------------- #
def _condense_transcript(s: Session) -> str:
    """A compact human/Claude text view for the titler (tool noise dropped)."""
    parts: list[str] = []
    expansions = _expansion_uuids(s.entries)
    for e in s.entries:
        role = (e.get("message") or {}).get("role")
        if role == "user":
            if (_is_noise(e) or _is_meta(e) or _is_tool_result_only(e)
                    or e.get("uuid") in expansions):
                continue
            txt = _plain_user_text(e).strip()
            if txt:
                parts.append("User: " + _truncate(txt, 500))
        elif role == "assistant" and not _is_sentinel_assistant(e):
            c = _content(e)
            if not isinstance(c, list):
                continue
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    parts.append("Claude: " + _truncate(_strip_ansi(b["text"]), 500))
    text = "\n".join(parts)
    if len(text) > TITLE_CONDENSE_HEAD + TITLE_CONDENSE_TAIL:
        text = text[:TITLE_CONDENSE_HEAD] + "\n…\n" + text[-TITLE_CONDENSE_TAIL:]
    return text


def _session_fingerprint(s: Session, condensed: str) -> str:
    h = hashlib.sha1()
    h.update(f"v{TITLE_PROMPT_VERSION}|{TITLE_MODEL}|".encode())
    h.update(condensed.encode("utf-8", "replace"))
    return h.hexdigest()


def _claude_title(condensed: str, model: str) -> tuple[str, str] | None:
    """Call `claude -p` to get (title, summary); None on any failure."""
    try:
        proc = subprocess.run(
            ["claude", "-p", TITLE_INSTRUCTIONS + condensed,
             "--model", model, "--no-session-persistence"],
            capture_output=True, text=True, timeout=TITLE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    line = _strip_ansi(proc.stdout).strip().splitlines()
    line = line[0] if line else ""
    if "||" not in line:
        return None
    title, summary = (p.strip().strip('"') for p in line.split("||", 1))
    if not title:
        return None
    return _truncate(title, 80), _truncate(summary, 160)


def resolve_cached_titles(
    sessions: list[Session], output_dir: str
) -> tuple[dict, str, list[tuple[Session, str, str]]]:
    """Apply cached (and live-session) titles immediately — no network — and return
    `(cache, cache_path, todo)` where todo = sessions still needing a `claude -p`
    call as (session, fingerprint, condensed). Lets the caller render the dashboard
    before the slow calls run."""
    cache_path = os.path.join(output_dir, TITLE_CACHE)
    cache: dict = {}
    if not shutil.which("claude"):
        print("  (claude CLI not found — keeping heuristic titles)")
        return cache, cache_path, []
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (OSError, json.JSONDecodeError):
            cache = {}

    todo: list[tuple[Session, str, str]] = []  # (session, fingerprint, condensed)
    for s in sessions:
        condensed = _condense_transcript(s)
        if not condensed.strip():
            continue
        fp = _session_fingerprint(s, condensed)
        hit = cache.get(s.session_id)
        if hit and hit.get("fp") == fp and hit.get("title"):
            s.title, s.summary = hit["title"], hit.get("summary", "")
        elif _is_active(s.last_ts):
            # In-progress session: its transcript (and fingerprint) changes on every
            # run, so re-titling it each invocation is the main cost of `/chats` from
            # inside a live session — and titling an unfinished session is premature.
            # Reuse the last cached title if any; otherwise keep the heuristic. It gets
            # a fresh LLM title on a later run once the session goes cold (>15 min idle).
            if hit and hit.get("title"):
                s.title, s.summary = hit["title"], hit.get("summary", "")
        else:
            todo.append((s, fp, condensed))
    return cache, cache_path, todo


def generate_titles(todo: list[tuple[Session, str, str]], cache: dict,
                    cache_path: str, model: str) -> int:
    """Run the `claude -p` calls for `todo`, update each session's title/summary in
    place, and persist the cache. Returns the number of titles successfully set."""
    if not todo:
        return 0
    print(f"  titling {len(todo)} new/changed session(s) via claude ({model})…")
    done = 0
    with ThreadPoolExecutor(max_workers=TITLE_WORKERS) as pool:
        futs = {pool.submit(_claude_title, c, model): (s, fp) for s, fp, c in todo}
        for fut in as_completed(futs):
            s, fp = futs[fut]
            res = fut.result()
            if res:
                s.title, s.summary = res
                cache[s.session_id] = {"fp": fp, "title": s.title, "summary": s.summary}
                done += 1
    _atomic_write_json(cache_path, cache)
    return done


def apply_llm_titles(sessions: list[Session], output_dir: str, model: str) -> None:
    """Blocking: resolve cached titles, then generate any missing ones (old behavior;
    kept for callers that want titles fully applied before rendering)."""
    cache, cache_path, todo = resolve_cached_titles(sessions, output_dir)
    generate_titles(todo, cache, cache_path, model)


def _atomic_write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def open_in_browser(path: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", path], check=False)
        elif sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not auto-open browser: {exc})", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build a static HTML dashboard of Claude Code chat history.")
    ap.add_argument("--open", action="store_true", help="open the dashboard in your browser when done")
    ap.add_argument("--no-titles", action="store_true",
                    help="skip LLM title generation (use offline heuristic titles)")
    ap.add_argument("--title-model", default=TITLE_MODEL, help="model for the claude CLI titler")
    ap.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR)
    ap.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    args = ap.parse_args(argv)

    if not os.path.isdir(args.projects_dir):
        print(f"No projects directory at {args.projects_dir}", file=sys.stderr)
        return 1

    print(f"Scanning {args.projects_dir} …")
    sessions = load_sessions(args.projects_dir)
    print(f"Found {len(sessions)} session(s).")

    os.makedirs(args.output_dir, exist_ok=True)
    index = os.path.join(args.output_dir, "index.html")

    # Resolve cached/heuristic titles instantly (no network), render, and open — so
    # the dashboard appears right away. Only genuinely new/cold sessions need a slow
    # `claude -p` call; do those *after* opening, then rewrite the affected pages.
    cache: dict = {}
    cache_path = ""
    todo: list[tuple[Session, str, str]] = []
    if not args.no_titles:
        cache, cache_path, todo = resolve_cached_titles(sessions, args.output_dir)

    write_site(sessions, args.output_dir)
    print(f"Dashboard written to {index}")
    if args.open:
        open_in_browser(index)

    if todo:
        if generate_titles(todo, cache, cache_path, args.title_model):
            write_site(sessions, args.output_dir)  # rewrite with the fresh titles
            print(f"  titles updated for {len(todo)} session(s) — "
                  f"refresh the dashboard to see them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
