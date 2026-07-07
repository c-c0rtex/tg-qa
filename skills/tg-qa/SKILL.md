---
name: tg-qa
description: Autonomous Telegram-bot QA — Telethon-driven E2E dialogs (send/click/expect), reply & keyboard snapshot testing, scenarios and specs generated from the bot's source code, role matrix over multiple sessions, Mini App handoff to web-qa. Use when the user asks to test a Telegram bot, verify bot flows, or run bot regressions.
---

# tg-qa — Autonomous Telegram Bot QA

## Locating the tooling

All scripts below live under the plugin root: call them as
`${CLAUDE_PLUGIN_ROOT}/bin/<script>`. When `${CLAUDE_PLUGIN_ROOT}` is unset
(classic git-clone install into `~/.claude/skills/tg-qa`), use the repository root —
the directory two levels above this file.

First run needs no manual setup beyond [uv](https://docs.astral.sh/uv/): every bin
wrapper is `uv run --project <root>`-based, and `uv run` creates the venv and installs
pinned dependencies automatically on first invocation. No browsers, no compilers.

Pipeline (mirrors web-qa; executor is a Telegram **user session**, not a browser):

1. **Explore** — mine the bot's source code (`bot_mine`, commands/handlers/keyboards/reply
   texts) into `.tg-qa/bot.map.json`. LLM mining runs for EVERY framework/language by
   default; node-telegram-bot-api/aiogram additionally get a deterministic `file:line`
   pass merged underneath (`--no-llm` = deterministic only, zero tokens). Verify the map
   against the live bot via the MCP tools (`tg_bot_commands`, `tg_send /start`).
2. **Generate** — write test-case scenarios (`.tg-qa/scenarios/*.md`) from the bot map +
   the user's task or git diff. Each TC declares `**Type:** passive|mutating`.
3. **Automate** — generate declarative YAML specs (`.tg-qa/specs/*.yaml`); validate the
   schema and probe that referenced commands/buttons exist on the live bot.
4. **Run** — zero-token execution over `core/driver.py`: send / click / expect
   (reply_within, text snapshot with `text_masks`, keyboard snapshot, media type),
   report to `.tg-qa/reports/`.
5. **Maintain** — heal failing specs; the fix prompt includes the actual failed dialog.

## Tooling

- `${CLAUDE_PLUGIN_ROOT}/bin/tg-qa-register-project <alias> --bot @bot --api-id … --api-hash …` — registry entry
  (secrets: `~/.config/tg-qa/projects.json`) + `.tg-qa/` skeleton in the project.
- `${CLAUDE_PLUGIN_ROOT}/bin/tg-qa-login --session <name> [--qr]` — create/verify a session (interactive; tell
  the user to run it with `! ${CLAUDE_PLUGIN_ROOT}/bin/tg-qa-login …`, never run it yourself). Existing
  `.session` files are used as-is via the registry.
- `${CLAUDE_PLUGIN_ROOT}/bin/tg-qa-mine --project <alias>` — bot map from source → `.tg-qa/bot.map.json`.
- `${CLAUDE_PLUGIN_ROOT}/bin/tg-qa-generate --project <alias> --task "…"|--diff <ref>` — scenario TCs from the
  map; review the md with the user before automating.
- `${CLAUDE_PLUGIN_ROOT}/bin/tg-qa-spec-gen --project <alias> --scenario <file.md>` — YAML specs, validated and
  probed against the map (`--no-probe`, `--tc`, `--force`).
- `${CLAUDE_PLUGIN_ROOT}/bin/tg-qa-run --project <alias>` — zero-token run; `--passive-only` (gate mutating),
  `--update-baseline`, `--junit`, `--role`, `--no-fixtures`, `--specs <glob>`.
- `${CLAUDE_PLUGIN_ROOT}/bin/tg-qa-maintain --project <alias> [--dry-run]` — heal failing specs from the last
  run; a `PRODUCT-BUG:` verdict means the bot (not the spec) is wrong — report it, never
  weaken assertions.
- `${CLAUDE_PLUGIN_ROOT}/bin/tg-qa-miniapp --project <alias> [--url <u>] [--emit script|json|url]`
  — resolve the bot's Mini App with LIVE signed initData and emit the browser shim; feed
  the full `webview_url` to web-qa's Playwright (the SDK reads initData from the URL hash),
  or use `--emit script` as web-qa's `auth_init_data_cmd`.
- `${CLAUDE_PLUGIN_ROOT}/bin/tg-qa-mcp --project <alias>` — MCP server, stdio; `--http --token …` for VPS
  daemon mode (see `deploy/`).
- MCP tools (all accept optional `role`): `tg_me`, `tg_send`, `tg_click`, `tg_history`,
  `tg_send_media` (photo/voice/video_note/sticker/…), `tg_download_media`,
  `tg_bot_commands`, `tg_webview_url`.

## Media & Mini Apps

- Send any media in a spec step or via `tg_send_media`: `photo | document | voice (гс) |
  video_note (кружок) | sticker | video | audio | gif | auto`. The caller supplies a file
  fitting the kind (voice→.ogg, video_note→square .mp4, sticker→.webp/.tgs).
- Mini App testing is web-qa's job; tg-qa is the bridge. `tg-qa-miniapp` fetches a fresh,
  correctly-signed initData (short-lived — always resolve per run, never store) and the
  SDK initialises natively when Playwright loads the full `webview_url`. The emitted
  `init_script` is a shim for apps that read `window.Telegram.WebApp` directly — it renders
  MainButton/BackButton as real DOM buttons (`data-testid="tg-main-button"` /
  `"tg-back-button"`) so a headless browser can drive the confirm/pay flow, and records
  requested payments on `window.__tgInvoices`. Apps built on `@twa-dev/sdk` / the official
  `telegram-web-app.js` use the SDK's own native buttons (not DOM) — for those the
  purchase click needs the real client; assert the flow up to invoice creation instead.

## Rules

- Never commit or read out `.session` files, api_id/api_hash, phone numbers. Secrets live
  only in the registry.
- Test dialogs run on the tester's REAL account: the driver mutes + marks-read the bot
  dialog by default, and `create_group` does the same for any test chat it creates
  (auto_mute/auto_read config switches, false = sound / unread back). Never leave a
  test chat un-muted.
- One `.session` file = one live client. If the MCP server holds the session, point
  runners at a copy (a second `.session` path with the same auth key) so they don't
  collide on the SQLite lock.
- User accounts are throttled hard (FloodWait): keep `send_delay ≥ 1s`, one session = one
  dialog at a time, no parallel sends from the same role.
- Mutating flows (payments, data wipes) — only against test stands, never a production
  bot, and only when the TC explicitly declares them.
