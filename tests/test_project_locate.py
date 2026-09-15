"""Exercises the cached project lookup that served calls depend on."""

import json
import threading

from agent_parley import roster


def register(home, key, root):
    directory = home / "projects" / key
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "project.json").write_text(json.dumps({"root": root}))
    return directory


def test_a_repeated_lookup_reads_the_manifests_once(tmp_path, monkeypatch):
    directory = register(tmp_path, "key-a", "/repo/a")
    register(tmp_path, "key-b", "/repo/b")
    scans = []
    original = roster._index_projects

    def counted(home):
        scans.append(home)
        return original(home)

    monkeypatch.setattr(roster, "_index_projects", counted)
    assert roster.locate(tmp_path, "/repo/a") == directory
    assert roster.locate(tmp_path, "/repo/a") == directory
    assert roster.locate(tmp_path, "/repo/a") == directory
    assert len(scans) == 1


def test_a_project_registered_after_a_miss_is_found(tmp_path):
    assert roster.locate(tmp_path, "/repo/late") is None
    directory = register(tmp_path, "key-late", "/repo/late")
    assert roster.locate(tmp_path, "/repo/late") == directory


def test_a_rewritten_manifest_stops_answering_for_the_old_root(tmp_path):
    directory = register(tmp_path, "key-c", "/repo/before")
    assert roster.locate(tmp_path, "/repo/before") == directory
    register(tmp_path, "key-c", "/repo/after-the-rewrite")
    assert roster.locate(tmp_path, "/repo/before") is None
    assert roster.locate(tmp_path, "/repo/after-the-rewrite") == directory


def test_a_removed_project_stops_being_located(tmp_path):
    directory = register(tmp_path, "key-d", "/repo/d")
    assert roster.locate(tmp_path, "/repo/d") == directory
    (directory / "project.json").unlink()
    directory.rmdir()
    assert roster.locate(tmp_path, "/repo/d") is None


def test_two_homes_do_not_answer_for_each_other(tmp_path):
    first = tmp_path / "home-one"
    second = tmp_path / "home-two"
    one = register(first, "key-e", "/repo/e")
    two = register(second, "key-e", "/repo/e")
    assert roster.locate(first, "/repo/e") == one
    assert roster.locate(second, "/repo/e") == two
    assert roster.locate(first, "/repo/e") == one


def test_concurrent_lookups_agree_on_the_directory(tmp_path):
    expected = register(tmp_path, "key-f", "/repo/f")
    for index in range(20):
        register(tmp_path, f"key-noise-{index}", f"/repo/noise-{index}")
    answers = []
    barrier = threading.Barrier(8)

    def lookup():
        barrier.wait()
        answers.append(roster.locate(tmp_path, "/repo/f"))

    threads = [threading.Thread(target=lookup) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert answers == [expected] * 8
