"""Telethon core driver — the ONLY place tg-qa talks MTProto.

Both consumers sit on top of this module:
  - mcp/server.py       — agent-facing tools (explore, debug, manual checks)
  - runners/*           — zero-token spec execution

Design notes:
  - A user-account session (not a bot token): tests see exactly what a real user
    sees — inline keyboards, edits, media, service messages.
  - Menu bots answer a button click in TWO ways: a new message OR an in-place edit
    of the clicked message. `click()` watches both; qa_tg_agent's polling missed
    edits and this was the most common false "no reply".
  - User accounts are throttled far harder than bots (FloodWait): every outgoing
    action is spaced by `send_delay`, and FloodWaitError surfaces as a typed error
    with the wait time instead of a stack trace.
  - Formatting helpers are pure functions over duck-typed objects so unit tests
    don't need Telethon.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path


class DriverError(Exception):
    """Actionable driver failure (auth missing, button not found, flood wait)."""


def format_button(btn) -> dict:
    info = {"text": btn.text}
    # MessageButton.url covers only KeyboardButtonUrl; a Mini App button
    # (KeyboardButtonWebView) hides its url in the raw TL object underneath
    raw = getattr(btn, "button", btn)
    url = getattr(btn, "url", None) or getattr(raw, "url", None)
    if url:
        info["url"] = url
    if type(raw).__name__ == "KeyboardButtonWebView":
        info["web_app"] = True
    if getattr(btn, "data", None):
        info["data"] = btn.data.decode("utf-8", errors="replace")
    return info


def format_buttons(message) -> list[list[dict]]:
    if not getattr(message, "buttons", None):
        return []
    return [[format_button(b) for b in row] for row in message.buttons]


def format_message(msg) -> dict:
    """Message → plain dict: the wire format for MCP results, spec assertions and
    maintain's failure context. Everything downstream depends on these keys."""
    out = {
        "id": msg.id,
        "from": "you" if msg.out else "bot",
        "text": msg.text or "",
        "date": msg.date.isoformat() if getattr(msg, "date", None) else "",
    }
    if getattr(msg, "media", None):
        out["media_type"] = type(msg.media).__name__
    buttons = format_buttons(msg)
    if buttons:
        out["buttons"] = buttons
    return out


def keyboard_texts(formatted: dict) -> list[list[str]]:
    """Button captions only — the shape keyboard snapshots assert against."""
    return [[b["text"] for b in row] for row in formatted.get("buttons", [])]


