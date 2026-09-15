"""Checks the WSL refusal, the platform report and pidfd availability."""

from pathlib import Path

import pytest

from agent_parley import process, protocol, store, views
from agent_parley.state import BridgeError

WSL2 = "5.15.153.1-microsoft-standard-WSL2"
WSL1 = "4.4.0-19041-Microsoft"
LINUX = "6.8.0-139-generic"


def test_a_mounted_drive_is_refused_on_wsl():
    with pytest.raises(BridgeError) as caught:
        process.check_repository_host(Path("/mnt/c/Users/dev/repo"), WSL2)
    assert str(caught.value) == process.MOUNTED_DRIVE
    assert "Linux file system" in str(caught.value)


def test_run_refuses_a_mounted_drive_before_touching_git(bridge, monkeypatch):
    monkeypatch.setattr(process, "kernel_release", lambda: WSL2)
    with pytest.raises(BridgeError, match="mounted Windows drive"):
        bridge.launch("dev", Path("/mnt/c/Users/dev/repo"), "task")
    assert not (bridge.home / "projects").exists()


def test_the_same_path_is_allowed_outside_wsl():
    assert process.check_repository_host(Path("/mnt/c/repo"), LINUX) is None
    assert process.check_repository_host(Path("/mnt/c/repo"), "") is None


def test_a_linux_file_system_path_is_allowed_on_wsl(tmp_path):
    assert process.check_repository_host(tmp_path, WSL2) is None
    assert process.check_repository_host(tmp_path, WSL1) is None


@pytest.mark.parametrize(
    ("release", "wsl"),
    [(WSL2, "2"), (WSL1, "1"), (LINUX, "none"), ("", "none")],
)
def test_the_wsl_generation_follows_the_kernel_release(release, wsl):
    reported = process.host_report(release)
    assert reported["kernel"] == release
    assert reported["wsl"] == wsl
    assert isinstance(reported["pidfd_open"], bool)


def test_a_missing_release_file_reads_as_no_kernel(tmp_path):
    assert process.kernel_release(tmp_path / "osrelease") == ""
    assert process.wsl_version("5.15-MICROSOFT-standard") == "1"


@pytest.mark.parametrize(
    ("release", "wsl"), [(WSL2, "2"), (WSL1, "1"), (LINUX, "none")]
)
def test_doctor_reports_the_platform(bridge, monkeypatch, release, wsl):
    monkeypatch.setattr(process, "kernel_release", lambda: release)
    store.initialize(bridge.home)
    reported = bridge.doctor()
    assert reported["platform"]["kernel"] == release
    assert reported["platform"]["wsl"] == wsl
    assert isinstance(reported["platform"]["pidfd_open"], bool)
    assert views.doctor(reported)["platform"] == reported["platform"]
    text = protocol.render(reported)
    assert f"kernel {release}, wsl {wsl}, pidfd_open" in text
