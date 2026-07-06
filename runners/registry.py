"""Registry (projects.json) + session resolution — stdlib only, importable from bin/.

The registry holds everything that must NEVER live in a project repo: api_id/api_hash
and per-role session references. A project's own `.tg-qa/config.json` holds only
non-secret keys (bot username, source dir, timeouts, masks).

Registry resolution order (same scheme as web-qa):
  1. TGQA_REGISTRY env var (tests, CI, power users)
  2. <skill root>/projects.json — classic `git clone` install
  3. $XDG_CONFIG_HOME/tg-qa/projects.json (~/.config by default) — stable home for
     plugin installs: the plugin cache dir changes on every update.

Session resolution — both ways of bringing your own session are first-class:
  - a bare name  ("main")            → $XDG_CONFIG_HOME/tg-qa/sessions/main.session
  - an absolute/relative path        → used as-is (drop an EXISTING .session anywhere
    and point the role at it; ~ expands)
Sessions are created by bin/tg-qa-login (phone+code or QR) — or reused from any
other Telethon-based tool.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent


def _xdg_base() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "tg-qa"


def sessions_dir() -> Path:
    return _xdg_base() / "sessions"


def registry_path() -> Path:
    """Registry to READ. Falls back to the skill-root path when nothing exists yet,
    so error messages point at the default location."""
    env = os.environ.get("TGQA_REGISTRY")
    if env:
        return Path(env)
    local = SKILL_ROOT / "projects.json"
    if local.is_file():
        return local
    xdg = _xdg_base() / "projects.json"
    if xdg.is_file():
        return xdg
    return local


def registry_write_path() -> Path:
    """Registry to WRITE. An existing registry always wins; when creating fresh,
    a plugin install (version-scoped cache dir) gets the XDG path."""
    env = os.environ.get("TGQA_REGISTRY")
    if env:
        return Path(env)
    local = SKILL_ROOT / "projects.json"
    if local.is_file():
        return local
    xdg = _xdg_base() / "projects.json"
    if xdg.is_file():
        return xdg
    if Path.home() / ".claude" / "plugins" in SKILL_ROOT.parents:
        return xdg
    return local


def load_registry() -> list[dict]:
    path = registry_path()
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def find_project(alias_or_path: str | None = None) -> dict:
    """Project entry by alias, by path prefix of cwd, or the single entry."""
    entries = load_registry()
    if not entries:
        raise SystemExit(f"Registry not found or empty: {registry_path()} — "
                         "run bin/tg-qa-register-project first")
    if alias_or_path:
        for e in entries:
            if e.get("alias") == alias_or_path or e.get("path") == alias_or_path:
                return e
        raise SystemExit(f"Project '{alias_or_path}' not in registry {registry_path()}")
    cwd = Path.cwd().resolve()
    for e in entries:
        p = e.get("path") or ""
        if p and p != "*" and (cwd == Path(p).resolve() or Path(p).resolve() in cwd.parents):
            return e
    real = [e for e in entries if e.get("path") not in (None, "*")]
    if len(real) == 1:
        return real[0]
    raise SystemExit("Cannot infer project from cwd — pass --project <alias>")


def resolve_session(proj: dict, role: str | None = None) -> Path:
    """Session file for a role (or the project's first/default role)."""
    roles = proj.get("roles") or []
    if not roles:
        raise SystemExit(f"Project '{proj.get('alias')}' has no roles in the registry — "
                         "add roles: [{name, session}] (session = name in "
                         f"{sessions_dir()} or a path to an existing .session)")
    entry = None
    if role:
        entry = next((r for r in roles if r.get("name") == role), None)
        if entry is None:
            raise SystemExit(f"Role '{role}' not found; known: {[r.get('name') for r in roles]}")
    else:
        entry = next((r for r in roles if r.get("default")), roles[0])
    return session_path(entry.get("session") or entry.get("name"))


def session_path(ref: str) -> Path:
    """Bare name → sessions_dir()/<name>.session; anything with a separator or
    .session suffix → literal path (bring-your-own-session)."""
    ref = os.path.expanduser(ref)
    if "/" in ref or ref.endswith(".session"):
        p = Path(ref)
        return p if p.suffix == ".session" else p.with_suffix(".session")
    return sessions_dir() / f"{ref}.session"


def resolve_api(proj: dict) -> tuple[int, str]:
    """api_id/api_hash: env wins (CI), registry entry otherwise. The user creates
    these at https://my.telegram.org — tg-qa never ships or stores them elsewhere."""
    api_id = os.environ.get("TGQA_API_ID") or proj.get("api_id")
    api_hash = os.environ.get("TGQA_API_HASH") or proj.get("api_hash")
    if not api_id or not api_hash:
        raise SystemExit("api_id/api_hash missing — create an app at https://my.telegram.org "
                         "(API development tools) and put them in the registry entry "
                         f"({registry_path()}) or export TGQA_API_ID/TGQA_API_HASH")
    return int(api_id), str(api_hash)


def load_config(proj: dict) -> dict:
    """Project's non-secret config: <path>/.tg-qa/config.json (may not exist yet)."""
    p = proj.get("path")
    if not p or p == "*":
        return {}
    f = Path(p) / ".tg-qa" / "config.json"
    if not f.is_file():
        return {}
    return json.loads(f.read_text(encoding="utf-8"))
