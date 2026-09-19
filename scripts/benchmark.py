"""Measures local coordination latency and payload size without model calls."""

import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agent_parley import store
from agent_parley.server import TOOLS

STARTUP_SAMPLES = 15
STARTUP_BUDGETS_MS = {
    "version": 20.0,
    "status": 50.0,
    "import_cli": 15.0,
}
LAUNCH = "from agent_parley.entry import main; raise SystemExit(main())"
STARTUP_COMMANDS = (
    ("interpreter", ("-c", "pass")),
    ("import_entry", ("-c", "import agent_parley.entry")),
    ("import_cli", ("-c", "import agent_parley.cli")),
    ("version", ("-c", LAUNCH, "--version")),
    ("doctor", ("-c", LAUNCH, "doctor")),
    ("status", ("-c", LAUNCH, "status")),
    ("problems", ("-c", LAUNCH, "problems")),
)


def startup(home: Path) -> dict:
    """Times the cheapest invocations end to end against a private state.

    Each sample starts with site initialization and the unsafe path disabled.
    The launcher then starts the way the installed console script starts it,
    so a reading covers interpreter start, the imports the invocation needs
    and the work the command does. The interpreter and the two import readings
    sit beside them because no command can beat the floor they set.

    Args:
        home: State directory the measured commands read.

    Returns:
        The median wall time of each measured invocation, in milliseconds.
    """
    environment = {
        **os.environ,
        "AGENT_PARLEY_HOME": str(home),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    measured = {}
    for name, arguments in STARTUP_COMMANDS:
        samples = []
        for _ in range(STARTUP_SAMPLES):
            start = time.perf_counter_ns()
            subprocess.run(
                [sys.executable, "-S", "-P", *arguments],
                check=False,
                capture_output=True,
                env=environment,
                timeout=120,
            )
            samples.append((time.perf_counter_ns() - start) / 1_000_000)
        measured[name] = round(statistics.median(samples), 1)
    return measured


def main() -> None:
    """Benchmarks committed sends and metadata reads against temporary state."""
    with tempfile.TemporaryDirectory(prefix="agent-parley-benchmark-") as path:
        home = Path(path)
        store.initialize(home)
        first = store.register(home, "/benchmark", "GreenCastle")
        second = store.register(home, "/benchmark", "BlueLake")
        sender = store.authenticate(home, first["registration_token"])
        reader = store.authenticate(home, second["registration_token"])
        if sender is None or reader is None:
            raise RuntimeError("Benchmark registration failed")
        results: dict[str, object] = {}
        for name, actor, arguments in (
            (
                "send_message",
                sender,
                {
                    "to": ["BlueLake"],
                    "subject": "Handoff",
                    "body_md": "API ready; tests passed.",
                },
            ),
            ("fetch_inbox", reader, {}),
        ):
            samples = []
            for index in range(200):
                values = (
                    {**arguments, "idempotency_key": str(index)}
                    if name == "send_message"
                    else arguments
                )
                start = time.perf_counter_ns()
                store.call(home, actor, name, values)
                samples.append((time.perf_counter_ns() - start) / 1_000_000)
            results[name] = {
                "samples": len(samples),
                "median_ms": round(statistics.median(samples), 3),
                "p95_ms": round(sorted(samples)[189], 3),
            }
        startup_results = startup(home)
        results["startup_ms"] = startup_results
        results["startup_budget_ms"] = STARTUP_BUDGETS_MS
        results["startup_over_budget"] = [
            name
            for name, budget in STARTUP_BUDGETS_MS.items()
            if startup_results[name] >= budget
        ]
        results["tool_catalog"] = {
            "tools": len(TOOLS),
            "utf8_bytes": len(
                json.dumps(TOOLS, separators=(",", ":")).encode()
            ),
        }
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
