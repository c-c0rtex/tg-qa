# tg-qa

Autonomous Telegram-bot QA for [Claude Code](https://claude.com/claude-code): drive real
dialogs with a bot through a user account (Telethon/MTProto), snapshot-test replies and
inline keyboards, generate scenarios and specs from the bot's **source code**, and run
the whole matrix without spending LLM tokens. Sister project of
[web-qa](https://github.com/c-c0rtex/web-qa) — same pipeline, different executor.

> Status: v0.1 — the full pipeline (mine → generate → spec-gen → run → maintain), the
> MCP server, session tooling and the Mini App bridge are implemented and dogfooded on
> two live demo bots (see below).

## Demos

Want to see it run before installing? Two worked examples, each with a pinned upstream bot,
committed test state and real reports:

- **[tg-qa-demo](https://github.com/c-c0rtex/tg-qa-demo)** — [Telebook](https://github.com/neSpecc/telebook)
  (node-telegram-bot-api): a **Mini App** bot with **Telegram Stars** payments. Shows the
  initData bridge running the real Vue app in headless chromium, and bot-dialog snapshots
  (7/7).
- **[tg-qa-feedback-demo](https://github.com/c-c0rtex/tg-qa-feedback-demo)** — MasterGroosha's
  feedback bot (aiogram 3, Fluent i18n): a **bot-centric** demo exercising **media, stickers
  and voice** — voice/photo confirmed, sticker/video-note rejected as unsupported (6/6). This
  bot drove the aiogram source-miner and i18n-locale mining features.

## How it works

```
bot source code ──bot_mine──▶ bot.context.md ──generate──▶ scenarios (TC .md)
                                                              │ automate (LLM → YAML spec, live probe)
user session (.session) ◀──core/driver.py──▶ Telegram ◀──run── specs (zero tokens)
                                                              │ maintain (heal from actual dialog)
```

- **Source of truth is the bot's code**: commands, callback handlers, keyboards and reply
  texts are mined from the repo, so expected results come from real strings, not model
  guesses. **LLM mining runs for every framework**: files are shortlisted by Bot API
  protocol tokens (`sendMessage`, `inline_keyboard`… are the same strings in Go, Rust
  or PHP) and the model fills the map schema quoting strings verbatim. Known frameworks
  (node-telegram-bot-api, aiogram) additionally get a deterministic pass with
  `file:line` sources merged underneath (`--no-llm` keeps only that, zero tokens).
  The live bot is used to *verify* the map, not to invent it.
- **Specs are declarative YAML**, executed by a deterministic runner over the Telethon
  driver: send / click / expect (reply within timeout, text snapshot with masks,
  keyboard snapshot, media type). No LLM in the loop at run time.
- **A user account, not a bot token**: tests see exactly what a person sees — inline
  keyboards, in-place edits, media. Clicks detect both new replies and edits.
- **Mini Apps**: the bot's web app is tested by web-qa; tg-qa resolves the webview URL
  with live, correctly-signed `tgWebAppData` (initData) and hands it over, so the real app
  runs in a headless browser under a genuine Telegram identity.

## Setup

1. **API credentials — yours, created by you.** Go to [my.telegram.org](https://my.telegram.org)
   → API development tools → Create application; copy `api_id` and `api_hash`.
   tg-qa never stores them anywhere except your local registry / env
   (`TGQA_API_ID` / `TGQA_API_HASH`).

2. **Register the project:**
   ```bash
   bin/tg-qa-register-project my-bot --bot @my_test_bot --api-id 123 --api-hash abc \
       --path /path/to/bot/repo
   ```
   Secrets live in the registry (`~/.config/tg-qa/projects.json`), never in the repo;
   the project gets a non-secret `.tg-qa/config.json` skeleton.

3. **Session — create one or bring your own:**
   ```bash
   bin/tg-qa-login --session main --project my-bot        # phone + code (+2FA)
   bin/tg-qa-login --session main --project my-bot --qr   # or scan a QR code
   ```
   Already have a Telethon `.session`? Drop it into `~/.config/tg-qa/sessions/` or point
   the registry role at its path (`roles: [{"name": "default", "session": "/path/to/it.session"}]`) —
   no login needed. Extra roles (admin / free user / …) are just extra sessions.

   ⚠️ A `.session` file is full access to that Telegram account. It is chmod 600 and
   git-ignored; revoke a leaked one via Telegram → Settings → Devices.

## MCP server — local or VPS

One server, your choice where it runs (the session file lives wherever the server runs):

| Mode | Config | When |
|---|---|---|
| **Local** (stdio) | `deploy/mcp.local.json` | default; zero infrastructure |
| **VPS via SSH** (stdio) | `deploy/mcp.vps-ssh.json` | stable IP, session off your laptop; no open ports — SSH is the auth |
| **VPS via HTTP** | `deploy/mcp.vps-http.json` + `deploy/tg-qa-mcp.service` | shared/team use; bearer token **required** |

Tools: `tg_me`, `tg_send`, `tg_click`, `tg_history`, `tg_send_file`, `tg_download_media`,
`tg_bot_commands`, `tg_webview_url` — every tool takes an optional `role` to act as a
different registered session.

## Development

```bash
uv sync --all-groups
uv run pytest -q        # unit tests, no Telegram needed
./ci-local.sh           # ruff + tests, mirrors CI
```

## License

MIT — see [LICENSE](LICENSE). If tg-qa saves you time, a link back to
[c-c0rtex](https://github.com/c-c0rtex) is appreciated.
