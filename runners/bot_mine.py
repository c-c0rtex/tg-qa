"""Mine the bot map from the bot's SOURCE CODE — the source of truth for generation.

Two mining paths, one map schema (commands / callbacks / keyboards / replies):

  DETERMINISTIC (fast path, known frameworks) — regex + bracket matching, no LLM,
  reproducible, `file:line` sources:
  - node-telegram-bot-api  (JS/TS): onText(/regex/), switch/case + === on msg.text,
    callback_data literals, inline_keyboard blocks, sendMessage texts
  - aiogram v3             (Python): Command()/CommandStart(), F.data filters,
    InlineKeyboardButton, answer()/reply()/send_message()/edit_text() texts

  LLM (universal path) — any language/framework: files are shortlisted by the
  Bot API protocol tokens they contain (`sendMessage`, `inline_keyboard`,
  `callback_data`… are the same strings in Go, Rust, PHP or a hand-rolled wrapper),
  then the model reads them and fills the SAME JSON schema, quoting strings verbatim.
  Used automatically when no known framework is detected, forced with --llm, or run
  ON TOP of the deterministic pass with --llm-augment (picks up i18n tables,
  concatenations, texts in constants the regexes can't see).

  A map whose top-level `mined_by` is "llm" is treated as WEAKER ground truth
  downstream: spec_gen's probe reports misses against it but does not burn a
  generation retry on them.

Honest limits of the deterministic pass: only static string/template literals.
Template placeholders are normalized to `{name}` so snapshots can mask them.

Usage:
  bot_mine.py --project <alias>            # bot_source_dir from .tg-qa/config.json
  bot_mine.py --source-dir <path> [--framework ...] [--llm | --llm-augment] [--out map.json]
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


# --- LLM mining (universal path) ---------------------------------------------------

# Bot API protocol tokens — identical strings in every language, because they ARE the
# wire protocol. A file mentioning them is bot-dialog code regardless of framework.
BOT_API_TOKENS = ("sendMessage", "send_message", "inline_keyboard", "callback_data",
                  "answerCallbackQuery", "answer_callback_query", "reply_markup",
                  "web_app", "WebAppInfo", "onText", "setMyCommands", "set_my_commands",
                  "InlineKeyboardButton", "editMessageText", "edit_message_text",
                  "pre_checkout_query", "sendInvoice", "send_invoice")

CODE_EXTS = (".ts", ".js", ".mjs", ".cjs", ".py", ".go", ".rs", ".rb", ".php",
             ".java", ".kt", ".cs", ".ex", ".exs", ".lua", ".swift", ".dart")

MAX_LLM_FILES = 20
MAX_FILE_CHARS = 12_000
MAX_TOTAL_CHARS = 60_000

LLM_MINE_PROMPT = """You are building a TEST MAP of a Telegram bot from its source code.
Below are the bot's dialog-handling files. Extract ONLY what is literally in the code —
never invent or paraphrase.

OUTPUT: a single JSON object, no fences, no commentary, exactly this schema:
{{
  "commands":  [{{"command": "/start", "source": "<file path>"}}],
  "callbacks": [{{"data": "<callback_data value>", "source": "<file path>"}}],
  "keyboards": [{{"buttons": [{{"text": "<caption>", "callback_data": "<optional>",
                               "web_app": true, "url": "<optional>"}}],
                 "source": "<file path>"}}],
  "replies":   [{{"text": "<reply text VERBATIM>", "source": "<file path>"}}]
}}

RULES:
- Quote reply texts and button captions VERBATIM (emoji, punctuation, newlines as \\n).
- Replace dynamic interpolations with a {{name}} placeholder (e.g. "Hi {{username}}").
- Include texts assembled from i18n tables/constants when the literal is visible in
  the provided files; if a text lives outside these files, SKIP it — do not guess.
- "web_app": true only for buttons that open a Mini App; omit the key otherwise.
- Empty categories stay as empty arrays.

