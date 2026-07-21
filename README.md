# Claude Code Chat Dashboard

A standalone tool that turns your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) chat history into a browsable static HTML dashboard. No server required — just open the generated HTML files in your browser.

![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue) ![No dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-green)

## What it does

Claude Code stores every session as a JSONL file under `~/.claude/projects/`. This tool scans all of them and generates a self-contained static site you can browse locally:

- **Three-pane layout** — project sidebar, session card list, transcript viewer (iframe)
- **Search and filter** — client-side filtering by project, date, or keyword
- **Token & cost tracking** — per-session, per-project, and per-date-group usage breakdowns
- **Markdown export** — download any session as a `.md` file
- **LLM-generated titles** — optional session titles via `claude` CLI (cached, uses Haiku by default)
- **Dark/light mode** — respects your system preference
- **SDK session detection** — badges sessions initiated programmatically (via `claude -p`) vs typed by you
- **Active session indicator** — highlights sessions with recent activity
- **Durable archive** — Claude Code itself deletes session files after ~30 days; this dashboard keeps and lists them anyway (flagged 🗄 archived), so your history survives past that cleanup
- **Delete, restore, and retention** — soft-delete sessions you don't want (recoverable), bulk-delete by age or project, and optionally auto-trash sessions past a retention period — all from the CLI or a command-builder panel in the browser (see [Managing sessions](#managing-sessions-delete-restore-retention) below)

All output is plain HTML/CSS/JS — no build step, no bundler, no runtime dependencies beyond Python's standard library.

## Quick start

```bash
# Clone the repo
git clone https://github.com/sailfromhere/claude-code-chat-history.git
cd claude-code-chat-history

# Generate the dashboard and open it in your browser
python3 chats_dashboard.py --open
```

> **Windows:** use `python` instead of `python3` (Windows' python.org installer doesn't ship a `python3` shim).

The dashboard is written to `~/.claude/history-dashboard/` by default. You can bookmark `index.html` and open it anytime without re-running the script.

## Usage

```
python3 chats_dashboard.py [options]

Options:
  --open                    Open the dashboard in your browser when done
  --no-titles               Skip LLM title generation (use heuristic titles)
  --title-model MODEL       Model for title generation (default: haiku)
  --projects-dir DIR        Claude Code projects directory (default: ~/.claude/projects)
  --output-dir DIR          Output directory (default: ~/.claude/history-dashboard)
  --prune-orphans           Delete rendered pages whose source session was removed
                            (default: keep them as a permanent archive)

Deleting, restoring, and retention (see below for details):
  --delete SID...           Move session(s) to trash (recoverable)
  --delete-older-than DAYS  Bulk: trash every session inactive more than DAYS days
  --delete-project NAME     Bulk: trash every session in project NAME
  --restore SID...          Restore session(s) from trash
  --list-trash              List trashed sessions and exit
  --empty-trash             Permanently delete every trashed session (requires --yes)
  --dry-run                 Preview a delete/restore/empty-trash action, change nothing
  --yes                     Confirm a destructive action (required for --empty-trash)
  --set-retention DAYS|off  Auto-trash sessions inactive past DAYS days on every build
  --set-purge DAYS|off      Auto-delete a trashed session once it's been in trash DAYS days
```

### Command shortcut (recommended)

**macOS / Linux (bash or zsh)** — add to your `~/.zshrc` or `~/.bashrc`:

```bash
alias chats='python3 /path/to/chats_dashboard.py --open'
```

**Windows (PowerShell)** — add a function to your PowerShell profile (find its path with `$PROFILE`):

```powershell
function chats { python C:\path\to\chats_dashboard.py --open }
```

**WSL / Git Bash on Windows** — these run bash, so the macOS/Linux `alias` snippet above works unchanged.

Then just run `chats` to regenerate and view the dashboard.

### Claude Code slash command

Copy the included command file to use `/chats` from within any Claude Code session.

**macOS / Linux / WSL / Git Bash:**

```bash
cp chats.md ~/.claude/commands/chats.md
```

**Windows (PowerShell):**

```powershell
Copy-Item chats.md $env:USERPROFILE\.claude\commands\chats.md
```

(`~/.claude` resolves to `%USERPROFILE%\.claude` on Windows.)

## How it works

