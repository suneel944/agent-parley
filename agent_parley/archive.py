"""Export and import of the coordination state as one archive without secrets.

An archive is a tar file holding a consistent snapshot of the store, taken
through the SQLite backup interface, beside the project manifests, ledgers,
participant records, activity files, retained event and report logs and
attachments of every exported project. The archive manifest names the store
schema, the export time and the SHA-256 digest of every member, so an import
validates each member before it writes anything into the state directory.

Credentials never travel. The registration token a participant presents is
stripped from its identity record, its digest is cleared from the store on
import, credential profiles and native MCP configurations are not exported,
and the manifest states that omission so an operator reading it knows every
imported participant must be launched with `run` to register again.
"""

import contextlib
import datetime
import hashlib
import io
import json
import shutil
import sqlite3
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from agent_parley import attachments, roster, store
from agent_parley.state import BridgeError, lock

FORMAT = 1
MANIFEST = "manifest.json"
STORE_MEMBER = "store/" + store.DATABASE
PROJECTS = "projects"
ATTACHMENTS = attachments.FOLDER
LOCK_TIMEOUT = 2.0
TOKEN_FIELD = "registration_token"
PROJECT_FILES = ("project.json", "issues.json", "cochanges.json")
PARTICIPANT_SUFFIXES = ("-identity.json", "-activity.json", ".jsonl")
ID_COLUMNS = {
    "project_id": "projects",
    "agent_id": "agents",
    "sender_id": "agents",
    "message_id": "messages",
}
TABLES = (
    "projects",
    "agents",
    "messages",
    "message_recipients",
    "file_reservations",
    "reservation_requests",
    "events",
    "participant_presence",
    "idempotent_calls",
    "scheduled_deliveries",
)


def _digest(data: bytes) -> str:
    """Returns the hexadecimal SHA-256 digest of one member's bytes."""
    return hashlib.sha256(data).hexdigest()


def _project_files(directory: Path) -> list[Path]:
    """Lists the exportable files of one project state directory.

    Lane worktrees live inside the project directory, so only named
    top-level records and the attachment tree are collected; locks, temporary
    files and native MCP configurations, which carry the service token, are
    never included.
    """
    files = [
        path
        for path in sorted(directory.iterdir())
        if path.is_file()
        and not path.is_symlink()
        and (
            path.name in PROJECT_FILES
            or path.name.endswith(PARTICIPANT_SUFFIXES)
        )
    ]
    attachments = directory / ATTACHMENTS
    if attachments.is_dir():
        files += [
            path
            for path in sorted(attachments.rglob("*"))
            if path.is_file() and not path.is_symlink()
        ]
    return files


def _stripped_identity(data: bytes) -> bytes:
    """Returns an identity record without its registration token."""
    record = json.loads(data)
    if not isinstance(record, dict):
        raise BridgeError("An identity record is not a JSON object.")
    record.pop(TOKEN_FIELD, None)
    return (json.dumps(record, indent=2) + "\n").encode()


def _project_entry(directory: Path, data: dict) -> dict:
    """Summarizes one project for the archive manifest."""
    issues = directory / "issues.json"
    count = 0
    if issues.exists():
        with contextlib.suppress(OSError, ValueError):
            count = len(json.loads(issues.read_text()).get("issues", {}))
    return {
        "key": directory.name,
        "root": data.get("root", ""),
        "base": data.get("base", ""),
        "issues": count,
        "participants": [
            {
                "name": name,
                "provider": entry.get("provider", ""),
                "lane": entry.get("lane", ""),
                "branch": entry.get("branch", ""),
            }
            for name, entry in sorted(data.get("participants", {}).items())
        ],
    }


@contextlib.contextmanager
def _project_locks(directories: list[Path]) -> Iterator[None]:
    """Holds the setup and ledger lock of every exported project briefly."""
    with contextlib.ExitStack() as stack:
        for directory in directories:
            for name in ("setup.lock", "issues.lock"):
                stack.enter_context(
                    lock(
                        directory / name,
                        f"Another operation holds {name} for "
                        f"{directory.name}; retry the export shortly.",
                        timeout=LOCK_TIMEOUT,
                    )
                )
        yield


