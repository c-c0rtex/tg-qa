import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from driver import BotDriver, DriverError, format_buttons, format_message, keyboard_texts  # noqa: E402


class FakeBtn:
    def __init__(self, text, url=None, data=None, on_click=None):
        self.text = text
        self.url = url
        self.data = data
        self._on_click = on_click

    async def click(self):
        if self._on_click:
            self._on_click()


class FakeMsg:
    def __init__(self, id, text="", out=False, buttons=None, media=None, date=None):
        self.id = id
        self.text = text
        self.out = out
        self.buttons = buttons
        self.media = media
        self.date = date


class FakeClient:
    """Duck-typed Telethon client over an in-memory message list (oldest→newest)."""

    def __init__(self, messages):
        self.messages = messages
        self.read_acks = 0
        self.requests = []
        self.sent_files = []

    async def send_file(self, entity, path, caption=None, **kwargs):
        self.sent_files.append({"path": path, "caption": caption, "kwargs": kwargs})
        self.messages.append(FakeMsg(max((m.id for m in self.messages), default=0) + 1,
                                     out=True))
        return self.messages[-1]

    async def get_messages(self, entity, limit=None, min_id=None, ids=None):
        if ids is not None:
            return next((m for m in self.messages if m.id == ids), None)
        out = [m for m in self.messages if min_id is None or m.id > min_id]
        out = list(reversed(out))  # newest first, like Telethon
        return out[:limit] if limit else out

    async def send_read_acknowledge(self, entity):
        self.read_acks += 1

    async def get_input_entity(self, ref):
        return object()

    async def __call__(self, request):
        self.requests.append(request)


def make_driver(messages, **kw):
    d = BotDriver("fake.session", 1, "h", "@bot",
                  send_delay=0, settle=0, poll_interval=0.01, **kw)
    d._client = FakeClient(messages)
    d._entity = object()
    return d


# -- pure formatting ---------------------------------------------------------

def test_format_message_basic_and_buttons():
    msg = FakeMsg(5, "hello", buttons=[[FakeBtn("A", data=b"cb_a"), FakeBtn("B", url="https://x")]])
    f = format_message(msg)
    assert f["id"] == 5 and f["from"] == "bot" and f["text"] == "hello"
    assert f["buttons"] == [[{"text": "A", "data": "cb_a"}, {"text": "B", "url": "https://x"}]]
    assert keyboard_texts(f) == [["A", "B"]]


def test_format_message_outgoing_no_buttons():
    f = format_message(FakeMsg(1, "hi", out=True))
    assert f["from"] == "you"
    assert "buttons" not in f
    assert keyboard_texts(f) == []


def test_format_message_media_type():
    class Photo:
        pass
    f = format_message(FakeMsg(2, media=Photo()))
    assert f["media_type"] == "Photo"


def test_format_buttons_none():
    assert format_buttons(FakeMsg(1)) == []


# -- click -------------------------------------------------------------------

def test_click_button_not_found_lists_available():
    d = make_driver([FakeMsg(10, "menu", buttons=[[FakeBtn("Настройки")]])])
    with pytest.raises(DriverError) as e:
        asyncio.run(d.click(10, "Оплата", wait=0.05))
    assert "Настройки" in str(e.value)


def test_click_message_not_found():
    d = make_driver([])
    with pytest.raises(DriverError):
        asyncio.run(d.click(99, "X", wait=0.05))


def test_click_new_reply_detected():
    msgs = [FakeMsg(10, "menu", buttons=[[FakeBtn("Go", on_click=lambda: msgs.append(FakeMsg(11, "done")))]])]
    msgs[0].buttons[0][0]._on_click = lambda: msgs.append(FakeMsg(11, "done"))
    d = make_driver(msgs)
    res = asyncio.run(d.click(10, "Go", wait=1))
    assert res["clicked"] == "Go"
    assert [m["text"] for m in res["replies"]] == ["done"]
    assert res["edited"] is None


