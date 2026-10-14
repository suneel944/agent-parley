"""Checks the host load reading, the launch refusal and the doctor line."""

import pytest

from agent_parley import (
    cli,
    process,
    protocol,
    refusal,
    roster,
    store,
    supervision,
)
from agent_parley.process import host_load as read_host
from agent_parley.state import BridgeError

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                             90000.
Pages inactive:                           50000.
Pages speculative:                         4000.
Pages wired down:                         30000.
"""


def test_linux_reads_mem_available_in_mib(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16384000 kB\nMemAvailable:    2097152 kB\n"
    )
    assert process.linux_available_mib(meminfo) == 2048


@pytest.mark.parametrize("text", [None, "MemTotal: 1 kB\n", "MemAvailable:\n"])
def test_unreadable_linux_memory_reads_as_none(tmp_path, text):
    meminfo = tmp_path / "meminfo"
    if text is not None:
        meminfo.write_text(text)
    assert process.linux_available_mib(meminfo) is None


def test_macos_counts_free_inactive_and_speculative_pages():
    assert process.darwin_available_mib(VM_STAT) == 64000 * 16384 // 2**20


@pytest.mark.parametrize(
    "output",
    ["", "Pages free: 1.\n", VM_STAT.replace("Pages inactive", "Pages gone")],
)
def test_unreadable_vm_stat_reads_as_none(output):
    assert process.darwin_available_mib(output) is None


def test_a_failing_vm_stat_reads_as_none(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("vm_stat")

    monkeypatch.setattr(process.subprocess, "run", missing)
    assert process.darwin_available_mib() is None


def test_the_reading_divides_the_load_by_the_cpu_count(monkeypatch):
    monkeypatch.setattr(process.os, "getloadavg", lambda: (12.0, 1.0, 1.0))
    monkeypatch.setattr(process.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(process, "linux_available_mib", lambda: 4096)
    assert read_host("linux") == process.HostLoad(1.5, 4096)


def test_an_unreadable_load_average_reads_as_none(monkeypatch):
    def unavailable():
        raise OSError("no load average")

    monkeypatch.setattr(process.os, "getloadavg", unavailable)
    monkeypatch.setattr(process, "darwin_available_mib", lambda: None)
    assert read_host("darwin") == process.HostLoad(None, None)


def test_windows_skips_the_reading_with_one_line():
    reading = read_host("win32")
    assert reading == process.HostLoad(None, None, process.LOAD_SKIPPED)
    assert "\n" not in reading.skipped


@pytest.mark.parametrize(
    ("reading", "named"),
    [
        (process.HostLoad(1.5, 1024), []),
        (process.HostLoad(None, None), []),
        (process.HostLoad(1.51, 4096), ["max_load_ratio 1.5"]),
        (process.HostLoad(0.2, 1023), ["min_available_mib 1024"]),
        (
            process.HostLoad(3.0, 10),
            ["max_load_ratio 1.5", "min_available_mib 1024"],
        ),
    ],
)
def test_only_a_readable_value_past_its_threshold_is_named(reading, named):
    cause = process.overloaded(reading, 1.5, 1024)
    assert bool(cause) is bool(named)
    for setting in named:
        assert setting in cause


@pytest.mark.parametrize(
    "value",
    [
        {"max_load_ratio": 0},
        {"max_load_ratio": True},
        {"max_load_ratio": 1001},
        {"min_available_mib": -1},
        {"min_available_mib": 1.5},
    ],
)
def test_an_invalid_threshold_is_refused(value):
    with pytest.raises(BridgeError):
        supervision.settings(value)


def test_the_thresholds_default_to_the_documented_values():
    resolved = supervision.settings({})
    assert resolved["max_load_ratio"] == 1.5
    assert resolved["min_available_mib"] == 1024


def test_a_launch_past_the_threshold_is_refused_with_the_numbers(
    bridge, repo, paired, monkeypatch
):
    monkeypatch.setattr(
        process, "host_load", lambda: process.HostLoad(2.25, 512)
    )
    with pytest.raises(BridgeError) as raised:
        bridge.launch("claude", repo, "continue")
    text = refusal(raised.value)
    assert "load ratio 2.25 per CPU is above max_load_ratio 1.5" in text
    assert "512 MiB available is below min_available_mib 1024" in text
    assert text.splitlines()[-1] == "next: agent-parley doctor"


@pytest.mark.parametrize(
    "reading",
    [
        process.HostLoad(0.5, 8192),
        process.HostLoad(None, None),
        process.HostLoad(None, None, process.LOAD_SKIPPED),
    ],
)
def test_a_launch_under_the_threshold_or_unread_goes_ahead(
    bridge, repo, paired, monkeypatch, capsys, reading
):
    monkeypatch.setattr(process, "host_load", lambda: reading)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(BridgeError, match="Install and sign in"):
        bridge.launch("claude", repo, "continue")
    assert (process.LOAD_SKIPPED in capsys.readouterr().err) is bool(
        reading.skipped
    )


def test_a_project_threshold_moves_the_launch_gate(
    bridge, repo, paired, monkeypatch
):
    directory = bridge.project(repo, create=False)[1]
    manifest = roster.read(directory)
    manifest["supervision"] = {"max_load_ratio": 4}
    cli.write_json(directory / "project.json", manifest)
    monkeypatch.setattr(
        process, "host_load", lambda: process.HostLoad(3.0, 8192)
    )
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(BridgeError, match="Install and sign in"):
        bridge.launch("claude", repo, "continue")


@pytest.mark.parametrize(
    ("reading", "state"),
    [
        (
            process.HostLoad(0.5, 8192),
            "load ratio 0.5 of at most 1.5, 8192 MiB available of at least "
            "1024; under the launch threshold",
        ),
        (
            process.HostLoad(2.0, 8192),
            "load ratio 2.0 of at most 1.5, 8192 MiB available of at least "
            "1024; over the launch threshold, so run refuses and dispatch "
            "defers",
        ),
        (
            process.HostLoad(None, None),
            "load ratio unreadable of at most 1.5, unreadable MiB available "
            "of at least 1024; under the launch threshold",
        ),
        (process.HostLoad(None, None, process.LOAD_SKIPPED), None),
    ],
)
def test_doctor_prints_the_reading_against_the_threshold(
    bridge, monkeypatch, reading, state
):
    store.initialize(bridge.home)
    monkeypatch.setattr(process, "host_load", lambda: reading)
    reported = bridge.doctor()
    [entry] = [
        entry
        for entry in reported["components"]
        if entry["component"] == "host load"
    ]
    assert entry["state"] == (state or process.LOAD_SKIPPED)
    assert entry["compatible"] is True
    assert entry["state"] in protocol.render(reported)
