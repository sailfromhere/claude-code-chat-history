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
MAX_IMAGE_B64 = 2_000_000  # base64 chars (~1.5 MB binary); bigger pastes get a placeholder
SNIPPET_CHARS = 200
TITLE_FALLBACK_CHARS = 70
ACTIVE_WINDOW_MIN = 15  # a session is "active" if its last activity is within this many minutes

# Public API list prices, USD per million tokens: (input, output).
# Cache read ≈ 0.1× input; cache write (5-min) ≈ 1.25× input.
PRICING = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
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
    dur: str = ""      # formatted turn duration ("2m 41s"), from system/turn_duration


@dataclass
class Session:
    session_id: str
    project_path: str = ""
    entries: list[dict] = field(default_factory=list)  # raw user/assistant entries
    ai_title: str | None = None
    agent_name: str = ""  # harness-assigned session display name (last agent-name line)
    turn_durations: dict = field(default_factory=dict)  # assistant uuid → durationMs

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

    # per-session render caches (entries are immutable after load)
    _expansions: set | None = field(default=None, repr=False, compare=False)
    _results: dict | None = field(default=None, repr=False, compare=False)

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
    agent_names: dict[str, str] = {}
    durations: dict[str, dict] = {}

    def _mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    # Oldest file first: entries get re-sorted by timestamp anyway, but
    # timestamp-less lines (agent-name) rely on "last read wins" being
    # chronological — the current name lives in the most recently written file.
    for path in sorted(glob.glob(os.path.join(projects_dir, "*", "*.jsonl")),
                       key=lambda p: (_mtime(p), p)):
        for line in _iter_jsonl(path):
            etype = line.get("type")
            if etype == "ai-title":
                sid = line.get("sessionId")
                t = line.get("aiTitle")
                if sid and t:
                    titles[sid] = t
                continue
            if etype == "system":
                # turn_duration lines: parentUuid points at the turn's final
                # assistant entry (verified 2026-07-09). Other system lines skipped.
                if (line.get("subtype") == "turn_duration"
                        and line.get("sessionId") and line.get("parentUuid")):
                    durations.setdefault(line["sessionId"], {})[line["parentUuid"]] = \
                        line.get("durationMs")
                continue
            if etype == "agent-name":
                # Harness-assigned display name for its OWN session (the name the
                # Claude Code UI shows). Renames accrue; the last one is current.
                sid = line.get("sessionId")
                nm = line.get("agentName")
                if sid and nm:
                    agent_names[sid] = nm
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
            # "_ptext" is OUR memo slot on the entry dict; a crafted jsonl line
            # pre-seeding it could poison the interrupt/noise classifiers (they
            # trust the cache) and hide the real content. Strip at the boundary.
            line.pop("_ptext", None)
            s.entries.append(line)

    result: list[Session] = []
    for sid, s in sessions.items():
        if not s.entries:
            continue
        s.entries.sort(key=lambda e: e.get("timestamp") or "")
        s.ai_title = titles.get(sid)
        s.agent_name = agent_names.get(sid, "")
        s.turn_durations = durations.get(sid, {})
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

    expansions = _session_expansions(s)
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
        if (_is_meta(e) or _is_tool_result_only(e) or _is_interrupt(e)
                or _is_compact_summary(e) or e.get("uuid") in expansions):
            continue
        if s.n_user == 0:  # the first real prompt = what initiated the session
            s.initiated_by_sdk = _prompt_submitted_by(e) == "sdk"
        s.n_user += 1
        if not s.snippet:
            text = _plain_user_text(e).strip()
            if text:
                s.snippet = _truncate(text, SNIPPET_CHARS)

    _compute_usage(s)

    # Prefer the AI title; then the harness-assigned session name (what Claude
    # Code's own UI shows); then a human snippet; then the command; else untitled.
    if s.ai_title:
        s.title = s.ai_title
    elif s.agent_name:
        s.title = _truncate(s.agent_name, TITLE_FALLBACK_CHARS)
    elif s.snippet:
        s.title = _truncate(s.snippet, TITLE_FALLBACK_CHARS)
    elif first_command:
        s.title = first_command
        s.snippet = s.snippet or first_command
    else:
        s.title = "(untitled session)"


def _price_for(model) -> tuple[float, float] | None:
    """(input, output) $/MTok for a model id. Exact match first, then a
    dash-boundary prefix match so date-suffixed ids (claude-haiku-4-5-20251001)
    price as their base model instead of counting as unpriced."""
    if not isinstance(model, str) or not model:
        return None
    price = PRICING.get(model)
    if price is not None:
        return price
    for known, p in PRICING.items():
        if model.startswith(known + "-"):
            return p
    return None


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

        price = _price_for(msg.get("model"))
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


# The user entry written when the human presses Esc mid-turn. Its content is
# exactly one of these strings (verified on real data 2026-07-09) — a human
# message merely QUOTING one has surrounding prose, so it won't exact-match.
_INTERRUPT_SENTINELS = {
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
}


def _is_interrupt(entry: dict) -> bool:
    return _plain_user_text(entry).strip() in _INTERRUPT_SENTINELS


def _is_compact_summary(entry: dict) -> bool:
    """The continuation-summary user entry /compact writes ("This session is
    being continued…"). isMeta is ABSENT on it, so the meta rule can't catch it."""
    return entry.get("isCompactSummary") is True


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


def _render_interrupt(entry: dict) -> str:
    """Esc interrupt — a status marker (the CLI shows 'Interrupted'), not speech."""
    tool = "for tool use" in _plain_user_text(entry)
    label = "Interrupted by user" + (" (during tool use)" if tool else "")
    return f'<div class="interrupt">⏹ {label}</div>'


def _render_compact_summary(entry: dict) -> str:
    """Collapsed divider chip for the /compact continuation summary."""
    body = _esc(_clip(_cap_lines(_plain_user_text(entry).strip())))
    return ('<details class="sys-event"><summary><span class="sys-icon">🗜</span> '
            'conversation compacted — continuation summary</summary>'
            f'<div class="sys-body">{body}</div></details>')


def _render_image(block: dict) -> str:
    """Inline a pasted image (the .jsonl already stores it as base64)."""
    src = block.get("source")
    if not isinstance(src, dict):
        src = {}
    data = src.get("data")
    if not isinstance(data, str) or not data or src.get("type") != "base64":
        return ('<div class="sys-event"><span class="sys-icon">🖼</span> '
                'image (unsupported source)</div>')
    if len(data) > MAX_IMAGE_B64:
        kb = len(data) * 3 // 4 // 1024
        return (f'<div class="sys-event"><span class="sys-icon">🖼</span> pasted image '
                f'({kb:,} KB — too large to embed; the original .jsonl has it)</div>')
    media = _esc(src.get("media_type") or "image/png")
    return (f'<img class="msg-img" src="data:{media};base64,{_esc(data)}" '
            f'alt="pasted image" loading="lazy" '
            f'onclick="this.classList.toggle(\'expanded\')">')


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
        # A manual /compact's continuation summary (and an Esc interrupt) can
        # directly follow the command entry — neither is the command's prompt
        # expansion; leave them to their own renderers.
        if (nxt and (nxt.get("message") or {}).get("role") == "user"
                and not _is_noise(nxt) and not _is_tool_result_only(nxt)
                and not _is_compact_summary(nxt) and not _is_interrupt(nxt)
                and _plain_user_text(nxt).strip()):
            uid = nxt.get("uuid")
            if uid:
                out.add(uid)
    return out


def _session_expansions(s: "Session") -> set:
    """Cached _expansion_uuids — metadata, HTML, markdown, and the titler all need it."""
    if s._expansions is None:
        s._expansions = _expansion_uuids(s.entries)
    return s._expansions


def _session_results(s: "Session") -> dict:
    """Cached _result_map (HTML + markdown renders)."""
    if s._results is None:
        s._results = _result_map(s.entries)
    return s._results


