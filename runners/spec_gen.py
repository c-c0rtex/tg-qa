"""Generate YAML specs from scenario TCs — validated, probed against the bot map,
one retry with real feedback.

For each `## TC-…` block the model writes a declarative spec (the run_specs.py
vocabulary). Before a spec is accepted:
  1. YAML parses and validate_spec() passes (schema errors → retry with the errors);
  2. PROBE: every `send: /command` and every clicked button caption is checked against
     bot.map.json — a command the bot never handles or a button no keyboard carries is
     a hallucination. Misses → one retry with the real command/button lists; if the
     retry still misses, the spec is saved WITH probe_warnings (dynamic keyboards are
     invisible to the static map — absence of evidence is not proof).
  3. Type conflict: a TC declared passive whose spec sends plain text is flagged
     (structural mutation evidence, see tclib.classify_tc).

Usage:
  spec_gen.py --project my-bot --scenario settings.md [--tc TC-G1] [--no-probe] [--force]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from bot_mine import render_context_section
from registry import find_project, load_config
from run_specs import validate_spec
from tclib import call_claude, classify_tc, declared_type, slugify, split_tcs

PROMPT = """You are a QA automation engineer. Convert ONE Telegram-bot test case into a
declarative YAML spec executed by a deterministic runner (no code, YAML only).

