"""Run a command several times and check the output file is byte-identical.

Usage:
    determinism.py --out-file out/pred.json --json out/determinism.json [--runs 2] -- <cmd...>
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def digest(path: Path) -> str:
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    digest_hash = hashlib.sha256()
    files = sorted(file for file in path.rglob("*") if file.is_file())
    if not files:
        raise FileNotFoundError(f"no files in {path}")
    for file in files:
        digest_hash.update(file.relative_to(path).as_posix().encode())
        digest_hash.update(b"\0")
        digest_hash.update(file.read_bytes())
    return digest_hash.hexdigest()


def clear(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


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
        clear(out)
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
