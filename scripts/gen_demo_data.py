#!/usr/bin/env python3
"""
Generate a fully synthetic set of Claude Code-shaped .jsonl session files for
README screenshots. No real session data is used anywhere in this script —
every prompt, reply, file path, and tool result below is made up.

Usage:
    python3 scripts/gen_demo_data.py /path/to/scratch/demo-projects

Then render it like any real projects dir:
    python3 chats_dashboard.py --projects-dir /path/to/scratch/demo-projects \\
        --output-dir /path/to/scratch/demo-site --no-titles
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
from fixtures import (  # noqa: E402
    agent_name_line, ai_title_line, assistant_blocks, now_offset_iso, tool_result_entry,
    tool_use_block, typed_msg, text_block, turn_duration_line, write_jsonl,
)

MODEL = "claude-sonnet-5-20251015"


def _usage(input=0, output=0, cache_read=0, cache_write=0) -> dict:
    return {"input_tokens": input, "output_tokens": output,
            "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write}


def _write(out_dir: str, project_dir: str, filename: str, entries: list[dict], *,
           session_id: str, cwd: str) -> None:
    proj_path = os.path.join(out_dir, project_dir)
    os.makedirs(proj_path, exist_ok=True)
    # write_jsonl always targets <dir>/proj/<filename>; write straight into our
    # own project_dir instead so each demo project gets its own folder.
    import json
    with open(os.path.join(proj_path, filename), "w", encoding="utf-8") as fh:
        for e in entries:
            line = dict(e)
            if line.get("type") in ("user", "assistant"):
                line.setdefault("sessionId", session_id)
                line.setdefault("cwd", cwd)
            fh.write(json.dumps(line) + "\n")


def recipe_planner_session(days_ago: float) -> tuple[str, list[dict]]:
    sid = "demo-recipe-1"
    cwd = "/home/demo/recipe-planner"
    ts = lambda s=0: now_offset_iso(days=days_ago, seconds=s)  # noqa: E731

    u1 = typed_msg("Add a calories-per-serving field to the Recipe model and "
                   "show it on the recipe card.")
    u1["timestamp"] = ts(300)

    a1_read = assistant_blocks([
        text_block("I'll look at the current Recipe model first."),
        tool_use_block("Read", {"file_path": "models/recipe.py"}, "t1"),
    ], model=MODEL, usage=_usage(input=1200, output=90))
    a1_read["timestamp"] = ts(280)

    r1 = tool_result_entry("t1", (
        "class Recipe:\n"
        "    def __init__(self, name, servings, ingredients):\n"
        "        self.name = name\n"
        "        self.servings = servings\n"
        "        self.ingredients = ingredients\n"
    ))
    r1["timestamp"] = ts(275)

    a2_edit = assistant_blocks([
        text_block("Adding a `calories_per_serving` field and a small helper to "
                    "compute it from the ingredient list."),
        tool_use_block("Edit", {
            "file_path": "models/recipe.py",
            "old_string": "        self.ingredients = ingredients",
            "new_string": ("        self.ingredients = ingredients\n"
                           "        self.calories_per_serving = "
                           "self._compute_calories() / servings"),
        }, "t2"),
    ], model=MODEL, usage=_usage(input=1400, output=140))
    a2_edit["timestamp"] = ts(260)

    r2 = tool_result_entry("t2", "Applied 1 edit to models/recipe.py")
    r2["timestamp"] = ts(255)

    a3_bash = assistant_blocks([
        text_block("Running the model tests to make sure nothing broke."),
        tool_use_block("Bash", {"command": "pytest tests/test_recipe.py -q"}, "t3"),
    ], model=MODEL, usage=_usage(input=1500, output=60))
    a3_bash["timestamp"] = ts(240)

    r3 = tool_result_entry("t3", "5 passed in 0.31s")
    r3["timestamp"] = ts(235)

    a4 = assistant_blocks([text_block(
        "Done — `Recipe` now tracks `calories_per_serving`, computed from the "
        "ingredient list and divided by `servings`. The recipe card template "
        "already reads arbitrary model fields, so it picks this up automatically:\n\n"
        "```python\n"
        "recipe.calories_per_serving  # e.g. 410\n"
        "```\n\n"
        "All 5 existing recipe tests still pass."
    )], model=MODEL, usage=_usage(input=1550, output=210))
    a4["timestamp"] = ts(200)

    entries = [u1, a1_read, r1, a2_edit, r2, a3_bash, r3, a4,
               turn_duration_line(a4["uuid"], 41200, session_id=sid),
               agent_name_line(sid, "add-calorie-field")]
    return sid, entries


def garden_tracker_session() -> tuple[str, list[dict]]:
    sid = "demo-garden-1"
    cwd = "/home/demo/garden-tracker"
    ts = lambda s=0: now_offset_iso(days=1, seconds=s)  # noqa: E731

    u1 = typed_msg("Sketch out a watering-schedule notifier — plants have "
                   "different watering intervals and I want a daily digest.")
    u1["timestamp"] = ts(500)

    a1 = assistant_blocks([text_block(
        "Here's a simple approach:\n\n"
        "1. Each `Plant` row stores `watering_interval_days` and `last_watered_at`.\n"
        "2. A daily cron computes `due = last_watered_at + interval <= today` per plant.\n"
        "3. Due plants get batched into one digest notification instead of one "
        "push per plant.\n\n"
        "```python\n"
        "def plants_due_today(plants, today):\n"
        "    return [p for p in plants\n"
        "            if p.last_watered_at + timedelta(days=p.watering_interval_days) "
        "<= today]\n"
        "```\n\n"
        "Want me to wire this into the existing notification service, or keep it "
        "as a standalone script for now?"
    )], model=MODEL, usage=_usage(input=900, output=260, cache_read=400))
    a1["timestamp"] = ts(460)

    u2 = typed_msg("Standalone script for now, plus a quick README section.")
    u2["timestamp"] = ts(440)

    a2_write = assistant_blocks([
        text_block("Adding `scripts/watering_digest.py` and a README section."),
        tool_use_block("Write", {
            "file_path": "scripts/watering_digest.py",
            "content": (
                "from datetime import timedelta\n\n"
                "def plants_due_today(plants, today):\n"
                "    return [p for p in plants\n"
                "            if p.last_watered_at + timedelta(days="
                "p.watering_interval_days) <= today]\n"
            ),
        }, "t1"),
    ], model=MODEL, usage=_usage(input=950, output=120))
    a2_write["timestamp"] = ts(420)

    r1 = tool_result_entry("t1", "File created: scripts/watering_digest.py")
    r1["timestamp"] = ts(415)

    a3 = assistant_blocks([text_block(
        "Added the script and a short usage note in the README. Let me know if "
        "you'd like the digest grouped by garden bed instead of one flat list."
    )], model=MODEL, usage=_usage(input=1000, output=80))
    a3["timestamp"] = ts(400)

    entries = [u1, a1, u2, a2_write, r1, a3,
               turn_duration_line(a3["uuid"], 18400, session_id=sid),
               agent_name_line(sid, "watering-schedule-digest")]
    return sid, entries


def api_gateway_session() -> tuple[str, list[dict]]:
    sid = "demo-gateway-1"
    cwd = "/home/demo/api-gateway-poc"
    ts = lambda s=0: now_offset_iso(days=6, seconds=s)  # noqa: E731

    u1 = typed_msg("Rate-limit the /search endpoint to 10 req/min per API key.")
    u1["timestamp"] = ts(200)

    a1 = assistant_blocks([
        text_block("Adding a token-bucket limiter keyed on the API key header."),
        tool_use_block("Edit", {
            "file_path": "gateway/middleware.py",
            "old_string": "def handle_search(request):",
            "new_string": "@rate_limit(key='api_key', limit=10, window_s=60)\ndef handle_search(request):",
        }, "t1"),
    ], model=MODEL, usage=_usage(input=800, output=150))
    a1["timestamp"] = ts(160)

    r1 = tool_result_entry("t1", "Applied 1 edit to gateway/middleware.py")
    r1["timestamp"] = ts(155)

    a2 = assistant_blocks([text_block(
        "Done — `/search` now allows 10 requests/minute per API key, "
        "returning `429 Too Many Requests` once exceeded."
    )], model=MODEL, usage=_usage(input=850, output=70))
    a2["timestamp"] = ts(120)

    entries = [u1, a1, r1, a2, turn_duration_line(a2["uuid"], 9800, session_id=sid),
               agent_name_line(sid, "rate-limit-search-endpoint")]
    return sid, entries


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    out_dir = sys.argv[1]

    sid1, e1 = recipe_planner_session(days_ago=0)
    _write(out_dir, "recipe-planner", "session1.jsonl", e1,
           session_id=sid1, cwd="/home/demo/recipe-planner")
    with open(os.path.join(out_dir, "recipe-planner", "session1.jsonl"), "a") as fh:
        import json
        fh.write(json.dumps(ai_title_line(sid1, "Add calorie tracking to Recipe model")) + "\n")

    sid2, e2 = garden_tracker_session()
    _write(out_dir, "garden-tracker", "session1.jsonl", e2,
           session_id=sid2, cwd="/home/demo/garden-tracker")
    with open(os.path.join(out_dir, "garden-tracker", "session1.jsonl"), "a") as fh:
        import json
        fh.write(json.dumps(ai_title_line(sid2, "Design a watering-schedule digest")) + "\n")

    sid3, e3 = api_gateway_session()
    _write(out_dir, "api-gateway-poc", "session1.jsonl", e3,
           session_id=sid3, cwd="/home/demo/api-gateway-poc")
    with open(os.path.join(out_dir, "api-gateway-poc", "session1.jsonl"), "a") as fh:
        import json
        fh.write(json.dumps(ai_title_line(sid3, "Rate-limit the search endpoint")) + "\n")

    print(f"Wrote 3 synthetic sessions under {out_dir}")


if __name__ == "__main__":
    main()
