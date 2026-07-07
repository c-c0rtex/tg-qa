import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runners"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
import maintain  # noqa: E402


GOOD_FIX = """tc: TC-G1
title: старт
type: passive
steps:
  - send: "/start"
    expect:
      contains: ["Welcome"]
"""


def setup_project(tmp_path, spec_body="tc: TC-G1\nsteps:\n  - send: '/old'\n"):
    tg = tmp_path / ".tg-qa"
    (tg / "specs").mkdir(parents=True)
    (tg / "scenarios").mkdir()
    (tg / "specs" / "tc-g1.yaml").write_text(spec_body)
    (tg / "scenarios" / "s.md").write_text(
        "# s\n\n## TC-G1 — старт\n**Type:** passive\n**Steps:**\n1. Send `/start`\n")
    return tg


RESULT = {"tc": "TC-G1", "spec": "tc-g1.yaml", "status": "fail",
          "failures": ["step 1: missing text: 'Welcome'"],
          "dialog": [{"you": "/start"},
                     {"bot": {"id": 2, "text": "Здравствуйте",
                              "buttons": [[{"text": "Меню"}]]}}]}


def test_format_dialog():
    s = maintain.format_dialog(RESULT["dialog"])
    assert "you: /start" in s
    assert "'Здравствуйте'" in s and '["Меню"]' in s
    assert maintain.format_dialog([]) == "(empty — the bot never reacted)"


def test_find_tc_body(tmp_path):
    tg = setup_project(tmp_path)
    body = maintain.find_tc_body(tg / "scenarios", "TC-G1")
    assert "TC-G1 — старт" in body
    assert "not found" in maintain.find_tc_body(tg / "scenarios", "TC-NOPE")


def test_heal_one_writes_fix_and_backup(tmp_path, monkeypatch):
    tg = setup_project(tmp_path)
    prompts = []
    monkeypatch.setattr(maintain, "call_claude", lambda p, **kw: prompts.append(p) or GOOD_FIX)
    out = maintain.heal_one(RESULT, tg, "ctx", {}, dry_run=False)
    assert out["status"] == "healed"
    healed = (tg / "specs" / "tc-g1.yaml").read_text()
    assert "Welcome" in healed and "healed by tg-qa-maintain" in healed
    assert (tg / "specs" / "tc-g1.yaml.bak").read_text().startswith("tc: TC-G1")
    # the prompt carried the real dialog and the failures
    assert "Здравствуйте" in prompts[0] and "missing text" in prompts[0]


def test_heal_one_product_bug_verdict(tmp_path, monkeypatch):
    tg = setup_project(tmp_path)
    monkeypatch.setattr(maintain, "call_claude",
                        lambda p, **kw: "PRODUCT-BUG: бот отвечает «Здравствуйте» вместо welcome-текста")
    out = maintain.heal_one(RESULT, tg, "ctx", {}, dry_run=False)
    assert out["status"] == "product-bug" and "Здравствуйте" in out["reason"]
    # spec untouched
    assert (tg / "specs" / "tc-g1.yaml").read_text().startswith("tc: TC-G1")


def test_heal_one_product_bug_verdict_after_reasoning(tmp_path, monkeypatch):
    # models often explain before the marker — the verdict must still be recognized
    reply = ("Спека корректна, ожидание оправдано.\n"
             "Живой бот на голосовое отвечает ошибкой неподдерживаемого типа.\n\n"
             "PRODUCT-BUG: бот больше не принимает voice, хотя /help их обещает")
    tg = setup_project(tmp_path)
    monkeypatch.setattr(maintain, "call_claude", lambda p, **kw: reply)
    out = maintain.heal_one(RESULT, tg, "ctx", {}, dry_run=False)
    assert out["status"] == "product-bug"
    assert "voice" in out["reason"] and out["reason"].startswith("бот больше не")
    assert (tg / "specs" / "tc-g1.yaml").read_text().startswith("tc: TC-G1")  # untouched


def test_heal_one_invalid_fix(tmp_path, monkeypatch):
    tg = setup_project(tmp_path)
    monkeypatch.setattr(maintain, "call_claude", lambda p, **kw: "tc: TC-G1\nsteps: []\n")
    out = maintain.heal_one(RESULT, tg, "ctx", {}, dry_run=False)
    assert out["status"] == "invalid-fix" and "schema errors" in out["error"]


def test_heal_one_dry_run(tmp_path, monkeypatch):
    tg = setup_project(tmp_path)
    monkeypatch.setattr(maintain, "call_claude", lambda p, **kw: GOOD_FIX)
    before = (tg / "specs" / "tc-g1.yaml").read_text()
    out = maintain.heal_one(RESULT, tg, "ctx", {}, dry_run=True)
    assert out["status"] == "healed"
    assert (tg / "specs" / "tc-g1.yaml").read_text() == before
    assert not (tg / "specs" / "tc-g1.yaml.bak").exists()


def test_heal_one_probe_warnings_carried(tmp_path, monkeypatch):
    tg = setup_project(tmp_path)
    monkeypatch.setattr(maintain, "call_claude", lambda p, **kw: GOOD_FIX.replace("/start", "/ghost"))
    bot_map = {"commands": [{"command": "/start"}], "keyboards": []}
    out = maintain.heal_one(RESULT, tg, "ctx", bot_map, dry_run=True)
    assert out["status"] == "healed" and "/ghost" in out["probe_warnings"][0]


def test_heal_one_missing_spec(tmp_path):
    tg = setup_project(tmp_path)
    out = maintain.heal_one({"tc": "X", "spec": "nope.yaml"}, tg, "ctx", {}, dry_run=False)
    assert out["status"] == "missing-spec"