class BotDriver:
    """One authorized user session driving one bot dialog.

    async with BotDriver(session, api_id, api_hash, bot="@my_bot") as d:
        result = await d.send("/start")
        await d.click(result["replies"][-1]["id"], "Settings")
    """

    def __init__(self, session_path: str | Path, api_id: int, api_hash: str,
                 bot: str, send_delay: float = 1.0, settle: float = 1.2,
                 poll_interval: float = 1.0, auto_mute: bool = True,
                 auto_read: bool = True):
        self.session_path = str(session_path)
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot = bot
        self.send_delay = send_delay
        self.settle = settle
        self.poll_interval = poll_interval
        self.auto_mute = auto_mute
        self.auto_read = auto_read
        self._client = None
        self._entity = None
        self._last_action = 0.0

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def connect(self):
        from telethon import TelegramClient
        self._client = TelegramClient(self.session_path, self.api_id, self.api_hash)
        await self._client.connect()
        if not await self._client.is_user_authorized():
            await self._client.disconnect()
            raise DriverError(
                f"Session '{self.session_path}' is not authorized — run bin/tg-qa-login "
                "(or point the registry at an existing .session file)")
        self._entity = await self._client.get_entity(self.bot)
        await self._apply_notify_settings()

    async def _mute_peer(self, target):
        """Apply the driver's mute policy to any peer (the bot dialog, or a test
        group/chat we create). auto_mute=True → mute forever+silent; False → unmute.
        Best-effort — a notify-settings failure must never block testing."""
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings
        settings = (InputPeerNotifySettings(mute_until=2**31 - 1, silent=True)
                    if self.auto_mute
                    else InputPeerNotifySettings(mute_until=0, silent=False))
        try:
            peer = await self._client.get_input_entity(target)
            await self._client(UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(peer=peer), settings=settings))
        except Exception:
            pass

    async def _apply_notify_settings(self):
        await self._mute_peer(self.bot)

    async def _mark_read(self):
        """Auto-read: clear the unread badge the test traffic creates in the user's
        client. Configurable off (auto_read=False) for watching runs live."""
        if not self.auto_read:
            return
        try:
            await self._client.send_read_acknowledge(self._entity)
        except Exception:
            pass

    async def close(self):
        if self._client:
            await self._client.disconnect()
            self._client = None

    async def me(self) -> dict:
        u = await self._client.get_me()
        return {"id": u.id, "username": u.username, "first_name": u.first_name}

    async def create_group(self, title: str, members: list[str]) -> dict:
        """Create a test group (e.g. an admin chat for a feedback bot) with the given
        members and return {id, chat_id}. Like the bot dialog, a freshly created test
        chat is muted + marked read by default so it doesn't beep the tester's real
        account or pile up an unread badge (auto_mute/auto_read honour the same flags)."""
        from telethon import functions, utils
        await self._client(functions.messages.CreateChatRequest(users=members, title=title))
        entity = None
        async for d in self._client.iter_dialogs(limit=30):
            if d.title == title and d.is_group:
                entity = d.entity
                break
        if entity is None:
            raise DriverError(f"group '{title}' created but not found in dialogs")
        await self._mute_peer(entity)
        if self.auto_read:
            try:
                await self._client.send_read_acknowledge(entity)
            except Exception:
                pass
        return {"id": entity.id, "chat_id": utils.get_peer_id(entity)}

    # -- outgoing actions ----------------------------------------------------

    async def _throttle(self):
        gap = self.send_delay - (time.monotonic() - self._last_action)
        if gap > 0:
            await asyncio.sleep(gap)
        self._last_action = time.monotonic()

    async def _guard_flood(self, coro):
        from telethon.errors import FloodWaitError
        try:
            return await coro
        except FloodWaitError as e:
            raise DriverError(f"FloodWait: Telegram throttled this account for {e.seconds}s — "
                              "increase send_delay / reduce parallelism") from e

    async def send(self, text: str, wait: float = 10) -> dict:
        await self._throttle()
        sent = await self._guard_flood(self._client.send_message(self._entity, text))
        replies = await self._poll_new(sent.id, wait)
        await self._mark_read()
        return {"sent_id": sent.id, "replies": replies}

    # kind → send_file kwargs. "photo"/"sticker"/"video"/"audio"/"gif"/"auto" carry no
    # special flag — Telethon infers the type from the file (a .webp becomes a sticker,
    # an .ogg an audio, etc.); the flagged kinds force a specific presentation.
    _MEDIA_FLAGS = {
        "voice": {"voice_note": True},        # гс — needs an .ogg/opus file
        "video_note": {"video_note": True},   # кружок — needs a SQUARE video
        "document": {"force_document": True},
        "gif": {},
        "sticker": {},
        "video": {},
        "audio": {},
        "photo": {},
        "auto": {},
    }

    async def send_media(self, path: str | Path, kind: str = "auto", caption: str = "",
                         wait: float = 15) -> dict:
        """Send any media kind: photo | document | voice | video_note | sticker | video |
        audio | gif | auto. The caller supplies a file appropriate to the kind (voice→ogg,
        video_note→square mp4, sticker→webp/tgs)."""
        if kind not in self._MEDIA_FLAGS:
            raise DriverError(f"unknown media kind {kind!r}; "
                              f"use one of {sorted(self._MEDIA_FLAGS)}")
        p = Path(path).expanduser()
        if not p.is_file():
            raise DriverError(f"media file not found: {p}")
        await self._throttle()
        sent = await self._guard_flood(self._client.send_file(
            self._entity, str(p), caption=caption or None, **self._MEDIA_FLAGS[kind]))
        replies = await self._poll_new(sent.id, wait)
        await self._mark_read()
        return {"sent_id": sent.id, "kind": kind, "replies": replies}

    async def send_file(self, path: str | Path, caption: str = "", voice: bool = False,
                        wait: float = 15) -> dict:
        """Back-compat thin wrapper over send_media."""
        return await self.send_media(path, kind="voice" if voice else "auto",
                                     caption=caption, wait=wait)

    async def click(self, msg_id: int, button_text: str, wait: float = 10) -> dict:
        msg = await self._client.get_messages(self._entity, ids=msg_id)
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if msg is None:
            raise DriverError(f"Message {msg_id} not found in dialog with {self.bot}")
        target = None
        for row in msg.buttons or []:
            for btn in row:
                if btn.text.strip() == button_text.strip():
                    target = btn
                    break
            if target:
                break
        if target is None:
            raise DriverError(f"Button '{button_text}' not found on message {msg_id}; "
                              f"available: {keyboard_texts(format_message(msg))}")
        latest = await self._client.get_messages(self._entity, limit=1)
        anchor = latest[0].id if latest else msg_id
        before = format_message(msg)

        await self._throttle()
        await self._guard_flood(target.click())

        new, edited = await self._poll_after_click(anchor, msg_id, before, wait)
        await self._mark_read()
        return {"clicked": button_text, "on_message": msg_id,
                "replies": new, "edited": edited}

    async def download_media(self, msg_id: int, out_dir: str | Path) -> str | None:
        msg = await self._client.get_messages(self._entity, ids=msg_id)
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if msg is None or not msg.media:
            return None
        return await self._client.download_media(msg, file=str(out_dir))

    # -- reading -------------------------------------------------------------

    async def history(self, limit: int = 20) -> list[dict]:
        msgs = await self._client.get_messages(self._entity, limit=limit)
        await self._mark_read()
        return [format_message(m) for m in reversed(msgs)]

    async def bot_commands(self) -> list[dict]:
        """Command menu the bot declares via BotFather/setMyCommands — free seed
        for the bot map, no source code needed."""
        from telethon.tl.functions.users import GetFullUserRequest
        full = await self._client(GetFullUserRequest(self._entity))
        info = getattr(full.full_user, "bot_info", None)
        cmds = getattr(info, "commands", None) or []
        return [{"command": "/" + c.command, "description": c.description} for c in cmds]

    async def webview_url(self, url: str, platform: str = "android") -> str:
        """Resolve a Mini App URL WITH live tgWebAppData (initData) — the web-qa
        bridge. Experimental until the bridge lands."""
        from telethon.tl.functions.messages import RequestWebViewRequest
        res = await self._client(RequestWebViewRequest(
            peer=self._entity, bot=self._entity, platform=platform, url=url))
        return res.url

    # -- reply detection -----------------------------------------------------

    async def _poll_new(self, after_id: int, timeout: float) -> list[dict]:
        """New bot messages after `after_id`. On first hit, waits one `settle`
        beat and re-reads: bots often answer in bursts (text + menu)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_interval)
            batch = await self._fetch_new(after_id)
            if batch:
                await asyncio.sleep(self.settle)
                return await self._fetch_new(after_id)
        return []

    async def _fetch_new(self, after_id: int) -> list[dict]:
        msgs = await self._client.get_messages(self._entity, limit=10, min_id=after_id)
        return [format_message(m) for m in reversed(msgs) if not m.out]

    async def _poll_after_click(self, anchor: int, msg_id: int, before: dict,
                                timeout: float) -> tuple[list[dict], dict | None]:
        """A click is answered by new messages, an in-place edit, or both."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_interval)
            new = await self._fetch_new(anchor)
            edited = await self._fetch_edit(msg_id, before)
            if new or edited:
                await asyncio.sleep(self.settle)
                return await self._fetch_new(anchor), await self._fetch_edit(msg_id, before)
        return [], None

    async def _fetch_edit(self, msg_id: int, before: dict) -> dict | None:
        msg = await self._client.get_messages(self._entity, ids=msg_id)
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if msg is None:
            return {"deleted": True}
        now = format_message(msg)
        if now.get("text") != before.get("text") or now.get("buttons") != before.get("buttons"):
            return now
        return None
