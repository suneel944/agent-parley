import shutil
import subprocess
from pathlib import Path

import pytest

from agent_parley.entry import WINDOWS_REFUSAL

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"
SYSTEM = "/usr/bin:/bin"

UV = """#!/bin/sh
printf 'uv %s\\n' "$*" >> "$STUB_LOG"
case "$*" in
    "tool list")
        [ -f "$STUB_STATE/installed" ] && printf 'agent-parley v0\\n' ;;
    "tool install agent-parley" | "tool install --force "*)
        touch "$STUB_STATE/installed" ;;
    "tool dir --bin") printf '%s\\n' "$STUB_BIN" ;;
esac
exit 0
"""

TOOL = """#!/bin/sh
printf '{name} %s\\n' "$*" >> "$STUB_LOG"
exit 0
"""


def stub(directory, name, text):
    path = directory / name
    path.write_text(text)
    path.chmod(0o755)


def build_machine(tmp_path, clis=("agent-parley", "claude")):
    home = tmp_path / "home"
    binary = tmp_path / "bin"
    state = tmp_path / "state"
    for directory in (home, binary, state):
        directory.mkdir()
    for name in clis:
        stub(binary, name, TOOL.format(name=name))
    environment = {
        "HOME": str(home),
        "PATH": f"{binary}:{SYSTEM}",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "STUB_STATE": str(state),
        "STUB_BIN": str(binary),
    }

    def install(**extra):
        return subprocess.run(
            ["sh", str(SCRIPT)],
            env=environment | extra,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls():
        return (tmp_path / "calls.log").read_text().splitlines()

    return install, calls, binary, home


@pytest.fixture
def machine(tmp_path):
    return build_machine(tmp_path)


def test_install_then_rerun_force_installs_the_latest(machine):
    install, calls, binary, home = machine
    stub(binary, "uv", UV)
    first = install()
    assert first.returncode == 0, first.stderr
    assert first.stdout.splitlines()[-1] == (
        "Next: cd your-repo && agent-parley run claude"
    )
    assert calls() == [
        "uv tool list",
        "uv tool install agent-parley",
        "uv tool dir --bin",
        "agent-parley --version",
        "agent-parley plugins install",
        "agent-parley doctor",
    ]
    second = install()
    assert second.returncode == 0, second.stderr
    assert "uv tool install --force agent-parley" in calls()
    assert not any(call.startswith("uv tool upgrade") for call in calls())
    assert calls().count("agent-parley plugins install") == 2
    assert "export PATH" not in first.stdout + second.stdout
    assert list(home.iterdir()) == []


def test_a_spec_override_force_installs_on_every_run(machine, tmp_path):
    install, calls, binary, _ = machine
    stub(binary, "uv", UV)
    wheel = str(tmp_path / "dist" / "agent_parley-0-py3-none-any.whl")
    for _ in range(2):
        result = install(AGENT_PARLEY_SPEC=wheel)
        assert result.returncode == 0, result.stderr
        assert f"Installing agent-parley from {wheel}" in result.stdout
    assert calls().count(f"uv tool install --force {wheel}") == 2
    assert not any(
        call in ("uv tool list", "uv tool install agent-parley")
        or call.startswith("uv tool upgrade")
        for call in calls()
    )
    assert calls().count("agent-parley plugins install") == 2


def test_missing_uv_uses_the_official_installer_without_rc_edits(machine):
    if shutil.which("uv", path=SYSTEM):
        pytest.skip("a system uv would shadow the missing one")
    install, calls, binary, home = machine
    installer = (
        "#!/bin/sh\n"
        'printf \'curl %s\\n\' "$*" >> "$STUB_LOG"\n'
        "cat <<'EOF'\n"
        'printf \'modify %s\\n\' "$UV_NO_MODIFY_PATH" >> "$STUB_LOG"\n'
        'mkdir -p "$HOME/.local/bin"\n'
        'cp "$STUB_BIN/uv.pending" "$HOME/.local/bin/uv"\n'
        "EOF\n"
    )
    stub(binary, "curl", installer)
    stub(binary, "uv.pending", UV)
    result = install()
    assert result.returncode == 0, result.stderr
    assert calls()[:2] == [
        "curl -LsSf https://astral.sh/uv/install.sh",
        "modify 1",
    ]
    assert f'export PATH="{home}/.local/bin:$PATH"' in result.stdout
    assert [path.name for path in home.iterdir()] == [".local"]


def test_native_windows_is_refused_with_the_wsl2_pointer(machine):
    install, _, binary, _ = machine
    stub(binary, "uname", "#!/bin/sh\necho MINGW64_NT-10.0\n")
    result = install()
    assert result.returncode == 2
    assert result.stderr.strip() == WINDOWS_REFUSAL


def test_next_command_prefers_codex_when_claude_is_missing(tmp_path):
    install, _, binary, _ = build_machine(
        tmp_path, clis=("agent-parley", "codex")
    )
    stub(binary, "uv", UV)
    result = install()
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == (
        "Next: cd your-repo && agent-parley run codex"
    )


def test_next_command_names_both_clis_when_neither_is_installed(tmp_path):
    install, _, binary, _ = build_machine(tmp_path, clis=("agent-parley",))
    stub(binary, "uv", UV)
    result = install()
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "agent-parley run claude" not in result.stdout
    assert "agent-parley run codex" not in result.stdout
    assert "install claude or codex" in lines[-2]
    assert lines[-1] == "No CLI yet? Try: agent-parley demo"