def _snapshot(home: Path, destination: Path, roots: set[str]) -> None:
    """Copies the store through the backup interface, scoped to roots."""
    with contextlib.closing(
        sqlite3.connect(home / store.DATABASE, timeout=store.BUSY_TIMEOUT)
    ) as source:
        with contextlib.closing(sqlite3.connect(destination)) as copy:
            source.backup(copy)
    with contextlib.closing(sqlite3.connect(destination)) as copy:
        copy.execute("PRAGMA journal_mode=DELETE")
        kept = [
            int(row[0])
            for row in copy.execute("SELECT id,human_key FROM projects")
            if row[1] in roots
        ]
        placeholders = ",".join("?" for _ in kept) or "-1"
        agents = f"SELECT id FROM agents WHERE project_id IN ({placeholders})"
        for table in reversed(TABLES):
            columns = _columns(copy, table)
            if table == "projects":
                condition = f"id NOT IN ({placeholders})"
            elif "project_id" in columns:
                condition = f"project_id NOT IN ({placeholders})"
            else:
                condition = f"agent_id NOT IN ({agents})"
            copy.execute(f"DELETE FROM {table} WHERE {condition}", kept)
        copy.execute("UPDATE agents SET token_digest=NULL")
        copy.commit()
        copy.execute("VACUUM")


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    """Names the columns of one table in declaration order."""
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})")]


def export(home: Path, output: Path, project: Path | None = None) -> dict:
    """Writes the coordination state, or one project of it, as one archive.

    Args:
        home: Private bridge state root.
        output: Archive path to write; an existing file is refused.
        project: State directory of the only project to export, or None for
            every registered project.

    Returns:
        The archive manifest that was written.

    Raises:
        BridgeError: If the output exists, a project lock stays busy, or the
            store on disk is newer than this build.
        OSError: If the archive cannot be written.
    """
    if output.exists():
        raise BridgeError(f"{output} already exists; choose another path.")
    schema = store.schema_version(home)
    if store.schema_state(schema) == store.SCHEMA_UNSUPPORTED:
        raise BridgeError("Unsupported store schema; use a newer bridge.")
    directories = (
        [project]
        if project is not None
        else sorted(
            path.parent for path in (home / PROJECTS).glob("*/project.json")
        )
    )
    manifests = {}
    for directory in directories:
        if not (directory / "project.json").exists():
            raise BridgeError(f"No registered project at {directory}.")
        manifests[directory] = roster.read(directory)
    files: dict[str, str] = {}
    exported = datetime.datetime.now(datetime.UTC).isoformat()
    with tempfile.TemporaryDirectory(dir=output.parent) as scratch:
        staged = Path(scratch)
        members: list[tuple[str, Path]] = []
        with _project_locks(directories):
            if schema:
                database = staged / store.DATABASE
                _snapshot(
                    home,
                    database,
                    {data["root"] for data in manifests.values()},
                )
                members.append((STORE_MEMBER, database))
            for directory in manifests:
                for path in _project_files(directory):
                    relative = path.relative_to(directory).as_posix()
                    member = f"{PROJECTS}/{directory.name}/{relative}"
                    if path.name.endswith("-identity.json"):
                        copied = staged / directory.name / relative
                        copied.parent.mkdir(parents=True, exist_ok=True)
                        copied.write_bytes(
                            _stripped_identity(path.read_bytes())
                        )
                        members.append((member, copied))
                    else:
                        members.append((member, path))
            providers = home / roster.PROVIDERS
            if providers.is_file():
                members.append((roster.PROVIDERS, providers))
        manifest = {
            "format": FORMAT,
            "schema": schema,
            "exported": exported,
            "credentials": (
                "excluded: registration tokens, credential profiles and "
                "native MCP configurations are never archived; every "
                "imported participant registers again through run"
            ),
            "projects": [
                _project_entry(directory, data)
                for directory, data in manifests.items()
            ],
            "files": files,
        }
        partial = output.with_name(output.name + ".partial")
        try:
            with tarfile.open(partial, "w:gz") as archive:
                for member, path in members:
                    content = path.read_bytes()
                    files[member] = _digest(content)
                    _add(archive, member, content)
                _add(
                    archive,
                    MANIFEST,
                    (json.dumps(manifest, indent=2) + "\n").encode(),
                )
            partial.chmod(0o600)
            partial.replace(output)
        finally:
            partial.unlink(missing_ok=True)
    return manifest


def _add(archive: tarfile.TarFile, member: str, data: bytes) -> None:
    """Appends one regular, private, owner-less member to the archive."""
    info = tarfile.TarInfo(member)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def _safe_name(name: str) -> str:
    """Returns a validated relative member name, or raises."""
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or "\\" in name
        or ".." in path.parts
        or any(part in ("", ".") for part in path.parts)
    ):
        raise BridgeError(f"Refusing archive member {name!r}: unsafe path.")
    return name


