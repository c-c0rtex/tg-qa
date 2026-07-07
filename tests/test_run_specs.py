import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runners"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from driver import DriverError  # noqa: E402
from run_specs import Baselines, check_step, render_report, run_all, run_spec, validate_spec  # noqa: E402


# -- validate_spec ----------------------------------------------------------------

def test_validate_ok():
    spec = {"tc": "TC-1", "steps": [{"send": "/start", "expect": {"contains": ["hi"]}},
                                    {"click": "Go", "expect": {"edited": True}}]}
    assert validate_spec(spec) == []


def test_validate_errors():
    errs = validate_spec({"steps": [
        {"send": "/a", "click": "B"},
        {"expect": {}},
        {"send": "/b", "expect": {"typo_key": 1}},
        {"send": "/c", "expect": {"edited": True}},
        {"click": {}},
    ]})
    joined = "\n".join(errs)
    assert "missing `tc`" in joined
    assert "exactly one of send/click" in joined
    assert "typo_key" in joined
    assert "`edited` only applies to click steps" in joined
    assert "button caption" in joined


def test_validate_empty():
    assert validate_spec({"tc": "x"}) == ["missing or empty `steps`"]


def test_validate_send_media():
    assert validate_spec({"tc": "T", "steps": [
        {"send_media": {"path": "/tmp/a.ogg", "kind": "voice"}, "expect": {"contains": ["ok"]}}]}) == []
    errs = validate_spec({"tc": "T", "steps": [{"send_media": {"kind": "voice"}}]})
    assert any("send_media must be" in e for e in errs)
    errs2 = validate_spec({"tc": "T", "steps": [
        {"send": "/x", "send_media": {"path": "a"}}]})
    assert any("exactly one of send/click/send_media" in e for e in errs2)


# -- baselines ---------------------------------------------------------------------

def test_baseline_create_then_match_then_diff(tmp_path):
    b = Baselines(tmp_path, update=False, masks=[r"\d{2}:\d{2}"])
    assert b.check_text("hello", "hi at 12:45") is None
    assert b.created == ["hello"]
    assert b.check_text("hello", "hi at 09:01") is None      # masked → identical
    err = b.check_text("hello", "bye at 09:01")
    assert err and "differs" in err and "-hi at" in err and "+bye at" in err


def test_baseline_update_mode(tmp_path):
    b = Baselines(tmp_path, update=False, masks=[])
    b.check_text("s", "v1")
    b2 = Baselines(tmp_path, update=True, masks=[])
    assert b2.check_text("s", "v2") is None
    assert b2.updated == ["s"]
    assert (tmp_path / "s.txt").read_text() == "v2"


def test_keyboard_baseline(tmp_path):
    b = Baselines(tmp_path, update=False, masks=[])
    kb = [["A", "B"], ["C"]]
    assert b.check_keyboard("kb", kb) is None
    assert b.check_keyboard("kb", kb) is None
    err = b.check_keyboard("kb", [["A"]])
    assert err and "keyboard snapshot" in err


# -- check_step ----------------------------------------------------------------------

def msg(text="", buttons=None, media=None, id=1):
    m = {"id": id, "from": "bot", "text": text, "date": ""}
    if buttons:
        m["buttons"] = [[{"text": t} for t in row] for row in buttons]
    if media:
        m["media_type"] = media
    return m


def bl(tmp_path):
    return Baselines(tmp_path, update=False, masks=[])


def test_check_no_reaction(tmp_path):
    fails = check_step({"contains": ["x"]}, [], None, bl(tmp_path))
    assert fails == ["no reaction from bot (no new message, no edit)"]


def test_check_no_reply_expected(tmp_path):
    assert check_step({"no_reply": True}, [], None, bl(tmp_path)) == []
    fails = check_step({"no_reply": True}, [msg("hi")], None, bl(tmp_path))
    assert "expected silence" in fails[0]


def test_check_contains_and_regex(tmp_path):
    produced = [msg("Welcome to the bot"), msg("Menu below", buttons=[["Go"]])]
    assert check_step({"contains": ["Welcome", "Menu"], "regex": "W.lcome"},
                      produced, None, bl(tmp_path)) == []
    fails = check_step({"contains": ["Оплата"]}, produced, None, bl(tmp_path))
    assert "missing text: 'Оплата'" in fails[0]


def test_check_buttons_exact_and_contain(tmp_path):
    produced = [msg("m", buttons=[["A", "B"], ["C"]])]
    assert check_step({"buttons": [["A", "B"], ["C"]], "buttons_contain": ["C"]},
                      produced, None, bl(tmp_path)) == []
    fails = check_step({"buttons": [["A"]], "buttons_contain": ["X"]},
                       produced, None, bl(tmp_path))
    assert len(fails) == 2


