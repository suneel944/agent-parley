"""Exports and verifies a hash-chained, optionally signed audit bundle.

An operator answering "what did the lanes do between these two releases"
needs one artifact a third party can check, not a set of live state files
that keep changing. `export` reads what coordination already keeps, through
the modules that own each record, and writes one JSON document: the lanes
with their provider and model, the issues resolved, claims and handoffs,
ready reports with their evidence, review rounds with their findings count,
and the pull requests and commits each lane produced inside the window.

Every record carries the SHA-256 of its canonical JSON (sorted keys, no
insignificant whitespace) together with the hash of the record before it,
so changing, removing or reordering one record breaks every hash after it,
and `verify` names the first record that no longer matches. The chain shows
the bundle is internally consistent; only a signature shows who exported it.
Signing uses the operator's existing SSH key through `ssh-keygen -Y sign`
under `NAMESPACE`, and `verify` checks it with `ssh-keygen -Y
check-novalidate`, which needs no allowed-signers file and reports the
signing key's fingerprint for the operator to compare.

Reading is the whole contract on the state side: the store is opened
read-only by `history`, no lock is taken, and the only file written is the
one the operator names. Credentials, tokens and prompt transcripts are
excluded: lanes are copied field by field, and every record is passed
through `scrub`, which drops any key named like a secret or a transcript
and withholds any text that claims assistant authorship.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from agent_parley import history, issues, metrics, policy, reviews, supervision
from agent_parley.state import BridgeError, write_text
from agent_parley.worktrees import git

FORMAT = 1
KIND = "agent-parley-audit"
NAMESPACE = "agent-parley-audit"
GENESIS = "0" * 64
RESOLVED_ACTIONS = frozenset({"resolve", "complete"})
LANE_FIELDS = ("provider", "model", "effort", "branch")
SECRET_KEY = re.compile(
    r"(token|secret|password|passphrase|passcode|credential|api_?key|"
    r"private_?key|authorization|cookie|transcript|prompt)s?$",
    re.IGNORECASE,
)
WITHHELD = "[withheld: assistant attribution]"
SIGN_SECONDS = 120


def scrub(value: object) -> object:
    """Removes credential, token and transcript fields from one record.

    A key whose name ends like a secret or a transcript (``token``,
    ``registration_token``, ``password``, ``credential``, ``api_key``,
    ``handoff_prompt`` and the like) is dropped with its value, at any
    depth. A numeric value under such a key is kept, because it is a count,
    such as the ``tokens`` a claim used, and cannot carry a secret. A string
    that claims assistant authorship under `policy` is withheld.

    Args:
        value: A JSON-compatible value.

    Returns:
        The value without the excluded fields.
    """
    if isinstance(value, dict):
        return {
            str(key): scrub(item)
            for key, item in value.items()
            if not SECRET_KEY.search(str(key)) or isinstance(item, int | float)
        }
    if isinstance(value, list | tuple):
        return [scrub(item) for item in value]
    if isinstance(value, str) and policy.has_attribution(value):
        return WITHHELD
    return value


def canonical(value: object) -> bytes:
    """Serializes a value the one way its hash and signature are taken over.

    Args:
        value: A JSON-compatible value.

    Returns:
        UTF-8 JSON with sorted keys and no insignificant whitespace.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(index: int, kind: str, data: object, previous: str) -> str:
    """Hashes one record's content together with the hash before it."""
    body = {"index": index, "kind": kind, "data": data, "previous": previous}
    return hashlib.sha256(canonical(body)).hexdigest()


def chain(entries: list[tuple[str, object]]) -> list[dict]:
    """Links records into a hash chain in the order given.

    Args:
        entries: Record kind and scrubbed data, in bundle order.

    Returns:
        One record per entry with its index, kind, data, the previous
        record's hash (`GENESIS` for the first) and its own hash.
    """
    linked = []
    previous = GENESIS
    for index, (kind, data) in enumerate(entries):
        digest = _digest(index, kind, data, previous)
        linked.append(
            {
                "index": index,
                "kind": kind,
                "data": data,
                "previous": previous,
                "hash": digest,
            }
        )
        previous = digest
    return linked


