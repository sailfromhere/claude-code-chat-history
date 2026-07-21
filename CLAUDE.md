# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

A tool that renders Claude Code chat history into a **browsable static HTML dashboard**.
(It started as a single-chat "export to md/html/image" script — that pivoted on 2026-06-10
into a full dashboard. The single-chat export survives as a per-session `.md` download.)

Chat history lives in `~/.claude/projects/<project-dir>/<session-uuid>.jsonl` — one file per
session, one JSON object per line. Sessions resumed later share a `sessionId` across files.

## Architecture (as built)

```
chats_dashboard.py        # the whole generator (stdlib-only except vendored highlight.js)
assets/                   # vendored highlight.js + light/dark themes (copied into output)
~/.claude/commands/chats.md   # global slash command: runs the generator + opens browser
```

`python3 chats_dashboard.py [--open] [--no-titles] [--title-model M] [--projects-dir D] [--output-dir D] [--prune-orphans]`

Pipeline: scan `~/.claude/projects/*/*.jsonl` → group entries by `sessionId` (merges resumed
sessions) → per session compute metadata + token/cost usage → optionally generate LLM
titles → render a static site to `~/.claude/history-dashboard/`:
- `index.html` — three-pane UI: projects | session cards | transcript `<iframe>`. Client-side
  search/filter, date grouping, keyboard nav, drag-resizable panes (localStorage-persisted).
- `sessions/<id>.html` — one transcript page (loaded in the iframe), with a frozen header,
  "Show tool calls" toggle, and an Export `.md` download link.
- `sessions/<id>.md` — plain-markdown export (also the Export button's target).
- `assets/` — highlight.js + `hl.css`.

Files are written atomically (temp + `os.replace`); `index.html` is written last.

**Retention/archive (added 2026-07-14):** Claude Code itself deletes source `.jsonl` files older
than `cleanupPeriodDays` (default 30 — not set in this user's `~/.claude/settings.json`, so the
default applies). The dashboard is deliberately NOT a mirror of that: by default, a session whose
source `.jsonl` disappears keeps its rendered `sessions/<id>.{html,md}` AND stays **listed** in
`index.html`, flagged `🗄 archived` (dimmed card style), reconstructed from a snapshot of its
display fields (title/cost/tokens/counts/timestamps — see `_CARD_FIELDS`/`ArchivedCard` in
`chats_dashboard.py`) captured into `.archive-cards.json` on every run. Sidebar cost/token rollups
include archived sessions. Pass `--prune-orphans` to restore the old mirror-only behavior (delete
pages once their source is gone) — off by default because the safer behavior (keep everything) is
the sane default. `.render-manifest.json` drives incremental skip-if-unchanged rendering and (when
`--prune-orphans` is passed) which pages to delete; all three dotfiles (`.render-manifest.json`,
`.archive-cards.json`, `.title-cache.json`) live alongside `index.html` in the output dir and are
generator-owned (never hand-edit).

**Deletion, trash, and retention (added 2026-07-20):** the opposite control — removing sessions the
user doesn't want. Deletion is **soft**: `--delete SID...` (or bulk `--delete-older-than DAYS` /
`--delete-project NAME`) moves a session into `.deleted-sessions.json` (the trash), a pure
display/render filter applied in `write_site(deleted_sids=...)` — the rendered page is neither
re-rendered nor listed, but its `.render-manifest.json`/`.archive-cards.json` entries are carried
forward untouched, so `--restore SID...` (dropping it back out of trash) always brings it back
losslessly. **Trash only ever ADDS; the only code that hard-deletes pages and drops bookkeeping
entries is `--empty-trash` (gated behind `--yes` or `--dry-run`) or the automatic purge sweep** —
both go through `_empty_trash()`, which reuses the `--prune-orphans` path-traversal guard. Note: if
a session's source `.jsonl` is still live when purged, this same run's `write_site()` re-renders it
fresh afterward (the tool mirrors the source; it can't forget a session Claude Code still has) —
`main()` prints a `_warn_reappearing()` note when this happens. `--list-trash` prints the trash;
`--dry-run` previews any of the above (including what the config-driven sweep below would ALSO do)
with zero side effects. `.dashboard-config.json` holds `retention_days`/`purge_days` (both `null` =
off = today's keep-forever behavior), set via `--set-retention DAYS|off` / `--set-purge DAYS|off`;
every normal build then auto-trashes sessions older than `retention_days` (reason `"retention"`,
still recoverable) and auto-purges trash entries whose `deleted_at` has itself aged past
`purge_days`. A session named in the same invocation's `--restore` is exempt from that same run's
auto-retention sweep (otherwise an explicit restore of an old session would be silently undone
before the command finished — a real bug caught by adversarial review, since fixed and regression-
tested). The browser can't delete files itself (no server) — `index.html` embeds the trash list +
config read-only and offers a **command-builder UI** (⚙ panel, per-card 🗑) that builds the exact
`chats …` CLI command with a Copy button; `shQuote()` POSIX-shell-quotes any embedded user string
(project names) before it's shown. All five dotfiles are generator-owned; never hand-edit.

## Three entry points (least-Claude first)
1. Open the bookmarked `~/.claude/history-dashboard/index.html` (last snapshot — no Claude).
2. `chats` shell alias → `python3 ~/claude/export_chat/chats_dashboard.py --open` (refresh+view, no Claude).
3. `/chats` global slash command (refresh from inside any session).
Script location is **kept in the project** (single source of truth); the alias/command hardcode that path.

## Source data format

Each JSONL line has a top-level `type`. Relevant ones: `user`, `assistant`, `ai-title`
(title in the **`aiTitle`** field, keyed by `sessionId` — may live in a different file of a
resumed session). Skip `system` and metadata types.
- `user.message.content` is a string OR an array of blocks (`text`, `tool_result`).
- `assistant.message.content` is always an array: `text`, `thinking` (skip — encrypted), `tool_use`.
- `assistant.message.usage` has token counts; `assistant.message.model` is the model id.

## Rendering rules — see project memory before changing
The hard-won handling of non-human / injected content, attribution traps, ANSI, performance,
titling, layout, etc. is documented in project memory:
`~/.claude/projects/-Users-kevinyu-claude-export-chat/memory/jsonl_rendering_quirks.md`.
**Read it before touching the renderer** — most edge cases there were found by user testing and
are easy to regress (e.g. wrapper-only vs `isMeta` detection of injected content; pairing
tool_result→tool_use; never attributing harness-injected text to the user).

## Project conventions
- `TODO.md` and `IMPROVEMENTS.md` are maintained continuously (decisions + status).
- Validate the generator after edits (it ships HTML+JS+CSS as Python strings — easy to break);
  watch for an infinite-loop class of bug in the markdown parser, and always re-run on real data.
- **Run the test suite after any renderer/parser change:** `python3 -m unittest discover -s tests`
  (stdlib `unittest`, synthetic fixtures in `tests/fixtures.py`, ~0.8s). It locks in the documented
  attribution traps, markdown termination (subprocess+timeout), and cost math. When user testing
  finds a new render bug, **add a fixture reproducing it before fixing** — the corpus grows from
  real bugs so they can't silently regress.
- Heads-up when grepping this repo's own dashboard output: it contains this project's source as
  transcript text, so naive greps for code strings false-positive. Check the real `<script>`/elements.
