"""Synthetic fixture builders for the chats_dashboard tests.

No real session data is used (privacy + brittleness — real captures carry the
secrets this tool surfaces). These helpers emit the entry dicts the generator
expects, so trap cases read declaratively in the tests.

Importing this module also puts the project root on sys.path, so every test can
`from fixtures import cd, ...` without worrying about the discover invocation.
"""
from __future__ import annotations

import itertools
import json
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import chats_dashboard as cd  # noqa: E402

# Monotonic ids/timestamps so entries sort deterministically in the order built.
_uid = itertools.count(1)
_BASE = datetime(2026, 6, 1, 12, 0, 0)


def _ts(step: int) -> str:
    return (_BASE + timedelta(seconds=step)).isoformat()


def now_offset_iso(days: float = 0, seconds: float = 0) -> str:
    """A timestamp relative to *now* — for clock-dependent code (_date_group)."""
    return (datetime.now().astimezone() - timedelta(days=days, seconds=seconds)).isoformat()


def _entry(role: str, content, *, is_meta: bool = False, model=None, usage=None) -> dict:
    n = next(_uid)
    msg: dict = {"role": role, "content": content}
    if model is not None:
        msg["model"] = model
    if usage is not None:
        msg["usage"] = usage
    e = {"type": role, "uuid": f"u{n}", "timestamp": _ts(n), "message": msg}
    if is_meta:
        e["isMeta"] = True
    return e


# --- user entries ---------------------------------------------------------- #
def user_msg(text: str, **kw) -> dict:
    """A plain human message (string content)."""
    return _entry("user", text, **kw)


def user_blocks(blocks: list[dict], **kw) -> dict:
    return _entry("user", blocks, **kw)


def sdk_submitted_msg(text: str, *, source: str = "both", **kw) -> dict:
    """A user prompt submitted programmatically by Claude/a tool (headless
    `claude -p`), NOT typed by the human. Marked by entrypoint:"sdk-cli" and/or
    promptSource:"sdk". `source` toggles which markers are present to mimic
    different CLI versions: "both", "entrypoint" (older, no promptSource), "sdk"."""
    e = _entry("user", text, **kw)
    if source in ("both", "entrypoint"):
        e["entrypoint"] = "sdk-cli"
    if source in ("both", "sdk"):
        e["promptSource"] = "sdk"
    return e


def typed_msg(text: str, **kw) -> dict:
    """A user prompt the human typed (entrypoint:"cli", promptSource:"typed")."""
    e = _entry("user", text, **kw)
    e["entrypoint"] = "cli"
    e["promptSource"] = "typed"
    return e


def slash_command(name: str, args: str = "") -> dict:
    """The <command-name> marker entry a slash command writes (wrapper-only)."""
    body = (f"<command-message>{name.lstrip('/')}</command-message>\n"
            f"<command-name>{name}</command-name>\n"
            f"<command-args>{args}</command-args>")
    return _entry("user", body)


def expansion(prompt_text: str) -> dict:
    """The separate user-text entry holding the prompt a slash command expands to."""
    return _entry("user", prompt_text, is_meta=True)


def skill_body(name: str, extra: str = "<search_first>do the search</search_first>") -> dict:
    """A skill body injected when a Skill runs (isMeta, not wrapper-only)."""
    text = f"Base directory for this skill: /Users/x/.claude/skills/{name}\n\n{extra}"
    return _entry("user", text, is_meta=True)


def bash_io(cmd: str, stdout: str = "", stderr: str = "") -> dict:
    """A `!` bash command + its output. stdout/stderr are HTML-entity-encoded at
    the source, exactly as Claude Code records them."""
    body = (f"<bash-input>{cmd}</bash-input>\n"
            f"<bash-stdout>{stdout}</bash-stdout><bash-stderr>{stderr}</bash-stderr>")
    return _entry("user", body)


