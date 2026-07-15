# IMPROVEMENTS — Closed (archive)

Full detail of completed / decided / dropped items, moved out of `IMPROVEMENTS.md` to
keep the active backlog lean. **Open items live in `IMPROVEMENTS.md`.** Entries are kept
**verbatim** (rationale preserved, not summarized) — grep here when a stub in the
`IMPROVEMENTS.md` "Closed" index points you to one. Status legend: Built · Decided · Fixed ·
Superseded · Dropped · Rejected · Closed.

## Log — 2026-07-14 — dashboard becomes a durable, browsable archive

**Trigger:** user noticed chats older than ~30 days were missing from the dashboard. Root cause was
two-layered: (1) Claude Code's own `cleanupPeriodDays` (default 30, unset in this user's
`~/.claude/settings.json`) deletes source `.jsonl` under `~/.claude/projects/` — verified: oldest
local file was exactly ~30 days old, `find -mtime +30` returned zero; (2) `write_site()` mirrored
that: it `os.remove`d a session's rendered `sessions/<id>.{html,md}` the moment its source
disappeared. No local recovery path existed (no Time Machine destination configured on this
machine) — this only fixes retention *going forward*.

- **P1 — Stop pruning by default; make it opt-in.** `Built:` `write_site(sessions, output_dir,
  prune_orphans: bool = False)` — pruning (the old behavior) now requires `--prune-orphans`. Default
  keeps orphaned pages and carries their render-manifest entry forward so a later `--prune-orphans`
  run can still find/clean them. User explicitly chose NOT to touch `cleanupPeriodDays` itself —
  source retention stays 30 days; only the *rendered* archive becomes durable.
- **P1 — User changed the design mid-session: archived sessions must be LISTED, not just kept on
  disk.** `Built:` new `.archive-cards.json` sidecar captures each live session's card-display
  fields (title/cost/tokens/counts/timestamps — `_CARD_FIELDS`/`_card_fields()`) on every run. When
  a session goes orphan, its last-captured fields reconstruct a lightweight `ArchivedCard`
  (duck-typed stand-in for `Session`, same attribute names, `is_archived=True`) that `_index_page`
  renders as a normal card — interleaved by date, `🗄 archived` pill, dimmed `.card.archived` style —
  linking to the still-frozen `sessions/<id>.html`. **User-confirmed:** sidebar cost/token rollups
  include archived sessions (true all-time totals, not just live).
- **Two-stage adversarial review, both caught real bugs:**
  - Stage 1 (prune→archive-default): clean — the default path deletes nothing, no other call sites
    broke on the 3→4-tuple return change.
  - Stage 2 (listed archive): duck-typing surface came back clean (`_index_page` and every helper it
    calls only ever touch attributes both `Session` and `ArchivedCard` share — verified no path
    reaches `.entries`/`.project_path`/`._expansions`/`.turn_durations`), but found two real MEDIUM
    bugs, both fixed: (a) the `archived` return count included orphans that were kept on disk but had
    **no card metadata to render** (e.g. the first run right after this upgrade, before the sidecar
    existed) — it was claiming "still listed" for pages that weren't actually listed; fixed by
    deriving `archived = len(archived_stubs)` instead of counting every orphan. (b) `ArchivedCard(**fields)`
    doesn't type-check — a corrupt/hand-edited sidecar row (e.g. `cost_usd` as a string) would flow
    into unguarded downstream code (`sorted(..., key=lambda s: s.last_ts)`, `.4f` cost formatting,
    token-sum arithmetic) and crash the **whole index render**, not just that card; fixed with a new
    `_archived_card_from_fields()` validator (explicit isinstance checks per field, bool-vs-int
    subclass trap handled) that drops non-conforming rows instead of constructing from them.
  - **Lesson for next time:** a mid-session design pivot (user changed their mind after the first
    review) is a fresh trigger for adversarial review, not a "already reviewed this area" skip — the
    new persistence layer (sidecar + duck-typed merge into rendering) was a materially different risk
    surface than the first stage's plain opt-in flag.
- **Storage impact (measured, not estimated):** 160 sessions → 70MB rendered output (62MB html +
  7.8MB md, ~437KB/session avg) vs 311MB of source `.jsonl` (steady-state, bounded by the 30-day
  cleanup). Sidecars are negligible (`.archive-cards.json` 109KB, `.render-manifest.json` 13.7KB,
  `.title-cache.json` 48KB for 160 sessions). Net effect of this feature: the dashboard output was
  previously ALSO steady-state (~70MB, mirroring the 30-day source window); now it only grows.
  Using current session-creation pace as a proxy: **~70MB/month, ~840MB/year, linear** (not
  compounding — each page is written once). Trivial on modern disk; not a real constraint.
