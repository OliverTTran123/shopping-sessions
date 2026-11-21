"""Time one or more commands. Alternates commands (A, B, A, B) so drift hits every side.

Usage: bench.py --runs 5 --limit 600 --json out/bench.json -- "cmd a" ["cmd b" ...]
Reports median and range per command. Counts failed runs. Exits 1 if any run fails or a
median exceeds --limit.
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--limit", type=float, default=0.0, help="seconds, 0 means no limit")
    parser.add_argument("--json", required=True)
    parser.add_argument("commands", nargs="+")
    args = parser.parse_args()
    commands = [c for c in args.commands if c != "--"]

    times: dict[str, list[float]] = {c: [] for c in commands}
    failures = 0
    for _ in range(args.runs):
        for command in commands:
            start = time.perf_counter()
            result = subprocess.run(command, shell=True, check=False, stdout=subprocess.DEVNULL)
            elapsed = time.perf_counter() - start
            if result.returncode != 0:
                failures += 1
            else:
                times[command].append(elapsed)

    report = {}
    over_limit = False
    for command, values in times.items():
        if not values:
            report[command] = {"median_s": None, "min_s": None, "max_s": None, "runs": 0}
            continue
        median = statistics.median(values)
        report[command] = {
            "median_s": round(median, 3),
            "min_s": round(min(values), 3),
            "max_s": round(max(values), 3),
            "runs": len(values),
        }
        if args.limit and median > args.limit:
            over_limit = True
        print(f"bench: median {median:.2f}s range {min(values):.2f}-{max(values):.2f}s  {command}")

    payload = {"failures": failures, "over_limit": over_limit, "commands": report}
    Path(args.json).write_text(json.dumps(payload))
    if failures:
        print(f"bench: {failures} failed runs", file=sys.stderr)
    if over_limit:
        print(f"bench: median over the {args.limit}s limit", file=sys.stderr)
    return 1 if failures or over_limit else 0


if __name__ == "__main__":
    sys.exit(main())
