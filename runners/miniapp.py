"""Mini App bridge — resolve a bot's Web App with LIVE initData and emit the shim that
lets a plain browser (web-qa's Playwright) run it as if launched from Telegram.

A Telegram Mini App reads its identity from `window.Telegram.WebApp` (the @twa-dev/sdk
and Telegram's own telegram-web-app.js both do). Opened in a bare browser that object
is absent, the SDK reports `platform: 'unknown'`, and the app falls back to a degraded
no-Telegram mode. To exercise the REAL app we:

  1. ask Telegram (via the user session) for the Web App URL carrying a fresh,
     correctly-SIGNED `tgWebAppData` (initData) — RequestWebViewRequest;
  2. build an init script that installs a minimal `window.Telegram.WebApp` populated
     from that initData BEFORE any app script runs (Playwright add_init_script /
     web-qa's `auth_init_data_cmd`).

initData is short-lived and signed for one user+bot+time — hence a COMMAND that emits it
fresh per run, never a stored value. This module is the tg-qa half; web-qa consumes the
`init_script` to drive the app (crawl, visual, a11y) unchanged.

Usage:
  miniapp.py --project <alias> [--url <webapp url>] [--emit script|json|url]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from driver import BotDriver  # noqa: E402
from registry import find_project, load_config, resolve_api, resolve_session  # noqa: E402


def parse_init_data(init_data: str) -> dict:
    """tgWebAppData query string → the `initDataUnsafe` object the SDK exposes
    (user/receiver/chat are JSON-encoded fields; everything else stays a string)."""
    unsafe: dict = {}
    for k, v in urllib.parse.parse_qsl(init_data, keep_blank_values=True):
        if k in ("user", "receiver", "chat"):
            try:
                unsafe[k] = json.loads(v)
            except json.JSONDecodeError:
                unsafe[k] = v
        else:
            unsafe[k] = v
    return unsafe


def extract_init_data(webview_url: str) -> str | None:
    """Pull the raw `tgWebAppData` value out of the resolved webview URL fragment."""
    frag = urllib.parse.urlparse(webview_url).fragment
    params = dict(urllib.parse.parse_qsl(frag))
    return params.get("tgWebAppData")


def build_init_script(init_data: str, unsafe: dict, version: str = "7.0",
                      platform: str = "web") -> str:
    """JS installed before app scripts run: a minimal but SDK-satisfying
    window.Telegram.WebApp. Covers what real Mini Apps read at startup — initData(Unsafe),
    version/platform/colorScheme, viewport, themeParams, and no-op stubs for the UI
    surface (MainButton/BackButton/HapticFeedback) so the app initialises instead of
    throwing on a missing method."""
    payload = {
        "initData": init_data,
        "initDataUnsafe": unsafe,
        "version": version,
        "platform": platform,
        "colorScheme": "light",
        "themeParams": {"bg_color": "#ffffff", "text_color": "#000000",
                        "button_color": "#3390ec", "button_text_color": "#ffffff"},
        "viewportHeight": 720,
        "viewportStableHeight": 720,
    }
    return (
        "(function () {\n"
        f"  var d = {json.dumps(payload, ensure_ascii=False)};\n"
        "  var noop = function () {};\n"
        "  var button = function (extra) {\n"
        "    return Object.assign({ isVisible: false, isActive: true, text: '',\n"
        "      show: noop, hide: noop, enable: noop, disable: noop,\n"
        "      onClick: noop, offClick: noop, setText: noop, setParams: noop,\n"
        "      showProgress: noop, hideProgress: noop }, extra || {});\n"
        "  };\n"
        "  var WebApp = Object.assign({}, d, {\n"
        "    ready: noop, expand: noop, close: noop,\n"
        "    isExpanded: true, isClosingConfirmationEnabled: false,\n"
        "    MainButton: button(), BackButton: button(),\n"
        "    HapticFeedback: { impactOccurred: noop, notificationOccurred: noop,\n"
        "      selectionChanged: noop },\n"
        "    onEvent: noop, offEvent: noop, sendData: noop, switchInlineQuery: noop,\n"
        "    openLink: noop, openTelegramLink: noop, openInvoice: noop,\n"
        "    showAlert: function (m, cb) { if (cb) cb(); },\n"
        "    showConfirm: function (m, cb) { if (cb) cb(true); },\n"
        "    showPopup: function (p, cb) { if (cb) cb(); },\n"
        "    setHeaderColor: noop, setBackgroundColor: noop,\n"
        "    enableClosingConfirmation: noop, disableClosingConfirmation: noop,\n"
        "    requestViewport: noop\n"
        "  });\n"
        "  window.Telegram = { WebApp: WebApp };\n"
        "})();\n"
    )


async def resolve(proj: dict, cfg: dict, url: str | None) -> dict:
    api_id, api_hash = resolve_api(proj)
    bot = cfg.get("bot") or proj.get("bot")
    d = BotDriver(resolve_session(proj), api_id, api_hash, bot,
                  send_delay=float(cfg.get("send_delay") or 1.0))
    await d.connect()
    try:
        webapp_url = url or cfg.get("miniapp_url")
        if not webapp_url:
            raise SystemExit("no Mini App URL — pass --url or set miniapp_url in config")
        resolved = await d.webview_url(webapp_url)
    finally:
        await d.close()
    init_data = extract_init_data(resolved)
    if not init_data:
        raise SystemExit(f"resolved webview URL carried no tgWebAppData: {resolved}")
    unsafe = parse_init_data(init_data)
    return {"base_url": resolved.split("#")[0], "webview_url": resolved,
            "init_data": init_data, "init_data_unsafe": unsafe,
            "init_script": build_init_script(init_data, unsafe,
                                             platform=cfg.get("miniapp_platform") or "web")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", help="registry alias")
    ap.add_argument("--url", help="Mini App URL (overrides config miniapp_url)")
    ap.add_argument("--emit", choices=["script", "json", "url"], default="json",
                    help="script = just the init script (for auth_init_data_cmd); "
                         "url = the base URL; json = everything")
    args = ap.parse_args()

    proj = find_project(args.project)
    cfg = load_config(proj)
    out = asyncio.run(resolve(proj, cfg, args.url))

    if args.emit == "script":
        print(out["init_script"])
    elif args.emit == "url":
        print(out["base_url"])
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
