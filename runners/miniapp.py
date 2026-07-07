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
    version/platform/colorScheme, viewport, themeParams.

    Crucially, MainButton and BackButton are backed by REAL DOM buttons
    (data-testid="tg-main-button" / "tg-back-button"): in the real client these are
    native chrome outside the page, so a headless browser could never click them and
    the purchase/confirm flow was undrivable. Rendering them as DOM elements wired to
    the app's own onClick handlers makes the whole flow clickable by web-qa.
    openInvoice records the last invoice URL on window.__tgInvoices so a test can
    assert the payment was requested (real payment still happens in Telegram)."""
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
        "  function domButton(testid, label) {\n"
        "    var handlers = [];\n"
        "    var el = null;\n"
        "    function ensure() {\n"
        "      if (el || !document.body) return;\n"
        "      el = document.createElement('button');\n"
        "      el.setAttribute('data-testid', testid);\n"
        "      el.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99999;'\n"
        "        + 'padding:14px;border:0;font-size:16px;display:none';\n"
        "      el.addEventListener('click', function () {\n"
        "        handlers.slice().forEach(function (h) { try { h(); } catch (e) {} });\n"
        "      });\n"
        "      document.body.appendChild(el);\n"
        "    }\n"
        "    if (document.readyState !== 'loading') ensure();\n"
        "    else document.addEventListener('DOMContentLoaded', ensure);\n"
        "    var api = { isVisible: false, isActive: true, text: label || '',\n"
        "      show: function () { ensure(); if (el) { el.style.display = 'block'; } this.isVisible = true; return this; },\n"
        "      hide: function () { if (el) { el.style.display = 'none'; } this.isVisible = false; return this; },\n"
        "      setText: function (t) { this.text = t; ensure(); if (el) el.textContent = t; return this; },\n"
        "      setParams: function (p) { if (p && p.text) this.setText(p.text); return this; },\n"
        "      onClick: function (cb) { handlers.push(cb); return this; },\n"
        "      offClick: function (cb) { handlers = handlers.filter(function (h) { return h !== cb; }); return this; },\n"
        "      enable: function () { this.isActive = true; return this; },\n"
        "      disable: function () { this.isActive = false; return this; },\n"
        "      showProgress: noop, hideProgress: noop };\n"
        "    return api;\n"
        "  }\n"
        "  window.__tgInvoices = [];\n"
        "  var WebApp = Object.assign({}, d, {\n"
        "    ready: noop, expand: noop, close: noop,\n"
        "    isExpanded: true, isClosingConfirmationEnabled: false,\n"
        "    MainButton: domButton('tg-main-button', 'CONTINUE'),\n"
        "    BackButton: domButton('tg-back-button', 'BACK'),\n"
        "    HapticFeedback: { impactOccurred: noop, notificationOccurred: noop,\n"
        "      selectionChanged: noop },\n"
        "    onEvent: noop, offEvent: noop, sendData: noop, switchInlineQuery: noop,\n"
        "    openLink: noop, openTelegramLink: noop,\n"
        "    openInvoice: function (url, cb) { window.__tgInvoices.push(url);\n"
        "      if (cb) cb('paid'); },\n"
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
