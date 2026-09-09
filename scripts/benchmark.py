"""Measures local coordination latency and payload size without model calls."""

import json
import statistics
import tempfile
import time
from pathlib import Path

from agent_parley import store
from agent_parley.server import TOOLS


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
        results = {}
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
        results["tool_catalog"] = {
            "tools": len(TOOLS),
            "utf8_bytes": len(
                json.dumps(TOOLS, separators=(",", ":")).encode()
            ),
        }
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