def read_manifest(archive: Path) -> dict:
    """Reads and validates the manifest and member table of one archive.

    Every member is checked here rather than while writing: a member that is
    not a regular file, that names an unsafe path, that the manifest does not
    list, or whose bytes do not match the listed digest refuses the whole
    archive before an import touches the state directory.

    Args:
        archive: Archive path written by `export`.

    Returns:
        The archive manifest.

    Raises:
        BridgeError: If the archive is unreadable, unsafe or inconsistent.
    """
    try:
        with tarfile.open(archive, "r:*") as tar:
            members = tar.getmembers()
            names = {member.name for member in members}
            if MANIFEST not in names:
                raise BridgeError(f"{archive} carries no {MANIFEST}.")
            for member in members:
                _safe_name(member.name)
                if not member.isfile():
                    raise BridgeError(
                        f"Refusing archive member {member.name!r}: only "
                        "regular files are restored."
                    )
            stream = tar.extractfile(MANIFEST)
            if stream is None:
                raise BridgeError(f"{MANIFEST} is not readable.")
            manifest = json.loads(stream.read())
            if not isinstance(manifest, dict):
                raise BridgeError(f"{MANIFEST} is not an export manifest.")
            files = manifest.get("files")
            if (
                not isinstance(files, dict)
                or manifest.get("format") != FORMAT
                or not isinstance(manifest.get("schema"), int)
                or not isinstance(manifest.get("projects"), list)
            ):
                raise BridgeError(f"{MANIFEST} is not an export manifest.")
            missing = sorted(set(files) - names)
            unlisted = sorted(names - set(files) - {MANIFEST})
            if missing or unlisted:
                raise BridgeError(
                    "Archive members and manifest disagree: "
                    f"missing {missing}, unlisted {unlisted}."
                )
            for member in members:
                if member.name == MANIFEST:
                    continue
                stream = tar.extractfile(member)
                if stream is None or _digest(stream.read()) != files.get(
                    member.name
                ):
                    raise BridgeError(
                        f"Archive member {member.name!r} does not match its "
                        "manifest digest; the archive is damaged or altered."
                    )
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise BridgeError(f"Cannot read {archive}: {exc}") from exc
    return manifest


def describe(manifest: dict) -> str:
    """Renders what an archive holds without importing it."""
    lines = [
        f"Exported {manifest.get('exported', '?')} at store schema "
        f"{manifest.get('schema', '?')}; credentials excluded.",
    ]
    for project in manifest.get("projects", []):
        names = ", ".join(
            entry.get("name", "?") for entry in project.get("participants", [])
        )
        lines.append(
            f"{project.get('root', '?')} ({project.get('key', '?')}): "
            f"{project.get('issues', 0)} issues; participants: "
            f"{names or 'none'}"
        )
    if not manifest.get("projects"):
        lines.append("No projects.")
    return "\n".join(lines)


def _selected(manifest: dict, project: str | None) -> list[dict]:
    """Returns the manifest projects an import restores."""
    projects = manifest.get("projects", [])
    if project is None:
        return list(projects)
    chosen = [entry for entry in projects if entry.get("root") == project]
    if not chosen:
        raise BridgeError(f"The archive holds no project rooted at {project}.")
    return chosen


def _occupied(home: Path) -> bool:
    """Reports whether the state directory already holds coordination state."""
    return (home / store.DATABASE).exists() or any(
        (home / PROJECTS).glob("*/project.json")
    )