def interrupt_entry(tool_use: bool = False) -> dict:
    """The user entry the harness writes when the human presses Esc mid-turn.
    Real shape (verified 2026-07-09): a list with ONE text block whose text is
    exactly the sentinel — never mixed with other blocks."""
    text = ("[Request interrupted by user for tool use]" if tool_use
            else "[Request interrupted by user]")
    return _entry("user", [{"type": "text", "text": text}])


def compact_summary_entry(summary: str = (
        "This session is being continued from a previous conversation that ran "
        "out of context. The summary below covers the earlier portion.")) -> dict:
    """The continuation-summary user entry written after /compact. Marked by
    isCompactSummary (isMeta is ABSENT on it, so the meta rule can't catch it)."""
    e = _entry("user", summary)
    e["isCompactSummary"] = True
    e["isVisibleInTranscriptOnly"] = True
    return e


def image_block(data: str = "iVBORw0KGgoAAAANSUhEUgFAKE", media: str = "image/png") -> dict:
    """A pasted-image content block (base64, exactly as Claude Code records it)."""
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


def tool_result_entry(tool_use_id: str, content, is_error: bool = False) -> dict:
    """A user entry whose only content is a tool_result (the harness's reply)."""
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return _entry("user", [block])


# --- assistant entries ----------------------------------------------------- #
def assistant_text(text: str, **kw) -> dict:
    return _entry("assistant", [{"type": "text", "text": text}], **kw)


def assistant_blocks(blocks: list[dict], **kw) -> dict:
    return _entry("assistant", blocks, **kw)


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def tool_use_block(name: str, inp: dict, tid: str) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def assistant_usage(model: str, *, input=0, output=0, cache_read=0, cache_write=0) -> dict:
    usage = {
        "input_tokens": input,
        "output_tokens": output,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }
    return _entry("assistant", [{"type": "text", "text": "ok"}], model=model, usage=usage)


def ai_title_line(session_id: str, title: str) -> dict:
    """An ai-title-type JSONL line (top-level, keyed by sessionId)."""
    return {"type": "ai-title", "sessionId": session_id, "aiTitle": title}


def turn_duration_line(parent_uuid: str, ms: int, session_id: str = "s1") -> dict:
    """A system/turn_duration line — parentUuid always points at the turn's
    final ASSISTANT entry (verified 2026-07-09, 1028/1028)."""
    return {"type": "system", "subtype": "turn_duration", "durationMs": ms,
            "parentUuid": parent_uuid, "sessionId": session_id}


def agent_name_line(session_id: str, name: str) -> dict:
    """An agent-name JSONL line — the harness-assigned session display name
    (kebab-case, e.g. 'fix-sticky-header-gap'). Marks its OWN session; a session
    can carry several as work progresses (the last one is current)."""
    return {"type": "agent-name", "sessionId": session_id, "agentName": name}


# --- assembling sessions / files ------------------------------------------- #
def make_session(entries: list[dict], *, session_id: str = "s1",
                 project: str = "/Users/x/myproj", ai_title: str | None = None) -> "cd.Session":
    """Build a Session and run the real metadata/usage computation over it."""
    s = cd.Session(session_id=session_id, project_path=project)
    s.entries = list(entries)
    s.ai_title = ai_title
    cd._compute_metadata(s)
    return s


def write_jsonl(directory: str, filename: str, entries: list[dict], *,
                session_id: str = "s1", cwd: str = "/Users/x/myproj") -> str:
    """Write entries as a .jsonl file under directory/<project>/, the way
    load_sessions expects to find them (sessionId + cwd stamped per line)."""
    proj_dir = os.path.join(directory, "proj")
    os.makedirs(proj_dir, exist_ok=True)
    path = os.path.join(proj_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        for e in entries:
            line = dict(e)
            if line.get("type") in ("user", "assistant"):
                line.setdefault("sessionId", session_id)
                line.setdefault("cwd", cwd)
            fh.write(json.dumps(line) + "\n")
    return path


def turns_by_role(session: "cd.Session", role: str) -> list["cd.Turn"]:
    return [t for t in cd._render_turns(session) if t.role == role]


def all_turn_html(session: "cd.Session") -> str:
    return "\n".join(t.html for t in cd._render_turns(session))
