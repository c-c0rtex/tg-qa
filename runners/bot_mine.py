"""Mine the bot map from the bot's SOURCE CODE — the source of truth for generation.

Deterministic (regex + bracket matching), no LLM: commands, callback handlers, inline
keyboards (text / callback_data / web_app), and reply texts are extracted from string
literals in the repo. Generated scenarios then assert against the bot's real strings
instead of model guesses; the live bot is only used to VERIFY this map (probe), not to
invent it.

Framework adapters (detected from manifest dependencies, deepest manifest wins):
  - node-telegram-bot-api  (JS/TS): onText(/regex/), switch/case + === on msg.text,
    callback_data literals, inline_keyboard blocks, sendMessage texts
  - aiogram v3             (Python): Command()/CommandStart(), F.data filters,
    InlineKeyboardButton, answer()/reply()/send_message()/edit_text() texts

Honest limits: only static string/template literals are mined. Texts built from i18n
tables, databases or concatenation are invisible here — the Explore phase supplements
the map by reading the code with the model and by talking to the live bot. Template
placeholders are normalized to `{name}` so snapshots can mask them.

Usage:
  bot_mine.py --project <alias>            # bot_source_dir from .tg-qa/config.json
  bot_mine.py --source-dir <path> [--framework node-telegram-bot-api|aiogram] [--out map.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from registry import find_project, load_config

SKIP_DIRS = {"node_modules", "dist", "build", ".git", ".venv", "venv", "__pycache__", "coverage"}

# --- shared string helpers ----------------------------------------------------

RE_JS_STRING = r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`"
RE_PY_STRING = r"'''(?:.|\n)*?'''|\"\"\"(?:.|\n)*?\"\"\"|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\""


def unquote_js(lit: str) -> str:
    body = lit[1:-1]
    body = re.sub(r"\$\{([^}]*)\}", lambda m: "{%s}" % m.group(1).split(".")[-1].strip(), body)
    return body.replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n").replace("\\`", "`")


def unquote_py(lit: str) -> str:
    for q in ("'''", '"""'):
        if lit.startswith(q):
            return lit[3:-3]
    body = lit[1:-1]
    return body.replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n")


def balanced(text: str, start: int, open_ch: str, close_ch: str) -> str | None:
    """Slice of `text` from the opener at/after `start` to its matching closer.
    String-literal aware enough for mining: skips over quoted segments."""
    i = text.find(open_ch, start)
    if i < 0:
        return None
    depth, j = 0, i
    in_str: str | None = None
    while j < len(text):
        ch = text[j]
        if in_str:
            if ch == "\\":
                j += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "'\"`":
            in_str = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
        j += 1
    return None


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# --- framework detection --------------------------------------------------------

def detect_framework(root: Path) -> tuple[str | None, Path | None]:
    """Search manifests (package.json / pyproject.toml / requirements*.txt) breadth-first;
    the SHALLOWEST manifest naming a known framework wins. Returns (framework, dir)."""
    manifests: list[Path] = []
    for pat in ("package.json", "pyproject.toml", "requirements.txt", "requirements/*.txt"):
        manifests += [p for p in root.rglob(pat) if not (set(p.parts) & SKIP_DIRS)]
    manifests.sort(key=lambda p: len(p.parts))
    for m in manifests:
        try:
            body = m.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if m.name == "package.json":
            try:
                deps = {**(json.loads(body).get("dependencies") or {}),
                        **(json.loads(body).get("devDependencies") or {})}
            except json.JSONDecodeError:
                continue
            for fw in ("node-telegram-bot-api", "grammy", "telegraf"):
                if fw in deps:
                    return fw, m.parent
        else:
            for fw in ("aiogram", "python-telegram-bot", "pytelegrambotapi"):
                if re.search(rf"^\s*{re.escape(fw)}\b", body, re.I | re.M):
                    return fw, m.parent
    return None, None


def source_files(root: Path, exts: tuple[str, ...]) -> list[Path]:
    return sorted(p for p in root.rglob("*")
                  if p.suffix in exts and p.is_file() and not (set(p.parts) & SKIP_DIRS))


