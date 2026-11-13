import json
import os
import sys
import urllib.error

import pytest

from agent_parley import cli, updates

DAY = updates.INTERVAL
UV = "uv tool upgrade agent-parley"


class Index:
    def __init__(self, answer="0.23.0"):
        self.answer = answer
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def notice(home, index, *, installed="0.22.0", now=1000.0, environ=None):
    return updates.notice(
        home,
        installed=installed,
        now=now,
        fetch=index,
        environ={} if environ is None else environ,
        command=UV,
    )


def test_an_older_launcher_prints_the_versions_and_the_upgrade(tmp_path):
    assert notice(tmp_path, Index("0.23.0")) == (
        "agent-parley 0.22.0 is installed; 0.23.0 is available: "
        "uv tool upgrade agent-parley"
    )


def test_an_equal_launcher_prints_nothing(tmp_path):
    assert notice(tmp_path, Index("0.22.0")) == ""


def test_a_newer_launcher_prints_nothing(tmp_path):
    assert notice(tmp_path, Index("0.21.3")) == ""


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("unreachable"),
        TimeoutError("timed out"),
        ValueError("not JSON"),
    ],
)
def test_an_unreachable_index_prints_nothing_and_is_cached(tmp_path, failure):
    index = Index(failure)
    assert notice(tmp_path, index) == ""
    assert notice(tmp_path, index, now=1000.0 + DAY - 1) == ""
    assert index.calls == 1
    cached = json.loads((tmp_path / updates.CACHE).read_text())
    assert cached == {"checked": 1000.0, "latest": ""}


def test_the_index_is_asked_at_most_once_a_day(tmp_path):
    index = Index("0.23.0")
    assert notice(tmp_path, index)
    index.answer = "0.24.0"
    assert "0.23.0 is available" in notice(
        tmp_path, index, now=1000.0 + DAY - 1
    )
    assert index.calls == 1


def test_an_expired_cache_asks_the_index_again(tmp_path):
    index = Index("0.23.0")
    assert notice(tmp_path, index)
    index.answer = "0.24.0"
    assert "0.24.0 is available" in notice(tmp_path, index, now=1000.0 + DAY)
    assert index.calls == 2


def test_a_cache_from_the_future_is_treated_as_expired(tmp_path):
    index = Index("0.23.0")
    assert notice(tmp_path, index, now=5000.0)
    assert notice(tmp_path, index, now=1000.0)
    assert index.calls == 2


def test_a_corrupt_cache_is_replaced(tmp_path):
    (tmp_path / updates.CACHE).write_text("{not json")
    index = Index("0.23.0")
    assert notice(tmp_path, index)
    assert index.calls == 1


def test_a_missing_state_root_never_fails_the_check(tmp_path):
    index = Index("0.23.0")
    assert notice(tmp_path / "absent", index)
    assert index.calls == 1


@pytest.mark.parametrize("value", ["1", "true"])
def test_the_environment_switch_disables_the_check(tmp_path, value):
    index = Index("0.23.0")
    environ = {updates.DISABLE: value}
    assert notice(tmp_path, index, environ=environ) == ""
    assert index.calls == 0
    assert not (tmp_path / updates.CACHE).exists()


@pytest.mark.parametrize(
    ("installed", "latest", "result"),
    [
        ("0.22.0", "0.23.0", True),
        ("0.9.0", "0.10.0", True),
        ("1.0", "1.0.0", False),
        ("0.23.0rc1", "0.23.0", False),
        ("v0.22.0", "0.22.1", True),
        ("0.22.0", "", False),
        ("0.22.0", "garbage", False),
        ("0.23.0", "0.22.9", False),
    ],
)
def test_versions_compare_by_numeric_release(installed, latest, result):
    assert updates.older(installed, latest) is result


def test_a_uv_tool_environment_names_uv(tmp_path):
    (tmp_path / "uv-receipt.toml").write_text("")
    assert updates.upgrade_command(str(tmp_path), "/usr/bin/python3") == UV
    assert (
        updates.upgrade_command(
            "/home/u/.local/share/uv/tools/agent-parley", "/x/python"
        )
        == UV
    )


def test_a_pipx_environment_names_pipx():
    assert (
        updates.upgrade_command(
            "/home/u/.local/pipx/venvs/agent-parley", "/x/python"
        )
        == "pipx upgrade agent-parley"
    )


def test_any_other_environment_names_pip(tmp_path):
    assert (
        updates.upgrade_command(str(tmp_path), "/usr/bin/python3")
        == "pip install -U agent-parley"
    )


def test_the_index_answer_is_read_from_info_version(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"info": {"version": "0.30.1"}}).encode()

    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert updates.fetch_latest() == "0.30.1"
    assert seen == {"url": updates.INDEX, "timeout": 2.0}


def test_an_index_answer_without_a_version_is_a_failure(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"info": {}}'

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout: Response()
    )
    with pytest.raises(ValueError):
        updates.fetch_latest()


def test_doctor_prints_the_notice_on_standard_error(
    bridge, repo, paired, monkeypatch, capsys
):
    monkeypatch.delenv(updates.DISABLE)
    monkeypatch.setattr(updates, "fetch_latest", Index("999.0.0"))
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), "doctor"]
    )
    cli.main()
    captured = capsys.readouterr()
    assert "999.0.0 is available" in captured.err
    assert "999.0.0" not in captured.out


def test_bare_status_prints_the_notice_on_standard_error(
    bridge, repo, paired, monkeypatch, capsys
):
    monkeypatch.delenv(updates.DISABLE)
    monkeypatch.setattr(updates, "fetch_latest", Index("999.0.0"))
    monkeypatch.setenv("AGENT_PARLEY_HOME", str(bridge.home))
    monkeypatch.setattr(sys, "argv", ["agent-parley", "status"])
    monkeypatch.chdir(repo)
    assert cli.main() == 0
    assert "999.0.0 is available" in capsys.readouterr().err


def test_parsed_status_prints_the_notice_once_a_day(
    bridge, repo, paired, monkeypatch, capsys
):
    index = Index("999.0.0")
    monkeypatch.delenv(updates.DISABLE)
    monkeypatch.setattr(updates, "fetch_latest", index)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "status", "--json"],
    )
    monkeypatch.chdir(repo)
    cli.main()
    cli.main()
    captured = capsys.readouterr()
    assert captured.err.count("999.0.0 is available") == 2
    assert index.calls == 1


def test_the_suite_keeps_the_check_off():
    assert os.environ.get(updates.DISABLE) == "1"