def test_click_inplace_edit_detected():
    msgs = [FakeMsg(10, "page 1", buttons=[[FakeBtn("Next")]])]

    def edit():
        msgs[0].text = "page 2"
    msgs[0].buttons[0][0]._on_click = edit
    d = make_driver(msgs)
    res = asyncio.run(d.click(10, "Next", wait=1))
    assert res["replies"] == []
    assert res["edited"]["text"] == "page 2"


def test_click_timeout_no_reaction():
    msgs = [FakeMsg(10, "menu", buttons=[[FakeBtn("Silent")]])]
    d = make_driver(msgs)
    res = asyncio.run(d.click(10, "Silent", wait=0.05))
    assert res["replies"] == [] and res["edited"] is None


# -- polling / history ---------------------------------------------------------

def test_poll_new_ignores_own_messages():
    msgs = [FakeMsg(1, "cmd", out=True)]

    async def run():
        d = make_driver(msgs)
        task = asyncio.ensure_future(d._poll_new(1, timeout=1))
        await asyncio.sleep(0.02)
        msgs.append(FakeMsg(2, "you said", out=True))
        msgs.append(FakeMsg(3, "reply"))
        return await task

    replies = asyncio.run(run())
    assert [m["text"] for m in replies] == ["reply"]


def test_poll_new_timeout_empty():
    d = make_driver([FakeMsg(1, "x", out=True)])
    assert asyncio.run(d._poll_new(1, timeout=0.05)) == []


def test_fetch_edit_deleted_message():
    d = make_driver([])
    res = asyncio.run(d._fetch_edit(5, {"text": "was", "buttons": None}))
    assert res == {"deleted": True}


def test_send_media_kind_flags(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    cases = {
        "voice": {"voice_note": True},
        "video_note": {"video_note": True},
        "document": {"force_document": True},
        "photo": {},
        "sticker": {},
        "video": {},
        "gif": {},
        "auto": {},
    }
    for kind, expected in cases.items():
        d = make_driver([])
        res = asyncio.run(d.send_media(f, kind=kind, caption="c"))
        assert res["kind"] == kind
        sent = d._client.sent_files[0]
        assert sent["caption"] == "c"
        assert sent["kwargs"] == expected


def test_send_media_unknown_kind(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"x")
    d = make_driver([])
    with pytest.raises(DriverError):
        asyncio.run(d.send_media(f, kind="hologram"))


def test_send_media_missing_file():
    d = make_driver([])
    with pytest.raises(DriverError):
        asyncio.run(d.send_media("/no/such/file.ogg", kind="voice"))


def test_send_file_backcompat_voice(tmp_path):
    f = tmp_path / "v.ogg"
    f.write_bytes(b"x")
    d = make_driver([])
    res = asyncio.run(d.send_file(f, voice=True))
    assert res["kind"] == "voice"
    assert d._client.sent_files[0]["kwargs"] == {"voice_note": True}


def test_auto_read_marks_after_actions():
    msgs = [FakeMsg(10, "menu", buttons=[[FakeBtn("Silent")]])]
    d = make_driver(msgs)
    asyncio.run(d.history(limit=5))
    asyncio.run(d.click(10, "Silent", wait=0.05))
    assert d._client.read_acks == 2


def test_auto_read_off_keeps_unread():
    d = make_driver([FakeMsg(1, "x")], auto_read=False)
    asyncio.run(d.history(limit=5))
    assert d._client.read_acks == 0


def test_auto_mute_sends_mute_settings():
    d = make_driver([])
    asyncio.run(d._apply_notify_settings())
    assert len(d._client.requests) == 1
    assert d._client.requests[0].settings.mute_until == 2**31 - 1


def test_auto_mute_off_unmutes():
    d = make_driver([], auto_mute=False)
    asyncio.run(d._apply_notify_settings())
    assert d._client.requests[0].settings.mute_until == 0
    assert d._client.requests[0].settings.silent is False


def test_history_oldest_first():
    d = make_driver([FakeMsg(1, "a"), FakeMsg(2, "b", out=True), FakeMsg(3, "c")])
    h = asyncio.run(d.history(limit=10))
    assert [m["text"] for m in h] == ["a", "b", "c"]
    assert [m["from"] for m in h] == ["bot", "you", "bot"]
