import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runners"))
import registry  # noqa: E402


@pytest.fixture
def reg(tmp_path, monkeypatch):
    """Isolated registry + XDG home."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    reg_file = tmp_path / "projects.json"
    monkeypatch.setenv("TGQA_REGISTRY", str(reg_file))

    def write(entries):
        reg_file.write_text(json.dumps(entries))
        return reg_file
    return write


PROJ = {
    "alias": "demo", "path": "/tmp/demo", "bot": "@demo_bot",
    "api_id": 42, "api_hash": "hh",
    "roles": [
        {"name": "default", "session": "main", "default": True},
        {"name": "admin", "session": "~/mysessions/admin.session"},
        {"name": "bare", "session": "reader"},
    ],
}


def test_registry_env_override(reg):
    path = reg([PROJ])
    assert registry.registry_path() == path
    assert registry.registry_write_path() == path


def test_find_project_by_alias_and_missing(reg):
    reg([PROJ])
    assert registry.find_project("demo")["bot"] == "@demo_bot"
    with pytest.raises(SystemExit):
        registry.find_project("nope")


def test_find_project_single_real_entry_fallback(reg):
    reg([{"alias": "default", "path": "*"}, PROJ])
    assert registry.find_project(None)["alias"] == "demo"


def test_session_path_bare_name_goes_to_xdg(reg, monkeypatch):
    p = registry.session_path("main")
    assert p == registry.sessions_dir() / "main.session"


def test_session_path_explicit_path_used_as_is():
    p = registry.session_path("/opt/x/qa_session.session")
    assert p == Path("/opt/x/qa_session.session")


def test_session_path_expands_home_and_adds_suffix():
    p = registry.session_path("~/mysessions/admin")
    assert str(p).endswith("/mysessions/admin.session")
    assert "~" not in str(p)


def test_resolve_session_default_and_named(reg):
    reg([PROJ])
    proj = registry.find_project("demo")
    assert registry.resolve_session(proj) == registry.sessions_dir() / "main.session"
    assert registry.resolve_session(proj, "bare") == registry.sessions_dir() / "reader.session"
    assert str(registry.resolve_session(proj, "admin")).endswith("admin.session")
    with pytest.raises(SystemExit):
        registry.resolve_session(proj, "ghost")


def test_resolve_session_no_roles(reg):
    reg([{"alias": "demo", "path": "/tmp/demo"}])
    with pytest.raises(SystemExit):
        registry.resolve_session(registry.find_project("demo"))


def test_resolve_api_env_wins(reg, monkeypatch):
    reg([PROJ])
    proj = registry.find_project("demo")
    assert registry.resolve_api(proj) == (42, "hh")
    monkeypatch.setenv("TGQA_API_ID", "7")
    monkeypatch.setenv("TGQA_API_HASH", "envhash")
    assert registry.resolve_api(proj) == (7, "envhash")


def test_resolve_api_missing(reg):
    reg([{"alias": "demo", "path": "/tmp/demo"}])
    with pytest.raises(SystemExit):
        registry.resolve_api(registry.find_project("demo"))


def test_load_config_reads_project_dir(reg, tmp_path):
    proj_dir = tmp_path / "app"
    (proj_dir / ".tg-qa").mkdir(parents=True)
    (proj_dir / ".tg-qa" / "config.json").write_text(json.dumps({"reply_timeout": 20}))
    reg([{"alias": "demo", "path": str(proj_dir)}])
    assert registry.load_config(registry.find_project("demo")) == {"reply_timeout": 20}


def test_load_config_absent_is_empty(reg):
    reg([PROJ])
    assert registry.load_config(registry.find_project("demo")) == {}