def _result_map(entries: list[dict]) -> dict[str, dict]:
    """Map tool_use_id → tool_result block, so results pair with their call."""
    out: dict[str, dict] = {}
    for e in entries:
        c = _content(e)
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                    # AskUserQuestion's real answer (custom-typed text, notes) lives only in
                    # the entry's top-level toolUseResult, not in this string content — stash
                    # it on the block so _render_askq can read it. See _render_askq.
                    b["_tur"] = e.get("toolUseResult")
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
    """Human-typed text from a user entry (string content or text blocks).
    Memoized on the entry dict — the noise/command/interrupt predicates each
    call this, so it runs many times per entry across metadata + rendering."""
    cached = entry.get("_ptext")
    if cached is not None:
        return cached
    c = _content(entry)
    if isinstance(c, str):
        text = _strip_ansi(c)
    elif isinstance(c, list):
        parts = [b.get("text", "") for b in c
                 if isinstance(b, dict) and b.get("type") == "text"]
        text = _strip_ansi("\n".join(p for p in parts if p))
    else:
        text = ""
    entry["_ptext"] = text
    return text


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

    def link(m):
        label, href = m.group(1), m.group(2)
        # href text is untrusted (chat content, or — via AskUserQuestion — harness-controlled
        # toolUseResult); block script-executing schemes so a crafted link can't become clickable.
        if re.match(r"(?i)^\s*(javascript|data|vbscript):", href):
            return m.group(0)
        return f'<a href="{href}" target="_blank" rel="noopener">{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, text)
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

        lm = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)        # list (may nest)
        if lm:
            items = []
            while i < n:
                im = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", lines[i])
                if not im:
                    break
                items.append((len(im.group(1).expandtabs(4)),
                              im.group(2).endswith("."), im.group(3)))
                i += 1
            out.append(_render_list(items))
            continue

        para = [s]                                                 # paragraph (consume ≥1 line)
        i += 1
        while i < n and lines[i].strip() and not _para_breaks(lines[i], lines[i + 1] if i + 1 < n else ""):
            para.append(lines[i].strip()); i += 1
        out.append(f'<p>{"<br>".join(_inline(p) for p in para)}</p>')
    return "\n".join(out)


_CHECKBOX_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")


def _render_list(items: list[tuple[int, bool, str]]) -> str:
    """Render consecutive (indent, ordered, text) list items with nesting by
    indent. Deeper indent opens a nested <ul>/<ol>; shallower pops back out
    (the outermost list never closes until the end). `- [ ]`/`- [x]` render as
    checkboxes. Bounded: one pass over items, stack pops are ≤ pushes."""
    html: list[str] = []
    stack: list[tuple[int, str]] = []  # (indent, tag) of open lists
    for indent, ordered, text in items:
        tag = "ol" if ordered else "ul"
        if not stack:
            stack.append((indent, tag))
            html.append(f"<{tag}>")
        elif indent > stack[-1][0]:
            stack.append((indent, tag))
            html.append(f"<{tag}>")
        else:
            while len(stack) > 1 and indent < stack[-1][0]:
                html.append(f"</{stack.pop()[1]}>")
        m = _CHECKBOX_RE.match(text)
        if m:
            mark = "☑" if m.group(1) in "xX" else "☐"
            html.append(f'<li class="task">{mark} {_inline(m.group(2))}</li>')
        else:
            html.append(f"<li>{_inline(text)}</li>")
    while stack:
        html.append(f"</{stack.pop()[1]}>")
    return "".join(html)


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


def _plan_outcomes(entries: list[dict], results: dict) -> dict[str, str]:
    """Map plan-Write tool_use id → "approved"/"rejected". The outcome lives on
    the *ExitPlanMode* call's tool_result ("User has approved your plan…" vs an
    is_error rejection), so pair each plan write with the next ExitPlanMode call.
    Consecutive writes before one ExitPlanMode are revisions of the same plan and
    share its outcome."""
    pending: list[str] = []
    out: dict[str, str] = {}
    for e in entries:
        c = _content(e)
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            if _is_plan_write(b.get("name", ""), b.get("input") or {}):
                if b.get("id"):
                    pending.append(b["id"])
            elif b.get("name") == "ExitPlanMode":
                res = results.get(b.get("id"))
                if res is not None:
                    outcome = "rejected" if res.get("is_error") else "approved"
                    for w in pending:
                        out[w] = outcome
                pending = []
    return out


def _render_plan_card(content: str, file_path: str = "", allowed_prompts: list | None = None,
                      outcome: str = "") -> str:
    """Render a plan as an expanded markdown card (not a collapsed tool chip)."""
    path_html = (
        f'<div class="plan-path">{_esc(file_path)}</div>'
        if file_path else ""
    )
    badge = {"approved": ' <span class="plan-outcome ok">✓ approved</span>',
             "rejected": ' <span class="plan-outcome no">✗ rejected</span>'}.get(outcome, "")
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
        f'<div class="plan-header">📋 Plan{badge}</div>'
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


def _split_multiselect(ans: str, labels: set[str]) -> tuple[set[str], str]:
    """Peel known option labels out of a comma-joined multi-select answer — longest label
    first, so a label that itself contains a comma isn't mis-split into leftover text.
    Whatever isn't accounted for by a label is the user's own typed addition."""
    remaining = ans
    chosen: set[str] = set()
    for lbl in sorted((l for l in labels if l), key=len, reverse=True):
        idx = remaining.find(lbl)
        if idx != -1:
            chosen.add(lbl)
            remaining = remaining[:idx] + remaining[idx + len(lbl):]
    typed = re.sub(r"^[\s,]+|[\s,]+$", "", remaining)
    typed = re.sub(r"[\s,]{2,}", ", ", typed)
    return chosen, typed


def _askq_answer(q: dict, tur, legacy_chosen: set[str]) -> tuple[set[str], str, str]:
    """For one AskUserQuestion sub-question, return (chosen option labels, leftover
    typed/custom "Other" text, attached note). Prefers the structured toolUseResult
    (answers/annotations keyed by question text — stashed onto the result block by
    _result_map). Falls back to `legacy_chosen` (labels scraped out of the old
    '...="Label"...' result string) when toolUseResult isn't the expected dict — a
    rejected/timed-out call carries a plain string there instead, and older captures may
    have none at all.

    toolUseResult is harness-controlled JSONL, i.e. untrusted: every nested value is
    independently type-checked below so a malformed shape degrades to "no answer" rather
    than raising and taking down the whole dashboard build (write_site has no per-session
    try/except around rendering)."""
    if not isinstance(q, dict):
        return set(), "", ""
    qtext = q.get("question")
    qtext = qtext if isinstance(qtext, str) else ""
    options = q.get("options")
    labels = ({o.get("label", "") for o in options if isinstance(o, dict)}
              if isinstance(options, list) else set())

    if not isinstance(tur, dict):
        return {lbl for lbl in labels if lbl in legacy_chosen}, "", ""

    answers = tur.get("answers")
    ans = answers.get(qtext) if isinstance(answers, dict) else None
    ans = ans if isinstance(ans, str) else ""

    annotations = tur.get("annotations")
    qa = annotations.get(qtext) if isinstance(annotations, dict) else None
    note = qa.get("notes") if isinstance(qa, dict) else ""
    note = note if isinstance(note, str) else ""

    if not ans:
        return set(), "", note
    if q.get("multiSelect"):
        chosen, typed = _split_multiselect(ans, labels)
        return chosen, typed, note
    if ans in labels:
        return {ans}, "", note
    return set(), ans, note  # single-select, didn't match a listed option → custom-typed text


def _render_askq(inp: dict, result: dict | None) -> str:
    """Readable Q&A card for an AskUserQuestion call: marks the chosen option(s), and — since
    neither lives in the tool_result string the old version scraped — surfaces custom-typed
    ("Other") answers and any note the user attached, both read from the entry's structured
    toolUseResult via _askq_answer."""
    tur = result.get("_tur") if result else None
    answer_text = _tool_result_text(result.get("content")) if result else ""
    # Legacy fallback for captures with no structured toolUseResult (older data, or a
    # rejected/timed-out call whose toolUseResult is a plain string): scrape the chosen label
    # out of the rendered "...=\"Label\"..." result string, as the old renderer did.
    legacy_chosen = set(re.findall(r'="([^"]+)"', answer_text)) if not isinstance(tur, dict) else set()
    out = ['<div class="askq">']
    any_answer = False
    questions = inp.get("questions") if isinstance(inp, dict) else None
    for q in questions if isinstance(questions, list) else []:
        if not isinstance(q, dict):
            continue
        chosen, typed, note = _askq_answer(q, tur, legacy_chosen)
        if chosen or typed or note:
            any_answer = True
        out.append(f'<div class="askq-q">❓ {_inline(q.get("question", ""))}</div>')
        out.append('<ul class="askq-opts">')
        options = q.get("options")
        for opt in options if isinstance(options, list) else []:
            if not isinstance(opt, dict):
                continue
            lbl = opt.get("label", "")
            sel = " chosen" if lbl in chosen else ""
            mark = "✓ " if lbl in chosen else ""
            prev = opt.get("preview")
            pv = (f'<details class="askq-prev"><summary>preview</summary>'
                  f'<pre class="code">{_esc(prev)}</pre></details>') if prev else ""
            out.append(f'<li class="askq-opt{sel}">{mark}<b>{_esc(lbl)}</b>'
                       f' — {_inline(opt.get("description", ""))}{pv}</li>')
        if typed:
            out.append(f'<li class="askq-opt typed">✎ <b>{_inline(typed)}</b>'
                       f' <span class="muted">(typed answer)</span></li>')
        out.append("</ul>")
        if note:
            out.append(f'<div class="askq-note">💬 {_inline(note)}</div>')
    if not any_answer and result is not None:
        if "away from keyboard" in answer_text or "No response after" in answer_text:
            out.append('<div class="askq-note">⏱ no answer (timed out)</div>')
        elif result.get("is_error"):
            out.append('<div class="askq-note">↩ sent back to clarify</div>')
    out.append("</div>")
    return "".join(out)