# --- node-telegram-bot-api (and close JS cousins) --------------------------------

RE_ONTEXT = re.compile(r"\.onText\(\s*/((?:\\.|[^/\\])*)/")
RE_CASE_CMD = re.compile(r"case\s*(['\"]/[^'\"]*['\"])\s*:")
RE_EQ_CMD = re.compile(r"(?:text|data)\s*===?\s*(['\"](?:\\.|[^'\"\\])*['\"])")
RE_CALLBACK_DATA = re.compile(r"callback_data\s*:\s*(%s)" % RE_JS_STRING)
RE_SEND_TEXT = re.compile(r"\.(?:sendMessage|editMessageText)\(\s*[^,()]*,\s*(%s)" % RE_JS_STRING, re.S)
RE_JS_BTN_TEXT = re.compile(r"text\s*:\s*(%s)" % RE_JS_STRING)
RE_JS_BTN_URL = re.compile(r"\burl\s*:\s*(%s)" % RE_JS_STRING)


def command_from_ontext_regex(pattern: str) -> str | None:
    m = re.match(r"\\?/(\w+)", pattern.lstrip("^"))
    return "/" + m.group(1) if m else None


def mine_js_file(path: Path, rel: str, out: dict) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")

    for m in RE_ONTEXT.finditer(text):
        cmd = command_from_ontext_regex(m.group(1))
        if cmd:
            out["commands"].append({"command": cmd, "source": f"{rel}:{line_of(text, m.start())}"})
    for m in RE_CASE_CMD.finditer(text):
        out["commands"].append({"command": unquote_js(m.group(1)),
                                "source": f"{rel}:{line_of(text, m.start())}"})
    for m in RE_EQ_CMD.finditer(text):
        val = unquote_js(m.group(1))
        kind = "commands" if val.startswith("/") else "callbacks"
        key = "command" if val.startswith("/") else "data"
        out[kind].append({key: val, "source": f"{rel}:{line_of(text, m.start())}"})
    for m in RE_CALLBACK_DATA.finditer(text):
        out["callbacks"].append({"data": unquote_js(m.group(1)),
                                 "source": f"{rel}:{line_of(text, m.start())}"})
    for m in RE_SEND_TEXT.finditer(text):
        out["replies"].append({"text": unquote_js(m.group(1)),
                               "source": f"{rel}:{line_of(text, m.start())}"})

    for m in re.finditer(r"inline_keyboard\s*:", text):
        block = balanced(text, m.end(), "[", "]")
        if not block:
            continue
        buttons: list[dict] = []
        for bm in re.finditer(r"\{", block):
            obj = balanced(block, bm.start(), "{", "}")
            if not obj or "text" not in obj:
                continue
            tm = RE_JS_BTN_TEXT.search(obj)
            if not tm:
                continue
            btn = {"text": unquote_js(tm.group(1))}
            dm = RE_CALLBACK_DATA.search(obj)
            if dm:
                btn["callback_data"] = unquote_js(dm.group(1))
            if re.search(r"web_app\s*:", obj):
                btn["web_app"] = True
                um = RE_JS_BTN_URL.search(obj)
                if um:
                    btn["url"] = unquote_js(um.group(1))
            if btn not in buttons:
                buttons.append(btn)
        if buttons:
            out["keyboards"].append({"buttons": buttons,
                                     "source": f"{rel}:{line_of(text, m.start())}"})


# --- aiogram v3 -------------------------------------------------------------------

