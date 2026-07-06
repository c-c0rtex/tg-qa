import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runners"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
import spec_gen  # noqa: E402
from tclib import classify_tc, declared_type, slugify, spec_mutating_sends, split_tcs, tc_roles  # noqa: E402

SCENARIO = """# Меню настроек

## TC-S1 — /start показывает приветствие
**Type:** passive
**Steps:**
1. Send `/start`
**Expected:**
- Ответ содержит «Welcome»

## TC-S2 — Смена стиля
**Type:** mutating
**Roles:** admin, editor
**Steps:**
1. Send `/settings`
2. Click "🎨 Style"
**Expected:**
- Меню отредактировано

## TC-S3 — Без типа
**Steps:**
1. Send `/help`
"""


# -- tclib --------------------------------------------------------------------------

def test_split_tcs_and_fields():
    tcs = split_tcs(SCENARIO)
    assert [t["id"] for t in tcs] == ["TC-S1", "TC-S2", "TC-S3"]
    assert tcs[1]["title"] == "Смена стиля"
    assert declared_type(tcs[0]["body"]) == "passive"
    assert tc_roles(tcs[1]["body"]) == ["admin", "editor"]
    assert tc_roles(tcs[0]["body"]) == []


def test_classify_declared_wins_without_evidence():
    kind, reasons = classify_tc("**Type:** passive\n", {"steps": [{"send": "/start"}]})
    assert kind == "passive" and reasons == []


def test_classify_plain_send_overrides_passive():
    spec = {"steps": [{"send": "/start"}, {"send": "моя новая статья"}]}
    assert spec_mutating_sends(spec) == ["моя новая статья"]
    kind, reasons = classify_tc("**Type:** passive\n", spec)
    assert kind == "mutating" and "plain text" in reasons[0]


def test_classify_undeclared_is_mutating():
    kind, reasons = classify_tc("**Steps:**\n1. x\n")
    assert kind == "mutating" and "no **Type:**" in reasons[0]


def test_slugify():
    assert slugify("TC-S2") == "tc-s2"
    assert slugify("Смена стиля!") == "tc"      # non-ASCII collapses to the fallback


# -- probe -------------------------------------------------------------------------

BOT_MAP = {
    "commands": [{"command": "/start"}, {"command": "/settings"}],
    "keyboards": [
        {"buttons": [{"text": "🎨 Style", "callback_data": "style"},
                     {"text": "🦄 Open {appName}", "web_app": True}]},
    ],
}


def test_probe_ok():
    spec = {"steps": [{"send": "/settings"}, {"click": "🎨 Style"}]}
    assert spec_gen.probe_spec(spec, BOT_MAP) == []


def test_probe_placeholder_caption_matches():
    spec = {"steps": [{"click": "🦄 Open Telebook"}]}
    assert spec_gen.probe_spec(spec, BOT_MAP) == []


def test_probe_unknown_command_and_button():
    spec = {"steps": [{"send": "/pay"}, {"click": "Оплатить"}]}
    warns = spec_gen.probe_spec(spec, BOT_MAP)
    assert len(warns) == 2
    assert "`/pay`" in warns[0] and "'Оплатить'" in warns[1]


def test_probe_plain_text_and_empty_map_sections():
    spec = {"steps": [{"send": "просто текст"}, {"click": "X"}]}
    assert spec_gen.probe_spec(spec, {"commands": [], "keyboards": []}) == []
    assert spec_gen.probe_spec(spec, BOT_MAP) != []          # click still checked


def test_probe_command_with_bot_suffix_and_args():
    spec = {"steps": [{"send": "/start@my_bot deep-link-payload"}]}
    assert spec_gen.probe_spec(spec, BOT_MAP) == []


# -- gen_one retry loop (LLM mocked) ---------------------------------------------------

GOOD_YAML = """tc: TC-S1
title: старт
type: passive
steps:
  - send: "/start"
    expect:
      contains: ["Welcome"]
"""


def test_gen_one_first_try(monkeypatch):
    calls = []
    monkeypatch.setattr(spec_gen, "call_claude", lambda p, **kw: calls.append(p) or GOOD_YAML)
    tc = {"id": "TC-S1", "title": "старт", "body": "**Type:** passive"}
    spec, err, warns = spec_gen.gen_one(tc, "ctx", BOT_MAP, probe=True)
    assert err is None and warns == [] and len(calls) == 1
    assert spec["tc"] == "TC-S1"


def test_gen_one_schema_retry(monkeypatch):
    outputs = ["tc: TC-S1\nsteps: []\n", GOOD_YAML]
    calls = []

    def fake(prompt, **kw):
        calls.append(prompt)
        return outputs[len(calls) - 1]

    monkeypatch.setattr(spec_gen, "call_claude", fake)
    tc = {"id": "TC-S1", "title": "старт", "body": ""}
    spec, err, _ = spec_gen.gen_one(tc, "ctx", BOT_MAP, probe=True)
    assert err is None and len(calls) == 2
    assert "REJECTED" in calls[1] and "schema errors" in calls[1]


def test_gen_one_probe_retry_keeps_warnings_if_still_missing(monkeypatch):
    bad = GOOD_YAML.replace("/start", "/ghost")
    monkeypatch.setattr(spec_gen, "call_claude", lambda p, **kw: bad)
    tc = {"id": "TC-S1", "title": "старт", "body": ""}
    spec, err, warns = spec_gen.gen_one(tc, "ctx", BOT_MAP, probe=True)
    assert err is None
    assert warns and "/ghost" in warns[0]      # saved WITH warnings, not rejected


def test_gen_one_double_schema_failure_is_fatal(monkeypatch):
    monkeypatch.setattr(spec_gen, "call_claude", lambda p, **kw: "not: [valid")
    spec, err, _ = spec_gen.gen_one({"id": "T", "title": "t", "body": ""}, "ctx", {}, probe=False)
    assert spec is None and "YAML parse error" in err


def test_gen_one_tc_id_enforced(monkeypatch):
    monkeypatch.setattr(spec_gen, "call_claude",
                        lambda p, **kw: GOOD_YAML.replace("TC-S1", "TC-WRONG"))
    spec, err, _ = spec_gen.gen_one({"id": "TC-S1", "title": "t", "body": ""}, "ctx", {}, probe=False)
    assert spec["tc"] == "TC-S1"
