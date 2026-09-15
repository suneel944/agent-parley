"""Asks the running service for a hook decision, deciding locally if it cannot.

Every native tool call runs one hook process, so the price this module pays
at import sets the floor of every call. It imports only what the request
itself needs and reaches the checkpoint engine solely on the fallback path,
where a coordination outage is already the slower case.
"""

import json
import os
import socket
import sys

CONNECT_TIMEOUT = 0.25
REPLY_TIMEOUT = 2.0
MAX_INPUT_BYTES = 1_000_001
PATH = "/hook/"
RAW_REPLY = "application/vnd.agent-parley.hook+raw"
STATUS_HEADER = "X-Parley-Status"
STDOUT_HEADER = "X-Parley-Stdout-Bytes"
CLIENT_NAME = "hook-client.sh"

CLIENT_SCRIPT = r"""#!/usr/bin/env bash
export LC_ALL=C
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
body="$body,\"payload\":$payload}"

exec 3<>"/dev/tcp/127.0.0.1/$port" 2>/dev/null || decide_in_process
printf 'POST @PATH@ HTTP/1.1\r\nHost: 127.0.0.1:%s\r\n'\
'Authorization: Bearer %s\r\nContent-Type: application/json\r\n'\
'Accept: @ACCEPT@\r\nContent-Length: %s\r\nConnection: close\r\n\r\n%s' \
  "$port" "$token" "${#body}" "$body" >&3 2>/dev/null || decide_in_process

line=""
IFS= read -r -t @TIMEOUT@ line <&3 || decide_in_process
[[ $line =~ ^HTTP/1\.[01][[:space:]]+200 ]] || decide_in_process

status=""
stdout_bytes=""
length=""
while IFS= read -r -t @TIMEOUT@ line <&3; do
  line="${line%$'\r'}"
  [ -z "$line" ] && break
  case "${line,,}" in
    "@status_header@: "*) status="${line#*: }" ;;
    "@stdout_header@: "*) stdout_bytes="${line#*: }" ;;
    "content-length: "*) length="${line#*: }" ;;
  esac
done
[[ $status =~ ^[0-9]+$ ]] || decide_in_process
[[ $stdout_bytes =~ ^[0-9]+$ ]] || decide_in_process
[[ $length =~ ^[0-9]+$ ]] || decide_in_process

reply=""
if [ "$length" -gt 0 ]; then
  IFS= read -r -t @TIMEOUT@ -N "$length" reply <&3 || decide_in_process
fi
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
        .replace("@TIMEOUT@", str(REPLY_TIMEOUT))
        .replace("@status_header@", STATUS_HEADER.lower())
        .replace("@stdout_header@", STDOUT_HEADER.lower())
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


def fallback(raw: str, cause: str) -> int:
    """Decides in-process, recording why the service was not used.

    Args:
        raw: Native hook input exactly as it was read from standard input.
        cause: Bounded text naming the failure that skipped the service.

    Returns:
        The hook process exit status.
    """
    import io

    from agent_parley import checkpoints

    sys.stdin = io.StringIO(raw)
    return checkpoints.main(fallback=cause)


def main() -> int:
    """Serves one native hook event through the service or in-process.

    The identity file the launcher wrote at registration supplies the
    credential; nothing secret travels on the command line. Any failure to
    obtain a served decision, from a missing file to a refused credential
    to a non-200 reply, falls back to the in-process path so the decision
    and its recorded cause are the same ones an outage produced before.
    """
    raw = sys.stdin.read(MAX_INPUT_BYTES)
    try:
        selected = options(sys.argv[1:])
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
            "payload": json.loads(raw),
        }
        for name in ("adapter", "protocol"):
            if name in selected:
                body[name] = selected[name]
        status, reply = request(port, str(token), json.dumps(body).encode())
        if status != 200:
            raise OSError(f"service answered {status}")
        served = json.loads(reply)
        sys.stdout.write(served["stdout"])
        sys.stderr.write(served["stderr"])
        return int(served["status"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return fallback(raw, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
