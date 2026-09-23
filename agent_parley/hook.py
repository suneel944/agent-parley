"""Asks the running service for a hook decision, deciding locally if it cannot.

Every native tool call runs one hook process, so the price this module pays
at import sets the floor of every call. It imports only what the request
itself needs and reaches the checkpoint engine solely on the fallback path,
where a coordination outage is already the slower case.

The native client kills a hook at the timeout the launcher registered for it,
which is `checkpoints.HOOK_TIMEOUT` seconds, and discards whatever the hook
had produced. Every path here is bounded so the worst case fits inside that
budget:

* Served: `CONNECT_TIMEOUT` to reach the service, then at most
  `server.DECISION_SECONDS` until it answers with a decision or with
  `DECIDING`. `DECIDING` ends the hook immediately with no context, so a
  slow decision costs 0.25 + 1.5 seconds and is never repeated. A decision
  that injected coordination closes the reply after at most
  `server.DELIVERY_SECONDS` more, once the lane records it as delivered.
* Outage: the connection fails within `CONNECT_TIMEOUT`, then the in-process
  decision runs, which waits at most `checkpoints.LOCK_SECONDS` for the
  lane's checkpoint lock.
* Refused: the service answers a status the client cannot use. The answer
  itself proves the service will not decide this event, so `ANSWERED_ENV`
  carries that status into the in-process path and suppresses a second post.

The largest of those is 0.25 + 1.5 + 1.0 = 2.75 seconds against a 3-second
budget. Raising `REPLY_TIMEOUT`, `server.DECISION_SECONDS`,
`checkpoints.LOCK_SECONDS`, or `checkpoints.HOOK_TIMEOUT` requires redoing
this arithmetic.
"""

import json
import os
import socket
import sys

CONNECT_TIMEOUT = 0.25
REPLY_TIMEOUT = 2.0
SHELL_TIMEOUT = int(REPLY_TIMEOUT) + (REPLY_TIMEOUT % 1 > 0)
MAX_INPUT_BYTES = 1_000_001
PATH = "/hook/"
RAW_REPLY = "application/vnd.agent-parley.hook+raw"
STATUS_HEADER = "X-Parley-Status"
STDOUT_HEADER = "X-Parley-Stdout-Bytes"
CLIENT_NAME = "hook-client.sh"
RELAUNCH_STAMP = "relaunch.stamp"
RELAUNCH_INTERVAL = 60.0
HOOK_PID_ENV = "AGENT_PARLEY_HOOK_PID"
ANSWERED_ENV = "AGENT_PARLEY_SERVICE_ANSWERED"
DECIDING = 202

CLIENT_SCRIPT = r"""#!/usr/bin/env bash
export LC_ALL=C
export AGENT_PARLEY_HOOK_PID="$$"
set -u

arguments=("$@")
home=""
directory=""
participant=""
protocol=""
adapter=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --home) home="${2-}"; shift 2 || exit 1 ;;
    --directory) directory="${2-}"; shift 2 || exit 1 ;;
    --participant) participant="${2-}"; shift 2 || exit 1 ;;
    --protocol) protocol="${2-}"; shift 2 || exit 1 ;;
    --adapter) adapter="${2-}"; shift 2 || exit 1 ;;
    *) shift ;;
  esac
done

payload=""
IFS= read -r -d '' payload || true

decide_in_process() {
  printf '%s' "$payload" | "@PYTHON@" -m agent_parley.hook "${arguments[@]}"
  exit "$?"
}

quote() {
  local text="$1"
  text="${text//\\/\\\\}"
  text="${text//\"/\\\"}"
  printf '"%s"' "$text"
}

read_file() {
  local content=""
  IFS= read -r -d '' content < "$1" || true
  printf '%s' "$content"
}

identity="$(read_file "$directory/$participant-identity.json" 2>/dev/null)"
[[ $identity =~ \"registration_token\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]] \
  || decide_in_process
token="${BASH_REMATCH[1]}"

configuration="$(read_file "$home/config.json" 2>/dev/null)"
[[ $configuration =~ \"port\"[[:space:]]*:[[:space:]]*([0-9]+) ]] \
  || decide_in_process
port="${BASH_REMATCH[1]}"

body="{\"directory\":$(quote "$directory")"
body="$body,\"participant\":$(quote "$participant")"
[ -n "$adapter" ] && body="$body,\"adapter\":$(quote "$adapter")"
[ -n "$protocol" ] && body="$body,\"protocol\":$(quote "$protocol")"
body="$body,\"hook_pid\":$AGENT_PARLEY_HOOK_PID"
body="$body,\"payload\":$payload}"

{ exec 3<>"/dev/tcp/127.0.0.1/$port"; } 2>/dev/null || decide_in_process
printf 'POST @PATH@ HTTP/1.1\r\nHost: 127.0.0.1:%s\r\n'\
'Authorization: Bearer %s\r\nContent-Type: application/json\r\n'\
'Accept: @ACCEPT@\r\nContent-Length: %s\r\nConnection: close\r\n\r\n%s' \
  "$port" "$token" "${#body}" "$body" >&3 2>/dev/null || decide_in_process

line=""
IFS= read -r -t @TIMEOUT@ line <&3 || decide_in_process
[[ $line =~ ^HTTP/1\.[01][[:space:]]+([0-9]+) ]] || decide_in_process
answered="${BASH_REMATCH[1]}"
if [ "$answered" = "@DECIDING@" ]; then
  exec 3<&- 3>&-
  printf '{}'
  exit 0
fi
if [ "$answered" != "200" ]; then
  exec 3<&- 3>&-
  export @ANSWERED@="$answered"
  decide_in_process
fi

status=""
stdout_bytes=""
while IFS= read -r -t @TIMEOUT@ line <&3; do
  line="${line%$'\r'}"
  [ -z "$line" ] && break
  case $line in
    "@status_header@: "*) status="${line#*: }" ;;
    "@stdout_header@: "*) stdout_bytes="${line#*: }" ;;
  esac
done
[[ $status =~ ^[0-9]+$ ]] || decide_in_process
[[ $stdout_bytes =~ ^[0-9]+$ ]] || decide_in_process

reply=""
IFS= read -r -t @TIMEOUT@ -d '' reply <&3
result=$?
[ "$result" -gt 128 ] && decide_in_process
exec 3<&- 3>&-
printf '%s' "${reply:0:stdout_bytes}"
printf '%s' "${reply:stdout_bytes}" >&2
exit "$status"
"""