- **Tests:** 112 → 114 (2 new regression tests for the stage-2 review findings:
  `test_archived_count_reflects_only_actually_listed_cards`,
  `test_corrupt_archive_card_row_is_dropped_not_crashed`), plus 3 new stage-2 tests for the listing
  feature itself and 1 renamed/split stage-1 test. All verified end-to-end on real data in scratch
  dirs (never touched `~/.claude/projects` directly) across all three code paths (archive-default,
  `--prune-orphans`, and the real live dashboard refresh, `--no-titles` to keep it $0).
- **CLAUDE.md updated** with a "Retention/archive" section documenting `cleanupPeriodDays`'s role,
  the default-archive/opt-in-prune behavior, and the three generator-owned dotfiles.
- **Status:** Built, shipped, live dashboard refreshed.

## Codebase review — 2026-07-09

Fidelity gaps verified against real `~/.claude/projects` data (counts as of 2026-07-09):

- **P1 — Interrupts render as user speech.** `Built (2026-07-09):` exact-sentinel match (`_is_interrupt`) → red `⏹ Interrupted by user` status row (`(during tool use)` variant); excluded from n_user/snippet/titler; human prose *quoting* the sentinel stays visible (exact-match guard). Verified on real data: 15 markers, 0 left in user bubbles.
- **P1 — Compact summary renders as a "You" bubble.** `Built (2026-07-09):` `isCompactSummary` → collapsed `🗜 conversation compacted — continuation summary` chip; excluded from n_user/snippet/titler. **Real-data catch:** a manual /compact's summary directly follows the `⌘ /compact` command entry, so `_expansion_uuids` was folding it as the command's "expanded prompt" — compact/interrupt entries are now excluded from expansion-claiming (regression tests for both). `system/compact_boundary` metadata (trigger, preTokens) still unrendered — fold into the chip if system-line ingestion ever lands.
- **P1 — Pasted images silently dropped.** `Built (2026-07-09):` `image` blocks embed as `<img src="data:…;base64,…">` (max-height 340px, click-to-toggle full size, lazy-loaded, `_esc` on media/data), capped at `MAX_IMAGE_B64` (2M b64 chars ≈1.5MB) → placeholder chip above; malformed source/data → placeholder, never a crash (adversarial-review finding: unguarded types would have aborted the whole generation). Counts as visible content for the Tools toggle. 63 images embed on real data.
- **P2 — `agent-name` entries ignored.** `Built (2026-07-09):` ingested in `load_sessions` (last per session wins **by file mtime** — the lines carry no timestamp, so glob order was nondeterministic across resumed files; adversarial-review finding); title fallback between ai_title and snippet (truncated to `TITLE_FALLBACK_CHARS`, second review finding); added to the card's `data-search` haystack so the harness name is searchable even when an LLM title displays. Card chip not built (visual call, revisit if wanted).
- **P2 — TodoWrite renders as a JSON chip.** `Superseded (2026-07-09):` **zero** TodoWrite calls in the entire corpus — the CLI moved to TaskCreate/TaskUpdate. Those get readable chip summaries instead (`subject`, `#id → status`); a stateful checklist card reconstruction from TaskUpdate deltas is not worth it.
- **P2 — Plan card doesn't show approval outcome.** `Built (2026-07-09):` `_plan_outcomes` pairs each plan-file Write with the next ExitPlanMode's tool_result (verified shapes: 96× "User has approved your plan", 3× is_error rejection) → `✓ approved` / `✗ rejected` badge on the plan-card header. No result → no badge. Multiple plans per session pair independently (cross-pairing test).
- **P2 — `_tool_summary` misses newer tools.** `Built (2026-07-09):` key list gained `skill`/`subject`/`description` (before `prompt`, so Agent chips show the description, Bash still shows its command); TaskUpdate special-cased to `#id → status`.
- **P3 — Per-turn model + duration.** `Built (2026-07-09), user picked from a rendered mockup:` **model → a divider row on switch** (mockup C; buffered so an invisible switching entry — empty thinking block — never strands a divider; 30 dividers on real data) and **durations on every turn** (mockup D-all): `system/turn_duration` lines ingested (`parentUuid` → the turn's final assistant entry, verified 1028/1028) → `⏱ 2m 41s` in the turn timestamp; duration count+sum folded into the render fingerprint since those lines change neither entry count nor last_ts. Third adversarial review found + fixed: `_ptext` memo key was poisonable from crafted jsonl (now stripped at the load boundary), prune could delete files the generator never created (now scoped to previous-manifest entries only, with a path-part guard).
- **Closable: thinking-block visibility.** All 7,599 thinking blocks in real data have an EMPTY `thinking` field (signature only) — there is nothing to render. See "Thinking block visibility" below (Closed 2026-07-09).
- **Housekeeping: `TODO.md` was git-tracked in the public repo**, contra the global gitignore rule. `Built (2026-07-09):` untracked (`git rm --cached` + gitignore). **Verified still true 2026-07-14** (re-checked during this archival pass: `git ls-files` confirms `TODO.md` is untracked, `.gitignore` lists it). Original note said "needs user OK since the repo is public" — that OK was given and the action completed the same session.

Code health / perf (defer perf until measured — full run is ~0.2s):

- **P2 — Markdown: nested lists flatten.** `Built (2026-07-09):` `_render_list` renders consecutive items with an indent stack (deeper → nested `<ul>`/`<ol>` as a sibling of the previous `<li>` — deliberate simplicity, browsers render it nested); `- [ ]`/`- [x]` → ☐/☑ `li.task`. Bounded by construction (pops ≤ pushes); two new pathological corpus entries (200-deep nesting, ragged indents). Multi-line list items still unhandled (they end the list — acceptable).
- **P2 — `_atomic_write_json` tmp path isn't pid-suffixed.** `Built (2026-07-09):` delegates to `_atomic_write_text`.
- **P3 — PRICING prefix-match fallback.** `Built (2026-07-09):` `_price_for` — exact match, else dash-boundary prefix (`claude-haiku-4-5-20251001` prices as haiku; `claude-sonnet-55` does NOT match sonnet-5 — test).
- **P3 — Repeated re-parsing.** `Built (2026-07-09):` `_plain_user_text` memoized on the entry dict (`_ptext` key); `_expansion_uuids`/`_result_map` cached per Session (`_session_expansions`/`_session_results` — safe because entries are immutable after load). Load 1.52s→1.29s, md render 0.29s→0.22s on 141 sessions / 72k lines.
- **P3 — `write_site` rewrites everything every run.** `Built (2026-07-09):` incremental rendering via `.render-manifest.json` — per-session fingerprint over (entry count, first/last ts, title, summary, project_path, generator-source+TZ hash); skip render+write on match, self-heal missing files, prune orphan pages of deleted sessions, index always rewritten last. **Steady state 3.1s → 1.27s** (all remaining time is json parsing). The live session's fingerprint changes every run so it always re-renders — correct, and only 1 page. Mutation-verified tests in `tests/test_writesite.py`. (Note: "prune orphan pages" became opt-in on 2026-07-14, see the Log entry above.)
- **Measured & REJECTED (2026-07-09, don't re-try): substring pre-filter before `json.loads`** (skip lines whose type we ignore): 0.86s vs 0.79s baseline — the per-line needle scans cost more than the skipped parses. Parsing is at its practical floor; the next step would be a file-level parsed-entry cache (pickle), rejected for staleness/complexity.

## Log — 2026-07-01

- **Pricing table refresh** — `Built:` added `claude-fable-5` / `claude-mythos-5` ($10/$50 per MTok) and `claude-sonnet-5` ($3/$15, standard rate — not the temporary $2/$10 intro) to `PRICING` in `chats_dashboard.py`. Sonnet 5 and Fable/Mythos sessions were previously counted as unpriced (shown with the `+` incomplete-cost marker).
- **Sticky date-header leak fix** — `Built:` the pinned group header (`TODAY`, etc.) left a gap on top through which the previous card leaked. Root cause: the `.cardlist` scroll container's `padding-top:10px` — a `position:sticky;top:0` element is clamped to its containing block (padding-excluded content box), so top padding pins the header that far below the scrollport and content scrolls through the gap. Fix: `.cardlist{padding:0 10px 10px}` + `.cardlist>:first-child{margin-top:10px}` for breathing room; header keeps opaque `--bg` + `z-index:10`. NOT the flex `gap` (a first attempt covering the flex gap did nothing). Verified with headless system Chrome (extension was down) — see project memory `build-test-harness-learnings`.

## Pivot 2026-06-10 — product is now a dashboard, not a single-chat exporter

The slash command now opens a **static HTML dashboard** to browse *all* Claude Code history.
Single-chat export becomes an in-page Export button. Decisions made in the pivot:

- **Dashboard type** — `Decided:` static HTML site, opened in browser (no server, no lingering process).
- **File structure** — `Decided:` hybrid. Small `index.html` (metadata + snippets) + per-session `sessions/<id>.html`. Plain `<a href>` navigation, because `fetch()` is blocked over `file://` (CORS) — a lazy-loading SPA would silently break on double-click.
- **Search depth** — `Decided:` titles + project + ~200-char snippet, client-side filter. Keeps index tiny at scale. Full-text deferred (would bloat index to MBs).
- **Tool use rendering** — `Decided:` collapsible sections, summarized by default. (Resolves the P0 "Tool use rendering" item — see that entry below, closed alongside this pivot.)
- **Location** — `Decided:` `~/.claude/history-dashboard/`, regenerated each run. (Standing tool, not a Downloads export.)
- **Command name** — `Decided:` `/chats`.
- **Entry points / decoupling** — `Decided:` viewing needs no Claude session (static files). Generator is standalone `python3 chats_dashboard.py --open`. Three paths: open the HTML directly; `chats` shell alias (refresh+view, no Claude); `/chats` global slash command (works from any dir). The slash command is a convenience, not a requirement. (Resolves the P1 "Primary interface: slash command vs. standalone script" item — see that entry below.)
- **Layout** — `Built:` two-pane master–detail (left card list + right `<iframe>` transcript). iframe chosen over a fetch-based SPA because `fetch()` is blocked over `file://`.
- **Session titles** — `Built:` LLM-generated title + one-line summary via the local `claude -p --model haiku --no-session-persistence`, cached by content fingerprint in `.title-cache.json`. `--no-titles` for offline/heuristic mode. Replaced uninformative first-sentence/`/init` titles.
- **Transcript fidelity (from user testing)** — `Built:` slash-command boilerplate folded under `⌘ /cmd`; tool results paired under Claude's call (not attributed to user); ANSI codes stripped; command stdout shown in full; `No response requested.` sentinel filtered.

### Stale dashboard / in-page refresh — Decided 2026-06-12: Option C (don't build)
A regenerated static site is a snapshot; the active session keeps growing after generation.
Today you re-run via the `chats` alias or `/chats`. User wanted to refresh *from the page* without
touching the terminal.
- **Hard constraint:** the page is static HTML opened over `file://`. A static page can't regenerate
  itself, and browsers block `fetch()` over `file://`. So real in-page refresh is **impossible
  without a local server** — there is no no-server option that actually works (a reload button just
  re-reads the same static HTML).
- **Option A — `--serve` mode + Refresh button:** ship a `python3 chats_dashboard.py --serve` that
  runs a tiny stdlib `http.server` on localhost; you open `http://localhost:PORT` instead of the
  file; a Refresh button hits a `/regenerate` endpoint that re-runs `load_sessions`+`write_site`,
  then reloads. Cost: moderate; introduces a long-running process + port, and shifts the *live* entry
  point from the `file://` bookmark to localhost (the bookmark still works as a frozen snapshot).
- **Option B — `--serve` + `--watch` auto-refresh:** same server, but it watches
  `~/.claude/projects` and the page polls a version endpoint and reloads itself — zero clicks. Cost:
  higher (file-watching/mtime scan, debounce the growing live session, poll loop).
- **Option C — don't build, document:** keep the static-file design; the `chats` alias / `/chats`
  are already one-step refreshes. Add only a "generated at <time>" freshness hint so staleness is
  visible.
- **Tension:** A/B conflict with the tool's stated "no server, no lingering process" design value
  (see Pivot Dashboard-type decision). User invited the "just document" off-ramp if it's getting too
  complicated.
- **Decided (2026-06-12): Option C — do NOT build in-page refresh.** Keep the static-file, no-server
  design; refresh stays the existing `chats` alias / `/chats`. Rationale: a localhost server reverses
  the deliberate "no server, no lingering process" decision (Pivot, above) to save a single terminal
  command, while two low-friction refresh paths already exist. Not worth the operational surface
  (port, long-running process). Even the small "generated at" freshness stamp was declined for now.
  If the terminal hop later becomes a real annoyance, build Option A (server + Refresh button); skip
  Option B (auto-watch is too much machinery for the benefit).

### Multi-file session merge — Decided/Built (resolved by the "File structure" pivot decision)
Sessions resumed across files share `sessionId` — merge into one transcript vs list separately.
Leaning merge at the time this question was raised. **Confirmed built:** `load_sessions` groups
entries by `sessionId` across files (documented in the repo's CLAUDE.md pipeline description:
"group entries by `sessionId` (merges resumed sessions)"). Closed during the 2026-07-14 archival
pass — verified against current CLAUDE.md rather than left as a stale duplicate TBD. (A duplicate of
this question also appeared as "Multi-session merging" under the old pre-pivot P1 list — same
resolution, closed together.)

## Testing / regression safety

### Automated test harness — Built (2026-06-11)
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
  reproducing it, then fix — so it can't silently regress. The corpus grows from real bugs. (This
  workflow is now also codified as a standing rule in the repo's CLAUDE.md.)
- **Status:** Built (2026-06-11). 7 files in `tests/`, 48 tests at the time, ~0.8s (114 tests as of
  2026-07-14). Mutation-verified the suite isn't vacuous (disabling the sentinel filter fails a
  test). Runner documented in CLAUDE.md.

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
classes; visual-only styling (color/font) can stay shared.

### Selection highlight reworked — no longer fights project color — Built (2026-06-13)
**Problem:** project identity is carried by each card's left stripe (`border-left:3px hsl(project-hue)`)
+ the badge pill, but the selected-card style set `border-color:accent` AND an `inset 3px` accent bar
*on that same left edge* — painting over the project stripe. Two cues collided on one edge.
**Decided (user, 2026-06-13): soft accent fill.** `.card.active` now just sets `background:var(--sel)`
(new token: light `#e6e0ff`, dark `#322c52`); all borders incl. the project-hue left stripe stay put.
Selection and project-color now live in separate visual channels.
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
- (Considered + rejected: gear glyph on the title; muted "Claude-run" text in meta line.)

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
  positives.

### Active-session badge — Built (2026-06-11)
Sessions whose last activity is within `ACTIVE_WINDOW_MIN` (15) show a green "● live" badge with an
"active Nm ago" tooltip. **Dates stay absolute everywhere else by design** — relative time is used
ONLY for the live session, where recency is the actual signal (matches user's stated preference).
(Open sub-thread, never revisited: whether to broaden relative dates elsewhere — user prefers
absolute, undecided; low priority.)

### Thinking block visibility — Closed (2026-07-09)
Encrypted currently, but architecture may change.
- **Options considered:** Always hide, or expose as `--include-thinking` flag for future use.
- **Closed (2026-07-09)** — verified all 7,599 thinking blocks in real data have an EMPTY `thinking`
  field (signature only); there is nothing to render, now or retroactively.

### Tier-2 truly-detached background titling — Decided: deferred (2026-06-16)
Cold-session LLM titling already runs *after* the dashboard is written+opened (open-first titling),
so steady state is ~0.28s. A further step would fully detach titling into a background process so
the command returns instantly and titles fill in later. **Decided: deferred** — adds
process-detachment + a cache-write race + silent-failure surface, and the open `file://` page can't
auto-refresh to show new titles (no `fetch` over `file://`) — so payoff is capped now that the
common case is instant.

## Old P0 — resolved by the 2026-06-10 pivot

### Tool use rendering — Decided (pivot, 2026-06-10)
How much of Claude's tool activity to show?
- **Options considered:** hide all; summarize (`[Read: src/main.py]`); expand full inputs+outputs;
  collapsible sections in HTML.
- **Decided:** collapsible sections in HTML, summarized by default (best of all worlds for the
  chosen HTML format). Explicitly cross-referenced as resolved in the pivot's "Tool use rendering"
  decision — see the Pivot 2026-06-10 entry above.

## Old P1 — resolved by the 2026-06-10 pivot

### Format selection UX — Decided
If no format arg provided to slash command, ask interactively (md / html / image). If arg provided,
skip the prompt. (Predates the pivot to a standing dashboard; superseded in spirit by the dashboard
having no per-run format prompt at all, but recorded here as the decision actually made at the time.)

### Export location — Decided
Default to `~/Downloads`. Always ask: "Export to ~/Downloads?" with options "Yes" and "Set custom
location." No silent defaults. (Predates the pivot; the dashboard's per-session Export button
downloads directly rather than following this ask-flow — recorded here as the decision actually made
at the time, before the product pivoted.)

### Multi-session merging — Decided/Built (duplicate of "Multi-file session merge" above)
If a conversation was resumed across multiple sessions (same logical thread, different UUIDs),
should the export stitch them together? Same question, same resolution as "Multi-file session
merge" under the Pivot 2026-06-10 entry above (`load_sessions` groups by `sessionId`) — closed
together during the 2026-07-14 archival pass to remove the stale duplicate.

### Primary interface: slash command vs. standalone script — Decided/Built
- **Options considered:** slash command calls the script; Claude renders everything itself; both
  (script is the engine, slash command is the UX layer).
- **Decided/Built:** both — confirmed in the repo's CLAUDE.md "Three entry points" section (script
  stays the single source of truth; the `chats` alias and `/chats` slash command both call it).
  Closed during the 2026-07-14 archival pass, verified against current CLAUDE.md.
