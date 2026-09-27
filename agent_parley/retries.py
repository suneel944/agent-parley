"""Retry contracts shared by the coordination store and the issue ledger.

A coordination call can fail after its effect has committed: a transport can
drop the reply, a lane can be restarted, an operator can rerun a script. The
caller cannot tell that failure apart from one that changed nothing, so it
retries. An idempotency key makes the two cases the same call. The first call
carrying a key performs the effect and records the key beside it. A later call
carrying the same key from the same participant performs nothing further and
returns what the first call returned, including a refusal.

Keys are scoped to one participant and one operation. A key replayed with
different arguments is refused with an enumerated reason rather than applied,
so a stale retry cannot land on a different issue, path or recipient. Each
backend stores its own keys in its own substrate, in the same transaction that
carries the effect, because a record in one substrate cannot make a mutation in
another atomic.
"""

import hashlib
import json
import time

from agent_parley.state import BridgeError

KEY_CHARACTERS = 80
RETAINED_CALLS = 500
DENIED = "denied"
SERVED = "served"


def digest(operation: str, request: dict) -> str:
    """Returns a stable digest of the arguments that define one call's effect.

    Args:
        operation: Tool or transition the key was issued against.
        request: Arguments that decide what the call does. Fields that do not
            change the effect are excluded by the caller, so retrying with a
            different ordering or a different display field still replays.

    Returns:
        Hexadecimal digest compared against the digest recorded with the key.
    """
    seed = json.dumps(
        [operation, request], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(seed.encode()).hexdigest()


def validate(value: object, required: bool = False) -> str:
    """Returns a well-formed idempotency key, or an empty key when omitted.

    Args:
        value: Candidate key supplied by a caller.
        required: Whether the operation refuses a call that carries no key.

    Returns:
        The validated key, or an empty string when none was supplied and none
        is required.

    Raises:
        BridgeError: If the key is absent where required, is not text, or is
            longer than the retained key length.
    """
    if value is None or value == "":
        if required:
            raise BridgeError("idempotency_key is required.")
        return ""
    if not isinstance(value, str) or len(value) > KEY_CHARACTERS:
        raise BridgeError(
            "idempotency_key must be text of at most "
            f"{KEY_CHARACTERS} characters."
        )
    return value


def scope(agent: str, operation: str, key: str) -> str:
    """Names one retained key, scoped to a lane and a single operation."""
    return f"{agent}\x00{operation}\x00{key}"


def remember(
    state: dict, scoped: str, fingerprint: str, outcome: str, result: object
) -> None:
    """Records one served or refused call in the JSON state being written.

    The record joins the document the effect itself wrote, so both reach disk
    in the same locked write and an interruption cannot leave a key without
    its effect. Retention is bounded at `RETAINED_CALLS` entries, oldest
    discarded first; a discarded key behaves as a first call again.

    Args:
        state: Ledger or activity document about to be written.
        scoped: Retained key name from `scope`.
        fingerprint: Digest of the arguments the key was issued against.
        outcome: `SERVED` or `DENIED`.
        result: Result to replay, or the refusal text to raise again.
    """
    retained = state.setdefault("retries", {})
    retained[scoped] = {
        "request_digest": fingerprint,
        "outcome": outcome,
        "result": result,
        "at": time.time(),
    }
    for stale in sorted(
        retained, key=lambda name: retained[name]["at"], reverse=True
    )[RETAINED_CALLS:]:
        del retained[stale]


def mismatch(operation: str, key: str) -> BridgeError:
    """Builds the enumerated refusal for a key reused with other arguments."""
    return BridgeError(
        f"Idempotency key {key!r} already names a different {operation} call "
        "from this participant. A retry must repeat the original arguments; "
        "a different call needs a different key."
    )


def replayed(recorded: dict, operation: str, key: str, current: str) -> dict:
    """Returns the recorded result of a replayed call, or raises its refusal.

    Args:
        recorded: Stored outcome, digest and result of the first call.
        operation: Tool or transition being replayed.
        key: Idempotency key carried by both calls.
        current: Digest of the arguments the replay carries.

    Returns:
        The result the first call returned, marked as a replay so a caller can
        tell a served retry from a first delivery.

    Raises:
        BridgeError: If the key names different arguments, or if the first
            call was refused. A refusal is replayed as a refusal, so a retry
            never gains authority the original call was denied.
    """
    if recorded["request_digest"] != current:
        raise mismatch(operation, key)
    if recorded["outcome"] == DENIED:
        raise BridgeError(recorded["result"])
    return {**recorded["result"], "replayed": True}


def operator_key(name: str, subject: str, body: str) -> str:
    """Derives a stable idempotency key from an operator message itself.

    Args:
        name: Participant the message addresses.
        subject: Subject line of the message.
        body: Message body.

    Returns:
        A key that repeats only for an identical message, so retyping the same
        steer redelivers nothing while a changed one is a new message.
    """
    digest = hashlib.sha256("\x00".join((name, subject, body)).encode())
    return f"operator-{digest.hexdigest()[:48]}"