def write_client(home: str, python: str) -> str:
    """Writes the shell hook client this installation runs, and names it.

    Every native tool call spawns one hook process, and the interpreter is
    the whole bill: an empty Python process costs some sixty milliseconds
    against a shell process under one. The client therefore asks the running
    service over a loopback connection the shell opens itself, and starts
    Python only when that does not produce an answer, which is the outage
    path this module already treats as the slower case.

    The client is written for the oldest Bash it can be asked to run on.
    macOS ships 3.2 as ``/bin/bash``, which rejects a fractional ``read``
    timeout, so the reply timeout is expressed in whole seconds. A rejected
    timeout is not a slow service: the read fails at once, after the request
    was already sent and served, and the fallback then asks a second time for
    a decision the service has already recorded as delivered.

    Args:
        home: Private bridge state root the client is written into.
        python: Interpreter the client runs on its fallback path.

    Returns:
        The path of the written client, which the launcher configures as the
        native hook command.
    """
    path = os.path.join(home, CLIENT_NAME)
    script = (
        CLIENT_SCRIPT.replace("@PYTHON@", python)
        .replace("@PATH@", PATH)
        .replace("@ACCEPT@", RAW_REPLY)
        .replace("@TIMEOUT@", str(SHELL_TIMEOUT))
        .replace("@DECIDING@", str(DECIDING))
        .replace("@ANSWERED@", ANSWERED_ENV)
        .replace("@status_header@", STATUS_HEADER)
        .replace("@stdout_header@", STDOUT_HEADER)
    )
    with open(path, "w") as stream:
        stream.write(script)
    os.chmod(path, 0o755)
    return path


def options(argv: list[str]) -> dict:
    """Reads the hook command line without importing the argument parser.

    Args:
        argv: Arguments after the module name, as the launcher wrote them.

    Returns:
        Option values keyed by their long name; ``--agent`` is read as
        ``participant``.
    """
    values: dict = {}
    key = None
    for item in argv:
        if key is not None:
            values[key] = item
            key = None
        elif item.startswith("--"):
            key = item.removeprefix("--")
    if "agent" in values:
        values["participant"] = values.pop("agent")
    return values


def hook_pid() -> int:
    """Returns the generated hook process ID carried across fallback.

    The shell client exports its own PID before either serving the request or
    starting Python. A direct Python hook uses its current PID. The checkpoint
    service uses this process only to inspect its controlling terminal while
    the hook is waiting; native payload fields never supply process identity.

    Returns:
        Positive hook process ID, falling back to this process for a missing or
        invalid internal environment value.
    """
    try:
        value = int(os.environ.get(HOOK_PID_ENV, os.getpid()))
    except (TypeError, ValueError):
        return os.getpid()
    return value if value > 1 else os.getpid()


