"""Entry point. Reads the input file and writes predictions in the track's submission format."""

import argparse
import json
import random

SEED = 0


def solve(data: object) -> object:
    raise NotImplementedError("Implement the solution.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    random.seed(SEED)
    with open(args.input) as f:
        data = json.load(f)
    result = solve(data)
    with open(args.output, "w") as f:
        json.dump(result, f, sort_keys=True)


if __name__ == "__main__":
    main()