def _render_tool_use(block: dict, result: dict | None,
                     plan_outcomes: dict[str, str] | None = None) -> str:
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
            outcome=(plan_outcomes or {}).get(block.get("id"), ""),
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
    if name == "TaskUpdate" and inp.get("taskId"):  # delta-shaped: "#3 → completed"
        status = f' → {inp["status"]}' if inp.get("status") else ""
        return _truncate(f'#{inp["taskId"]}{status}', 100)
    # First matching key wins; "description"/"prompt" late so Agent/Task chips show
    # the human-readable line, and Bash keeps showing its command.
    for key in ("command", "file_path", "path", "pattern", "query", "url",
                "skill", "subject", "description", "prompt"):
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
    results = _session_results(s)
    consumed: set[str] = set()  # tool_use_ids already shown under their call
    expansions = _session_expansions(s)
    plan_outcomes = _plan_outcomes(s.entries, results)
    entries = s.entries

    turns: list[Turn] = []
    cur_model: str | None = None  # last real model seen, for the switch divider
    pending_divider: tuple[str, str] | None = None
    i, n = 0, len(entries)
    while i < n:
        e = entries[i]
        role = (e.get("message") or {}).get("role")
        ts = e.get("timestamp", "")
        if role == "user":
            if e.get("uuid") in expansions:
                i += 1
                continue  # boilerplate already folded under its command
            if _is_interrupt(e):
                turns.append(Turn("command", ts, _render_interrupt(e)))
                i += 1
                continue
            if _is_compact_summary(e):
                turns.append(Turn("command", ts, _render_compact_summary(e)))
                i += 1
                continue
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
            # Model-switch divider (user choice: divider row, not per-turn chips).
            # Buffered: emitted only when a VISIBLE assistant turn follows, so an
            # invisible switching entry (empty thinking block, tools-only) never
            # strands a divider next to nothing.
            model = (e.get("message") or {}).get("model")
            if isinstance(model, str) and model and model != "<synthetic>":
                if cur_model is not None and model != cur_model:
                    label = model.removeprefix("claude-")
                    pending_divider = (ts,
                                       f'<div class="model-divider">model → {_esc(label)}</div>')
                cur_model = model
            inner = _render_assistant_blocks(e, results, consumed, plan_outcomes)
            if inner.strip():
                if pending_divider:
                    turns.append(Turn("command", *pending_divider))
                    pending_divider = None
                turns.append(Turn("assistant", ts, inner,
                                  tools_only=not _has_visible_nontool(e),
                                  dur=_fmt_dur(s.turn_durations.get(e.get("uuid")))
                                  if e.get("uuid") in s.turn_durations else ""))
        i += 1
    return turns


def _has_visible_nontool(entry: dict) -> bool:
    """Does the entry render anything that survives 'hide tool calls'? (text, a pasted
    image, an AskUserQuestion card, or a plan card — all stay visible; plain
    tool_use/result don't.)"""
    c = _content(entry)
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                return True
            if b.get("type") == "image":
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
            elif t == "image":
                out.append(_render_image(b))
            elif t == "tool_result":
                # Orphan results only; matched ones render under their tool_use.
                tid = b.get("tool_use_id")
                if tid not in consumed:
                    out.append(_render_orphan_result(b))
    return "\n".join(out)


def _render_assistant_blocks(entry: dict, results: dict, consumed: set,
                             plan_outcomes: dict[str, str] | None = None) -> str:
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
                out.append(_render_tool_use(b, res, plan_outcomes))
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


def _days_since(ts: str) -> float | None:
    m = _minutes_since(ts)
    return None if m is None else m / 1440


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


def _fmt_dur(ms) -> str:
    """Compact human duration from milliseconds: 45s · 2m 41s · 1h 5m."""
    try:
        sec = int(round(float(ms) / 1000))
    except (TypeError, ValueError, OverflowError):  # Overflow: json accepts Infinity
        return ""
    if sec < 1:
        return "<1s"
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


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


RENDER_MANIFEST = ".render-manifest.json"
ARCHIVE_CARDS = ".archive-cards.json"
TRASH_FILE = ".deleted-sessions.json"
CONFIG_FILE = ".dashboard-config.json"
_GEN_HASH: str | None = None


def _read_json_dict(path: str) -> dict:
    """Read a JSON object from path, self-healing to {} on missing/corrupt/wrong-
    type content — the pattern already used inline for the render manifest and
    the archive-cards sidecar, now shared so every generator-owned dotfile loads
    the same defensive way."""
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        if isinstance(data, dict):
            return data
    return {}

# Card fields needed to render an index card without the source .jsonl (title,
# cost, tokens, counts, timestamps) — captured every render for every live
# session, so a stub can still be built after the session's source disappears.
_CARD_FIELDS = ("session_id", "project_name", "title", "agent_name", "summary",
                "snippet", "last_ts", "n_user", "n_asst", "initiated_by_sdk",
                "cost_usd", "cost_complete", "tok_in", "tok_out",
                "tok_cache_read", "tok_cache_write")


@dataclass
class ArchivedCard:
    """Lightweight stand-in for a Session whose source .jsonl is gone. Carries
    just the fields _index_page needs to render a card; the full transcript
    still lives in its frozen sessions/<id>.html — this only restores its
    listing."""
    session_id: str
    project_name: str
    title: str
    agent_name: str
    summary: str
    snippet: str
    last_ts: str
    n_user: int
    n_asst: int
    initiated_by_sdk: bool
    cost_usd: float
    cost_complete: bool
    tok_in: int
    tok_out: int
    tok_cache_read: int
    tok_cache_write: int
    is_archived: bool = True


def _card_fields(s: Session) -> dict:
    return {k: getattr(s, k) for k in _CARD_FIELDS}


def _archived_card_from_fields(fields: dict) -> ArchivedCard | None:
    """Build an ArchivedCard from a persisted .archive-cards.json row,
    defensively. The dataclass constructor does NOT type-check, so a
    corrupt/hand-edited or cross-version sidecar row (e.g. cost_usd as a
    string, last_ts as null) would otherwise flow straight into unguarded
    downstream code — sorting by last_ts, `.4f` cost formatting, token
    arithmetic — and crash the WHOLE index render, not just that one card.
    Anything that doesn't conform is dropped (returns None), matching this
    file's existing posture of corrupt-data-self-heals-by-disappearing (see
    the manifest's isinstance(sid, str) guard)."""
    def _is_int(v: object) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)  # bool is an int subclass

    str_fields = ("session_id", "project_name", "title", "agent_name",
                  "summary", "snippet", "last_ts")
    bool_fields = ("initiated_by_sdk", "cost_complete")
    int_fields = ("n_user", "n_asst", "tok_in", "tok_out",
                  "tok_cache_read", "tok_cache_write")
    if not all(isinstance(fields.get(k), str) for k in str_fields):
        return None
    if not all(isinstance(fields.get(k), bool) for k in bool_fields):
        return None
    if not all(_is_int(fields.get(k)) for k in int_fields):
        return None
    cost = fields.get("cost_usd")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool):
        return None
    try:
        return ArchivedCard(**{k: fields[k] for k in _CARD_FIELDS}, is_archived=True)
    except KeyError:
        return None


def _known_cards(sessions: list[Session], output_dir: str) -> list:
    """Every session the trash/retention/bulk-delete selection logic can act on:
    live sessions plus archived stubs rebuilt from .archive-cards.json for
    sessions whose source .jsonl is already gone. Each element exposes
    .session_id/.project_name/.last_ts and works with _card_fields(). Live
    sessions take precedence on id collision (shouldn't happen in practice —
    a live session is never also an orphan — but this keeps the fresher data)."""
    cards: list = list(sessions)
    live_ids = {s.session_id for s in sessions}
    card_meta = _read_json_dict(os.path.join(output_dir, ARCHIVE_CARDS))
    for sid, fields in card_meta.items():
        if sid in live_ids or not isinstance(fields, dict):
            continue
        stub = _archived_card_from_fields(fields)
        if stub is not None:
            cards.append(stub)
    return cards