RE_AIO_COMMAND = re.compile(r"Command\(\s*(%s)" % RE_PY_STRING)
RE_AIO_COMMANDSTART = re.compile(r"CommandStart\(")
RE_AIO_FDATA = re.compile(r"F\.data(?:\.startswith)?\s*(?:==\s*|\(\s*)(%s)" % RE_PY_STRING)
RE_AIO_CBFACTORY = re.compile(r"prefix\s*=\s*(%s)" % RE_PY_STRING)
RE_AIO_BTN = re.compile(r"InlineKeyboardButton\(", re.S)
RE_AIO_KW_TEXT = re.compile(r"text\s*=\s*(%s)" % RE_PY_STRING)
RE_AIO_KW_DATA = re.compile(r"callback_data\s*=\s*(%s)" % RE_PY_STRING)
RE_AIO_WEBAPP = re.compile(r"web_app\s*=")
RE_AIO_KW_URL = re.compile(r"\burl\s*=\s*(%s)" % RE_PY_STRING)
RE_AIO_REPLY = re.compile(r"\.(?:answer|reply|edit_text)\(\s*(%s)" % RE_PY_STRING, re.S)
RE_AIO_SEND = re.compile(r"\.send_message\(\s*[^,()]*,\s*(?:text\s*=\s*)?(%s)" % RE_PY_STRING, re.S)


def mine_py_file(path: Path, rel: str, out: dict) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")

    for m in RE_AIO_COMMAND.finditer(text):
        cmd = unquote_py(m.group(1)).lstrip("/")
        out["commands"].append({"command": "/" + cmd, "source": f"{rel}:{line_of(text, m.start())}"})
    for m in RE_AIO_COMMANDSTART.finditer(text):
        out["commands"].append({"command": "/start", "source": f"{rel}:{line_of(text, m.start())}"})
    for m in RE_AIO_FDATA.finditer(text):
        out["callbacks"].append({"data": unquote_py(m.group(1)),
                                 "source": f"{rel}:{line_of(text, m.start())}"})
    for m in RE_AIO_CBFACTORY.finditer(text):
        out["callbacks"].append({"data": unquote_py(m.group(1)) + ":*",
                                 "source": f"{rel}:{line_of(text, m.start())}"})
    for m in RE_AIO_REPLY.finditer(text):
        out["replies"].append({"text": unquote_py(m.group(1)),
                               "source": f"{rel}:{line_of(text, m.start())}"})
    for m in RE_AIO_SEND.finditer(text):
        out["replies"].append({"text": unquote_py(m.group(1)),
                               "source": f"{rel}:{line_of(text, m.start())}"})

    buttons: list[dict] = []
    for m in RE_AIO_BTN.finditer(text):
        obj = balanced(text, m.end() - 1, "(", ")")
        if not obj:
            continue
        tm = RE_AIO_KW_TEXT.search(obj)
        if not tm:
            continue
        btn = {"text": unquote_py(tm.group(1))}
        dm = RE_AIO_KW_DATA.search(obj)
        if dm:
            btn["callback_data"] = unquote_py(dm.group(1))
        if RE_AIO_WEBAPP.search(obj):
            btn["web_app"] = True
            um = RE_AIO_KW_URL.search(obj)
            if um:
                btn["url"] = unquote_py(um.group(1))
        if btn not in buttons:
            buttons.append(btn)
    if buttons:
        out["keyboards"].append({"buttons": buttons, "source": rel})


# --- orchestration ---------------------------------------------------------------

JS_LIKE = {"node-telegram-bot-api", "grammy", "telegraf"}
PY_LIKE = {"aiogram", "python-telegram-bot", "pytelegrambotapi"}
FULLY_SUPPORTED = {"node-telegram-bot-api", "aiogram"}


def dedupe(items: list[dict], key: str) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        if it[key] not in seen:
            seen.add(it[key])
            out.append(it)
    return out