FILES:
{files_section}
"""


def candidate_files(root: Path) -> list[tuple[Path, str]]:
    """Shortlist dialog-handling files by Bot API token density — language-agnostic."""
    hits: list[tuple[int, Path, str]] = []
    for f in source_files(root, CODE_EXTS):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        score = sum(text.count(tok) for tok in BOT_API_TOKENS)
        if score:
            hits.append((score, f, text))
    hits.sort(key=lambda h: -h[0])
    return [(f, text) for _, f, text in hits[:MAX_LLM_FILES]]


def validate_llm_map(data) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["not a JSON object"]
    checks = {"commands": "command", "callbacks": "data", "replies": "text"}
    for key, field in checks.items():
        for i, item in enumerate(data.get(key) or []):
            if not isinstance(item, dict) or not item.get(field):
                errors.append(f"{key}[{i}]: missing `{field}`")
    for i, kb in enumerate(data.get("keyboards") or []):
        if not isinstance(kb, dict) or not isinstance(kb.get("buttons"), list):
            errors.append(f"keyboards[{i}]: missing `buttons` list")
            continue
        for j, b in enumerate(kb["buttons"]):
            if not isinstance(b, dict) or not b.get("text"):
                errors.append(f"keyboards[{i}].buttons[{j}]: missing `text`")
    return errors


def llm_mine(source_dir: Path, scan_root: Path) -> dict:
    """Model-read map for any language. One retry on malformed output."""
    import json as _json

    from tclib import call_claude

    files = candidate_files(scan_root)
    if not files:
        raise SystemExit(f"no files under {scan_root} mention Bot API tokens "
                         f"({', '.join(BOT_API_TOKENS[:4])}…) — is this really a bot repo?")
    sections, total = [], 0
    for f, text in files:
        chunk = text[:MAX_FILE_CHARS]
        if total + len(chunk) > MAX_TOTAL_CHARS:
            break
        total += len(chunk)
        sections.append(f"--- {f.relative_to(source_dir)} ---\n{chunk}")
    prompt = LLM_MINE_PROMPT.format(files_section="\n\n".join(sections))

    raw = call_claude(prompt)
    for attempt in range(2):
        try:
            data = _json.loads(raw)
            errors = validate_llm_map(data)
        except _json.JSONDecodeError as e:
            data, errors = None, [f"JSON parse error: {e}"]
        if not errors:
            break
        if attempt == 0:
            raw = call_claude(prompt + "\n\nYOUR PREVIOUS OUTPUT WAS REJECTED:\n- "
                              + "\n- ".join(errors)
                              + "\nOutput the FULL corrected JSON object (no fences):")
    else:
        raise SystemExit("LLM mining failed twice: " + "; ".join(errors))

    out = {"framework": "llm", "source_dir": str(source_dir), "mined_by": "llm",
           "commands": data.get("commands") or [], "callbacks": data.get("callbacks") or [],
           "keyboards": data.get("keyboards") or [], "replies": data.get("replies") or []}
    for c in out["commands"]:
        if not str(c["command"]).startswith("/"):
            c["command"] = "/" + str(c["command"])
    return out


def merge_maps(det: dict, llm: dict) -> dict:
    """--llm-augment: deterministic map stays authoritative (map-level mined_by keeps
    the strict probe), LLM entries fill the gaps and are marked per-entry."""
    out = dict(det)
    out["mined_by"] = "deterministic+llm"
    known_cmds = {c["command"] for c in det["commands"]}
    known_data = {c["data"] for c in det["callbacks"]}
    known_replies = {r["text"] for r in det["replies"]}
    kb_keys = {tuple(b["text"] for b in kb["buttons"]) for kb in det["keyboards"]}
    for c in llm["commands"]:
        if c["command"] not in known_cmds:
            out["commands"].append({**c, "mined_by": "llm"})
    for c in llm["callbacks"]:
        if c["data"] not in known_data:
            out["callbacks"].append({**c, "mined_by": "llm"})
    for r in llm["replies"]:
        if r["text"] not in known_replies:
            out["replies"].append({**r, "mined_by": "llm"})
    for kb in llm["keyboards"]:
        if tuple(b.get("text") for b in kb["buttons"]) not in kb_keys:
            out["keyboards"].append({**kb, "mined_by": "llm"})
    return out


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


def mine(source_dir: Path, framework: str | None = None, mode: str | None = None) -> dict:
    """mode: None (auto: deterministic for known frameworks, LLM otherwise),
    "llm" (force LLM), "augment" (deterministic + LLM supplement)."""
    fw, fw_dir = (framework, source_dir) if framework else detect_framework(source_dir)
    scan_root = fw_dir or source_dir

    if mode == "llm" or (fw is None and mode != "augment"):
        if fw is None and mode != "llm":
            print(f"[mine] no known framework under {source_dir} — falling back to LLM mining",
                  file=sys.stderr)
        out = llm_mine(source_dir, scan_root)
        return _finalize(out)
    if fw is None:
        raise SystemExit(f"--llm-augment needs a detectable framework under {source_dir}; "
                         "use --llm for framework-less repos")

    out: dict = {"framework": fw, "source_dir": str(source_dir), "mined_by": "deterministic",
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

    if mode == "augment":
        out = merge_maps(out, llm_mine(source_dir, scan_root))
    return _finalize(out)


def _finalize(out: dict) -> dict:
    out["commands"] = dedupe(out["commands"], "command")
    out["callbacks"] = dedupe(out["callbacks"], "data")
    out["replies"] = dedupe(out["replies"], "text")
    out["miniapp"] = any(b.get("web_app") for kb in out["keyboards"] for b in kb["buttons"])
    return out


def render_context_section(bot_map: dict) -> str:
    """Markdown section for bot.context.md — what generation agents actually read."""
    lines = ["## Mined from source", ""]
    if bot_map.get("mined_by") == "llm":
        lines.append(f"Framework: `{bot_map['framework']}` — mined by an LLM reading the "
                     "source: verify captions against the live bot before trusting exact matches.")
    else:
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
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--llm", action="store_true",
                      help="force LLM mining (any language/framework)")
    mode.add_argument("--llm-augment", action="store_true",
                      help="deterministic pass + LLM supplement (i18n tables, constants)")
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

    bot_map = mine(src_path, args.framework,
                   mode="llm" if args.llm else "augment" if args.llm_augment else None)

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