def request(port: int, token: str, body: bytes) -> tuple[int, bytes]:
    """Sends one loopback HTTP request and returns its status and body.

    Args:
        port: Loopback port the service is configured to listen on.
        token: The lane's registration credential, sent as a bearer token.
        body: JSON request body.

    Returns:
        The response status and body; the service closes the connection
        after one response, so the body ends at end of stream. A service
        that answers from the headers alone, as it does for an oversized
        body, may close its side before the body is fully written; the
        reply it already sent is still read and returned.

    Raises:
        OSError: If the connection is refused or a timeout passes.
        ValueError: If the reply does not start with a status line, as
            when a service at its concurrency cap closes the accepted
            connection without answering.
    """
    head = (
        f"POST {PATH} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {token}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    chunks = []
    with socket.create_connection(
        ("127.0.0.1", port), timeout=CONNECT_TIMEOUT
    ) as sock:
        sock.settimeout(REPLY_TIMEOUT)
        try:
            sock.sendall(head + body)
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        except (BrokenPipeError, ConnectionResetError):
            if not chunks:
                raise
    header, _, reply = b"".join(chunks).partition(b"\r\n\r\n")
    parts = header.split(b" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError("reply has no status line")
    return int(parts[1]), reply


def relaunch(home: str) -> None:
    """Asks for a new service when the recorded one is no longer there.

    A reboot and a drift exit both leave a published record naming a
    process that is gone, and every native tool call then pays the
    in-process decision until an operator notices. This asks for a
    service once such a record is read, and asks again no sooner than
    ``RELAUNCH_INTERVAL`` seconds later, so a start that keeps failing
    costs one detached process a minute rather than one per hook.

    A home with no record is left alone: nothing claimed to serve it, and
    the first start belongs to the lane launch or to the operator. The
    request is detached and never waited on, so the hook that made it
    returns its decision on its own budget, and any failure to make it
    leaves that decision untouched.

    Args:
        home: Private bridge state root this hook was given.
    """
    import subprocess
    import time

    from agent_parley import process

    try:
        with open(os.path.join(home, "server.json")) as stream:
            record = json.load(stream)
        if process.alive(record.get("pid"), record.get("start_ticks")):
            return
        stamp = os.path.join(home, RELAUNCH_STAMP)
        if os.path.exists(stamp):
            if time.time() - os.stat(stamp).st_mtime < RELAUNCH_INTERVAL:
                return
        with open(stamp, "w"):
            pass
        subprocess.Popen(
            [sys.executable, "-m", "agent_parley", "--home", home, "up"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError, TypeError):
        return


def fallback(raw: str, cause: str, home: str = "") -> int:
    """Decides in-process, recording why the service was not used.

    Args:
        raw: Native hook input exactly as it was read from standard input.
        cause: Bounded text naming the failure that skipped the service.
        home: Private bridge state root, when the command line named one,
            so an outage can ask for the service back while this call is
            decided here.

    Returns:
        The hook process exit status.
    """
    import io

    from agent_parley import checkpoints

    if home:
        relaunch(home)
    sys.stdin = io.StringIO(raw)
    return checkpoints.main(fallback=cause)


def main() -> int:
    """Serves one native hook event through the service or in-process.

    The identity file the launcher wrote at registration supplies the
    credential; nothing secret travels on the command line. Any failure to
    obtain a served decision, from a missing file to a refused credential
    to a non-200 reply, falls back to the in-process path so the decision
    and its recorded cause are the same ones an outage produced before. That
    path also asks for the service back when the recorded one is gone, so an
    outage heals on the next native tool call.

    `DECIDING` is the exception, and it is not a failure: the service holds
    a decision for this exact event and is still running it. Deciding the
    event again here would contend with that decision for the lane's
    checkpoint lock and could deny a native call over coordination work
    already in progress, so this emits no context and succeeds instead.

    A status the shell client already collected arrives in `ANSWERED_ENV`.
    The service has answered once; asking it again would spend the rest of
    the hook's budget on the same refusal, so that case goes straight to the
    in-process decision.
    """
    raw = sys.stdin.read(MAX_INPUT_BYTES)
    selected = options(sys.argv[1:])
    if refused := os.environ.get(ANSWERED_ENV, ""):
        return fallback(
            raw,
            f"service answered {refused}",
            selected.get("home", ""),
        )
    try:
        directory = selected["directory"]
        participant = selected["participant"]
        with open(
            os.path.join(directory, f"{participant}-identity.json")
        ) as stream:
            token = json.load(stream)["registration_token"]
        with open(os.path.join(selected["home"], "config.json")) as stream:
            port = json.load(stream)["port"]
        body = {
            "directory": directory,
            "participant": participant,
            "hook_pid": hook_pid(),
            "payload": json.loads(raw),
        }
        for name in ("adapter", "protocol"):
            if name in selected:
                body[name] = selected[name]
        status, reply = request(port, str(token), json.dumps(body).encode())
        if status == DECIDING:
            sys.stdout.write("{}\n")
            return 0
        if status != 200:
            raise OSError(f"service answered {status}")
        served = json.loads(reply)
        sys.stdout.write(served["stdout"])
        sys.stderr.write(served["stderr"])
        return int(served["status"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return fallback(
            raw,
            f"{type(exc).__name__}: {exc}",
            selected.get("home", ""),
        )


if __name__ == "__main__":
    raise SystemExit(main())
