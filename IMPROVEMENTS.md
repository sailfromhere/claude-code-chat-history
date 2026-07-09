# IMPROVEMENTS.md

> Project/engineering work and design decisions. Actions the user must take in the real world go in TODO.md.

Tracks all design considerations, open questions, and improvement ideas for export-chat.
Categorization: **P0** = must decide before shipping | **P1** = important, should decide soon | **P2** = nice to have, can defer

Status values: `TBD` | `Decided: <summary>` | `Built`

---

## Codebase review — 2026-07-09 (all TBD unless noted)

Fidelity gaps verified against real `~/.claude/projects` data (counts as of 2026-07-09):

- **P1 — Interrupts render as user speech.** `Built (2026-07-09):` exact-sentinel match (`_is_interrupt`) → red `⏹ Interrupted by user` status row (`(during tool use)` variant); excluded from n_user/snippet/titler; human prose *quoting* the sentinel stays visible (exact-match guard). Verified on real data: 15 markers, 0 left in user bubbles.
- **P1 — Compact summary renders as a "You" bubble.** `Built (2026-07-09):` `isCompactSummary` → collapsed `🗜 conversation compacted — continuation summary` chip; excluded from n_user/snippet/titler. **Real-data catch:** a manual /compact's summary directly follows the `⌘ /compact` command entry, so `_expansion_uuids` was folding it as the command's "expanded prompt" — compact/interrupt entries are now excluded from expansion-claiming (regression tests for both). `system/compact_boundary` metadata (trigger, preTokens) still unrendered — fold into the chip if system-line ingestion ever lands.
- **P1 — Pasted images silently dropped.** `Built (2026-07-09):` `image` blocks embed as `<img src="data:…;base64,…">` (max-height 340px, click-to-toggle full size, lazy-loaded, `_esc` on media/data), capped at `MAX_IMAGE_B64` (2M b64 chars ≈1.5MB) → placeholder chip above; malformed source/data → placeholder, never a crash (adversarial-review finding: unguarded types would have aborted the whole generation). Counts as visible content for the Tools toggle. 63 images embed on real data.
- **P2 — `agent-name` entries ignored.** `Built (2026-07-09):` ingested in `load_sessions` (last per session wins **by file mtime** — the lines carry no timestamp, so glob order was nondeterministic across resumed files; adversarial-review finding); title fallback between ai_title and snippet (truncated to `TITLE_FALLBACK_CHARS`, second review finding); added to the card's `data-search` haystack so the harness name is searchable even when an LLM title displays. Card chip not built (visual call, revisit if wanted).
- **P2 — TodoWrite renders as a JSON chip.** `Superseded (2026-07-09):` **zero** TodoWrite calls in the entire corpus — the CLI moved to TaskCreate/TaskUpdate. Those get readable chip summaries instead (`subject`, `#id → status`); a stateful checklist card reconstruction from TaskUpdate deltas is not worth it.
- **P2 — Plan card doesn't show approval outcome.** `Built (2026-07-09):` `_plan_outcomes` pairs each plan-file Write with the next ExitPlanMode's tool_result (verified shapes: 96× "User has approved your plan", 3× is_error rejection) → `✓ approved` / `✗ rejected` badge on the plan-card header. No result → no badge. Multiple plans per session pair independently (cross-pairing test).
- **P2 — `_tool_summary` misses newer tools.** `Built (2026-07-09):` key list gained `skill`/`subject`/`description` (before `prompt`, so Agent chips show the description, Bash still shows its command); TaskUpdate special-cased to `#id → status`.
- **P3 — Per-turn model + duration.** `Built (2026-07-09), user picked from a rendered mockup:` **model → a divider row on switch** (mockup C; buffered so an invisible switching entry — empty thinking block — never strands a divider; 30 dividers on real data) and **durations on every turn** (mockup D-all): `system/turn_duration` lines ingested (`parentUuid` → the turn's final assistant entry, verified 1028/1028) → `⏱ 2m 41s` in the turn timestamp; duration count+sum folded into the render fingerprint since those lines change neither entry count nor last_ts. Third adversarial review found + fixed: `_ptext` memo key was poisonable from crafted jsonl (now stripped at the load boundary), prune could delete files the generator never created (now scoped to previous-manifest entries only, with a path-part guard).
- **P3 — Watch item:** newer CLIs also log slash commands as `system/local_command` lines (dashboard skips `system`). Harmless duplicate today; if a future CLI drops the user-role copy, commands vanish from transcripts.
- **Closable: thinking-block visibility (P2 item below).** All 7,599 thinking blocks in real data have an EMPTY `thinking` field (signature only) — there is nothing to render. Optionally a "✻ thought" marker; otherwise close.

Code health / perf (defer perf until measured — full run is ~0.2s):

- **P2 — Markdown: nested lists flatten.** `Built (2026-07-09):` `_render_list` renders consecutive items with an indent stack (deeper → nested `<ul>`/`<ol>` as a sibling of the previous `<li>` — deliberate simplicity, browsers render it nested); `- [ ]`/`- [x]` → ☐/☑ `li.task`. Bounded by construction (pops ≤ pushes); two new pathological corpus entries (200-deep nesting, ragged indents). Multi-line list items still unhandled (they end the list — acceptable).
- **P2 — `_atomic_write_json` tmp path isn't pid-suffixed.** `Built (2026-07-09):` delegates to `_atomic_write_text`.
- **P3 — PRICING prefix-match fallback.** `Built (2026-07-09):` `_price_for` — exact match, else dash-boundary prefix (`claude-haiku-4-5-20251001` prices as haiku; `claude-sonnet-55` does NOT match sonnet-5 — test).
- **P3 — Repeated re-parsing.** `Built (2026-07-09):` `_plain_user_text` memoized on the entry dict (`_ptext` key); `_expansion_uuids`/`_result_map` cached per Session (`_session_expansions`/`_session_results` — safe because entries are immutable after load). Load 1.52s→1.29s, md render 0.29s→0.22s on 141 sessions / 72k lines.
- **P3 — `write_site` rewrites everything every run.** `Built (2026-07-09):` incremental rendering via `.render-manifest.json` — per-session fingerprint over (entry count, first/last ts, title, summary, project_path, generator-source+TZ hash); skip render+write on match, self-heal missing files, prune orphan pages of deleted sessions, index always rewritten last. **Steady state 3.1s → 1.27s** (all remaining time is json parsing). The live session's fingerprint changes every run so it always re-renders — correct, and only 1 page. Mutation-verified tests in `tests/test_writesite.py`.
- **Measured & REJECTED (2026-07-09, don't re-try): substring pre-filter before `json.loads`** (skip lines whose type we ignore): 0.86s vs 0.79s baseline — the per-line needle scans cost more than the skipped parses. Parsing is at its practical floor; the next step would be a file-level parsed-entry cache (pickle), rejected for staleness/complexity.
- **Housekeeping (user decision): `TODO.md` is git-tracked in the public repo**, contra the global gitignore rule. Untrack (`git rm --cached`) + add to .gitignore — needs user OK since the repo is public (already published in history).

## Log — 2026-07-01

- **Pricing table refresh** — `Built:` added `claude-fable-5` / `claude-mythos-5` ($10/$50 per MTok) and `claude-sonnet-5` ($3/$15, standard rate — not the temporary $2/$10 intro) to `PRICING` in `chats_dashboard.py`. Sonnet 5 and Fable/Mythos sessions were previously counted as unpriced (shown with the `+` incomplete-cost marker).
- **Sticky date-header leak fix** — `Built:` the pinned group header (`TODAY`, etc.) left a gap on top through which the previous card leaked. Root cause: the `.cardlist` scroll container's `padding-top:10px` — a `position:sticky;top:0` element is clamped to its containing block (padding-excluded content box), so top padding pins the header that far below the scrollport and content scrolls through the gap. Fix: `.cardlist{padding:0 10px 10px}` + `.cardlist>:first-child{margin-top:10px}` for breathing room; header keeps opaque `--bg` + `z-index:10`. NOT the flex `gap` (a first attempt covering the flex gap did nothing). Verified with headless system Chrome (extension was down) — see project memory `build-test-harness-learnings`.

## Pivot 2026-06-10 — product is now a dashboard, not a single-chat exporter

The slash command now opens a **static HTML dashboard** to browse *all* Claude Code history.
Single-chat export becomes an in-page Export button. Decisions made in the pivot:

- **Dashboard type** — `Decided:` static HTML site, opened in browser (no server, no lingering process).
- **File structure** — `Decided:` hybrid. Small `index.html` (metadata + snippets) + per-session `sessions/<id>.html`. Plain `<a href>` navigation, because `fetch()` is blocked over `file://` (CORS) — a lazy-loading SPA would silently break on double-click.
- **Search depth** — `Decided:` titles + project + ~200-char snippet, client-side filter. Keeps index tiny at scale. Full-text deferred (would bloat index to MBs).
- **Tool use rendering** — `Decided:` collapsible sections, summarized by default. (Resolves the P0 "Tool use rendering" item below.)
- **Location** — `Decided:` `~/.claude/history-dashboard/`, regenerated each run. (Standing tool, not a Downloads export.)
- **Command name** — `Decided:` `/chats`.
- **Entry points / decoupling** — `Decided:` viewing needs no Claude session (static files). Generator is standalone `python3 chats_dashboard.py --open`. Three paths: open the HTML directly; `chats` shell alias (refresh+view, no Claude); `/chats` global slash command (works from any dir). The slash command is a convenience, not a requirement.
- **Layout** — `Built:` two-pane master–detail (left card list + right `<iframe>` transcript). iframe chosen over a fetch-based SPA because `fetch()` is blocked over `file://`.
- **Session titles** — `Built:` LLM-generated title + one-line summary via the local `claude -p --model haiku --no-session-persistence`, cached by content fingerprint in `.title-cache.json`. `--no-titles` for offline/heuristic mode. Replaced uninformative first-sentence/`/init` titles.
- **Transcript fidelity (from user testing)** — `Built:` slash-command boilerplate folded under `⌘ /cmd`; tool results paired under Claude's call (not attributed to user); ANSI codes stripped; command stdout shown in full; `No response requested.` sentinel filtered.

### Open questions raised by the pivot
- **Stale dashboard / in-page refresh** (P1, TBD): a regenerated static site is a snapshot; the active session keeps growing after generation. Today you re-run via the `chats` alias or `/chats`. User wants to refresh *from the page* without touching the terminal (2026-06-12).
  - **Hard constraint:** the page is static HTML opened over `file://`. A static page can't regenerate itself, and browsers block `fetch()` over `file://`. So real in-page refresh is **impossible without a local server** — there is no no-server option that actually works (a reload button just re-reads the same static HTML).
  - **Option A — `--serve` mode + Refresh button:** ship a `python3 chats_dashboard.py --serve` that runs a tiny stdlib `http.server` on localhost; you open `http://localhost:PORT` instead of the file; a Refresh button hits a `/regenerate` endpoint that re-runs `load_sessions`+`write_site`, then reloads. Cost: moderate; introduces a long-running process + port, and shifts the *live* entry point from the `file://` bookmark to localhost (the bookmark still works as a frozen snapshot).
  - **Option B — `--serve` + `--watch` auto-refresh:** same server, but it watches `~/.claude/projects` and the page polls a version endpoint and reloads itself — zero clicks. Cost: higher (file-watching/mtime scan, debounce the growing live session, poll loop).
  - **Option C — don't build, document:** keep the static-file design; the `chats` alias / `/chats` are already one-step refreshes. Add only a "generated at <time>" freshness hint so staleness is visible.
  - **Tension:** A/B conflict with the tool's stated "no server, no lingering process" design value (see Pivot Dashboard-type decision). User invited the "just document" off-ramp if it's getting too complicated.
  - **Decided (2026-06-12): Option C — do NOT build in-page refresh.** Keep the static-file, no-server design; refresh stays the existing `chats` alias / `/chats`. Rationale: a localhost server reverses the deliberate "no server, no lingering process" decision (Pivot, above) to save a single terminal command, while two low-friction refresh paths already exist. Not worth the operational surface (port, long-running process). Even the small "generated at" freshness stamp was declined for now. If the terminal hop later becomes a real annoyance, build Option A (server + Refresh button); skip Option B (auto-watch is too much machinery for the benefit).
  - **Status:** Decided: Option C (don't build).
- **Multi-file session merge** (P1, TBD): sessions resumed across files share `sessionId` — merge into one transcript vs list separately. Leaning merge.
- **Full-text search** (P2, deferred): sidecar `search-index.js` if snippet search proves too shallow.
- **Big transcripts** (P2): the 2 MB session renders to a large per-session page; fine since pages load independently, but watch render time.

---

## Testing / regression safety

### Automated test harness
The generator emits HTML/JS/CSS as Python strings and is documented as "easy to break"
(a markdown-parser infinite loop and an f-string-backslash error both shipped). Until now the
only safety net was re-running on real data manually.
- **Decided (2026-06-11):** stdlib `unittest` harness in `tests/`. Scope = Tier 1 (documented
  attribution traps + markdown termination + cost math + write_site smoke) **plus** markdown
  detail correctness. Termination tested via subprocess+timeout (a real hang fails one test
  cleanly). Fixtures are **synthetic only** (privacy + brittleness — real captures carry the
  secrets this tool surfaces). Clock-dependent code tested with now-relative timestamps, no
  production refactor. Run: `python3 -m unittest discover -s tests`.
- **Workflow going forward:** when a render bug is found by manual testing, add a small fixture
  reproducing it, then fix — so it can't silently regress. The corpus grows from real bugs.
- **Status:** Built (2026-06-11). 7 files in `tests/`, 48 tests, ~0.8s. Mutation-verified the suite
  isn't vacuous (disabling the sentinel filter fails a test). Runner documented in CLAUDE.md.

### Aggregate cost overview — Built (2026-06-11), reworked to SCOPED tallies
A single all-time total flattens out as history grows across many projects/months, so it was
replaced with three *scoped* tallies (more meaningful as the corpus grows):
1. **Per-session** — each session card shows the same usage line as the transcript header
   (`_usage_html`: in · out · cache · ~$cost, with breakdown tooltip).
2. **Per-project** — each left-pane project row shows `~$cost · Ntok`; the "All projects" row
   shows the grand total (replaces the old footer strip, which was removed as redundant).
3. **Per-date-group** — each Today/Yesterday/… header shows a tally of the cards in it.
   Computed in JS (`updateHeaders`) over *visible* cards, so it re-sums when you filter by
   project/search instead of showing a stale all-projects number. Cards carry
   `data-cost`/`data-tok`/`data-inc` for this. `cost_complete` → `+` suffix throughout.
   NOTE: the JS path (group tallies) needs a browser to verify — no node in this env.

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

### Plan-mode rendering — Built (2026-06-29)
`Write` tool calls targeting `~/.claude/plans/*.md` now render as expanded markdown cards
(not collapsed diffs). Card shows a `📋 Plan` header, file path, rendered markdown body, and
an optional "Approved actions" list (from `allowedPrompts` in old-format `ExitPlanMode` calls).

Key design decisions:
- **Always visible** — plan cards escape the Tools toggle. `_has_visible_nontool()` now counts
  `_is_plan_write()` as visible, so the assistant turn is never `tools-only`. CSS class `plan-card`
  has no `tool-use` class → not hidden by `body.hide-tools .tool-use{display:none}`.
- **Single source** — old sessions have both a `Write` AND an `ExitPlanMode` with `input.plan`
  inline (same content). Only the `Write` renders a card; `ExitPlanMode` falls back to the normal
  collapsed chip. Avoids double-rendering.
- **Data-shape:** current format → `Write` to `~/.claude/plans/*.md` + near-empty `ExitPlanMode`;
  old format → same Write + `ExitPlanMode` with `plan`/`planFilePath`/`allowedPrompts` fields.
  Plan-mode also emits `attachment {type:"plan_mode", planFilePath, planExists}` in user turns —
  metadata Claude uses; not rendered by the dashboard.

**Status:** Built.

### Header meta row justified instead of left-aligned — Fixed (2026-06-16)
**Symptom:** the transcript-pane header's second row (project badge · date range · `Np · Nr` ·
usage) spread across the pane (project badge stranded left, the rest flung right) instead of the
original left-aligned grouping. User hadn't touched the header.
**Root cause — shared-class leak.** `.date` is reused by *both* the session-card list (`.card-top`)
and the transcript header (`.hmeta`). On 2026-06-13 (SDK-pill card work) `.date` got
`margin-left:auto` to right-pin the date *in cards*. In a flexbox that auto-margin shoves the
element + everything after it to the right edge — harmless in the card (date is last), but in the
header the first `.date` flung the count + usage right too. The card-only change silently mutated
the header because they share the class.
**Fix:** moved `margin-left:auto` off the bare `.date` rule onto a scoped `.card-top .date` rule.
**Regression guard:** `test_smoke.py::test_date_margin_auto_scoped_to_cards` asserts the auto-margin
lives only on `.card-top .date`, never the bare `.date` (mutation-verified non-vacuous).
**Lesson:** put *layout* (margins, flex) on container-scoped selectors, not bare shared element
classes; visual-only styling (color/font) can stay shared. **Status:** Fixed.

### Selection highlight reworked — no longer fights project color — Built (2026-06-13)
**Problem:** project identity is carried by each card's left stripe (`border-left:3px hsl(project-hue)`)
+ the badge pill, but the selected-card style set `border-color:accent` AND an `inset 3px` accent bar
*on that same left edge* — painting over the project stripe. Two cues collided on one edge.
**Decided (user, 2026-06-13): soft accent fill.** `.card.active` now just sets `background:var(--sel)`
(new token: light `#e6e0ff`, dark `#322c52`); all borders incl. the project-hue left stripe stay put.
Selection and project-color now live in separate visual channels. **Status:** Built.
(Considered + rejected: accent ring/outline; color-neutral elevation/lift.)

### SDK / "not typed by you" marker on session cards — Built (2026-06-13)
So you can tell, *before* clicking, that a chat was run by Claude/a tool rather than typed by you.
- **Session-level flag** `Session.initiated_by_sdk` = the session's **first real prompt** is SDK-submitted
  (`_prompt_submitted_by == "sdk"`). Computed in `_compute_metadata`; a leading *typed* slash command
  correctly counts as human-initiated (the initiator is what's checked).
- **Render (user choice 2026-06-13): a `⚙ SDK` pill** in the card-top row next to the project badge,
  with a hover tip ("Submitted by Claude or a tool (a headless claude -p run), not typed by you").
  card-top switched from `space-between` to `gap` + `.date{margin-left:auto}` so the pill groups with
  the project badge on the left and the date stays right.
- **Tests:** 4 in `test_parsing.py` (flagged when sdk-first; not when typed/bare/leading-slash; pill
  appears in card HTML for exactly the sdk session). Mutation-verified. Real data: 1/13 sessions badged.
- **Status:** Built. (Considered + rejected: gear glyph on the title; muted "Claude-run" text in meta line.)

### SDK-submitted prompt badge — Built (2026-06-12)
A `user` prompt can be **submitted on your behalf** by Claude or a tool via a headless
`claude -p` subprocess (e.g. the translation tool spawned one to OCR a comic page → session
`0cd7cba9`, titled "Comic page transcription blocked by image permissions" — words the human
never typed). Previously the renderer attributed it to "You" — same attribution-trap class as
the documented injected-content rules.
- **Detection:** `_prompt_submitted_by(entry)` → `"sdk"` when `entrypoint=="sdk-cli"` OR
  `promptSource=="sdk"`. `entrypoint` is the robust cross-version flag; `promptSource` is newer
  (~CLI ≥2.1.172; absent ≤2.1.159, which predate SDK subprocesses anyway). Typed prompts are
  `promptSource=="typed"`.
- **Render:** such user turns are labelled **"Claude (SDK)"** with a `⚙ submitted` badge
  (hover tip explains it was sent programmatically, not typed). `Turn.submitted` carries it.
- **Tests:** 4 new in `test_attribution.py` (sdk flagged + not "You"; entrypoint-only older-CLI
  path; typed not flagged; bare fixture defaults to human). Mutation-verified non-vacuous.
- **Verified on real data:** exactly 1 of 11 sessions (the comic one) carries the badge; 0 false
  positives. **Status:** Built.

### Active-session badge — Built (2026-06-11)
Sessions whose last activity is within `ACTIVE_WINDOW_MIN` (15) show a green "● live" badge with an
"active Nm ago" tooltip. **Dates stay absolute everywhere else by design** — relative time is used
ONLY for the live session, where recency is the actual signal (matches user's stated preference).
Open: revisit whether to broaden relative dates; user prefers absolute, undecided.

---

## P0 — Must decide before shipping

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

### Tool use rendering
How much of Claude's tool activity to show?
- **Options:**
  - Hide all (cleanest, most readable for sharing)
  - Summarize: `[Read: src/main.py]`, `[Bash: git status]` (good default)
  - Expand full: show inputs + outputs (very long, useful for debugging)
  - Collapsible sections in HTML (best of all worlds for HTML format)
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

### Format selection UX
- **Decided:** If no format arg provided to slash command, ask interactively (md / html / image). If arg provided, skip the prompt.

### Export location
- **Decided:** Default to `~/Downloads`. Always ask: "Export to ~/Downloads?" with options "Yes" and "Set custom location." No silent defaults.

### HTML/image design
What does "looks nice" mean concretely?
- **Options to decide:**
  - Dark mode / light mode / respect system preference (CSS `prefers-color-scheme`)
  - Show timestamps per message?
  - Show model name, token counts in header?
  - Syntax highlighting in code blocks? (e.g. highlight.js, inline)
  - Collapsible tool use sections (JS needed)?
- **Status:** TBD

### Multi-session merging
If a conversation was resumed across multiple sessions (same logical thread, different UUIDs), should the export stitch them together?
- **Options:**
  - Always merge sessions with the same original `parentUuid` chain
  - Export each session file separately, let user decide
  - Offer both
- **Status:** TBD

### Primary interface: slash command vs. standalone script
- **Options:**
  - Slash command calls the script (Claude orchestrates, script does work) — clean separation
  - Claude does all the rendering itself (no script needed, but not reusable outside Claude Code)
  - Both: script is the engine, slash command is the UX layer
- **Status:** TBD

---

## P2 — Nice to have, defer

### Empty-state / no-sessions handling — P2, TBD
If `~/.claude/projects` has no parseable sessions (fresh machine, or `--projects-dir` points
somewhere empty), the dashboard currently renders an empty three-pane shell. Add a friendly
empty-state: a centered "No Claude Code sessions found — start chatting and re-run `chats`" card,
and skip writing per-session pages. Low effort; only matters on first run / misconfiguration.
- **Status:** TBD. (Migrated from TODO.md 2026-06-16 — was misfiled there as engineering work.)

### Tier-2 truly-detached background titling — P2, deferred
Cold-session LLM titling already runs *after* the dashboard is written+opened (open-first titling),
so steady state is ~0.28s. A further step would fully detach titling into a background process so
the command returns instantly and titles fill in later. Deferred: adds process-detachment + a
cache-write race + silent-failure surface, and the open `file://` page can't auto-refresh to show
new titles (no `fetch` over `file://`) — so payoff is capped now that the common case is instant.
- **Status:** Deferred. (Migrated from TODO.md 2026-06-16.)

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

### Thinking block visibility
Encrypted currently, but architecture may change.
- **Options:** Always hide, or expose as `--include-thinking` flag for future use
- **Status:** Closed (2026-07-09) — verified all 7,599 thinking blocks in real data have an
  EMPTY `thinking` field (signature only); there is nothing to render, now or retroactively.

### Cross-platform support
`~/.claude` and `~/Downloads` are macOS/Linux paths. Windows uses different conventions.
- **Status:** TBD (macOS-first for now is fine)

### Dark/light mode toggle in HTML export
JS-powered toggle in the HTML output itself.
- **Status:** TBD

### Export a range of messages
E.g. export only the last 20 messages, or messages from a specific time window. Useful for sharing snippets without the full context.
- **Status:** TBD

### Token/cost summary in export header
Show total tokens used, cache hits, model used — useful for cost-awareness.
- **Status:** TBD
