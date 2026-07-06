"""Heal failing specs from the last run — the fix prompt carries the ACTUAL dialog.

Reads .tg-qa/reports/results.json, and for every fail/error spec asks the model to
repair the spec: the prompt contains the spec, the source TC, the assertion failures
and the full dialog transcript the runner recorded (what the bot REALLY said — the
analog of web-qa's error-context.md). The model must decide:

  - the SPEC is wrong (stale caption, brittle assertion, missing reply_within,
    snapshot needs a mask) → output corrected YAML; or
  - the BOT is wrong (the dialog contradicts the TC's intent) → output a single line
    `PRODUCT-BUG: <reason>` — maintain never heals product bugs into green tests;
    these land in the summary as needs-human.

Healed specs are re-validated and re-probed like fresh ones; the old spec is kept
as .bak next to it.

Usage:
  maintain.py --project my-bot [--tc TC-G2] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from bot_mine import render_context_section
from registry import find_project, load_config
from spec_gen import parse_spec_yaml, probe_spec
from tclib import call_claude, split_tcs

FIX_PROMPT = """You are a QA automation engineer. A declarative Telegram-bot spec FAILED
against the live bot. Repair the spec — or report a product bug.

BOT MAP (mined from source — the ground truth for commands/captions/reply texts):
{bot_context}

SOURCE TEST CASE {tc_id}:
{tc_body}

CURRENT SPEC ({spec_name}):
```
{spec_yaml}
```

ASSERTION FAILURES:
{failures}

ACTUAL DIALOG the runner recorded (you = test, bot = live bot):
{dialog}

DECIDE:
- If the spec is at fault (wrong/renamed caption, too-tight timeout, asserting a
  volatile fragment, snapshot lacking a mask, wrong step order) — output the FULL
  corrected YAML spec (same format, no fences, no commentary). Keep the same tc id.
- If the DIALOG shows the bot violating the test case's intent — do NOT weaken the
  assertions to make it pass. Output exactly one line instead:
  PRODUCT-BUG: <one-sentence reason referencing the dialog>
"""


def format_dialog(dialog: list[dict]) -> str:
    lines = []
    for turn in dialog:
        if "you" in turn:
            lines.append(f"you: {turn['you']}")
        else:
            b = turn.get("bot") or {}
            kb = ""
            if b.get("buttons"):
                kb = " | keyboard: " + json.dumps(
                    [[btn["text"] for btn in row] for row in b["buttons"]], ensure_ascii=False)
            media = f" | media: {b['media_type']}" if b.get("media_type") else ""
            lines.append(f"bot: {b.get('text', '')!r}{kb}{media}")
    return "\n".join(lines) or "(empty — the bot never reacted)"


def find_tc_body(scenarios_dir: Path, tc_id: str) -> str:
    for f in sorted(scenarios_dir.glob("*.md")):
        for tc in split_tcs(f.read_text(encoding="utf-8")):
            if tc["id"] == tc_id:
                return f"## {tc['id']} — {tc['title']}\n{tc['body']}"
    return "(source TC not found in scenarios/)"


def heal_one(result: dict, tg: Path, bot_context: str, bot_map: dict,
             dry_run: bool) -> dict:
    spec_path = tg / "specs" / result["spec"]
    if not spec_path.is_file():
        return {"tc": result.get("tc"), "spec": result["spec"], "status": "missing-spec"}
    spec_yaml = spec_path.read_text(encoding="utf-8")

    prompt = FIX_PROMPT.format(
        bot_context=bot_context,
        tc_id=result.get("tc") or "?",
        tc_body=find_tc_body(tg / "scenarios", result.get("tc") or ""),
        spec_name=result["spec"],
        spec_yaml=spec_yaml,
        failures="\n".join(result.get("failures") or []) or "(none recorded)",
        dialog=format_dialog(result.get("dialog") or []),
    )
    out = call_claude(prompt)

    first_line = out.strip().splitlines()[0] if out.strip() else ""
    if first_line.startswith("PRODUCT-BUG:"):
        return {"tc": result.get("tc"), "spec": result["spec"],
                "status": "product-bug", "reason": first_line[len("PRODUCT-BUG:"):].strip()}

    spec, err = parse_spec_yaml(out)
    if err:
        return {"tc": result.get("tc"), "spec": result["spec"],
                "status": "invalid-fix", "error": err}
    if spec.get("tc") != result.get("tc") and result.get("tc"):
        spec["tc"] = result["tc"]
    warnings = probe_spec(spec, bot_map) if bot_map else []

    if not dry_run:
        spec_path.with_suffix(spec_path.suffix + ".bak").write_text(spec_yaml, encoding="utf-8")
        header = "# healed by tg-qa-maintain from a failing run\n"
        spec_path.write_text(header + yaml.safe_dump(spec, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")
    return {"tc": result.get("tc"), "spec": result["spec"], "status": "healed",
            **({"probe_warnings": warnings} if warnings else {})}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", help="registry alias")
    ap.add_argument("--tc", help="heal only this TC id")
    ap.add_argument("--dry-run", action="store_true", help="don't write, just report verdicts")
    args = ap.parse_args()

    proj = find_project(args.project)
    load_config(proj)
    tg = Path(proj["path"]) / ".tg-qa"
    results_file = tg / "reports" / "results.json"
    if not results_file.is_file():
        raise SystemExit(f"no {results_file} — run tg-qa-run first")
    results = json.loads(results_file.read_text(encoding="utf-8"))

    failing = [r for r in results if r.get("status") in ("fail", "error")
               and (not args.tc or r.get("tc") == args.tc)]
    if not failing:
        print(json.dumps({"healed": [], "note": "nothing failing"}))
        return 0

    map_file = tg / "bot.map.json"
    bot_map = json.loads(map_file.read_text(encoding="utf-8")) if map_file.is_file() else {}
    bot_context = (render_context_section(bot_map) if bot_map
                   else "(no bot.map.json — captions can't be cross-checked)")

    out = []
    for r in failing:
        print(f"[maintain] {r.get('tc')} ({r['spec']}) — {r['status']}", file=sys.stderr)
        out.append(heal_one(r, tg, bot_context, bot_map, args.dry_run))

    summary = {
        "healed": [o for o in out if o["status"] == "healed"],
        "product_bugs": [o for o in out if o["status"] == "product-bug"],
        "failed_to_heal": [o for o in out if o["status"] in ("invalid-fix", "missing-spec")],
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["failed_to_heal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
