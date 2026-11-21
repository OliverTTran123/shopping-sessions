"""Run a command several times and check the output file is byte-identical.

Usage:
    determinism.py --out-file out/pred.json --json out/determinism.json [--runs 2] -- <cmd...>
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-file", required=True)
    parser.add_argument("--json", required=True)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = [part for part in args.command if part != "--"]
    if not command:
        print("determinism: no command given", file=sys.stderr)
        return 2

    out = Path(args.out_file)
    digests = []
    for _ in range(args.runs):
        out.unlink(missing_ok=True)
        result = subprocess.run(command, check=False)
        if result.returncode != 0 or not out.exists():
            print("determinism: run failed or produced no output", file=sys.stderr)
            Path(args.json).write_text(json.dumps({"deterministic": False, "reason": "run failed"}))
            return 1
        digests.append(digest(out))

    same = len(set(digests)) == 1
    Path(args.json).write_text(json.dumps({"deterministic": same, "digests": digests}))
    print(f"determinism: {'identical' if same else 'DIFFERENT'} across {args.runs} runs")
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(main())
