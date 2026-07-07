"""Zero-token spec runner — executes declarative YAML dialogs over core/driver.py.

A spec is a YAML file in .tg-qa/specs/:

    tc: TC-S1
    title: /start shows welcome + mini app button
    type: passive              # passive | mutating — matrix uses this to gate
    role: default              # registry role (optional)
    steps:
      - send: "/start"
        expect:
          contains: ["Welcome"]
          snapshot: start-welcome        # text baseline with masks
          buttons: [["🦄 Open Telebook"]] # exact keyboard captions, row by row
      - click: "🎨 Style"                # clicks the LAST bot message with buttons
        expect:
          edited: true                   # bot updated the message in place
          buttons_contain: ["Back"]

Assertion vocabulary per step (all optional, all checked):
  reply_within: N     — at least one reaction within N s (default config reply_timeout)
  contains: [..]      — substrings, matched over ALL texts the step produced
  regex: "..."        — same corpus
  snapshot: name      — LAST text vs .tg-qa/baseline/<name>.txt, text_masks applied;
                        missing baseline is written and queued for human review
  buttons: [[..]]     — exact keyboard of the last button-bearing message
  buttons_contain: [] — caption FRAGMENTS present anywhere on that keyboard (substring
                        match — dynamic captions like "🦄 Open {appName}" stay assertable)
  buttons_snapshot: n — keyboard vs .tg-qa/baseline/<n>.kb.json
  media_type: Photo   — media class of the last reply
  edited: true        — the clicked message was edited in place (click steps only)
  no_reply: true      — silence is EXPECTED (skips the implicit reply check)

The runner is deterministic and LLM-free: failures carry the full dialog transcript,
which maintain feeds back to the model. Specs run serially per role session —
parallel sends from one user account trip FloodWait.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from driver import BotDriver, DriverError, keyboard_texts  # noqa: E402
from registry import find_project, load_config, resolve_api, resolve_session  # noqa: E402
from tclib import spec_mutating_sends  # noqa: E402

MASK = "▩"


# --- spec loading / validation --------------------------------------------------

KNOWN_EXPECT = {"reply_within", "contains", "regex", "snapshot", "buttons",
                "buttons_contain", "buttons_snapshot", "media_type", "edited", "no_reply"}


def validate_spec(spec: dict) -> list[str]:
    """Static shape errors (spec_gen retries generation on these; runner refuses)."""
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["spec is not a mapping"]
    if not spec.get("tc"):
        errors.append("missing `tc`")
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("missing or empty `steps`")
        return errors
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            errors.append(f"step {i}: not a mapping")
            continue
        actions = [k for k in ("send", "click", "send_media") if k in step]
        if len(actions) != 1:
            errors.append(f"step {i}: exactly one of send/click/send_media required, "
                          f"got {actions or 'none'}")
        if "click" in step:
            c = step["click"]
            if not (isinstance(c, str) or (isinstance(c, dict) and c.get("button"))):
                errors.append(f"step {i}: click must be a button caption or {{button: ...}}")
        if "send_media" in step:
            m = step["send_media"]
            if not (isinstance(m, dict) and m.get("path")):
                errors.append(f"step {i}: send_media must be {{path: ..., kind: ...}}")
        exp = step.get("expect") or {}
        if not isinstance(exp, dict):
            errors.append(f"step {i}: expect must be a mapping")
            continue
        unknown = set(exp) - KNOWN_EXPECT
        if unknown:
            errors.append(f"step {i}: unknown expect keys {sorted(unknown)}")
        if "edited" in exp and "click" not in step:
            errors.append(f"step {i}: `edited` only applies to click steps")
    return errors


def load_spec(path: Path) -> dict:
    import yaml
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors = validate_spec(spec)
    if errors:
        raise SystemExit(f"{path.name}: invalid spec: " + "; ".join(errors))
    return spec


# --- snapshots -------------------------------------------------------------------

def apply_masks(text: str, masks: list[str]) -> str:
    for pat in masks or []:
        text = re.sub(pat, MASK, text)
    return text


def text_diff(expected: str, actual: str) -> str:
    import difflib
    return "\n".join(difflib.unified_diff(
        expected.splitlines(), actual.splitlines(),
        fromfile="baseline", tofile="actual", lineterm=""))


class Baselines:
    def __init__(self, base_dir: Path, update: bool, masks: list[str]):
        self.dir = base_dir
        self.update = update
        self.masks = masks
        self.created: list[str] = []
        self.updated: list[str] = []

    def check_text(self, name: str, actual: str) -> str | None:
        """None = ok; otherwise a failure message with diff."""
        actual = apply_masks(actual, self.masks)
        f = self.dir / f"{name}.txt"
        if not f.exists() or self.update:
            self.dir.mkdir(parents=True, exist_ok=True)
            existed = f.exists()
            if existed and f.read_text(encoding="utf-8") == actual:
                return None
            f.write_text(actual, encoding="utf-8")
            (self.updated if existed else self.created).append(name)
            return None
        expected = f.read_text(encoding="utf-8")
        if expected == actual:
            return None
        return f"snapshot `{name}` differs:\n{text_diff(expected, actual)}"

    def check_keyboard(self, name: str, actual: list[list[str]]) -> str | None:
        f = self.dir / f"{name}.kb.json"
        payload = json.dumps(actual, ensure_ascii=False, indent=2)
        if not f.exists() or self.update:
            self.dir.mkdir(parents=True, exist_ok=True)
            existed = f.exists()
            if existed and f.read_text(encoding="utf-8") == payload:
                return None
            f.write_text(payload, encoding="utf-8")
            (self.updated if existed else self.created).append(name)
            return None
        expected = json.loads(f.read_text(encoding="utf-8"))
        if expected == actual:
            return None
        return (f"keyboard snapshot `{name}` differs:\n"
                f"baseline: {expected}\nactual:   {actual}")


# --- assertions -------------------------------------------------------------------

def check_step(exp: dict, produced: list[dict], edited: dict | None,
               baselines: Baselines) -> list[str]:
    """All failures for one step. `produced` = new replies (+ edit as last element)."""
    fails: list[str] = []
    corpus_msgs = produced + ([edited] if edited and not edited.get("deleted") else [])
    texts = [m.get("text", "") for m in corpus_msgs]
    corpus = "\n".join(texts)
    last = corpus_msgs[-1] if corpus_msgs else None
    with_kb = next((m for m in reversed(corpus_msgs) if m.get("buttons")), None)

    if exp.get("no_reply"):
        if corpus_msgs:
            fails.append(f"expected silence, got: {texts}")
        return fails
    if not corpus_msgs:
        fails.append("no reaction from bot (no new message, no edit)")
        return fails

    for sub in exp.get("contains") or []:
        if sub not in corpus:
            fails.append(f"missing text: {sub!r}\n--- got ---\n{corpus[:600]}")
    if exp.get("regex") and not re.search(exp["regex"], corpus, re.S):
        fails.append(f"regex {exp['regex']!r} not matched\n--- got ---\n{corpus[:600]}")

    if exp.get("snapshot"):
        err = baselines.check_text(exp["snapshot"], last.get("text", ""))
        if err:
            fails.append(err)

    if exp.get("buttons") is not None:
        actual = keyboard_texts(with_kb) if with_kb else []
        if actual != exp["buttons"]:
            fails.append(f"keyboard mismatch:\nexpected: {exp['buttons']}\nactual:   {actual}")
    for cap in exp.get("buttons_contain") or []:
        flat = [t for row in (keyboard_texts(with_kb) if with_kb else []) for t in row]
        # substring match: mined captions often carry {placeholders} ("🦄 Open {appName}"),
        # so specs assert a stable fragment of the caption, not its runtime rendering
        if not any(cap in t for t in flat):
            fails.append(f"no button containing {cap!r} on keyboard {flat}")
    if exp.get("buttons_snapshot"):
        err = baselines.check_keyboard(exp["buttons_snapshot"],
                                       keyboard_texts(with_kb) if with_kb else [])
        if err:
            fails.append(err)

    if exp.get("media_type"):
        got = last.get("media_type") if last else None
        if not got or exp["media_type"].lower() not in got.lower():
            fails.append(f"expected media {exp['media_type']!r}, got {got!r}")

    if exp.get("edited") and edited is None:
        fails.append("expected the clicked message to be edited in place — it was not")
    return fails


# --- execution ---------------------------------------------------------------------

async def make_driver(proj: dict, cfg: dict, role: str | None) -> BotDriver:
    """Split out so tests can monkeypatch a fake driver in."""
    api_id, api_hash = resolve_api(proj)
    bot = cfg.get("bot") or proj.get("bot")
    if not bot:
        raise SystemExit("bot username missing (registry entry or .tg-qa/config.json)")
    d = BotDriver(resolve_session(proj, role), api_id, api_hash, bot,
                  send_delay=float(cfg.get("send_delay") or 1.0),
                  auto_mute=cfg.get("auto_mute") is not False,
                  auto_read=cfg.get("auto_read") is not False)
    await d.connect()
    return d


async def run_spec(spec: dict, driver: BotDriver, timeout: float,
                   baselines: Baselines) -> dict:
    result = {"tc": spec.get("tc"), "title": spec.get("title") or "",
              "type": spec.get("type") or "passive", "role": spec.get("role"),
              "status": "pass", "failures": [], "dialog": [], "steps": 0}
    last_kb_msg_id: int | None = None

    for step in spec["steps"]:
        result["steps"] += 1
        exp = step.get("expect") or {}
        wait = float(exp.get("reply_within") or timeout)
        edited = None
        try:
            if "send" in step:
                r = await driver.send(str(step["send"]), wait=wait)
                produced = r["replies"]
                result["dialog"].append({"you": str(step["send"])})
            elif "send_media" in step:
                media = step["send_media"]
                r = await driver.send_media(media["path"], kind=media.get("kind", "auto"),
                                            caption=media.get("caption", ""), wait=wait)
                produced = r["replies"]
                result["dialog"].append({"you": f"[{r['kind']}] {media['path']}"})
            else:
                caption = step["click"]["button"] if isinstance(step["click"], dict) else step["click"]
                if last_kb_msg_id is None:
                    raise DriverError(f"click {caption!r}: no bot message with buttons seen yet")
                r = await driver.click(last_kb_msg_id, caption, wait=wait)
                produced = r["replies"]
                edited = r["edited"]
                result["dialog"].append({"you": f"[click] {caption}"})
        except DriverError as e:
            result["status"] = "error"
            result["failures"].append(f"step {result['steps']}: {e}")
            break

        for m in produced + ([edited] if edited else []):
            result["dialog"].append({"bot": m})
        for m in reversed(produced + ([edited] if edited and edited.get("buttons") else [])):
            if m.get("buttons"):
                last_kb_msg_id = m.get("id", last_kb_msg_id)
                break

        fails = check_step(exp, produced, edited, baselines)
        if fails:
            result["status"] = "fail"
            result["failures"] += [f"step {result['steps']}: {f}" for f in fails]
            break
    return result


async def run_all(spec_paths: list[Path], proj: dict, cfg: dict, role_override: str | None,
                  baselines: Baselines, driver_factory=make_driver,
                  passive_only: bool = False) -> list[dict]:
    timeout = float(cfg.get("reply_timeout") or 10)
    drivers: dict[str, BotDriver] = {}
    results: list[dict] = []
    try:
        for path in spec_paths:
            spec = load_spec(path)
            if passive_only and not is_passive_spec(spec):
                results.append({"spec": path.name, "tc": spec.get("tc"),
                                "role": spec.get("role"), "status": "skipped",
                                "failures": [], "dialog": [],
                                "skip_reason": "mutating spec under --passive-only"})
                continue
            role = role_override or spec.get("role")
            key = role or "__default__"
            if key not in drivers:
                drivers[key] = await driver_factory(proj, cfg, role)
            res = await run_spec(spec, drivers[key], timeout, baselines)
            res["spec"] = path.name
            res["role"] = role
            results.append(res)
    finally:
        for d in drivers.values():
            try:
                await d.close()
            except Exception:
                pass
    return results


# --- fixtures / gating / junit ----------------------------------------------------------

def run_fixture_cmd(proj: dict, cfg: dict, teardown: bool = False) -> None:
    """Seed (or tear down) deterministic data once per run. Non-zero exit is a hard
    stop: running dialog tests against an unseeded stand produces noise, not signal."""
    key = "fixture_teardown_cmd" if teardown else "fixture_cmd"
    cmd = cfg.get(key)
    if not cmd:
        return
    print(f"[fixtures] {cmd}", file=sys.stderr)
    proc = subprocess.run(cmd, shell=True, cwd=proj.get("path") or None, timeout=600)
    if proc.returncode != 0 and not teardown:
        raise SystemExit(f"{key} failed with exit {proc.returncode} — refusing to run on "
                         "an unseeded stand")


def is_passive_spec(spec: dict) -> bool:
    """Gate for --passive-only: the spec must DECLARE passive and carry no structural
    mutation evidence (plain-text sends override the label, see tclib)."""
    return (spec.get("type") or "").lower() == "passive" and not spec_mutating_sends(spec)


def render_junit(results: list[dict]) -> str:
    cases = []
    failures = 0
    for r in results:
        bits = [r.get("spec", ""), r.get("tc") or ""]
        if r.get("role"):
            bits.append(r["role"])
        name = "::".join(b for b in bits if b)
        if r["status"] == "pass":
            cases.append(f"  <testcase name={quoteattr(name)}/>")
        elif r["status"] == "skipped":
            cases.append(f"  <testcase name={quoteattr(name)}>"
                         f"<skipped message={quoteattr(r.get('skip_reason') or '')}/></testcase>")
        else:
            failures += 1
            tag = "error" if r["status"] == "error" else "failure"
            body = escape("\n".join(r.get("failures") or []))
            cases.append(f"  <testcase name={quoteattr(name)}>"
                         f"<{tag}>{body}</{tag}></testcase>")
    ran = [r for r in results if r["status"] != "skipped"]
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<testsuite name="tg-qa" tests="{len(ran)}" failures="{failures}">\n'
            + "\n".join(cases) + "\n</testsuite>\n")


# --- reporting ------------------------------------------------------------------------

def render_report(results: list[dict], baselines: Baselines) -> str:
    ran = [r for r in results if r["status"] != "skipped"]
    passed = sum(1 for r in ran if r["status"] == "pass")
    lines = ["# tg-qa run report", "",
             f"**{passed}/{len(ran)} passed**"
             + (f" ({len(results) - len(ran)} skipped)" if len(ran) != len(results) else ""),
             "", "| spec | TC | role | status |", "|---|---|---|---|"]
    icons = {"pass": "✅ pass", "skipped": "⏭ skipped"}
    for r in results:
        lines.append(f"| {r['spec']} | {r['tc']} | {r.get('role') or '-'} | "
                     f"{icons.get(r['status'], '❌ ' + r['status'])} |")
    fails = [r for r in results if r["status"] not in ("pass", "skipped")]
    if fails:
        lines.append("\n## Failures\n")
        for r in fails:
            lines.append(f"### {r['spec']} — {r['tc']}")
            for f in r["failures"]:
                lines.append(f"```\n{f}\n```")
            lines.append("<details><summary>dialog</summary>\n")
            for turn in r["dialog"]:
                if "you" in turn:
                    lines.append(f"- **you:** {turn['you']}")
                else:
                    b = turn["bot"]
                    kb = f" {keyboard_texts(b)}" if b.get("buttons") else ""
                    lines.append(f"- **bot:** {b.get('text', '')[:200]}{kb}")
            lines.append("\n</details>\n")
    if baselines.created or baselines.updated:
        lines.append("\n## Snapshot review queue\n")
        lines.append("New/updated baselines — review them like code:\n")
        for n in baselines.created:
            lines.append(f"- 🆕 `{n}`")
        for n in baselines.updated:
            lines.append(f"- ♻️ `{n}`")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", help="registry alias")
    ap.add_argument("--specs", default="*.yaml", help="glob under .tg-qa/specs/ (default: all)")
    ap.add_argument("--role", help="run everything under this role (overrides per-spec role)")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--passive-only", action="store_true",
                    help="skip mutating specs (declared type + structural evidence)")
    ap.add_argument("--no-fixtures", action="store_true", help="skip fixture_cmd/teardown")
    ap.add_argument("--junit", nargs="?", const="junit.xml",
                    help="write JUnit XML into reports/ (optional filename)")
    ap.add_argument("--json", action="store_true", help="print results.json to stdout too")
    args = ap.parse_args()

    proj = find_project(args.project)
    cfg = load_config(proj)
    tg_dir = Path(proj["path"]) / ".tg-qa"
    spec_paths = sorted((tg_dir / "specs").glob(args.specs))
    if not spec_paths:
        raise SystemExit(f"no specs match {args.specs} in {tg_dir / 'specs'}")

    baselines = Baselines(tg_dir / "baseline", args.update_baseline,
                          cfg.get("text_masks") or [])
    if not args.no_fixtures:
        run_fixture_cmd(proj, cfg)
    try:
        results = asyncio.run(run_all(spec_paths, proj, cfg, args.role, baselines,
                                      passive_only=args.passive_only))
    finally:
        if not args.no_fixtures:
            run_fixture_cmd(proj, cfg, teardown=True)

    reports = tg_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (reports / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    report_md = render_report(results, baselines)
    (reports / "report.md").write_text(report_md, encoding="utf-8")
    (reports / f"report-{stamp}.md").write_text(report_md, encoding="utf-8")
    if args.junit:
        (reports / args.junit).write_text(render_junit(results), encoding="utf-8")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(report_md + "\n")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    ran = [r for r in results if r["status"] != "skipped"]
    passed = sum(1 for r in ran if r["status"] == "pass")
    print(f"{passed}/{len(ran)} passed"
          + (f", {len(results) - len(ran)} skipped" if len(ran) != len(results) else "")
          + f" — {reports / 'report.md'}")
    return 0 if passed == len(ran) else 1


if __name__ == "__main__":
    sys.exit(main())