def load_trash(output_dir: str) -> dict:
    """Load the trash sidecar (.deleted-sessions.json): sid -> {deleted_at,
    reason, fields}. Malformed rows are dropped silently (corrupt-data-self-
    heals-by-disappearing, matching the manifest/archive-cards posture).
    deleted_at/reason are validated as strings and fields as a dict-or-absent
    HERE, at the load boundary, so every consumer (sorting/display/date
    parsing) can trust the shape without repeating the check — a hand-edited
    or cross-version row with e.g. deleted_at as a number would otherwise crash
    a str/str sort (_print_trash) or an isoformat parse downstream."""
    raw = _read_json_dict(os.path.join(output_dir, TRASH_FILE))
    trash: dict = {}
    for sid, row in raw.items():
        if not (isinstance(sid, str) and isinstance(row, dict)):
            continue
        if not (isinstance(row.get("deleted_at"), str) and isinstance(row.get("reason"), str)):
            continue
        fields = row.get("fields")
        if fields is not None and not isinstance(fields, dict):
            continue
        trash[sid] = row
    return trash


def save_trash(output_dir: str, trash: dict) -> None:
    _atomic_write_json(os.path.join(output_dir, TRASH_FILE), trash)


_CONFIG_DEFAULTS = {"retention_days": None, "purge_days": None}


def load_config(output_dir: str) -> dict:
    """Load the retention config sidecar (.dashboard-config.json). Both keys
    default to None ("off" — keep forever), matching today's behavior when the
    file doesn't exist yet. A malformed value self-heals to its default rather
    than crashing or silently misbehaving."""
    raw = _read_json_dict(os.path.join(output_dir, CONFIG_FILE))
    cfg = dict(_CONFIG_DEFAULTS)
    for k in _CONFIG_DEFAULTS:
        v = raw.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            cfg[k] = v
    return cfg


def save_config(output_dir: str, config: dict) -> None:
    _atomic_write_json(os.path.join(output_dir, CONFIG_FILE), config)


def _generator_hash() -> str:
    """Fingerprint of the generator itself (source bytes + timezone) — baked into
    every page fingerprint so a code/TZ change rewrites everything."""
    global _GEN_HASH
    if _GEN_HASH is None:
        tz = datetime.now().astimezone().strftime("%z")
        try:
            with open(os.path.abspath(__file__), "rb") as fh:
                _GEN_HASH = hashlib.sha1(fh.read() + tz.encode()).hexdigest()
        except OSError:
            _GEN_HASH = f"unknown-{os.getpid()}"  # can't read self → never skip
    return _GEN_HASH


def _session_render_fp(s: Session) -> str:
    """Cheap proxy for everything a session's page/.md depends on. The .jsonl is
    append-only, so (entry count, first/last ts) tracks content; title/summary
    track the LLM titler; project_path tracks aliases; the generator hash tracks
    code/CSS/JS changes."""
    # turn_durations included explicitly: a duration line can land AFTER the
    # session's final assistant entry, changing neither entry count nor last_ts.
    dur_sig = f"{len(s.turn_durations)}:{sum(v or 0 for v in s.turn_durations.values())}"
    key = "|".join([str(len(s.entries)), s.first_ts, s.last_ts, s.title,
                    s.summary, s.project_path, dur_sig, _generator_hash()])
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()


