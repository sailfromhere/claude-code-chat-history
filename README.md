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

All output is plain HTML/CSS/JS — no build step, no bundler, no runtime dependencies beyond Python's standard library.

## Quick start

```bash
# Clone the repo
git clone https://github.com/sailfromhere/claude-code-chat-history.git
cd claude-code-chat-history

# Generate the dashboard and open it in your browser
python3 chats_dashboard.py --open
```

The dashboard is written to `~/.claude/history-dashboard/` by default. You can bookmark `index.html` and open it anytime without re-running the script.

## Usage

```
python3 chats_dashboard.py [options]

Options:
  --open                Open the dashboard in your browser when done
  --no-titles           Skip LLM title generation (use heuristic titles)
  --title-model MODEL   Model for title generation (default: haiku)
  --projects-dir DIR    Claude Code projects directory (default: ~/.claude/projects)
  --output-dir DIR      Output directory (default: ~/.claude/history-dashboard)
```

### Shell alias (recommended)

Add to your `~/.zshrc` or `~/.bashrc`:

```bash
alias chats='python3 /path/to/chats_dashboard.py --open'
```

Then just run `chats` to regenerate and view the dashboard.

### Claude Code slash command

Copy the included command file to use `/chats` from within any Claude Code session:

```bash
cp chats.md ~/.claude/commands/chats.md
```

## How it works

1. Scans `~/.claude/projects/*/*.jsonl` for session files
2. Groups entries by `sessionId` (merging resumed sessions)
3. Computes metadata: timestamps, token counts, cost estimates, snippets
4. Optionally generates titles via `claude -p --model haiku` (cached by content fingerprint)
5. Writes a static site:
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

## Cost tracking

The dashboard estimates costs using public Claude API list prices. Models tracked:

| Model | Input (per 1M tokens) | Output (per 1M tokens) |
|-------|----------------------|------------------------|
| Opus 4.6–4.8 | $5.00 | $25.00 |
| Sonnet 4.6 | $3.00 | $15.00 |
| Haiku 4.5 | $1.00 | $5.00 |

Cache reads are priced at 10% of input; cache writes at 125% of input. Sessions using unrecognized models show token counts but no dollar estimate.

## Requirements

- **Python 3.7+** (uses dataclasses and `from __future__ import annotations`)
- **No third-party packages** — stdlib only
- Claude Code installed (it produces the JSONL history files this tool reads)
- `claude` CLI on your PATH (only needed for `--open` and LLM title generation; `--no-titles` skips it)

## Tests

```bash
python3 -m unittest discover -s tests
```

48 tests covering attribution logic, markdown rendering, cost math, date handling, and output structure. All tests use synthetic fixtures (no real chat data).

## License

MIT
