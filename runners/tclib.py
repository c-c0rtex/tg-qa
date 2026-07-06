"""Shared TC-markdown parsing + LLM invocation — same scenario format as web-qa.

A scenario file is `## TC-X — title` blocks with `**Type:**`, optional `**Role:**`,
`**Steps:**`, `**Expected:**`. The format vocabulary (`Type`, `Role`, field values)
is fixed regardless of the language the steps are written in — classification never
reads prose.

Mutation classification for Telegram is structural, like web-qa's HTTP-method rule:
a spec that SENDS plain (non-command) text is evidence of mutation — free text feeds
dialogs and creates content, while `/commands` and button clicks are navigation.
The declared `**Type:**` is the source of truth; declared passive + plain-text sends
in the generated spec = a conflict surfaced to the human, and matrix treats the TC
as mutating (a mislabel must not slip a data-writing dialog into a passive run).
"""

from __future__ import annotations

import os
import re
import subprocess

RE_TC_HEADER = re.compile(r"^##\s+(TC-[A-Za-z0-9-]+)\s*[—–-]\s*(.+)$", re.MULTILINE)
RE_TC_TYPE = re.compile(r"\*\*Type:\*\*\s*`?(passive|mutating)`?", re.IGNORECASE)
RE_TC_ROLE = re.compile(r"\*\*Roles?:\*\*\s*([^\n]+)", re.IGNORECASE)


def split_tcs(md: str) -> list[dict]:
    tcs = []
    headers = list(RE_TC_HEADER.finditer(md))
    for i, h in enumerate(headers):
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(md)
        tcs.append({"id": h.group(1), "title": h.group(2).strip(), "body": md[start:end]})
    return tcs


def declared_type(body: str) -> str | None:
    m = RE_TC_TYPE.search(body)
    return m.group(1).lower() if m else None


def tc_roles(body: str) -> list[str]:
    """`**Role:** admin` / `**Roles:** a, b`; empty = role-agnostic."""
    m = RE_TC_ROLE.search(body)
    if not m:
        return []
    return [r.strip().strip("`").lower() for r in m.group(1).split(",") if r.strip()]


def spec_mutating_sends(spec: dict) -> list[str]:
    """Plain-text sends in a generated spec — structural mutation evidence.
    Commands (`/...`) and click steps don't count."""
    out: list[str] = []
    for step in spec.get("steps") or []:
        if isinstance(step, dict) and "send" in step:
            text = str(step["send"]).strip()
            if text and not text.startswith("/"):
                out.append(text)
    return out


def classify_tc(body: str, spec: dict | None = None) -> tuple[str, list[str]]:
    """(kind, reasons). Declared type is the truth; structural evidence from the TC's
    generated spec overrides a declared passive; no signals at all = mutating."""
    t = declared_type(body)
    sends = spec_mutating_sends(spec) if spec else []
    if t == "passive":
        if sends:
            return "mutating", [f"declared passive, but the spec sends plain text {s!r} "
                                "(free text feeds dialogs/creates content)" for s in sends]
        return "passive", []
    if t == "mutating":
        return "mutating", []
    return "mutating", ["no **Type:** declared — treated as mutating, runners never guess from prose"]


def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", s).strip("-").lower()
    return s[:60] or "tc"


def call_claude(prompt: str, timeout: int | None = None) -> str:
    """`claude -p <prompt>` → stdout. TGQA_CLAUDE_MODEL overrides the model,
    TGQA_GEN_TIMEOUT (default 300 s) bounds one generation. Strips stray fences."""
    timeout = timeout or int(os.environ.get("TGQA_GEN_TIMEOUT", "300"))
    cmd = ["claude", "-p", prompt]
    model = os.environ.get("TGQA_CLAUDE_MODEL")
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {proc.stderr[:500]}")
    out = proc.stdout.strip()
    out = re.sub(r"^```(?:yaml|yml|markdown|md)?\s*\n?", "", out)
    out = re.sub(r"\n?```\s*$", "", out)
    return out
