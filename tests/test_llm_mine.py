import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runners"))
import bot_mine  # noqa: E402
import spec_gen  # noqa: E402
from tclib import strip_fences  # noqa: E402

GO_BOT = '''
package main

func handle(update Update) {
    if update.Message.Text == "/start" {
        api.Request(SendMessageConfig{Text: "Добро пожаловать!", ReplyMarkup: kb})
    }
}
var kb = NewInlineKeyboardMarkup(NewInlineKeyboardRow(
    NewInlineKeyboardButtonData("🛒 Каталог", "catalog")))
// callback_data + inline_keyboard + sendMessage appear in the wire types
'''

LLM_JSON = json.dumps({
    "commands": [{"command": "start", "source": "main.go"}],
    "callbacks": [{"data": "catalog", "source": "main.go"}],
    "keyboards": [{"buttons": [{"text": "🛒 Каталог", "callback_data": "catalog"}],
                   "source": "main.go"}],
    "replies": [{"text": "Добро пожаловать!", "source": "main.go"}],
}, ensure_ascii=False)


@pytest.fixture
def go_project(tmp_path):
    (tmp_path / "go.mod").write_text("module bot\n")
    (tmp_path / "main.go").write_text(GO_BOT)
    (tmp_path / "README.md").write_text("sendMessage docs")   # non-code ext, ignored
    return tmp_path


def test_strip_fences_json_tag():
    assert strip_fences("```json\n{\"a\": 1}\n```") == '{"a": 1}'
    assert strip_fences("plain") == "plain"


def test_candidate_files_scores_and_filters(go_project):
    files = bot_mine.candidate_files(go_project)
    assert [f.name for f, _ in files] == ["main.go"]


def test_candidate_files_skips_node_modules(tmp_path):
    nm = tmp_path / "node_modules" / "lib"
    nm.mkdir(parents=True)
    (nm / "api.js").write_text("sendMessage inline_keyboard")
    assert bot_mine.candidate_files(tmp_path) == []


def test_validate_llm_map():
    assert bot_mine.validate_llm_map(json.loads(LLM_JSON)) == []
    errs = bot_mine.validate_llm_map({"commands": [{}], "keyboards": [{"buttons": [{"x": 1}]}]})
    assert any("commands[0]" in e for e in errs)
    assert any("buttons[0]" in e for e in errs)
    assert bot_mine.validate_llm_map("nope") == ["not a JSON object"]


def test_llm_mine_happy_path(go_project, monkeypatch):
    import tclib
    monkeypatch.setattr(tclib, "call_claude", lambda p, **kw: LLM_JSON)
    m = bot_mine.llm_mine(go_project, go_project)
    assert m["mined_by"] == "llm"
    assert m["commands"][0]["command"] == "/start"      # slash enforced
    assert m["callbacks"][0]["data"] == "catalog"


def test_llm_mine_retry_on_bad_json(go_project, monkeypatch):
    outputs = ["{broken", LLM_JSON]
    calls = []

    def fake(prompt, **kw):
        calls.append(prompt)
        return outputs[len(calls) - 1]

    import tclib
    monkeypatch.setattr(tclib, "call_claude", fake)
    m = bot_mine.llm_mine(go_project, go_project)
    assert m["commands"] and len(calls) == 2
    assert "REJECTED" in calls[1]


def test_llm_mine_double_failure_exits(go_project, monkeypatch):
    import tclib
    monkeypatch.setattr(tclib, "call_claude", lambda p, **kw: "{broken")
    with pytest.raises(SystemExit):
        bot_mine.llm_mine(go_project, go_project)


def test_mine_auto_falls_back_to_llm(go_project, monkeypatch):
    import tclib
    monkeypatch.setattr(tclib, "call_claude", lambda p, **kw: LLM_JSON)
    m = bot_mine.mine(go_project)                        # no framework manifest → LLM
    assert m["mined_by"] == "llm" and m["miniapp"] is False


def test_mine_llm_needs_bot_tokens(tmp_path):
    (tmp_path / "main.go").write_text("package main // nothing bot-like")
    with pytest.raises(SystemExit):
        bot_mine.mine(tmp_path, mode="llm")


def test_merge_maps_augment_dedupes_and_marks():
    det = {"framework": "aiogram", "source_dir": "x", "mined_by": "deterministic",
           "commands": [{"command": "/start", "source": "a.py:1"}],
           "callbacks": [], "replies": [{"text": "Привет", "source": "a.py:2"}],
           "keyboards": [{"buttons": [{"text": "Меню"}], "source": "a.py:3"}]}
    llm = {"commands": [{"command": "/start", "source": "a.py"},      # dup — dropped
                        {"command": "/promo", "source": "i18n.py"}],
           "callbacks": [{"data": "promo", "source": "i18n.py"}],
           "replies": [{"text": "Привет", "source": "a.py"},          # dup — dropped
                       {"text": "Акция недели!", "source": "i18n.py"}],
           "keyboards": [{"buttons": [{"text": "Меню"}], "source": "a.py"}]}   # dup kb
    m = bot_mine._finalize(bot_mine.merge_maps(det, llm))
    assert m["mined_by"] == "deterministic+llm"
    assert [c["command"] for c in m["commands"]] == ["/start", "/promo"]
    assert m["commands"][1]["mined_by"] == "llm"
    assert [r["text"] for r in m["replies"]] == ["Привет", "Акция недели!"]
    assert len(m["keyboards"]) == 1


def test_probe_misses_on_llm_map_do_not_retry(monkeypatch):
    llm_map = {"mined_by": "llm",
               "commands": [{"command": "/start"}],
               "keyboards": [{"buttons": [{"text": "A"}]}]}
    bad_spec = "tc: T\nsteps:\n  - send: '/ghost'\n"
    calls = []
    monkeypatch.setattr(spec_gen, "call_claude", lambda p, **kw: calls.append(p) or bad_spec)
    spec, err, warns = spec_gen.gen_one({"id": "T", "title": "t", "body": ""},
                                        "ctx", llm_map, probe=True)
    assert err is None and len(calls) == 1               # no retry burned
    assert warns and "/ghost" in warns[0]                # but the miss is reported


def test_render_context_marks_llm_map():
    md = bot_mine.render_context_section(bot_mine._finalize(
        {"framework": "llm", "mined_by": "llm", "commands": [], "callbacks": [],
         "keyboards": [], "replies": []}))
    assert "mined by an LLM" in md
