import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runners"))
import bot_mine  # noqa: E402

JS_BOT = r"""
import TelegramBot from 'node-telegram-bot-api'
const bot = new TelegramBot(token)

bot.onText(/\/settings/, (msg) => {
  bot.sendMessage(msg.chat.id, 'Your settings:', {
    reply_markup: {
      inline_keyboard: [
        [{ text: '🎨 Style', callback_data: 'style' }],
        [{ text: `🦄 Open ${config.appName}`, web_app: { url: config.webAppUrl } }],
      ],
    },
  })
})

bot.on('message', (msg) => {
  switch (msg.text) {
    case '/start':
      bot.sendMessage(msg.chat.id, 'Welcome! It\'s a demo 🏨')
      return
    case '/help':
      return
  }
  if (msg.text === '/ping') {
    bot.sendMessage(msg.chat.id, `pong ${Date.now()}`)
  }
})

bot.on('callback_query', (q) => {
  if (q.data === 'style') {
    bot.editMessageText(q.id, 'Pick a style')
  }
})
"""

PY_BOT = '''
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, WebAppInfo
from aiogram.filters.callback_data import CallbackData

router = Router()


class OrderCb(CallbackData, prefix="order"):
    item_id: int


@router.message(CommandStart())
async def start(message):
    await message.answer("Привет! Я бот-магазин 🛒")


@router.message(Command("catalog"))
async def catalog(message):
    kb = [[InlineKeyboardButton(text="🧦 Носки", callback_data="buy:socks")],
          [InlineKeyboardButton(text="🛍 Магазин", web_app=WebAppInfo(url="https://shop.example"))]]
    await message.answer("Каталог:", reply_markup=kb)


@router.callback_query(F.data == "buy:socks")
async def buy(query):
    await query.message.edit_text("Добавлено в корзину ✅")


@router.callback_query(F.data.startswith("page:"))
async def page(query):
    await bot.send_message(query.from_user.id, "Страница обновлена")
'''


@pytest.fixture
def js_project(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps(
        {"dependencies": {"node-telegram-bot-api": "^0.63.0", "fastify": "^4"}}))
    src = tmp_path / "src"
    src.mkdir()
    (src / "bot.ts").write_text(JS_BOT)
    return tmp_path


@pytest.fixture
def py_project(tmp_path):
    (tmp_path / "requirements.txt").write_text("aiogram>=3.0\nuvloop\n")
    (tmp_path / "handlers.py").write_text(PY_BOT)
    return tmp_path


# -- detection -----------------------------------------------------------------

def test_detect_js(js_project):
    fw, d = bot_mine.detect_framework(js_project)
    assert fw == "node-telegram-bot-api" and d == js_project


def test_detect_aiogram(py_project):
    fw, _ = bot_mine.detect_framework(py_project)
    assert fw == "aiogram"


def test_detect_none(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "18"}}')
    assert bot_mine.detect_framework(tmp_path) == (None, None)


def test_detect_skips_node_modules(tmp_path):
    nm = tmp_path / "node_modules" / "x"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text('{"dependencies": {"telegraf": "4"}}')
    assert bot_mine.detect_framework(tmp_path) == (None, None)


def test_mine_unknown_framework_exits(tmp_path):
    with pytest.raises(SystemExit):
        bot_mine.mine(tmp_path, mode="det")


# -- node-telegram-bot-api ---------------------------------------------------------

def test_js_commands_all_styles(js_project):
    m = bot_mine.mine(js_project, mode="det")
    cmds = {c["command"] for c in m["commands"]}
    assert cmds == {"/settings", "/start", "/help", "/ping"}


def test_js_callbacks_from_keyboard_and_comparison(js_project):
    m = bot_mine.mine(js_project, mode="det")
    assert {c["data"] for c in m["callbacks"]} == {"style"}


def test_js_keyboard_buttons_and_webapp(js_project):
    m = bot_mine.mine(js_project, mode="det")
    assert len(m["keyboards"]) == 1
    btns = m["keyboards"][0]["buttons"]
    assert btns[0] == {"text": "🎨 Style", "callback_data": "style"}
    assert btns[1]["text"] == "🦄 Open {appName}"
    assert btns[1]["web_app"] is True
    assert m["miniapp"] is True


def test_js_replies_verbatim_with_escapes_and_templates(js_project):
    m = bot_mine.mine(js_project, mode="det")
    texts = {r["text"] for r in m["replies"]}
    assert "Welcome! It's a demo 🏨" in texts
    assert "pong {now()}" in texts            # template normalized to a maskable slot
    assert "Your settings:" in texts
    assert "Pick a style" in texts            # editMessageText counts as a reply


def test_js_sources_point_at_file_and_line(js_project):
    m = bot_mine.mine(js_project, mode="det")
    for c in m["commands"]:
        assert c["source"].startswith("src/bot.ts:")


# -- aiogram ------------------------------------------------------------------------

def test_aiogram_commands(py_project):
    m = bot_mine.mine(py_project, mode="det")
    assert {c["command"] for c in m["commands"]} == {"/start", "/catalog"}


def test_aiogram_callbacks_filters_and_factory(py_project):
    m = bot_mine.mine(py_project, mode="det")
    data = {c["data"] for c in m["callbacks"]}
    assert {"buy:socks", "page:", "order:*"} <= data


def test_aiogram_keyboard_and_webapp(py_project):
    m = bot_mine.mine(py_project, mode="det")
    btns = m["keyboards"][0]["buttons"]
    assert {"text": "🧦 Носки", "callback_data": "buy:socks"} in btns
    assert any(b.get("web_app") and b["text"] == "🛍 Магазин" for b in btns)
    assert m["miniapp"] is True


def test_aiogram_replies(py_project):
    m = bot_mine.mine(py_project, mode="det")
    texts = {r["text"] for r in m["replies"]}
    assert {"Привет! Я бот-магазин 🛒", "Каталог:", "Добавлено в корзину ✅",
            "Страница обновлена"} <= texts


# -- helpers / rendering --------------------------------------------------------------

def test_balanced_skips_strings():
    s = 'x: [ "a ] tricky", { y: 1 } ]'
    assert bot_mine.balanced(s, 0, "[", "]") == '[ "a ] tricky", { y: 1 } ]'


def test_unquote_js_template_placeholder():
    assert bot_mine.unquote_js("`hi ${this.config.appName}!`") == "hi {appName}!"


def test_command_from_ontext_regex():
    assert bot_mine.command_from_ontext_regex(r"\/echo (.+)") == "/echo"
    assert bot_mine.command_from_ontext_regex(r"^\/start") == "/start"
    assert bot_mine.command_from_ontext_regex(r"hello") is None


def test_render_context_section(js_project):
    md = bot_mine.render_context_section(bot_mine.mine(js_project, mode="det"))
    assert "### Commands" in md and "`/start`" in md
    assert "web_app" in md and "Mini App button present: **yes**" in md
