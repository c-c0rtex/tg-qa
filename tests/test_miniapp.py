import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runners"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
import miniapp  # noqa: E402

USER = '{"id":42,"first_name":"QA","username":"qa_bot"}'
INIT_DATA = ("query_id=AAA&user=" + urllib.parse.quote(USER)
             + "&auth_date=1700000000&signature=sig&hash=deadbeef")


def test_parse_init_data_decodes_user():
    unsafe = miniapp.parse_init_data(INIT_DATA)
    assert unsafe["user"] == {"id": 42, "first_name": "QA", "username": "qa_bot"}
    assert unsafe["auth_date"] == "1700000000"
    assert unsafe["hash"] == "deadbeef"


def test_parse_init_data_bad_user_stays_string():
    unsafe = miniapp.parse_init_data("user=not-json&hash=x")
    assert unsafe["user"] == "not-json"


def test_extract_init_data_from_fragment():
    url = "https://app.example/#tgWebAppData=" + \
        urllib.parse.quote(INIT_DATA) + "&tgWebAppVersion=7.0"
    got = miniapp.extract_init_data(url)
    assert miniapp.parse_init_data(got)["hash"] == "deadbeef"


def test_extract_init_data_absent():
    assert miniapp.extract_init_data("https://app.example/#tgWebAppVersion=7.0") is None


def test_build_init_script_installs_webapp():
    unsafe = miniapp.parse_init_data(INIT_DATA)
    js = miniapp.build_init_script(INIT_DATA, unsafe, platform="ios")
    assert "window.Telegram" in js and "WebApp" in js
    assert '"platform": "ios"' in js
    assert INIT_DATA in js                       # raw initData embedded for the SDK
    assert '"id": 42' in js                      # initDataUnsafe.user reachable
    # the UI surface the SDK touches at startup must be present as no-ops
    for member in ("MainButton", "BackButton", "HapticFeedback", "ready", "expand",
                   "showAlert", "openInvoice"):
        assert member in js


def test_build_init_script_is_valid_json_payload():
    unsafe = miniapp.parse_init_data(INIT_DATA)
    js = miniapp.build_init_script(INIT_DATA, unsafe)
    start = js.index("var d = ") + len("var d = ")
    end = js.index(";\n", start)
    payload = json.loads(js[start:end])
    assert payload["initData"] == INIT_DATA
    assert payload["initDataUnsafe"]["user"]["id"] == 42
    assert payload["platform"] == "web"