def write_site(sessions: list[Session], output_dir: str,
               prune_orphans: bool = False,
               deleted_sids: frozenset = frozenset(),
               trash: dict | None = None, config: dict | None = None) -> tuple[int, int, int, int]:
    """Render the site incrementally: only sessions whose fingerprint changed (or
    whose files are missing) are re-rendered. Pages of sessions whose source
    .jsonl has disappeared (e.g. pruned by Claude Code's cleanupPeriodDays) are
    KEPT and stay LISTED by default (as an ArchivedCard reconstructed from their
    last-captured card fields, flagged 🗄 archived) — the dashboard doubles as a
    permanent, browsable archive — unless prune_orphans=True, which restores the
    old mirror-only behavior and deletes them.

    deleted_sids (the trash, see load_trash/save_trash) is a pure DISPLAY/render
    filter, not a deletion mechanism: a trashed session's page is neither
    (re)rendered nor listed, but its manifest/archive-cards entries are carried
    forward untouched, so restoring it (dropping it from the trash file) makes it
    reappear with no data loss. The ONLY code that removes files or manifest/
    archive-cards entries for a trashed session is --empty-trash/purge (batch 2).

    trash/config, when given, are embedded (read-only) into index.html so its
    Cleanup/Retention/Trash panel can show the current state without a fetch()
    (this stays a static site — file:// pages can't fetch). If omitted,
    deleted_sids alone still drives the render filter; the panel just shows an
    empty/default state, which is what every non-UI test wants.

    Returns (written, skipped, pruned, archived)."""
    sessions_dir = os.path.join(output_dir, "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    _write_assets(output_dir)

    manifest_path = os.path.join(output_dir, RENDER_MANIFEST)
    manifest = _read_json_dict(manifest_path)

    cards_path = os.path.join(output_dir, ARCHIVE_CARDS)
    card_meta = _read_json_dict(cards_path)

    new_manifest: dict = {}
    written = skipped = 0
    for s in sessions:
        if s.session_id in deleted_sids:
            # Trashed: skip render/listing, but carry forward any existing
            # manifest entry (if this session was rendered before being
            # trashed) so the page file isn't orphaned out of the ledger and a
            # future restore doesn't force a needless rewrite. Never touch the
            # file itself here.
            if s.session_id in manifest:
                new_manifest[s.session_id] = manifest[s.session_id]
            continue
        fp = _session_render_fp(s)
        html_path = os.path.join(sessions_dir, f"{s.session_id}.html")
        md_path = os.path.join(sessions_dir, f"{s.session_id}.md")
        if (manifest.get(s.session_id) == fp
                and os.path.isfile(html_path) and os.path.isfile(md_path)):
            new_manifest[s.session_id] = fp
            skipped += 1
            continue
        _atomic_write_text(html_path, _session_page(s))
        _atomic_write_text(md_path, _session_markdown(s))
        new_manifest[s.session_id] = fp
        written += 1

    # Card fields (title/cost/tokens/etc.) for every live session, captured every
    # run so a stub can still be built once its source .jsonl disappears. Kept
    # even for a trashed live session (harmless — its card just isn't listed —
    # and keeps the fields fresh in case it's later restored).
    new_card_meta: dict = {s.session_id: _card_fields(s) for s in sessions}

    # Sessions whose source .jsonl is gone (Claude Code pruned it, or it moved).
    # Default: archive — keep their pages, carry them forward in the manifest so
    # a later --prune-orphans run can still find/clean them, and reconstruct an
    # ArchivedCard from their last-captured fields so they stay listed in the
    # index (flagged, not deleted). Opt-in prune_orphans restores the old mirror
    # behavior and deletes the pages — but ONLY files this generator created
    # (present in the previous manifest). --output-dir can point anywhere; never
    # delete files we didn't write.
    current = {s.session_id for s in sessions}
    pruned = 0
    archived_stubs: list[ArchivedCard] = []
    for sid in manifest:
        if sid in current:
            continue
        if sid in deleted_sids:
            # Trashed AND source gone: same soft-delete contract as above — keep
            # the manifest/card-metadata entries carried forward (so a restore
            # can rebuild the stub) but don't list it. This takes priority over
            # prune_orphans: trash is only emptied by --empty-trash/purge, never
            # by the unrelated --prune-orphans flag.
            if isinstance(sid, str):
                new_manifest.setdefault(sid, manifest[sid])
                fields = card_meta.get(sid)
                if isinstance(fields, dict):
                    new_card_meta.setdefault(sid, fields)
            continue
        if not prune_orphans:
            if isinstance(sid, str):
                new_manifest.setdefault(sid, manifest[sid])
                fields = card_meta.get(sid)
                if isinstance(fields, dict):
                    stub = _archived_card_from_fields(fields)
                    if stub is not None:
                        archived_stubs.append(stub)
                        new_card_meta.setdefault(sid, fields)
                    # else: no usable card metadata (e.g. this is the first run
                    # after upgrading, or the sidecar row is corrupt) — the page
                    # is still kept on disk, just not listed until it's rebuilt.
            continue
        for ext in ("html", "md"):
            fn = f"{sid}.{ext}"
            if os.path.basename(fn) != fn:  # manifest poisoning guard: no path parts
                continue
            try:
                os.remove(os.path.join(sessions_dir, fn))
                pruned += 1
            except OSError:
                pass

    _atomic_write_json(manifest_path, new_manifest)
    _atomic_write_json(cards_path, new_card_meta)
    all_cards = sorted(
        [s for s in sessions if s.session_id not in deleted_sids] + archived_stubs,
        key=lambda s: s.last_ts, reverse=True)
    # Write index last (atomically) so it never references a half-written page.
    _atomic_write_text(os.path.join(output_dir, "index.html"),
                      _index_page(all_cards, trash=trash or {}, config=config or dict(_CONFIG_DEFAULTS)))
    # archived == cards actually listed, NOT every orphan kept on disk (an orphan
    # with no usable card metadata is kept but not counted — see the loop above).
    return written, skipped, pruned, len(archived_stubs)


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


def _fmt_retention(v) -> str:
    return "off" if v is None else f"{v:g}d"


def _input_days_value(v) -> str:
    """Pre-fill value for the retention/purge number inputs: blank when off (so
    the placeholder's "off" hint still shows), else the plain number — NOT the
    "d"-suffixed display string. Pre-filling with the CURRENT setting (rather
    than leaving both inputs blank) means submitting the field you didn't touch
    reproduces its existing value instead of silently coercing it to 'off' —
    the config-loss bug an adversarial review caught in the first draft."""
    return "" if v is None else f"{v:g}"


def _index_page(sessions: list[Session], trash: dict | None = None,
                config: dict | None = None) -> str:
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    trash = trash or {}
    config = config or dict(_CONFIG_DEFAULTS)

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
        haystack = _esc(f"{s.title} {s.agent_name} {s.project_name} {blurb}".lower())
        href = f"sessions/{_esc(s.session_id)}.html"
        active = _is_active(s.last_ts)
        badge = (f'<span class="active-badge" data-tip="{_esc(_active_label(s.last_ts))}">'
                 f'● live</span>') if active else ""
        toks = _session_tokens(s)
        tally = f' · {_tally_str(toks, s.cost_usd, s.cost_complete)}' if toks else ""
        sdk_pill = ('<span class="sdk-pill" data-tip="Submitted by Claude or a tool '
                    '(a headless claude -p run), not typed by you">⚙ SDK</span>'
                    if s.initiated_by_sdk else "")
        is_archived = getattr(s, "is_archived", False)
        archived_pill = ('<span class="archived-pill" data-tip="Source session was '
                          'removed (e.g. by Claude Code’s 30-day cleanup) — this '
                          'is a saved snapshot">🗄 archived</span>' if is_archived else "")
        sid_esc = _esc(s.session_id)
        del_btn = (f'<span class="card-del" data-tip="Delete this session" '
                   f'onclick="event.preventDefault();event.stopPropagation();'
                   f'cmdDelete(\'{sid_esc}\')">🗑</span>')
        cards.append(
            f'<a class="card{" active" if idx == 0 else ""}'
            f'{" archived" if is_archived else ""}" href="{href}" target="content" '
            f'onclick="selectCard(this)" style="--phue:{_hue(s.project_name)}" '
            f'data-sid="{sid_esc}" '
            f'data-project="{_esc(s.project_name)}" data-search="{haystack}" '
            f'data-cost="{s.cost_usd:.4f}" data-tok="{toks}" '
            f'data-inc="{0 if s.cost_complete else 1}">'
            f'<div class="card-top"><span class="proj-badge">{_esc(s.project_name)}</span>'
            f'{sdk_pill}{archived_pill}'
            f'<span class="date">{badge}{_esc(_fmt_date(s.last_ts))}</span>{del_btn}</div>'
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

    # Cleanup/Retention/Trash panel: a command-BUILDER, not a delete button — this
    # stays a static site (no server, no fetch()), so it can only produce the exact
    # `chats …` CLI command for the user to copy and run themselves. Trash/config
    # are embedded read-only at generation time; the panel reflects the state as of
    # this build and updates after the next regen.
    trash_rows = []
    for sid, row in sorted(trash.items(), key=lambda kv: str(kv[1].get("deleted_at", "")), reverse=True):
        fields = row.get("fields") or {}
        title = fields.get("title") or "(untitled)"
        proj = fields.get("project_name") or "?"
        reason = row.get("reason", "?")
        deleted_disp = _fmt(row.get("deleted_at", "")) or "?"
        trash_rows.append(
            f'<div class="trash-row">'
            f'<div class="trash-meta"><span class="proj-badge">{_esc(proj)}</span> '
            f'{_esc(title)} <span class="muted">({_esc(reason)}, {_esc(deleted_disp)})</span></div>'
            f'<button class="cmd-btn" onclick="cmdRestore(\'{_esc(sid)}\')">Restore</button>'
            f'</div>'
        )
    trash_html = "".join(trash_rows) if trash_rows else '<div class="muted">Trash is empty.</div>'
    project_options = "".join(f'<option value="{_esc(p)}">{_esc(p)}</option>' for p in projects)
    retention_disp = _fmt_retention(config.get("retention_days"))
    purge_disp = _fmt_retention(config.get("purge_days"))

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
      <button id="settingsBtn" class="search-btn" onclick="toggleSettings()" title="Cleanup &amp; trash">⚙</button>
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
<div id="settingsPanel" class="settings-overlay" hidden onclick="if(event.target===this)toggleSettings()">
  <div class="settings-box">
    <div class="settings-head"><span>Cleanup &amp; trash</span>
      <span class="settings-close" onclick="toggleSettings()">✕</span></div>
    <p class="settings-note">This is a static site — it can't delete files itself. Every action
      here builds the exact <code>chats</code> command to copy and run yourself.</p>
    <section>
      <h3>Bulk cleanup</h3>
      <div class="settings-row">
        <label>Older than <input id="bulkDays" type="number" min="0" value="90" style="width:5em">
          days</label>
        <button class="cmd-btn" onclick="cmdBulkAge()">Build command</button>
      </div>
      <div class="settings-row">
        <label>Project <select id="bulkProject"><option value="">(choose)</option>
          {project_options}</select></label>
        <button class="cmd-btn" onclick="cmdBulkProject()">Build command</button>
      </div>
    </section>
    <section>
      <h3>Retention</h3>
      <div class="settings-current">Current: auto-trash {_esc(retention_disp)}
        · auto-purge {_esc(purge_disp)}</div>
      <div class="settings-row">
        <label>Auto-trash after <input id="retDays" type="number" min="0" placeholder="off"
          value="{_esc(_input_days_value(config.get('retention_days')))}" style="width:5em"> days</label>
        <label>Auto-purge after <input id="purgeDays" type="number" min="0" placeholder="off"
          value="{_esc(_input_days_value(config.get('purge_days')))}" style="width:5em"> days</label>
        <button class="cmd-btn" onclick="cmdRetention()">Build command</button>
      </div>
    </section>
    <section>
      <h3>Trash ({len(trash)})</h3>
      <div id="trashList">{trash_html}</div>
      {'<button class="cmd-btn" onclick="cmdEmptyTrash()">Empty trash (permanent)</button>' if trash else ''}
    </section>
  </div>
</div>
<div id="cmdDialog" class="cmd-overlay" hidden onclick="if(event.target===this)closeCmdDialog()">
  <div class="cmd-box">
    <div class="cmd-title">Run to apply:</div>
    <code id="cmdText"></code>
    <div class="cmd-actions">
      <button class="cmd-btn" onclick="copyCmd()">Copy</button>
      <button class="cmd-btn" onclick="closeCmdDialog()">Close</button>
    </div>
  </div>
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
        dur = f'<span class="dur">{_esc(t.dur)}</span>' if t.dur else ""
        rows.append(
            f'<div class="row {t.role}{extra}">'
            f'<div class="who">{who}{badge}<span class="ts">{_esc(_fmt(t.ts))}{dur}</span></div>'
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
    results = _session_results(s)
    expansions = _session_expansions(s)
    lines = [f"# {s.title}", "", f"_{s.project_name} · {_fmt(s.first_ts)} → {_fmt(s.last_ts)}_", ""]
    for e in s.entries:
        role = (e.get("message") or {}).get("role")
        if role == "user":
            if e.get("uuid") in expansions:
                continue  # command boilerplate, not human text
            if _is_interrupt(e):
                lines.append("> ⏹ interrupted by user\n")
                continue
            if _is_compact_summary(e):
                lines.append("> 🗜 conversation compacted (continuation summary)\n")
                continue
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
            c = _content(e)
            n_img = (sum(1 for b in c if isinstance(b, dict) and b.get("type") == "image")
                     if isinstance(c, list) else 0)
            if n_img:
                note = f"*[{n_img} pasted image{'s' if n_img > 1 else ''}]*"
                txt = f"{txt}\n{note}".strip()
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
                elif b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                    res = results.get(b.get("id"))
                    tur = res.get("_tur") if res else None
                    inp = b.get("input", {}) or {}
                    answer_text = _tool_result_text(res.get("content")) if res else ""
                    legacy_chosen = (set(re.findall(r'="([^"]+)"', answer_text))
                                     if not isinstance(tur, dict) else set())
                    any_answer = False
                    questions = inp.get("questions") if isinstance(inp, dict) else None
                    for q in questions if isinstance(questions, list) else []:
                        if not isinstance(q, dict):
                            continue
                        lines.append(f"> ❓ {q.get('question', '')}")
                        chosen, typed, note = _askq_answer(q, tur, legacy_chosen)
                        if chosen or typed or note:
                            any_answer = True
                        options = q.get("options")
                        for opt in options if isinstance(options, list) else []:
                            if not isinstance(opt, dict):
                                continue
                            lbl = opt.get("label", "")
                            mark = "✓ " if lbl in chosen else "- "
                            lines.append(f">   {mark}{lbl}")
                        if typed:
                            lines.append(f">   ✎ {typed} (typed answer)")
                        if note:
                            lines.append(f">   💬 {note}")
                    if not any_answer and res is not None:
                        if "away from keyboard" in answer_text or "No response after" in answer_text:
                            lines.append("> ↳ ⏱ no answer (timed out)")
                        elif res.get("is_error"):
                            lines.append("> ↳ ↩ sent back to clarify")
                        else:
                            lines.append(f"> ↳ {_truncate(answer_text, 200)}")
                    lines.append("")
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
/* No top padding on the scroll container: a position:sticky header is clamped to its
   containing block (the padding-excluded content box), so a padding-top would pin the header
   that many px BELOW the scrollport top and let cards scroll through the gap above it. Top
   breathing room is restored on the first child instead — it scrolls away and doesn't offset
   the pin. (z-index/opaque bg on the header are belt-and-suspenders for paint order.) */
.cardlist{flex:1;overflow:auto;padding:0 10px 10px;display:flex;flex-direction:column;gap:8px}
.cardlist>:first-child{margin-top:10px}
.group-header{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;
color:var(--muted);padding:6px 4px 2px;position:sticky;top:0;z-index:10;background:var(--bg);
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
.archived-pill{font-size:10.5px;font-weight:600;color:var(--muted);background:var(--code);
  padding:2px 7px;border-radius:20px;cursor:help;white-space:nowrap;border:1px dashed var(--line)}
.card.archived{opacity:.72}
.card.archived:hover{opacity:1}
.date{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:6px}
.card-top .date{margin-left:auto}  /* right-pin the date in cards only; header .hmeta stays left-aligned */
.card-del{flex:none;opacity:0;pointer-events:none;color:var(--muted);cursor:pointer;font-size:12px;
padding:1px 3px;border-radius:5px;transition:opacity .1s}
.card:hover .card-del,.card:focus-within .card-del{opacity:.55;pointer-events:auto}
.card-del:hover{opacity:1 !important;background:var(--code)}
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
/* Esc interrupt — a status marker row, not user speech */
.interrupt{color:#cf222e;font-size:12.5px;font-weight:600;padding:2px 0}
@media (prefers-color-scheme:dark){.interrupt{color:#f85149}}
/* model-switch divider (user choice 2026-07-09: divider row, not per-turn chips) */
.model-divider{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:11px;margin:16px 0}
.model-divider::before,.model-divider::after{content:"";flex:1;border-top:1px solid var(--line)}
/* turn duration in the timestamp (from system/turn_duration lines) */
.dur{color:var(--muted);font-weight:400;margin-left:8px;font-size:11px}
.dur::before{content:"⏱ ";opacity:.7}
/* pasted images (base64-embedded from the .jsonl); click toggles full size */
.msg-img{display:block;max-width:100%;max-height:340px;width:auto;
border:1px solid var(--line);border-radius:8px;margin:8px 0;cursor:zoom-in}
.msg-img.expanded{max-height:none;cursor:zoom-out}
#tip{position:fixed;z-index:1000;max-width:440px;background:var(--ink);color:var(--bg);
font-size:12px;line-height:1.45;padding:7px 10px;border-radius:7px;pointer-events:none;
box-shadow:0 4px 16px rgba(0,0,0,.3);white-space:normal}
#tip[hidden]{display:none}
/* Cleanup/Retention/Trash panel + the shared command dialog it (and the per-card
   🗑) both open. Both are simple centered overlays — no server, so every action
   just builds a `chats …` command string for the user to copy/run. */
.settings-overlay,.cmd-overlay{position:fixed;inset:0;z-index:1100;background:rgba(0,0,0,.35);
display:flex;align-items:center;justify-content:center}
.settings-overlay[hidden],.cmd-overlay[hidden]{display:none}
.settings-box{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;width:min(480px,92vw);max-height:82vh;overflow:auto;
box-shadow:0 12px 40px rgba(0,0,0,.35)}
.settings-head{display:flex;justify-content:space-between;align-items:center;font-weight:700;
font-size:15px;margin-bottom:4px}
.settings-close{cursor:pointer;color:var(--muted);font-size:14px;padding:2px 6px;border-radius:6px}
.settings-close:hover{background:var(--code)}
.settings-note{color:var(--muted);font-size:12px;margin:4px 0 14px}
.settings-box section{margin-bottom:16px;padding-bottom:14px;border-bottom:1px solid var(--line)}
.settings-box section:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0}
.settings-box h3{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);
margin:0 0 8px}
.settings-current{font-size:12.5px;color:var(--muted);margin-bottom:8px}
.settings-row{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:8px;font-size:13px}
.settings-row label{display:flex;align-items:center;gap:6px}
.settings-row input,.settings-row select{border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--ink);padding:3px 6px;font-size:13px}
.cmd-btn{background:none;border:1px solid var(--line);border-radius:7px;cursor:pointer;
font-size:12.5px;padding:4px 10px;color:var(--ink)}
.cmd-btn:hover{border-color:var(--accent)}
.trash-row{display:flex;justify-content:space-between;align-items:center;gap:10px;
padding:6px 0;border-bottom:1px solid var(--line);font-size:12.5px}
.trash-row:last-child{border-bottom:none}
.trash-meta{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cmd-box{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;width:min(520px,92vw);box-shadow:0 12px 40px rgba(0,0,0,.35)}
.cmd-title{font-size:12px;color:var(--muted);margin-bottom:6px}
#cmdText{display:block;background:var(--code);border-radius:8px;padding:10px 12px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;
white-space:pre-wrap;word-break:break-all;margin-bottom:12px}
.cmd-actions{display:flex;gap:8px;justify-content:flex-end}
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
.bubble li.task{list-style:none;margin-left:-18px}
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
.plan-outcome{font-size:.85em;font-weight:600;padding:1px 7px;border-radius:10px;margin-left:6px}
.plan-outcome.ok{color:#1a7f37;background:rgba(46,160,67,.15)}
.plan-outcome.no{color:#cf222e;background:rgba(248,81,73,.15)}
@media (prefers-color-scheme:dark){.plan-outcome.ok{color:#3fb950}.plan-outcome.no{color:#f85149}}
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
.askq-opt.typed{border-style:dashed;border-color:var(--accent);font-style:italic}
.askq-note{margin-top:6px;padding:5px 9px;font-size:13px;color:var(--muted);background:var(--panel);border-radius:7px}
.muted{color:var(--muted);font-style:normal;font-weight:400}
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
  // An open overlay must own the keyboard: otherwise j/k/arrows still navigate
  // (and click) cards behind it, and '/' opens search behind it too. Escape
  // closes whichever overlay is open.
  const cmdOpen = !document.getElementById('cmdDialog').hidden;
  const settingsOpen = !document.getElementById('settingsPanel').hidden;
  if(cmdOpen || settingsOpen){
    if(e.key === 'Escape'){ if(cmdOpen) closeCmdDialog(); else toggleSettings(); }
    return;
  }
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
// Cleanup/Retention/Trash command-builder. This is a static site — it can't
// delete a file or run a command itself — so every action here only builds
// the exact `chats …` CLI command text and lets the user copy it. shQuote()
// wraps any user-controlled string (a project name, in practice) in
// POSIX-shell-safe single quotes so a stray quote/space/`$` in a directory
// name can't break — or inject into — the copied command.
function shQuote(s){ return "'" + String(s).replace(/'/g, "'\\''") + "'"; }
function showCmd(cmd){
  document.getElementById('cmdText').textContent = cmd;
  document.getElementById('cmdDialog').hidden = false;
}
function closeCmdDialog(){ document.getElementById('cmdDialog').hidden = true; }
function copyCmd(){
  const text = document.getElementById('cmdText').textContent;
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch(e) {}
    document.body.removeChild(ta);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(fallback);
  } else fallback();
}
function cmdDelete(sid){ showCmd('chats --delete ' + shQuote(sid)); }
function cmdRestore(sid){ showCmd('chats --restore ' + shQuote(sid)); }
function cmdBulkAge(){
  const days = document.getElementById('bulkDays').value || '0';
  showCmd('chats --delete-older-than ' + shQuote(days) + ' --dry-run');
}
function cmdBulkProject(){
  const proj = document.getElementById('bulkProject').value;
  if (!proj) return;
  showCmd('chats --delete-project ' + shQuote(proj) + ' --dry-run');
}
function cmdRetention(){
  const ret = document.getElementById('retDays').value.trim();
  const purge = document.getElementById('purgeDays').value.trim();
  showCmd('chats --set-retention ' + shQuote(ret === '' ? 'off' : ret) +
          ' --set-purge ' + shQuote(purge === '' ? 'off' : purge));
}
function cmdEmptyTrash(){ showCmd('chats --empty-trash --yes'); }
function toggleSettings(){
  const p = document.getElementById('settingsPanel');
  p.hidden = !p.hidden;
}
updateHeaders();  // populate date-group tallies on first load (all cards visible)
"""


# --------------------------------------------------------------------------- #
# LLM titling (via the local, already-authenticated `claude` CLI)
# --------------------------------------------------------------------------- #
def _condense_transcript(s: Session) -> str:
    """A compact human/Claude text view for the titler (tool noise dropped)."""
    parts: list[str] = []
    expansions = _session_expansions(s)
    for e in s.entries:
        role = (e.get("message") or {}).get("role")
        if role == "user":
            if (_is_noise(e) or _is_meta(e) or _is_tool_result_only(e)
                    or _is_interrupt(e) or _is_compact_summary(e)
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
    _atomic_write_text(path, json.dumps(data, indent=2))  # pid-suffixed temp inside


# --------------------------------------------------------------------------- #
# Trash CLI helpers
# --------------------------------------------------------------------------- #
def _delete_sessions(sids: list[str], sessions: list[Session], output_dir: str,
                     trash: dict, *, reason: str = "manual") -> int:
    """Move the given session ids into trash (mutates `trash` in place). Returns
    the count actually trashed. An id that doesn't match any known session (live
    or archived) is warned about and skipped — a typo shouldn't silently create a
    dangling trash entry with no metadata to list or restore."""
    cards = {c.session_id: c for c in _known_cards(sessions, output_dir)}
    now = datetime.now().astimezone().isoformat()
    n = 0
    for sid in sids:
        if sid in trash:
            continue  # already trashed
        card = cards.get(sid)
        if card is None:
            print(f"  warning: no session with id {sid!r} found — skipped", file=sys.stderr)
            continue
        trash[sid] = {"deleted_at": now, "reason": reason, "fields": _card_fields(card)}
        n += 1
    return n


def _restore_sessions(sids: list[str], trash: dict) -> int:
    """Drop the given ids out of trash (mutates `trash` in place). Returns the
    count actually restored."""
    n = 0
    for sid in sids:
        if trash.pop(sid, None) is not None:
            n += 1
        else:
            print(f"  warning: {sid!r} is not in trash — skipped", file=sys.stderr)
    return n


def _select_by_age(cards: list, min_days: float) -> list[str]:
    """Session ids whose last activity is at least min_days old. A card with no
    parseable last_ts is never selected — safer to under-select than to trash
    something whose age we can't confirm."""
    out = []
    for c in cards:
        age = _days_since(c.last_ts)
        if age is not None and age >= min_days:
            out.append(c.session_id)
    return out


def _select_by_project(cards: list, project_name: str) -> list[str]:
    return [c.session_id for c in cards if c.project_name == project_name]


def _stale_trash_sids(trash: dict, purge_days: float) -> set:
    """Trash entries whose deleted_at has itself aged past purge_days — shared
    by the real auto-purge sweep and the --dry-run preview so they can never
    drift apart. A row with a missing/unparseable deleted_at is never selected
    (same conservative posture as _select_by_age: under-select, don't guess)."""
    now = datetime.now().astimezone()
    stale = set()
    for sid, row in trash.items():
        try:
            dt = datetime.fromisoformat(row.get("deleted_at", ""))
        except (TypeError, ValueError):
            continue
        if (now - dt).total_seconds() / 86400 >= purge_days:
            stale.add(sid)
    return stale


def _live_ids(sessions: list[Session]) -> set:
    return {s.session_id for s in sessions}


def _warn_reappearing(sids: set) -> None:
    """--empty-trash/purge only reclaims disk and forgets bookkeeping — it can't
    make this generator forget a session whose source .jsonl Claude Code still
    has, since the whole point of the tool is to mirror that source. A sid
    that's still live will simply be rendered fresh again (no longer flagged
    trashed) by this very run's write_site() call. Warn rather than silently
    contradicting "permanently removed" — trash (not empty-trash) is the right
    tool for "hide this session, recoverably, for good"."""
    if not sids:
        return
    print(f"  note: {len(sids)} of those still exist in Claude Code's own history "
          f"and will reappear on the next build (their source .jsonl is still present) "
          f"— trash them again (without emptying) to keep them hidden: "
          f"{', '.join(sorted(sids))}")


def _empty_trash(trash: dict, output_dir: str, only: set | None = None) -> int:
    """Permanently delete rendered pages for trashed sessions and drop their
    manifest/archive-cards/trash entries (mutates `trash` in place). Without
    `only`, purges every trashed session (--empty-trash); with `only`, purges
    just that subset (used by the auto-purge sweep, which only ages out
    entries past purge_days, not the whole trash). This is the ONLY code that
    removes a file or forgets a session's bookkeeping row once it's been
    soft-deleted — everything else (delete/restore) only adds to or reads the
    trash file. Reuses the same path-traversal guard as the --prune-orphans
    loop (a poisoned sid with path parts is refused)."""
    sessions_dir = os.path.join(output_dir, "sessions")
    manifest_path = os.path.join(output_dir, RENDER_MANIFEST)
    cards_path = os.path.join(output_dir, ARCHIVE_CARDS)
    manifest = _read_json_dict(manifest_path)
    card_meta = _read_json_dict(cards_path)
    n = 0
    target_sids = list(trash) if only is None else [sid for sid in only if sid in trash]
    for sid in target_sids:
        for ext in ("html", "md"):
            fn = f"{sid}.{ext}"
            if os.path.basename(fn) != fn:  # traversal guard: no path parts
                continue
            try:
                os.remove(os.path.join(sessions_dir, fn))
            except OSError:
                pass
        manifest.pop(sid, None)
        card_meta.pop(sid, None)
        trash.pop(sid, None)
        n += 1
    _atomic_write_json(manifest_path, manifest)
    _atomic_write_json(cards_path, card_meta)
    return n


def _print_trash(trash: dict) -> None:
    if not trash:
        print("Trash is empty.")
        return
    print(f"{len(trash)} session(s) in trash:")
    # str()-coerce the sort key defensively: load_trash validates deleted_at is a
    # str, but a caller could hand this a raw/hand-built dict where it isn't —
    # a mixed-type sort (str vs int) would raise and abort the whole command.
    for sid, row in sorted(trash.items(),
                           key=lambda kv: str(kv[1].get("deleted_at", "")), reverse=True):
        fields = row.get("fields") or {}
        title = fields.get("title") or "(untitled)"
        project = fields.get("project_name") or "?"
        deleted_at = row.get("deleted_at", "?")
        reason = row.get("reason", "?")
        print(f"  {sid}  [{reason}]  {project} · {title}  (deleted {deleted_at})")


_UNSET = object()  # distinguishes "--set-retention not passed" from "--set-retention off" (None)


def _days_or_off(value: str):
    """argparse type for --set-retention/--set-purge: a non-negative number of
    days, or the literal 'off' to disable (-> None, meaning keep forever)."""
    if value.strip().lower() == "off":
        return None
    try:
        days = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number of days or 'off'")
    if days < 0:
        raise argparse.ArgumentTypeError("days must be >= 0")
    return days


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
    ap.add_argument("--prune-orphans", action="store_true",
                    help="delete rendered pages whose source session was removed "
                         "(default: keep them as a permanent archive)")
    ap.add_argument("--delete", nargs="+", metavar="SID",
                    help="move session(s) to trash — recoverable, unlisted on the "
                         "next build; use --restore to undo")
    ap.add_argument("--delete-older-than", type=float, metavar="DAYS",
                    help="bulk: move every session last active more than DAYS days "
                         "ago to trash")
    ap.add_argument("--delete-project", metavar="NAME",
                    help="bulk: move every session in project NAME to trash")
    ap.add_argument("--restore", nargs="+", metavar="SID",
                    help="restore session(s) from trash")
    ap.add_argument("--list-trash", action="store_true",
                    help="list trashed sessions and exit")
    ap.add_argument("--empty-trash", action="store_true",
                    help="permanently delete every trashed session's rendered pages "
                         "(hard delete, not recoverable — requires --yes)")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview a --delete*/--restore/--empty-trash action without "
                         "changing anything")
    ap.add_argument("--yes", action="store_true",
                    help="confirm a destructive action (required for --empty-trash)")
    ap.add_argument("--set-retention", type=_days_or_off, default=_UNSET, metavar="DAYS|off",
                    help="auto-trash sessions last active more than DAYS days ago on "
                         "every build ('off' disables; default: off, keep forever)")
    ap.add_argument("--set-purge", type=_days_or_off, default=_UNSET, metavar="DAYS|off",
                    help="permanently delete a session once it's been in trash more than "
                         "DAYS days ('off' disables; default: off, keep in trash forever)")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.projects_dir):
        print(f"No projects directory at {args.projects_dir}", file=sys.stderr)
        return 1

    print(f"Scanning {args.projects_dir} …")
    sessions = load_sessions(args.projects_dir)
    print(f"Found {len(sessions)} session(s).")

    os.makedirs(args.output_dir, exist_ok=True)
    index = os.path.join(args.output_dir, "index.html")
    trash = load_trash(args.output_dir)
    config = load_config(args.output_dir)

    # Compute the settings change WITHOUT saving yet — --dry-run must preview
    # with truly zero side effects, config included, like every action below.
    new_config = dict(config)
    config_dirty = False
    if args.set_retention is not _UNSET:
        new_config["retention_days"] = args.set_retention
        config_dirty = True
    if args.set_purge is not _UNSET:
        new_config["purge_days"] = args.set_purge
        config_dirty = True
    if config_dirty and new_config.get("purge_days") == 0:
        print("  warning: --set-purge 0 permanently deletes every trashed session's "
              "pages on every future build, with no further confirmation.", file=sys.stderr)
    if config_dirty and new_config.get("retention_days") == 0:
        print("  warning: --set-retention 0 auto-trashes every session with a "
              "parseable timestamp on every future build.", file=sys.stderr)

    if args.list_trash:
        _print_trash(trash)
        return 0

    if args.empty_trash and not (args.yes or args.dry_run):
        print("--empty-trash permanently deletes trashed sessions' pages — this "
              "cannot be undone. Re-run with --yes to confirm, or --dry-run to preview.",
              file=sys.stderr)
        return 1

    # Gather every id targeted by a manual or bulk delete this run WITHOUT
    # mutating `trash` yet, so --dry-run can preview with zero side effects.
    known = _known_cards(sessions, args.output_dir)
    to_delete: list[str] = list(args.delete or [])
    if args.delete_older_than is not None:
        to_delete += _select_by_age(known, args.delete_older_than)
    if args.delete_project:
        to_delete += _select_by_project(known, args.delete_project)
    seen: set = set()
    to_delete = [sid for sid in to_delete if not (sid in seen or seen.add(sid))]

    if args.dry_run:
        did_anything = False
        if config_dirty:
            print(f"Would set retention_days={new_config['retention_days']!r}, "
                  f"purge_days={new_config['purge_days']!r}")
            did_anything = True
        if to_delete:
            print(f"Would trash {len(to_delete)} session(s):")
            for sid in to_delete:
                print(f"  {sid}")
            did_anything = True
        if args.restore:
            print(f"Would restore {len(args.restore)} session(s): {', '.join(args.restore)}")
            did_anything = True
        if args.empty_trash:
            print(f"Would permanently delete {len(trash)} trashed session(s)'s pages.")
            did_anything = True
        # Preview what the config-driven auto-sweep would ALSO do on a real run
        # (using the settings this same invocation would apply, so a combined
        # `--set-retention N --dry-run` previews accurately) — otherwise a
        # dry-run can print "nothing to do" while the very next real build
        # silently auto-trashes/purges a pile of sessions.
        effective_retention = new_config["retention_days"]
        effective_purge = new_config["purge_days"]
        if effective_retention is not None:
            restoring = set(args.restore or [])
            auto_preview = [sid for sid in _select_by_age(known, effective_retention)
                           if sid not in trash and sid not in restoring]
            if auto_preview:
                print(f"Auto-retention would additionally trash {len(auto_preview)} "
                      f"session(s) inactive {effective_retention:g}+ days:")
                for sid in auto_preview:
                    print(f"  {sid}")
                did_anything = True
        if effective_purge is not None:
            stale_preview = _stale_trash_sids(trash, effective_purge)
            if stale_preview:
                print(f"Auto-purge would additionally permanently remove "
                      f"{len(stale_preview)} trashed session(s) older than "
                      f"{effective_purge:g} days in trash.")
                did_anything = True
        if not did_anything:
            print("Nothing to do (no --delete/--delete-older-than/--delete-project/"
                  "--restore/--empty-trash/--set-retention/--set-purge given).")
        return 0

    if config_dirty:
        config = new_config
        save_config(args.output_dir, config)
        print(f"  retention_days={config['retention_days']!r}, purge_days={config['purge_days']!r}")

    trash_dirty = False
    if to_delete:
        n = _delete_sessions(to_delete, sessions, args.output_dir, trash, reason="manual")
        print(f"  trashed {n}/{len(to_delete)} session(s).")
        trash_dirty = True
        save_trash(args.output_dir, trash)
    if args.restore:
        n = _restore_sessions(args.restore, trash)
        print(f"  restored {n}/{len(args.restore)} session(s).")
        trash_dirty = True
        save_trash(args.output_dir, trash)  # persist promptly: shrinks the crash window
    if args.empty_trash:
        reappearing = _live_ids(sessions) & set(trash)
        n = _empty_trash(trash, args.output_dir)
        print(f"  permanently removed {n} trashed session(s).")
        _warn_reappearing(reappearing)
        trash_dirty = True
        save_trash(args.output_dir, trash)

    # Automatic, config-driven housekeeping (never reached under --dry-run — that
    # returns above — so a preview run changes nothing at all). Retention moves
    # sessions inactive past retention_days into trash (still recoverable); purge
    # then hard-deletes trash entries that have themselves aged past purge_days.
    # Both are off (None) by default, so this is a no-op until the user opts in.
    # A session named in THIS run's --restore is exempt from the same-run sweep —
    # otherwise an explicit restore of an old session would be silently undone
    # before the command even finishes (auto-retention would just re-select it).
    just_restored = set(args.restore or [])
    if config["retention_days"] is not None:
        aged = set(_select_by_age(known, config["retention_days"]))
        auto_sids = [sid for sid in aged if sid not in trash and sid not in just_restored]
        if auto_sids:
            n = _delete_sessions(auto_sids, sessions, args.output_dir, trash, reason="retention")
            if n:
                print(f"  retention: auto-trashed {n} session(s) inactive for "
                      f"{config['retention_days']:g}+ days.")
                trash_dirty = True
                save_trash(args.output_dir, trash)
        still_old = aged & just_restored
        if still_old:
            print(f"  note: {len(still_old)} restored session(s) are still past the "
                  f"current retention_days={config['retention_days']:g} — a later build "
                  f"(without --restore) will auto-trash them again unless you raise or "
                  f"disable retention: {', '.join(sorted(still_old))}")

    if config["purge_days"] is not None:
        stale = _stale_trash_sids(trash, config["purge_days"])
        if stale:
            reappearing = _live_ids(sessions) & stale
            n = _empty_trash(trash, args.output_dir, only=stale)
            print(f"  purge: permanently removed {n} session(s) trashed for "
                  f"{config['purge_days']:g}+ days.")
            _warn_reappearing(reappearing)
            trash_dirty = True
            save_trash(args.output_dir, trash)

    # Resolve cached/heuristic titles instantly (no network), render, and open — so
    # the dashboard appears right away. Only genuinely new/cold sessions need a slow
    # `claude -p` call; do those *after* opening, then rewrite the affected pages.
    cache: dict = {}
    cache_path = ""
    todo: list[tuple[Session, str, str]] = []
    if not args.no_titles:
        cache, cache_path, todo = resolve_cached_titles(sessions, args.output_dir)
        todo = [t for t in todo if t[0].session_id not in trash]  # don't spend on trashed sessions

    written, skipped, pruned, archived = write_site(
        sessions, args.output_dir, prune_orphans=args.prune_orphans,
        deleted_sids=frozenset(trash), trash=trash, config=config)
    stats = f"{written} page(s) rendered, {skipped} unchanged"
    if pruned:
        stats += f", {pruned} stale file(s) pruned"
    if archived:
        stats += f", {archived} archived (source removed, still listed)"
    print(f"Dashboard written to {index} ({stats})")
    if args.open:
        open_in_browser(index)

    if todo:
        if generate_titles(todo, cache, cache_path, args.title_model):
            # rewrite with the fresh titles
            write_site(sessions, args.output_dir, prune_orphans=args.prune_orphans,
                      deleted_sids=frozenset(trash), trash=trash, config=config)
            print(f"  titles updated for {len(todo)} session(s) — "
                  f"refresh the dashboard to see them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