def commit_time(root: Path, ref: str) -> float:
    """Resolves a Git commit-ish of the project to its committer time.

    Args:
        root: Base checkout of the project.
        ref: Tag, branch or commit naming one commit.

    Returns:
        The commit's committer time as a Unix time.

    Raises:
        BridgeError: If the text names no commit in the repository.
    """
    if not ref or ref.startswith("-"):
        raise BridgeError(f"{ref!r} is not an instant, a window or a commit.")
    try:
        stamp = git(
            root, "log", "-1", "--format=%ct", f"{ref}^{{commit}}", "--"
        )
    except (BridgeError, subprocess.TimeoutExpired):
        stamp = ""
    if not stamp.isdigit():
        raise BridgeError(
            f"{ref!r} is not an instant such as 2026-10-15T09:01:00Z, a "
            "window such as 7d, or a commit of this repository."
        )
    return float(stamp)


def _inside(at: object, since: float, until: float) -> bool:
    """Reports whether a recorded time falls inside the window."""
    try:
        moment = float(str(at or 0))
    except (TypeError, ValueError):
        return False
    return since <= moment <= until


def _lanes(manifest: dict) -> list[tuple[str, dict]]:
    """Copies each lane's name, provider, model, effort and branch only."""
    return [
        (
            "lane",
            {
                "name": name,
                **{field: str(entry.get(field) or "") for field in LANE_FIELDS},
            },
        )
        for name, entry in sorted(manifest["participants"].items())
    ]


def _resolved(directory: Path, since: float, until: float) -> list[dict]:
    """Reads issue resolutions and completions recorded in the window."""
    found = []
    for number, record in issues.snapshot(directory)["issues"].items():
        for entry in record.get("history") or []:
            if entry.get("action") in RESOLVED_ACTIONS and _inside(
                entry.get("at"), since, until
            ):
                found.append({"issue": int(number), **entry})
    return sorted(found, key=lambda entry: float(entry.get("at") or 0))


def _reports(
    directory: Path, manifest: dict, since: float, until: float
) -> list[dict]:
    """Reads ready reports with their evidence from each lane's report log."""
    found = []
    for name in sorted(manifest["participants"]):
        for record in metrics.report_records(directory, name, since):
            if (
                record.get("kind") == "report"
                and record.get("state") == "ready"
                and _inside(record.get("at"), since, until)
            ):
                found.append({"lane": name, **record})
    return sorted(found, key=lambda entry: float(entry.get("at") or 0))


def _pull_requests(directory: Path, since: float, until: float) -> list[dict]:
    """Reads the pull requests supervision saw open during the window.

    A pull request counts when supervision first saw it before the window
    ended and it had not left the open list before the window began.
    """
    try:
        seen = json.loads(
            (directory / supervision.PULL_REQUEST_RECORD).read_text()
        )
    except (OSError, ValueError):
        return []
    if not isinstance(seen, dict):
        return []
    current = seen.get("pull_requests") or {}
    readings = [
        *(current.values() if isinstance(current, dict) else ()),
        *(seen.get("history") or []),
    ]
    found = []
    for reading in readings:
        if not isinstance(reading, dict):
            continue
        opened = float(reading.get("opened_at") or 0)
        gone = reading.get("gone_at")
        if opened > until or (gone and float(gone) < since):
            continue
        found.append(
            {
                "lane": str(reading.get("lane") or ""),
                "number": reading.get("number"),
                "url": reading.get("url", ""),
                "branch": reading.get("branch", ""),
                "sha": reading.get("sha", ""),
                "checks": reading.get("checks", ""),
                "issues": reading.get("issues") or [],
                "opened_at": opened,
                "gone_at": gone,
            }
        )
    return sorted(found, key=lambda entry: int(entry.get("number") or 0))


