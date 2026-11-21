"""Print one CHECK line that combines the local score, determinism and runtime."""

import json
from pathlib import Path


def load(path: str) -> dict:
    file = Path(path)
    return json.loads(file.read_text()) if file.exists() else {}


def main() -> None:
    score = load("out/score.json")
    determinism = load("out/determinism.json")
    bench = load("out/bench.json")

    medians = [c["median_s"] for c in bench.get("commands", {}).values() if c.get("median_s")]
    runtime = f"{medians[0]:.2f}s" if medians else "n/a"
    extras = " ".join(f"{k}={v}" for k, v in score.items() if k != "score")
    deterministic = "yes" if determinism.get("deterministic") else "NO"
    print(
        f"CHECK score={score.get('score', 'n/a')} {extras} "
        f"deterministic={deterministic} runtime_median={runtime}"
    )


if __name__ == "__main__":
    main()