1. Scans `~/.claude/projects/*/*.jsonl` for session files
2. Groups entries by `sessionId` (merging resumed sessions)
3. Computes metadata: timestamps, token counts, cost estimates, snippets
4. Optionally generates titles via `claude -p --model haiku` (cached by content fingerprint)
5. Merges in any sessions whose source `.jsonl` has since disappeared (reconstructed as archived
   stubs from a saved snapshot) and filters out anything currently in the trash
6. Writes a static site:
   - `index.html` — main dashboard with project sidebar, session cards, and iframe viewer
   - `sessions/<id>.html` — individual transcript pages
   - `sessions/<id>.md` — markdown exports

Files are written atomically (temp file + `os.replace`); `index.html` is written last.

## Output structure

```
~/.claude/history-dashboard/
├── index.html              # Main dashboard
├── assets/
│   ├── highlight.min.js    # Syntax highlighting (vendored)
│   ├── hl-light.css
│   └── hl-dark.css
└── sessions/
    ├── <session-id>.html   # Per-session transcript
    └── <session-id>.md     # Per-session markdown export
```

A handful of hidden `.json` state files also live in the output directory (render cache, archived-
session snapshots, trash, retention settings). They're generator-owned — never hand-edit them, but
it's always safe to delete the whole output directory and re-run for a clean rebuild.

## Cost tracking

The dashboard estimates costs using public Claude API list prices. Models tracked:

| Model | Input (per 1M tokens) | Output (per 1M tokens) |
|-------|----------------------|------------------------|
| Fable 5 / Mythos 5 | $10.00 | $50.00 |
| Opus 4.6–4.8 | $5.00 | $25.00 |
| Sonnet 4.6 / Sonnet 5 | $3.00 | $15.00 |
| Haiku 4.5 | $1.00 | $5.00 |

Cache reads are priced at 10% of input; cache writes at 125% of input. Sessions using unrecognized models show token counts but no dollar estimate.

## Managing sessions: delete, restore, retention

Deletion is **soft** and always recoverable: `--delete` moves a session into a trash file rather
than touching its rendered page. A regen won't pull it back in, but nothing is actually destroyed
until you explicitly empty the trash.

```bash
# Delete one or more sessions (recoverable)
python3 chats_dashboard.py --delete <session-id>

# Bulk delete by age or project — preview first with --dry-run
python3 chats_dashboard.py --delete-older-than 90 --dry-run
python3 chats_dashboard.py --delete-older-than 90
python3 chats_dashboard.py --delete-project my-old-project

# See what's in the trash, and undo a deletion
python3 chats_dashboard.py --list-trash
python3 chats_dashboard.py --restore <session-id>

# Reclaim disk permanently (irreversible — requires --yes)
python3 chats_dashboard.py --empty-trash --yes

# Auto-trash sessions inactive past N days on every future build (still recoverable),
# and/or auto-delete trash entries themselves once they've sat there past M days.
# Both are off by default — nothing changes until you opt in.
python3 chats_dashboard.py --set-retention 90 --set-purge 30
python3 chats_dashboard.py --set-retention off --set-purge off   # turn both back off
```

The dashboard is a static site with no server, so it can't delete anything from the browser
itself — instead, `index.html` has a ⚙ **Cleanup & trash** panel (and a 🗑 on each session card)
that build the exact command above for you to copy and run. If a session's source `.jsonl` is
still present in `~/.claude/projects` when you empty the trash, it will simply reappear on the
next build — the dashboard mirrors Claude Code's own history and can't forget something Claude
Code still has; the CLI prints a note when this happens.

## Requirements

- **Python 3.7+** (uses dataclasses and `from __future__ import annotations`). On Windows, the command is usually `python` (or `py`) rather than `python3` — swap it in wherever `python3` appears in this README.
- **No third-party packages** — stdlib only
- Claude Code installed (it produces the JSONL history files this tool reads)
- `claude` CLI on your PATH (only needed for `--open` and LLM title generation; `--no-titles` skips it)
- Works on macOS, Linux, and Windows (native, WSL, or Git Bash) — `~/.claude/...` paths resolve to `%USERPROFILE%\.claude\...` on native Windows.

## Tests

```bash
python3 -m unittest discover -s tests
```

173 tests covering attribution logic, markdown rendering, cost math, date handling, incremental rendering, the archive/trash/retention lifecycle, and output structure. All tests use synthetic fixtures (no real chat data).

## License

MIT
