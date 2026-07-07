"""tg-qa MCP server — agent-facing Telegram tools on top of core/driver.py.

Two deployment modes, same server:

  LOCAL (default, stdio) — Claude Code launches the process itself:
      {"command": "uv", "args": ["run", "--project", "<skill>", "python",
                                 "<skill>/mcp/server.py", "--project", "telebook"]}
    The session file stays on this machine.

  VPS — two options:
    a) stdio over SSH (recommended: no open ports, no tokens — SSH is the auth):
      {"command": "ssh", "args": ["my-vps", "uv", "run", "--project",
                                  "/opt/tg-qa", "python", "/opt/tg-qa/mcp/server.py",
                                  "--project", "telebook"]}
    b) streamable HTTP with a bearer token (systemd unit in deploy/):
      server.py --project telebook --http --host 0.0.0.0 --port 8976 --token $TOKEN
      client: {"type": "http", "url": "http://vps:8976/mcp",
               "headers": {"Authorization": "Bearer $TOKEN"}}

The server is per-PROJECT (one bot under test); the `role` argument on each tool
switches the acting session (registry roles). Drivers connect lazily and are kept
alive per role.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runners"))
sys.path.insert(0, str(ROOT / "core"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from driver import BotDriver, DriverError  # noqa: E402
from registry import find_project, load_config, resolve_api, resolve_session  # noqa: E402

app = FastMCP("tg-qa")

_proj: dict = {}
_cfg: dict = {}
_drivers: dict[str, BotDriver] = {}


async def _driver(role: str | None) -> BotDriver:
    key = role or "__default__"
    d = _drivers.get(key)
    if d is not None:
        return d
    api_id, api_hash = resolve_api(_proj)
    bot = _cfg.get("bot") or _proj.get("bot")
    if not bot:
        raise DriverError("bot username missing — set it in the registry entry or .tg-qa/config.json")
    d = BotDriver(resolve_session(_proj, role), api_id, api_hash, bot,
                  send_delay=float(_cfg.get("send_delay") or 1.0),
                  auto_mute=_cfg.get("auto_mute") is not False,
                  auto_read=_cfg.get("auto_read") is not False)
    await d.connect()
    _drivers[key] = d
    return d


@app.tool()
async def tg_me(role: str | None = None) -> dict:
    """Who the acting Telegram session is logged in as (sanity/auth check)."""
    return await (await _driver(role)).me()


@app.tool()
async def tg_send(text: str, wait: float = 10, role: str | None = None) -> dict:
    """Send a text message to the bot under test and return its replies
    (each reply: id, text, buttons, media_type)."""
    return await (await _driver(role)).send(text, wait=wait)


@app.tool()
async def tg_click(msg_id: int, button: str, wait: float = 10, role: str | None = None) -> dict:
    """Click an inline button by message id + caption. Returns new replies AND
    the in-place edit of the clicked message if the bot updated it."""
    return await (await _driver(role)).click(msg_id, button, wait=wait)


@app.tool()
async def tg_history(limit: int = 20, role: str | None = None) -> list[dict]:
    """Recent dialog with the bot, oldest first — what the user actually sees."""
    return await (await _driver(role)).history(limit=limit)


@app.tool()
async def tg_send_file(path: str, caption: str = "", voice: bool = False,
                       wait: float = 15, role: str | None = None) -> dict:
    """Send a file (photo/document, or .ogg as a voice note with voice=true)."""
    return await (await _driver(role)).send_file(path, caption=caption, voice=voice, wait=wait)


@app.tool()
async def tg_download_media(msg_id: int, out_dir: str = "/tmp", role: str | None = None) -> dict:
    """Download media from a bot message; returns the saved path (or null)."""
    saved = await (await _driver(role)).download_media(msg_id, out_dir)
    return {"path": saved}


@app.tool()
async def tg_bot_commands(role: str | None = None) -> list[dict]:
    """The bot's declared command menu (BotFather/setMyCommands) — a free seed
    for the bot map."""
    return await (await _driver(role)).bot_commands()


@app.tool()
async def tg_webview_url(url: str, role: str | None = None) -> dict:
    """Resolve a Mini App URL with LIVE tgWebAppData (initData) for the web-qa
    bridge. Experimental."""
    return {"url": await (await _driver(role)).webview_url(url)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", help="registry alias (default: inferred from cwd)")
    ap.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio (VPS mode)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8976)
    ap.add_argument("--token", default=os.environ.get("TGQA_MCP_TOKEN"),
                    help="bearer token required on every HTTP request (env TGQA_MCP_TOKEN)")
    args = ap.parse_args()

    global _proj, _cfg
    _proj = find_project(args.project)
    _cfg = load_config(_proj)

    if not args.http:
        app.run(transport="stdio")
        return 0

    if not args.token:
        print("HTTP mode without --token/TGQA_MCP_TOKEN is refused: the session file "
              "behind this server IS your Telegram account.", file=sys.stderr)
        return 64

    import uvicorn

    app.settings.host = args.host
    app.settings.port = args.port
    asgi = app.streamable_http_app()

    class BearerGate:
        def __init__(self, inner, token: str):
            self.inner, self.token = inner, token

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode()
                if auth != f"Bearer {self.token}":
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"text/plain")]})
                    await send({"type": "http.response.body", "body": b"unauthorized"})
                    return
            await self.inner(scope, receive, send)

    uvicorn.run(BearerGate(asgi, args.token), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