def test_check_buttons_contain_is_substring(tmp_path):
    produced = [msg("m", buttons=[["🦄 Open Telebook"]])]
    assert check_step({"buttons_contain": ["🦄 Open"]}, produced, None, bl(tmp_path)) == []
    fails = check_step({"buttons_contain": ["Оплатить"]}, produced, None, bl(tmp_path))
    assert "no button containing" in fails[0]


def test_check_media_and_edited(tmp_path):
    produced = [msg("pic", media="MessageMediaPhoto")]
    assert check_step({"media_type": "Photo"}, produced, None, bl(tmp_path)) == []
    fails = check_step({"media_type": "Document", "edited": True}, produced, None, bl(tmp_path))
    assert any("expected media" in f for f in fails)
    assert any("edited in place" in f for f in fails)
    assert check_step({"edited": True}, [], msg("new page"), bl(tmp_path)) == []


def test_check_snapshot_uses_last_message(tmp_path):
    b = bl(tmp_path)
    assert check_step({"snapshot": "s"}, [msg("first"), msg("second")], None, b) == []
    assert (tmp_path / "s.txt").read_text() == "second"


# -- run_spec / run_all with a fake driver ----------------------------------------------

class FakeDriver:
    """Scripted bot: maps sent text / clicked caption → canned reaction."""

    def __init__(self, script):
        self.script = script
        self.closed = False

    async def send(self, text, wait=10):
        return {"sent_id": 1, "replies": self.script.get(("send", text), [])}

    async def click(self, msg_id, caption, wait=10):
        key = ("click", caption)
        if key not in self.script:
            raise DriverError(f"Button '{caption}' not found")
        r = self.script[key]
        return {"clicked": caption, "on_message": msg_id,
                "replies": r.get("replies", []), "edited": r.get("edited")}

    async def send_media(self, path, kind="auto", caption="", wait=15):
        return {"sent_id": 1, "kind": kind,
                "replies": self.script.get(("send_media", kind), [])}

    async def close(self):
        self.closed = True


def run(spec, driver, tmp_path):
    return asyncio.run(run_spec(spec, driver, 0.1, bl(tmp_path)))


def test_run_spec_send_then_click_flow(tmp_path):
    d = FakeDriver({
        ("send", "/start"): [msg("Welcome", buttons=[["Settings"]], id=10)],
        ("click", "Settings"): {"edited": msg("Settings page", id=10)},
    })
    spec = {"tc": "T1", "steps": [
        {"send": "/start", "expect": {"contains": ["Welcome"]}},
        {"click": "Settings", "expect": {"edited": True, "contains": ["Settings page"]}},
    ]}
    res = run(spec, d, tmp_path)
    assert res["status"] == "pass" and res["steps"] == 2
    assert {"you": "[click] Settings"} in res["dialog"]


def test_run_spec_send_media_step(tmp_path):
    d = FakeDriver({("send_media", "voice"): [msg("Click the button below")]})
    spec = {"tc": "TM", "steps": [
        {"send_media": {"path": "/tmp/v.ogg", "kind": "voice"},
         "expect": {"contains": ["Click the button"]}}]}
    res = run(spec, d, tmp_path)
    assert res["status"] == "pass"
    assert {"you": "[voice] /tmp/v.ogg"} in res["dialog"]


def test_run_spec_click_without_keyboard_errors(tmp_path):
    d = FakeDriver({("send", "/start"): [msg("plain text")]})
    spec = {"tc": "T2", "steps": [{"send": "/start"}, {"click": "Go"}]}
    res = run(spec, d, tmp_path)
    assert res["status"] == "error"
    assert "no bot message with buttons" in res["failures"][0]


def test_run_spec_failure_stops_flow(tmp_path):
    d = FakeDriver({("send", "/a"): [msg("wrong")], ("send", "/b"): [msg("never")]})
    spec = {"tc": "T3", "steps": [{"send": "/a", "expect": {"contains": ["right"]}},
                                  {"send": "/b"}]}
    res = run(spec, d, tmp_path)
    assert res["status"] == "fail" and res["steps"] == 1


def test_run_spec_edit_updates_keyboard_anchor(tmp_path):
    d = FakeDriver({
        ("send", "/menu"): [msg("Menu", buttons=[["Next"]], id=5)],
        ("click", "Next"): {"edited": msg("Page 2", buttons=[["Prev"]], id=5)},
        ("click", "Prev"): {"edited": msg("Page 1", buttons=[["Next"]], id=5)},
    })
    spec = {"tc": "T4", "steps": [{"send": "/menu"},
                                  {"click": "Next", "expect": {"contains": ["Page 2"]}},
                                  {"click": "Prev", "expect": {"contains": ["Page 1"]}}]}
    res = run(spec, d, tmp_path)
    assert res["status"] == "pass" and res["steps"] == 3