def import_archive(
    home: Path,
    archive: Path,
    project: str | None = None,
    merge: bool = False,
) -> dict:
    """Restores an archive into the state directory.

    The archive is validated in full, extracted into a temporary directory
    inside the state root, migrated there to this build's schema, and moved
    into place only after every project was confirmed absent. A store row
    merge runs in one transaction, so a failure leaves the target as it was
    apart from directories that are removed again on the way out.

    Args:
        home: Private bridge state root.
        archive: Archive path written by `export`.
        project: Canonical root of the only project to restore, or None.
        merge: Whether to add projects beside existing state.

    Returns:
        The restored projects and every participant whose lane path does not
        exist on this machine.

    Raises:
        BridgeError: If the archive is unsafe, newer than this build, the
            target is occupied without `merge`, or a project already exists.
    """
    manifest = read_manifest(archive)
    if manifest["schema"] > store.SCHEMA_VERSION:
        raise BridgeError(
            f"The archive was written at store schema {manifest['schema']}; "
            f"this build reads up to {store.SCHEMA_VERSION}. Use a newer "
            "bridge."
        )
    if _occupied(home) and not merge:
        raise BridgeError(
            f"{home} already holds coordination state; pass --merge to add "
            "projects beside it."
        )
    selected = _selected(manifest, project)
    roots = {entry["root"] for entry in selected}
    (home / PROJECTS).mkdir(parents=True, exist_ok=True, mode=0o700)
    for entry in selected:
        if (home / PROJECTS / entry["key"]).exists():
            raise BridgeError(
                f"Project {entry['root']} already exists here as "
                f"{entry['key']}; remove it first or import elsewhere."
            )
    with tempfile.TemporaryDirectory(dir=home, prefix=".import-") as scratch:
        staged = Path(scratch)
        _extract(archive, manifest, staged)
        _rebind(staged, selected)
        placed = []
        try:
            for entry in selected:
                source = staged / PROJECTS / entry["key"]
                if not (source / "project.json").exists():
                    raise BridgeError(
                        f"The archive lacks a manifest for {entry['root']}."
                    )
                target = home / PROJECTS / entry["key"]
                shutil.move(str(source), str(target))
                placed.append(target)
            _merge_store(home, staged, roots)
            providers = staged / roster.PROVIDERS
            if providers.exists() and not (home / roster.PROVIDERS).exists():
                shutil.move(str(providers), str(home / roster.PROVIDERS))
        except BaseException:
            for target in placed:
                shutil.rmtree(target, ignore_errors=True)
            raise
    missing = [
        {"project": entry["root"], "name": item["name"], "lane": item["lane"]}
        for entry in selected
        for item in entry.get("participants", [])
        if not item.get("lane") or not Path(item["lane"]).is_dir()
    ]
    return {"projects": selected, "missing_lanes": missing}


def _extract(archive: Path, manifest: dict, staged: Path) -> None:
    """Writes every validated member below the staging directory."""
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            if member.name == MANIFEST:
                continue
            stream = tar.extractfile(member)
            if stream is None:
                raise BridgeError(f"Cannot read member {member.name!r}.")
            data = stream.read()
            if _digest(data) != manifest["files"].get(member.name):
                raise BridgeError(
                    f"Archive member {member.name!r} changed while reading."
                )
            target = staged / _safe_name(member.name)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(data)
            target.chmod(0o600)


def _rebind(staged: Path, selected: list[dict]) -> None:
    """Strips every credential the staged state could still carry."""
    for entry in selected:
        directory = staged / PROJECTS / entry["key"]
        if not directory.is_dir():
            continue
        for path in directory.glob("*-identity.json"):
            path.write_bytes(_stripped_identity(path.read_bytes()))
        for path in directory.glob("*-mcp.json"):
            path.unlink()
    database = staged / STORE_MEMBER
    if database.exists():
        store.initialize(database.parent)
        with contextlib.closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE agents SET token_digest=NULL")
            db.commit()


def _merge_store(home: Path, staged: Path, roots: set[str]) -> None:
    """Copies the selected projects' store rows into the local store."""
    database = staged / STORE_MEMBER
    if not database.exists():
        return
    store.initialize(home)
    with contextlib.closing(sqlite3.connect(database)) as source:
        source.row_factory = sqlite3.Row
        with store.connect(home, write=True) as target:
            for root in sorted(roots):
                if target.execute(
                    "SELECT 1 FROM projects WHERE human_key=?", (root,)
                ).fetchone():
                    raise BridgeError(
                        f"The store already knows project {root}; the "
                        "import was rolled back."
                    )
            maps: dict[str, dict[int, int]] = {name: {} for name in TABLES}
            for table in TABLES:
                columns = _columns(source, table)
                rows = source.execute(f"SELECT * FROM {table}").fetchall()
                for row in rows:
                    if table == "projects" and row["human_key"] not in roots:
                        continue
                    values = _remapped(row, columns, maps)
                    if values is None:
                        continue
                    names = ",".join(values)
                    placeholders = ",".join("?" for _ in values)
                    cursor = target.execute(
                        f"INSERT INTO {table} ({names}) VALUES "
                        f"({placeholders})",
                        list(values.values()),
                    )
                    if "id" in columns:
                        maps[table][row["id"]] = int(cursor.lastrowid or 0)


def _remapped(
    row: sqlite3.Row, columns: list[str], maps: dict[str, dict[int, int]]
) -> dict[str, object] | None:
    """Rewrites a row's foreign keys to the identifiers the target assigned."""
    values: dict[str, object] = {}
    for column in columns:
        value = row[column]
        if column == "id":
            continue
        if column in ID_COLUMNS:
            mapped = maps[ID_COLUMNS[column]].get(value)
            if mapped is None:
                return None
            value = mapped
        if column == "token_digest":
            value = None
        values[column] = value
    return values