def mine(source_dir: Path, framework: str | None = None) -> dict:
    fw, fw_dir = (framework, source_dir) if framework else detect_framework(source_dir)
    if fw is None:
        raise SystemExit(f"No known bot framework found under {source_dir} — "
                         "pass --framework explicitly if the dependency is indirect")
    scan_root = fw_dir or source_dir
    out: dict = {"framework": fw, "source_dir": str(source_dir),
                 "commands": [], "callbacks": [], "keyboards": [], "replies": []}
    if fw not in FULLY_SUPPORTED:
        out["warning"] = (f"framework '{fw}' detected but only heuristically supported "
                          "(patterns tuned for node-telegram-bot-api/aiogram)")
    if fw in JS_LIKE:
        for f in source_files(scan_root, (".ts", ".js", ".mjs", ".cjs")):
            mine_js_file(f, str(f.relative_to(source_dir)), out)
    else:
        for f in source_files(scan_root, (".py",)):
            mine_py_file(f, str(f.relative_to(source_dir)), out)

    out["commands"] = dedupe(out["commands"], "command")
    out["callbacks"] = dedupe(out["callbacks"], "data")
    out["replies"] = dedupe(out["replies"], "text")
    out["miniapp"] = any(b.get("web_app") for kb in out["keyboards"] for b in kb["buttons"])
    return out


def render_context_section(bot_map: dict) -> str:
    """Markdown section for bot.context.md — what generation agents actually read."""
    lines = ["## Mined from source", ""]
    lines.append(f"Framework: `{bot_map['framework']}` — mined deterministically; "
                 "dynamic/i18n texts are NOT here.")
    lines.append("")
    lines.append("### Commands")
    for c in bot_map["commands"] or []:
        lines.append(f"- `{c['command']}` ({c['source']})")
    if not bot_map["commands"]:
        lines.append("_(none found — the bot may react to plain text only)_")
    lines.append("")
    lines.append("### Inline keyboards")
    for kb in bot_map["keyboards"]:
        btns = " | ".join(
            f"[{b['text']}]" + ("(web_app)" if b.get("web_app") else
                                f"(cb:{b['callback_data']})" if b.get("callback_data") else "")
            for b in kb["buttons"])
        lines.append(f"- {btns} ({kb['source']})")
    if not bot_map["keyboards"]:
        lines.append("_(none)_")
    lines.append("")
    if bot_map["callbacks"]:
        lines.append("### Callback data")
        for cb in bot_map["callbacks"]:
            lines.append(f"- `{cb['data']}` ({cb['source']})")
        lines.append("")
    lines.append("### Reply texts (verbatim, for expected results)")
    for r in bot_map["replies"]:
        first = r["text"].strip().splitlines()[0] if r["text"].strip() else ""
        lines.append(f"- “{first[:120]}” ({r['source']})")
    if not bot_map["replies"]:
        lines.append("_(none)_")
    lines.append("")
    lines.append(f"Mini App button present: **{'yes' if bot_map['miniapp'] else 'no'}**")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", help="registry alias (uses .tg-qa/config.json bot_source_dir)")
    ap.add_argument("--source-dir", help="bot source dir (overrides config)")
    ap.add_argument("--framework", help="skip detection")
    ap.add_argument("--out", help="write map JSON here (default: <project>/.tg-qa/bot.map.json)")
    args = ap.parse_args()

    proj = None
    src = args.source_dir
    if not src:
        proj = find_project(args.project)
        cfg = load_config(proj)
        src = cfg.get("bot_source_dir")
        if not src:
            raise SystemExit("bot_source_dir is not set in .tg-qa/config.json and no --source-dir given")
    src_path = Path(src).expanduser().resolve()
    if not src_path.is_dir():
        raise SystemExit(f"source dir not found: {src_path}")

    bot_map = mine(src_path, args.framework)

    out_path = Path(args.out) if args.out else None
    if out_path is None and proj is None and args.project:
        proj = find_project(args.project)
    if out_path is None:
        base = Path(proj["path"]) / ".tg-qa" if proj and proj.get("path") not in (None, "*") else Path.cwd()
        base.mkdir(parents=True, exist_ok=True)
        out_path = base / "bot.map.json"
    out_path.write_text(json.dumps(bot_map, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "framework": bot_map["framework"],
        "commands": [c["command"] for c in bot_map["commands"]],
        "keyboards": len(bot_map["keyboards"]),
        "callbacks": len(bot_map["callbacks"]),
        "replies": len(bot_map["replies"]),
        "miniapp": bot_map["miniapp"],
        "map": str(out_path),
        **({"warning": bot_map["warning"]} if "warning" in bot_map else {}),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
