"""Generate scenario markdown for a Telegram bot — from a task description or a git
diff of the BOT's source repo, grounded in the mined bot map.

Expected results quote the bot's REAL reply strings (mined verbatim from source), so
the scenarios assert reality instead of model guesses.

Usage:
  gen_scenarios.py --project my-bot --task "проверка меню настроек"
  gen_scenarios.py --project my-bot --diff main
  gen_scenarios.py --project my-bot --task "..." --out settings.md --prefix S

Output: <project>/.tg-qa/scenarios/<name>.md (refuses to overwrite without --force)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from bot_mine import render_context_section
from registry import find_project, load_config
from tclib import call_claude, classify_tc, declared_type, slugify, split_tcs

MAX_DIFF_CHARS = 9000

PROMPT = """You are a senior QA engineer writing test-case scenarios for a TELEGRAM BOT.
Tests are executed by a real user account talking to the live bot: sending commands,
clicking inline buttons, reading replies and keyboards.

BOT MAP (mined from the bot's source code — commands, keyboards and reply texts are REAL,
target only what exists here):
{bot_context}
{roles_section}
{source_section}

TASK: Write 3-8 focused test cases covering the change/task above. Golden path first,
then edge cases (unknown command, clicking stale buttons, repeated actions).

OUTPUT FORMAT (STRICT — this file is parsed by regex, follow it exactly):
# <Scenario title, one line>

## TC-{prefix}1 — <short imperative title>
**Type:** passive|mutating
**Role:** <role name — ONLY for role-specific TCs; omit otherwise>
**Steps:**
1. Send `/command`
2. Click "Button caption"
**Expected:**
- <observable outcome: reply text, keyboard buttons, edit of the menu message>

## TC-{prefix}2 — <...>
...

RULES:
- `**Type:**` is REQUIRED on every TC: `passive` = navigation and reading only
  (/commands, menu clicks, reading replies); `mutating` = the dialog creates/changes
  data (free-text input, payments, destructive buttons). A TC without it is treated
  as mutating.
- Steps use ONLY two actions: Send `...` (backticked) and Click "..." (quoted button
  caption exactly as it appears on the keyboard in the bot map).
- Expected bullets must be OBSERVABLE in the dialog: quote the bot's reply texts from
  the map VERBATIM (they are asserted literally), name the buttons that must be on the
  keyboard, say explicitly when the bot must EDIT the menu message instead of sending
  a new one.
- If behaviour differs per role, write SEPARATE TCs annotated `**Role:** <name>`.
- Write steps/expected in {language}; keep commands/captions/technical terms as-is.
- Output ONLY the markdown, no commentary before or after.

Write the scenario now:
"""


def git_diff_summary(repo: Path, ref: str) -> str:
    def run(*args: str) -> str:
        proc = subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise SystemExit(f"git {' '.join(args)} failed: {proc.stderr[:300]}")
        return proc.stdout

    stat = run("diff", "--stat", ref)
    diff = run("diff", ref)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n…(diff truncated)"
    return (f"CHANGE UNDER TEST — git diff of the bot repo vs `{ref}`:\n\n"
            f"Files changed:\n{stat}\n\nDiff:\n```\n{diff}\n```")


def load_bot_context(proj_dir: Path) -> str:
    """bot.map.json rendered + human/agent notes from bot.context.md."""
    tg = proj_dir / ".tg-qa"
    parts: list[str] = []
    map_file = tg / "bot.map.json"
    if map_file.is_file():
        parts.append(render_context_section(json.loads(map_file.read_text(encoding="utf-8"))))
    ctx = tg / "bot.context.md"
    if ctx.is_file():
        parts.append(ctx.read_text(encoding="utf-8"))
    if not parts:
        return "(no bot.map.json — run tg-qa-mine first for grounded commands/buttons/replies)"
    return "\n\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", help="registry alias")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--diff", help="git ref of the BOT source repo to diff against")
    src.add_argument("--task", help="free-text feature/task description")
    ap.add_argument("--out", help="output name inside scenarios/ (default: derived)")
    ap.add_argument("--prefix", default="G", help="TC id prefix letter(s), default G")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    proj = find_project(args.project)
    cfg = load_config(proj)
    proj_dir = Path(proj["path"])
    scenarios_dir = proj_dir / ".tg-qa" / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)

    if args.diff:
        repo = Path(cfg.get("bot_source_dir") or proj_dir).expanduser()
        source_section = git_diff_summary(repo, args.diff)
        default_name = f"diff-{slugify(args.diff)}.md"
    else:
        source_section = f"CHANGE UNDER TEST — task description:\n\n{args.task}"
        default_name = f"{slugify(args.task)}.md"

    role_names = [r.get("name") for r in proj.get("roles") or [] if r.get("name")]
    roles_section = (f"\nPROJECT ROLES (a session exists for each): {', '.join(role_names)}\n"
                     if role_names else "")

    out_path = scenarios_dir / (args.out or default_name)
    if out_path.exists() and not args.force:
        print(json.dumps({"error": f"{out_path} exists; use --force or --out"}), file=sys.stderr)
        return 2

    prompt = PROMPT.format(bot_context=load_bot_context(proj_dir),
                           roles_section=roles_section,
                           source_section=source_section, prefix=args.prefix,
                           language=cfg.get("language") or "English")
    print(f"[generate] asking claude ({'diff ' + args.diff if args.diff else 'task'})…", file=sys.stderr)
    md = call_claude(prompt, timeout=240)
    if not md.strip():
        print(json.dumps({"error": "empty output from claude"}), file=sys.stderr)
        return 1

    tc_ids = re.findall(r"^##\s+(TC-[A-Za-z0-9-]+)", md, re.MULTILINE)
    if not tc_ids:
        print(json.dumps({"error": "output has no '## TC-…' headers, not saving",
                          "head": md[:300]}), file=sys.stderr)
        return 1

    out_path.write_text(md if md.endswith("\n") else md + "\n", encoding="utf-8")

    # Surface undeclared Types at birth, while the human is reviewing the plan
    warnings = []
    for tc in split_tcs(md):
        if declared_type(tc["body"]) is None:
            _, reasons = classify_tc(tc["body"])
            warnings.append({"id": tc["id"], "reasons": reasons})
            print(f"[generate] WARNING {tc['id']}: {'; '.join(reasons)}", file=sys.stderr)

    summary = {"out": str(out_path), "tc_count": len(tc_ids), "tc_ids": tc_ids}
    if warnings:
        summary["type_warnings"] = warnings
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
