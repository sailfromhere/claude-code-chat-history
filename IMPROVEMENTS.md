# IMPROVEMENTS.md

> Project/engineering work and design decisions. Actions the user must take in the real world go in TODO.md.

Tracks all design considerations, open questions, and improvement ideas for export-chat.
Categorization: **P0** = must decide before shipping | **P1** = important, should decide soon | **P2** = nice to have, can defer

Status values: `TBD` | `Decided: <summary>` | `Built`

> **This file is the ACTIVE backlog only.** Completed / decided / dropped items live in
> **`IMPROVEMENTS_DONE.md`** (full detail, verbatim). The "Closed" index at the bottom lists
> what's done with a one-line status + date — open the archive when a stub is relevant.
> When you finish an item here, MOVE its full entry to the archive and add a one-liner below.

---

## Watch items (not action items, just tracked)

- **P3 — Newer CLIs also log slash commands as `system/local_command` lines** (dashboard skips
  `system`). Harmless duplicate today; if a future CLI drops the user-role copy, commands vanish
  from transcripts. (From the 2026-07-09 codebase review — most of that review's findings are Built,
  see `IMPROVEMENTS_DONE.md`.)
- **Full-text search** (P2, deferred): sidecar `search-index.js` if snippet search proves too shallow.
- **Big transcripts** (P2): the 2 MB session renders to a large per-session page; fine since pages
  load independently, but watch render time.

---

### OPEN — date-group tally is skewed by last-activity bucketing (P1, TBD — don't fix yet)
A session is bucketed into a date group by its **`last_ts`** (most recent activity). So sending
one new message today pulls a long-running session — **and its entire accumulated cost/tokens** —
into "Today", even if nearly all of that cost was incurred on a previous day. This makes the
per-date-group tally read as "cost of sessions last touched in this range," not "cost incurred in
this range" — less useful than it looks, and arguably misleading for spend-by-day.
- **Why it's non-trivial:** a true cost-by-day tally requires attributing each assistant turn's
  usage to *its own* timestamp and re-bucketing at the message level (cross-day sessions split
  across groups), not the session level. That also forces a decision about whether the session
  *card* can appear under multiple date groups, or only its cost does.
- **Options to weigh later:** (a) leave session-level bucketing, relabel the tally to set
  expectations ("sessions active in …"); (b) per-message cost attribution → true spend-by-day,
  card stays in its last-activity group but the tally sums per-day message usage; (c) drop the
  date-group tally and rely on per-project + per-session only.
- **User decision (2026-06-11):** noted, deferred — needs thinking through. Do not implement yet.
- **Status:** TBD.

---

## P0 — Must decide before shipping

_Note (2026-07-14 archival pass): these predate the 2026-06-10 pivot to a standing dashboard and may
be partially stale — the pivot answered some of this in practice without formally closing these
entries. Left open rather than closed unilaterally; worth a fresh look if revisited._

### Session selection UX
How does a user identify and pick the session to export? UUIDs are meaningless at a glance.
- **Options:**
  - List sessions with title, project, date, message count — user picks from list
  - "Current session" magic value when invoked from inside Claude Code
  - Accept UUID directly (power-user fallback)
  - Combine: default to current session, offer list if user wants to choose another
- **Status:** TBD

### Export scope
What unit is being exported?
- **Options:**
  - Single session (one `.jsonl` file)
  - All sessions for a project (merged or separate files)
  - All sessions across all projects
  - Date range filter
  - Note: a conversation resumed across sessions shares a `sessionId` — merge those?
- **Status:** TBD

### Export naming convention
What should the output file be named?
- **Options:**
  - `<session-uuid>.md` (unique, ugly)
  - `<ai-title-slug>_<date>.md` (e.g. `export-chat-tool_2026-05-28.md`)
  - `<project-name>_<date>_<short-uuid>.md` (human + unique)
  - Let user provide a name (adds friction)
  - Combine: auto-name with `<title-slug>_<date>`, prompt to confirm or rename
- **Status:** TBD

### Command/noise filtering
Some user messages are internal commands, not human text (e.g. `<command-message>init</command-message>`). System prompts are injected noise.
- **Options:**
  - Strip all `<command-*>` tagged messages
  - Strip `system` type entries entirely
  - Expose as `--include-system` flag
- **Status:** TBD

---

## P1 — Important, decide soon

### HTML/image design
What does "looks nice" mean concretely?
- **Options to decide:**
  - Dark mode / light mode / respect system preference (CSS `prefers-color-scheme`)
  - Show timestamps per message?
  - Show model name, token counts in header?
  - Syntax highlighting in code blocks? (e.g. highlight.js, inline)
  - Collapsible tool use sections (JS needed)?
- **Status:** TBD

---

## P2 — Nice to have, defer

### Empty-state / no-sessions handling — P2, TBD
If `~/.claude/projects` has no parseable sessions (fresh machine, or `--projects-dir` points
somewhere empty), the dashboard currently renders an empty three-pane shell. Add a friendly
empty-state: a centered "No Claude Code sessions found — start chatting and re-run `chats`" card,
and skip writing per-session pages. Low effort; only matters on first run / misconfiguration.
- **Status:** TBD.

### `/btw` and `/fork` session handling (low priority open thread)
How Claude Code's `/btw` and `/fork` show up in the dashboard's data (verified 2026-06-12, see project memory `data_sources_and_session_kinds`).
- **`/fork`** — spins up a NEW `sessionId` → its own `.jsonl` → **already captured** as a separate session card (no code needed). Caveat: standalone card, no visual link to the parent it forked from; if fork copies prior turns, looks like a near-duplicate. Possible polish later: detect + badge/collapse forks under their parent.
- **`/btw`** — an *escaped* `/btw` writes ONLY to global `~/.claude/history.jsonl` (which the dashboard never reads), no transcript → **invisible to the dashboard**. Behavior of a *completed* `/btw` (merge into current sessionId vs new sessionId) is **unsampled** — need a real non-escaped capture before deciding any handling.
- **Status:** TBD, low priority. Next step when revisited: capture a completed `/btw`, add a fixture, then decide.