BOT MAP (real commands, keyboards, reply texts mined from the bot's source):
{bot_context}

TEST CASE {tc_id} — {tc_title}:
{tc_body}

SPEC FORMAT (the ONLY allowed keys):
```
tc: {tc_id}
title: <copy the TC title>
type: <copy **Type:** value>
role: <copy **Role:** value if the TC declares one, else omit the key>
steps:
  - send: "/command"            # send text to the bot
    expect:                     # all assertion keys are optional
      contains: ["substring"]   # matched over every text this step produced
      regex: "pat.tern"
      snapshot: kebab-name      # reply text baseline (use for stable texts)
      buttons: [["Caption A"], ["Caption B"]]   # exact keyboard, row by row
      buttons_contain: ["Caption"]              # weaker: captions present anywhere
      buttons_snapshot: kebab-name
      media_type: Photo
      reply_within: 15          # only when the default timeout is too tight
      no_reply: true            # the bot is EXPECTED to stay silent
  - click: "Caption"            # clicks the LAST bot message that had buttons
    expect:
      edited: true              # the bot edits the menu message in place
      contains: ["..."]
```

RULES:
- Assert reply texts with `contains` quoting the bot map's reply strings VERBATIM
  (a fragment is enough — pick a stable one, skip {{placeholders}}).
- Click captions must match keyboard captions from the map EXACTLY (emoji included).
  If a caption contains a {{placeholder}}, prefer `buttons_contain` assertions over
  clicking it, or reproduce the caption as the live bot renders it.
- snapshot names: unique across the project, kebab-case, prefixed with the tc id
  (e.g. "{tc_slug}-welcome").
- Use `no_reply: true` only when silence IS the expected behaviour.
- 1-6 steps. Do NOT invent commands or buttons absent from the map unless the TC
  explicitly introduces them.
- Output ONLY the YAML document, no fences, no commentary.
"""

RETRY_SUFFIX = """

YOUR PREVIOUS ATTEMPT WAS REJECTED:
{feedback}

Fix ONLY these problems and output the FULL corrected YAML again (no fences):
"""


# --- probe against the mined map ---------------------------------------------------

def _map_button_patterns(bot_map: dict) -> list[re.Pattern]:
    pats = []
    for kb in bot_map.get("keyboards") or []:
        for b in kb.get("buttons") or []:
            esc = re.escape(b["text"])
            pats.append(re.compile("^" + re.sub(r"\\\{[^}]*\\\}", ".+", esc) + "$"))
    return pats


def probe_spec(spec: dict, bot_map: dict) -> list[str]:
    """Hallucination check: commands/captions the spec uses but the map never saw.
    Empty map sections disable the corresponding check (nothing to compare against)."""
    warnings: list[str] = []
    known_cmds = {c["command"] for c in bot_map.get("commands") or []}
    btn_pats = _map_button_patterns(bot_map)
    for i, step in enumerate(spec.get("steps") or [], 1):
        if not isinstance(step, dict):
            continue
        if "send" in step:
            text = str(step["send"]).strip()
            if text.startswith("/") and known_cmds:
                cmd = text.split()[0].split("@")[0]
                if cmd not in known_cmds:
                    warnings.append(f"step {i}: command `{cmd}` is not in the bot map "
                                    f"(known: {sorted(known_cmds)})")
        if "click" in step and btn_pats:
            caption = step["click"]["button"] if isinstance(step["click"], dict) else str(step["click"])
            if not any(p.match(caption) for p in btn_pats):
                known = sorted({p.pattern for p in btn_pats})
                warnings.append(f"step {i}: button {caption!r} matches no mined keyboard "
                                f"(mined captions: {known})")
    return warnings


# --- generation --------------------------------------------------------------------

def parse_spec_yaml(raw: str) -> tuple[dict | None, str | None]:
    try:
        spec = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return None, f"YAML parse error: {e}"
    errors = validate_spec(spec) if spec else ["empty output"]
    if errors:
        return None, "schema errors: " + "; ".join(errors)
    return spec, None


def gen_one(tc: dict, bot_context: str, bot_map: dict, probe: bool) -> tuple[dict | None, str | None, list[str]]:
    """(spec, fatal_error, probe_warnings) — one retry covers schema AND probe misses.
    An LLM-mined map is weaker ground truth: misses against it are reported but never
    burn the retry (the map itself may be the one hallucinating)."""
    strict_map = bot_map.get("mined_by") != "llm"
    prompt = PROMPT.format(bot_context=bot_context, tc_id=tc["id"], tc_title=tc["title"],
                           tc_body=tc["body"].strip(), tc_slug=slugify(tc["id"]))
    raw = call_claude(prompt)
    spec, err = parse_spec_yaml(raw)
    warnings = probe_spec(spec, bot_map) if spec and probe else []

    if err or (warnings and strict_map):
        feedback = err or ("the runner probed your spec against the REAL bot map:\n- "
                           + "\n- ".join(warnings)
                           + "\nUse only mined commands/captions — EXCEPTION: keep a step "
                             "if its button only appears on a dynamic keyboard the TC "
                             "explicitly describes.")
        raw = call_claude(prompt + RETRY_SUFFIX.format(feedback=feedback))
        spec, err = parse_spec_yaml(raw)
        if err:
            return None, err, []
        warnings = probe_spec(spec, bot_map) if probe else []

    if spec.get("tc") != tc["id"]:
        spec["tc"] = tc["id"]
    return spec, None, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", help="registry alias")
    ap.add_argument("--scenario", required=True, help="file name inside .tg-qa/scenarios/")
    ap.add_argument("--tc", help="generate only this TC id")
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    proj = find_project(args.project)
    load_config(proj)  # validates project dir shape early
    tg = Path(proj["path"]) / ".tg-qa"
    scenario_path = tg / "scenarios" / args.scenario
    if not scenario_path.is_file():
        raise SystemExit(f"scenario not found: {scenario_path}")
    specs_dir = tg / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)

    map_file = tg / "bot.map.json"
    bot_map = json.loads(map_file.read_text(encoding="utf-8")) if map_file.is_file() else {}
    bot_context = (render_context_section(bot_map) if bot_map
                   else "(no bot.map.json — run tg-qa-mine first)")
    if not bot_map and not args.no_probe:
        print("[spec-gen] no bot.map.json — probe disabled", file=sys.stderr)

    tcs = split_tcs(scenario_path.read_text(encoding="utf-8"))
    if args.tc:
        tcs = [t for t in tcs if t["id"] == args.tc]
        if not tcs:
            raise SystemExit(f"{args.tc} not found in {args.scenario}")

    results, all_warnings, conflicts = [], {}, {}
    for tc in tcs:
        out_path = specs_dir / f"{slugify(tc['id'])}-{slugify(tc['title'])}.yaml"
        if out_path.exists() and not args.force:
            results.append({"tc": tc["id"], "spec": out_path.name, "skipped": "exists"})
            continue
        print(f"[spec-gen] {tc['id']} — {tc['title']}", file=sys.stderr)
        spec, err, warnings = gen_one(tc, bot_context, bot_map,
                                      probe=bool(bot_map) and not args.no_probe)
        if err:
            results.append({"tc": tc["id"], "error": err})
            continue
        kind, reasons = classify_tc(tc["body"], spec)
        if declared_type(tc["body"]) == "passive" and kind == "mutating":
            conflicts[tc["id"]] = reasons
        header = (f"# generated from {args.scenario}#{tc['id']}\n"
                  f"# regenerate: tg-qa-spec-gen --scenario {args.scenario} --tc {tc['id']} --force\n")
        out_path.write_text(header + yaml.safe_dump(spec, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        results.append({"tc": tc["id"], "spec": out_path.name,
                        **({"probe_warnings": warnings} if warnings else {})})
        if warnings:
            all_warnings[tc["id"]] = warnings

    summary = {"scenario": args.scenario, "specs": results}
    if all_warnings:
        summary["probe_warnings"] = all_warnings
    if conflicts:
        summary["type_conflicts"] = conflicts
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not any("error" in r for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
