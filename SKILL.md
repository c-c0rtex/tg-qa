---
name: tg-qa
description: Autonomous Telegram-bot QA — Telethon-driven E2E dialogs (send/click/expect), reply & keyboard snapshot testing, scenarios and specs generated from the bot's source code, role matrix over multiple sessions, Mini App handoff to web-qa. Use when the user asks to test a Telegram bot, verify bot flows, or run bot regressions.
---

# tg-qa — Autonomous Telegram Bot QA

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

- `bin/tg-qa-register-project <alias> --bot @bot --api-id … --api-hash …` — registry entry
  (secrets: `~/.config/tg-qa/projects.json`) + `.tg-qa/` skeleton in the project.
- `bin/tg-qa-login --session <name> [--qr]` — create/verify a session (interactive; tell
  the user to run it with `! bin/tg-qa-login …`, never run it yourself). Existing
  `.session` files are used as-is via the registry.
- `bin/tg-qa-mine --project <alias>` — bot map from source → `.tg-qa/bot.map.json`.
- `bin/tg-qa-generate --project <alias> --task "…"|--diff <ref>` — scenario TCs from the
  map; review the md with the user before automating.
- `bin/tg-qa-spec-gen --project <alias> --scenario <file.md>` — YAML specs, validated and
  probed against the map (`--no-probe`, `--tc`, `--force`).
- `bin/tg-qa-run --project <alias>` — zero-token run; `--passive-only` (gate mutating),
  `--update-baseline`, `--junit`, `--role`, `--no-fixtures`, `--specs <glob>`.
- `bin/tg-qa-maintain --project <alias> [--dry-run]` — heal failing specs from the last
  run; a `PRODUCT-BUG:` verdict means the bot (not the spec) is wrong — report it, never
  weaken assertions.
- `bin/tg-qa-mcp --project <alias>` — MCP server, stdio; `--http --token …` for VPS
  daemon mode (see `deploy/`).
- MCP tools (all accept optional `role`): `tg_me`, `tg_send`, `tg_click`, `tg_history`,
  `tg_send_file`, `tg_download_media`, `tg_bot_commands`, `tg_webview_url`.

## Rules

- Never commit or read out `.session` files, api_id/api_hash, phone numbers. Secrets live
  only in the registry.
- User accounts are throttled hard (FloodWait): keep `send_delay ≥ 1s`, one session = one
  dialog at a time, no parallel sends from the same role.
- Mutating flows (payments, data wipes) — only against test stands, never a production
  bot, and only when the TC explicitly declares them.