### AskUserQuestion "chose to discuss instead" outcome not reflected — P2, TBD
When Claude shows the options window and the user picks the **last/Other path to discuss it more**
(i.e. rejects the tool call to talk rather than selecting a listed option), the dashboard card shows
the question + all options with **none marked**, indistinguishable from a never-answered question —
it hides that the user deliberately chose to discuss.
- **Validated data shape (2026-06-13, this session):** the rejection is a `tool_result` paired to the
  `AskUserQuestion` tool_use, with `is_error: true` and content like *"The user doesn't want to proceed
  with this tool use… The user wants to clarify these questions… Questions asked: - \"…\" (No answer
  provided)"*. A real answer instead has content shaped `"Question"="Chosen Label" …`.
- **Why it renders blank:** `_render_askq` (chats_dashboard.py) finds the chosen label via
  `re.findall(r'="([^"]+)"', answer)`. The rejection text has no `="…"` token → `chosen` is empty →
  no option marked, and the card isn't flagged as rejected/error either.
- **Fix options:** detect `result.is_error` + the "doesn't want to proceed"/"No answer provided"
  signature → render an explicit outcome line on the card (e.g. *"↪ Chose to discuss instead — no
  option selected"*), distinct from a genuinely unanswered question. Related nuance: a typed **"Other"
  custom answer** (not a listed label) also won't match the regex — its text lands in the *next* user
  message; consider surfacing that too.
- **Add a fixture before fixing** (per project workflow): synthetic AskUserQuestion tool_use + an
  is_error rejection tool_result → assert the card shows the "discuss instead" outcome.
- **User (2026-06-14):** logged as an idea, not prioritized. Status: TBD.

### Filter by prompt provenance (hide/show SDK-run chats) — P2, TBD
Now that Claude/tool-run sessions are flagged (`initiated_by_sdk`, `⚙ SDK` pill), add a way to
filter the card list by it — e.g. a "hide SDK runs" toggle, or make the pill click-to-filter
(like project rows already do). Worth it only if SDK-run sessions start to pile up and clutter
the list. **User (2026-06-13): noted, non-priority right now.** Status: TBD.

### Privacy / redaction
Chat history can contain sensitive data (API keys in tool outputs, private code).
- **Options:** Warning on export, `--redact-tool-outputs` flag, regex-based scrubbing
- **Status:** TBD

### Cross-platform support
`~/.claude` and `~/Downloads` are macOS/Linux paths. Windows uses different conventions.
- **Status:** TBD (macOS-first for now is fine)

### Dark/light mode toggle in HTML export
JS-powered toggle in the HTML output itself (independent of the already-built OS-preference
auto-detection via `prefers-color-scheme`).
- **Status:** TBD

### Export a range of messages
E.g. export only the last 20 messages, or messages from a specific time window. Useful for sharing snippets without the full context.
- **Status:** TBD

### Token/cost summary in export header
Show total tokens used, cache hits, model used — useful for cost-awareness. **Verified NOT built**
(2026-07-14): `_session_markdown`'s header is just title + project + date range, no usage line.
- **Status:** TBD

---

## Closed (full detail → `IMPROVEMENTS_DONE.md`)
- Dashboard becomes a durable, browsable archive (kept + listed past Claude Code's 30-day cleanup) — Built 2026-07-14
- Interrupts / compact summaries / pasted images / agent-name / TodoWrite→Task* / plan outcome badges / tool-chip summaries / per-turn model+duration — Built 2026-07-09 (codebase review)
- Nested markdown lists, `_atomic_write_json` cleanup, PRICING prefix fallback, memoized re-parsing, incremental `write_site` — Built 2026-07-09
- Substring pre-filter before `json.loads` — Measured & REJECTED 2026-07-09 (don't re-try)
- `TODO.md` untracked from the public repo — Built 2026-07-09, re-verified 2026-07-14
- Pricing table refresh (fable-5/mythos-5/sonnet-5) — Built 2026-07-01
- Sticky date-header leak fix — Built 2026-07-01
- 2026-06-10 pivot: dashboard type, file structure, search depth, tool use rendering, location,
  command name, entry points, layout, session titles, transcript fidelity — Decided/Built 2026-06-10
- Stale dashboard / in-page refresh (Option A/B/C) — Decided 2026-06-12 (Option C, don't build)
- Multi-file/multi-session merge (both old duplicate entries) — Decided/Built (`sessionId` grouping)
- Automated test harness — Built 2026-06-11
- Aggregate cost overview (scoped tallies) — Built 2026-06-11
- Plan-mode rendering — Built 2026-06-29
- Header meta row justified-instead-of-left-aligned — Fixed 2026-06-16
- Selection highlight (soft accent fill) — Built 2026-06-13
- SDK "not typed by you" card pill — Built 2026-06-13
- SDK-submitted prompt badge (per-turn) — Built 2026-06-12
- Active-session badge — Built 2026-06-11
- Thinking block visibility — Closed 2026-07-09 (all blocks empty, nothing to render)
- Tier-2 truly-detached background titling — Decided: deferred 2026-06-16
- Old P0 "Tool use rendering" — Decided 2026-06-10 (resolved by the pivot)
- Old P1 "Format selection UX" — Decided (pre-pivot)
- Old P1 "Export location" — Decided (pre-pivot)
- Old P1 "Primary interface: slash command vs. standalone script" — Decided/Built
