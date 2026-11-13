"""Checks the notice a lane gets when a merge alters a symbol it calls."""

from pathlib import Path

from agent_parley import store, supervision
from agent_parley.cli import git

AUTHOR = (
    "-c",
    "user.name=Bridge Test",
    "-c",
    "user.email=test@example.com",
)


def commit(checkout, name, text):
    """Records one commit in a checkout, advancing the branch it is on."""
    path = Path(checkout) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(Path(checkout), "add", name)
    git(Path(checkout), *AUTHOR, "commit", "-m", f"change {name}")


def actor(bridge, root, name):
    """Registers one lane and resolves its own store identity."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    resolved = store.authenticate(bridge.home, token)
    assert resolved is not None
    return resolved


def inbox(bridge, lane):
    """Reads one lane's own mail with bodies."""
    return store.call(
        bridge.home, lane, "fetch_inbox", {"include_bodies": True}
    )["messages"]


def test_a_merged_rename_mails_the_lane_whose_diff_calls_the_old_name(
    bridge, repo, paired
):
    claude = actor(bridge, paired["root"], "claude")
    codex = actor(bridge, paired["root"], "codex")
    base = git(Path(repo), "symbolic-ref", "--short", "HEAD").strip()
    commit(repo, "auth.py", "def login(user):\n    return user\n")
    merger = paired["lanes"]["codex"]
    git(Path(merger), *AUTHOR, "merge", "--ff-only", base)
    commit(merger, "auth.py", "def sign_in(user):\n    return user\n")
    commit(
        paired["lanes"]["claude"],
        "app.py",
        "from auth import login\n\n\nprint(login('me'))\n",
    )
    branch = paired["participants"]["codex"]["branch"]
    git(Path(repo), *AUTHOR, "merge", "--no-ff", "-m", "merge", branch)
    merged = git(Path(repo), "rev-parse", "HEAD").strip()
    directory = Path(paired["lanes"]["claude"]).parent
    ended = {
        "7": {
            "branch": branch,
            "state": "MERGED",
            "pull_request": 12,
            "url": "https://forge.example/pull/12",
            "commit": merged,
            "claim_id": "claim",
        }
    }
    sent = supervision.symbol_notices(bridge.home, directory, paired, ended)
    assert sent == [("claude", "login")]
    assert supervision.symbol_notices(
        bridge.home, directory, paired, ended
    ) == ([])
    messages = inbox(bridge, claude)
    assert len(messages) == 1
    assert messages[0]["subject"] == "Merged #12 may affect login in your diff"
    body = messages[0]["body_md"]
    assert "Pull request #12 https://forge.example/pull/12" in body
    assert "definition of login in auth.py" in body
    assert "names it at app.py:1" in body
    assert "may affect your work" in body
    assert inbox(bridge, codex) == []


def test_python_definitions_change_by_signature_not_by_body():
    before = supervision._signatures(
        "class Session(Base):\n"
        "    def login(self, user):\n        return 1\n"
        "def helper():\n    def local():\n        pass\n"
    )
    after = supervision._signatures(
        "class Session(Base):\n"
        "    def login(self, user, token):\n        return 1\n"
        "def helper():\n    return 2\n"
    )
    assert sorted(before) == ["Session", "Session.login", "helper"]
    assert before["Session"] == after["Session"]
    assert before["helper"] == after["helper"]
    assert before["Session.login"] != after["Session.login"]
    assert supervision._signatures("def broken(:\n") == {}


def test_other_languages_use_one_top_level_pattern_per_extension():
    patterns = supervision.TOP_LEVEL_DEFINITIONS
    assert patterns[".ts"].match("export async function fetchUser(id) {")[
        1
    ] == ("fetchUser")
    assert patterns[".go"].match("func (s *Server) Serve() error {")[1] == (
        "Serve"
    )
    assert patterns[".rs"].match("pub(crate) fn parse(input: &str) {")[1] == (
        "parse"
    )
    assert ".rb" not in patterns