def _commits(
    root: Path, manifest: dict, since: float, until: float
) -> list[dict]:
    """Lists each lane's own commits, those on its branch beyond the base.

    A lane whose branch Git can no longer resolve contributes nothing; its
    merged work still appears through its reports and pull requests.
    """
    found = []
    base = str(manifest.get("base") or "HEAD")
    for name, entry in sorted(manifest["participants"].items()):
        branch = str(entry.get("branch") or "")
        if not branch or branch.startswith("-"):
            continue
        try:
            output = git(
                root,
                "log",
                "--format=%H%x1f%ct%x1f%s",
                f"{base}..{branch}",
                "--",
            )
        except (BridgeError, subprocess.TimeoutExpired):
            continue
        for line in output.splitlines():
            sha, stamp, subject = (line.split("\x1f", 2) + ["", ""])[:3]
            if stamp.isdigit() and _inside(stamp, since, until):
                found.append(
                    {
                        "lane": name,
                        "branch": branch,
                        "sha": sha,
                        "at": float(stamp),
                        "subject": subject,
                    }
                )
    return sorted(found, key=lambda entry: entry["at"])


def collect(
    home: Path, directory: Path, manifest: dict, since: float, until: float
) -> list[tuple[str, object]]:
    """Reads every audited record of one project inside the window.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding the roster.
        since: Window start as a Unix time, inclusive.
        until: Window end as a Unix time, inclusive.

    Returns:
        Kind and scrubbed data of each record, in bundle order: lanes,
        resolved issues, claims and handoffs, ready reports, review rounds,
        pull requests, then commits, each group oldest first.
    """
    claims, _ = history.query(
        home, directory, manifest, kinds=("claim", "handoff"), since=since
    )
    rounds = [
        entry
        for entry in reviews.rounds(directory)
        if _inside(entry.get("requested"), since, until)
    ]
    entries: list[tuple[str, dict]] = [
        *_lanes(manifest),
        *(("resolved", entry) for entry in _resolved(directory, since, until)),
        *(
            (record["kind"], record)
            for record in claims
            if record["at"] <= until
        ),
        *(
            ("report", entry)
            for entry in _reports(directory, manifest, since, until)
        ),
        *(("review", entry) for entry in rounds),
        *(
            ("pull_request", entry)
            for entry in _pull_requests(directory, since, until)
        ),
        *(
            ("commit", entry)
            for entry in _commits(
                Path(manifest["root"]), manifest, since, until
            )
        ),
    ]
    return [(kind, scrub(data)) for kind, data in entries]


def _payload(bundle: dict) -> bytes:
    """Returns the bytes a signature covers: the bundle without it."""
    return canonical(
        {key: value for key, value in bundle.items() if key != "signature"}
    )


def sign(payload: bytes, key: Path) -> str:
    """Signs bytes with the operator's SSH key through `ssh-keygen -Y sign`.

    Args:
        payload: Bytes to sign.
        key: Private key, or a public key whose private half an agent holds.

    Returns:
        The armored SSH signature.

    Raises:
        BridgeError: If `ssh-keygen` is missing or refuses to sign.
    """
    try:
        result = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
            input=payload,
            capture_output=True,
            timeout=SIGN_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError(f"ssh-keygen could not sign: {exc}") from exc
    signature = result.stdout.decode("utf-8", "replace")
    if result.returncode or "BEGIN SSH SIGNATURE" not in signature:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise BridgeError(f"ssh-keygen could not sign with {key}: {detail}")
    return signature