def test_run_all_role_driver_cache_and_close(tmp_path):
    import yaml
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    for i, role in enumerate(["admin", "admin", None]):
        body = {"tc": f"T{i}", "steps": [{"send": "/start", "expect": {"contains": ["hi"]}}]}
        if role:
            body["role"] = role
        (specs_dir / f"s{i}.yaml").write_text(yaml.safe_dump(body, allow_unicode=True))

    made = []

    async def factory(proj, cfg, role):
        d = FakeDriver({("send", "/start"): [msg("hi")]})
        made.append((role, d))
        return d

    results = asyncio.run(run_all(sorted(specs_dir.glob("*.yaml")), {}, {}, None,
                                  bl(tmp_path), driver_factory=factory))
    assert [r["status"] for r in results] == ["pass", "pass", "pass"]
    assert [r for r, _ in made] == ["admin", None]     # cached per role
    assert all(d.closed for _, d in made)


def test_run_all_passive_only_gates_mutating(tmp_path):
    import yaml
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / "a.yaml").write_text(yaml.safe_dump(
        {"tc": "T1", "type": "passive", "steps": [{"send": "/start", "expect": {"contains": ["hi"]}}]}))
    (specs_dir / "b.yaml").write_text(yaml.safe_dump(
        {"tc": "T2", "type": "mutating", "steps": [{"send": "/start"}]}))
    (specs_dir / "c.yaml").write_text(yaml.safe_dump(          # liar: passive label, plain send
        {"tc": "T3", "type": "passive", "steps": [{"send": "новый пост"}]}))

    async def factory(proj, cfg, role):
        return FakeDriver({("send", "/start"): [msg("hi")]})

    from run_specs import Baselines
    results = asyncio.run(run_all(sorted(specs_dir.glob("*.yaml")), {}, {}, None,
                                  Baselines(tmp_path, False, []), driver_factory=factory,
                                  passive_only=True))
    assert [(r["tc"], r["status"]) for r in results] == \
        [("T1", "pass"), ("T2", "skipped"), ("T3", "skipped")]


def test_render_junit():
    from run_specs import render_junit
    xml = render_junit([
        {"spec": "a.yaml", "tc": "T1", "role": "admin", "status": "pass"},
        {"spec": "b.yaml", "tc": "T2", "role": None, "status": "fail",
         "failures": ["step 1: missing text: 'x' <got>"]},
        {"spec": "c.yaml", "tc": "T3", "role": None, "status": "skipped",
         "skip_reason": "mutating spec under --passive-only"},
        {"spec": "d.yaml", "tc": "T4", "role": None, "status": "error",
         "failures": ["step 1: FloodWait"]},
    ])
    assert 'tests="3" failures="2"' in xml
    assert '<testcase name="a.yaml::T1::admin"/>' in xml
    assert "&lt;got&gt;" in xml and "<failure>" in xml
    assert "<skipped" in xml and "<error>" in xml


def test_fixture_cmd_hard_stop(tmp_path):
    from run_specs import run_fixture_cmd
    import pytest
    with pytest.raises(SystemExit):
        run_fixture_cmd({"path": str(tmp_path)}, {"fixture_cmd": "exit 3"})
    run_fixture_cmd({"path": str(tmp_path)}, {"fixture_teardown_cmd": "exit 3"}, teardown=True)
    run_fixture_cmd({"path": str(tmp_path)}, {})   # no cmd — noop
    marker = tmp_path / "seeded"
    run_fixture_cmd({"path": str(tmp_path)}, {"fixture_cmd": "touch seeded"})
    assert marker.exists()


def test_render_report_failures_and_queue(tmp_path):
    b = bl(tmp_path)
    b.created.append("start-welcome")
    results = [
        {"spec": "a.yaml", "tc": "T1", "role": None, "status": "pass", "failures": [], "dialog": []},
        {"spec": "b.yaml", "tc": "T2", "role": "admin", "status": "fail",
         "failures": ["step 1: missing text: 'x'"],
         "dialog": [{"you": "/start"}, {"bot": msg("y", buttons=[["Go"]])}]},
    ]
    md = render_report(results, b)
    assert "1/2 passed" in md
    assert "❌ fail" in md and "missing text" in md
    assert "Snapshot review queue" in md and "start-welcome" in md
    assert "[['Go']]" in md