def export(
    home: Path,
    directory: Path,
    manifest: dict,
    output: Path,
    *,
    since: tuple[str, float],
    until: tuple[str, float],
    key: Path | None = None,
) -> dict:
    """Writes the audit bundle of one project's window to one file.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        manifest: Project manifest holding the roster.
        output: The file the operator named.
        since: The reference the operator gave and its Unix time.
        until: The reference the operator gave and its Unix time.
        key: SSH key to sign with; the bundle is unsigned when None.

    Returns:
        The bundle written.

    Raises:
        BridgeError: If the window is empty or signing fails.
        OSError: If the file cannot be written.
    """
    if since[1] > until[1]:
        raise BridgeError(
            f"--since {since[0]} is later than --until {until[0]}."
        )
    records = chain(collect(home, directory, manifest, since[1], until[1]))
    bundle: dict = {
        "format": FORMAT,
        "kind": KIND,
        "project": str(manifest["root"]),
        "since": {"ref": since[0], "at": since[1]},
        "until": {"ref": until[0], "at": until[1]},
        "exported_at": time.time(),
        "records": records,
        "head": records[-1]["hash"] if records else GENESIS,
    }
    if key is not None:
        bundle["signature"] = sign(_payload(bundle), key)
    write_text(output, json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    return bundle


def counts(bundle: dict) -> dict[str, int]:
    """Counts a bundle's records by kind.

    Args:
        bundle: An exported bundle.

    Returns:
        The number of records of each kind present.
    """
    tally: dict[str, int] = {}
    for record in bundle.get("records") or []:
        kind = str(record.get("kind", ""))
        tally[kind] = tally.get(kind, 0) + 1
    return tally


def _check_chain(records: list) -> str:
    """Recomputes the chain and returns its head hash.

    Raises:
        BridgeError: Naming the first record by index that does not match.
    """
    previous = GENESIS
    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("index") != index:
            raise BridgeError(f"Record {index} is out of place in the chain.")
        if record.get("previous") != previous:
            raise BridgeError(
                f"Record {index} breaks the chain: it does not follow "
                "the record before it."
            )
        digest = _digest(
            index, str(record.get("kind")), record.get("data"), previous
        )
        if record.get("hash") != digest:
            raise BridgeError(
                f"Record {index} breaks the chain: its content does not "
                "match its hash."
            )
        previous = digest
    return previous


def _check_signature(bundle: dict) -> str:
    """Checks the embedded signature with `ssh-keygen -Y check-novalidate`.

    Returns:
        The line `ssh-keygen` printed, naming the signing key's fingerprint.

    Raises:
        BridgeError: If `ssh-keygen` is missing or rejects the signature.
    """
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "bundle.sig"
        path.write_text(str(bundle["signature"]))
        try:
            result = subprocess.run(
                [
                    "ssh-keygen",
                    "-Y",
                    "check-novalidate",
                    "-n",
                    NAMESPACE,
                    "-s",
                    str(path),
                ],
                input=_payload(bundle),
                capture_output=True,
                timeout=SIGN_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError(f"ssh-keygen could not verify: {exc}") from exc
    said = (result.stdout + result.stderr).decode("utf-8", "replace").strip()
    if result.returncode:
        raise BridgeError(f"The signature does not match the bundle: {said}")
    return said


def verify(path: Path, *, require_signature: bool = False) -> dict:
    """Checks a bundle's hash chain and, when present, its signature.

    Args:
        path: Bundle file.
        require_signature: Refuse a bundle that carries no signature.

    Returns:
        The record count, head hash, whether it was signed, and the
        signing key `ssh-keygen` reported.

    Raises:
        BridgeError: If the file is not a bundle, a record breaks the chain
            (named by index), the head does not match, the signature is
            rejected, or a required signature is missing.
    """
    try:
        bundle = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise BridgeError(f"{path} is not a readable bundle: {exc}") from exc
    if not isinstance(bundle, dict) or bundle.get("kind") != KIND:
        raise BridgeError(f"{path} is not an agent-parley audit bundle.")
    records = bundle.get("records")
    if not isinstance(records, list):
        raise BridgeError(f"{path} carries no record list.")
    head = _check_chain(records)
    if bundle.get("head") != head:
        raise BridgeError(
            f"Record {len(records)} is missing: the bundle head does not "
            "match the last record."
        )
    signer = ""
    if bundle.get("signature"):
        signer = _check_signature(bundle)
    elif require_signature:
        raise BridgeError(f"{path} carries no signature.")
    return {
        "records": len(records),
        "head": head,
        "signed": bool(signer),
        "signer": signer,
    }
