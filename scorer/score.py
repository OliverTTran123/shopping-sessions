"""Local scorer. Replace the body of `score` with the track's published scoring rule.

It must print one JSON object on stdout with at least a numeric "score" key.
Extra keys (for example "n" and "errors") are shown in the CHECK line.
Once it is correct, run `make freeze` so no agent can change it.
"""

import argparse
import json


def score(pred: object, truth: object) -> dict:
    raise NotImplementedError("Implement the track's scoring rule here.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pred", required=True)
    parser.add_argument("--truth", required=True)
    args = parser.parse_args()
    with open(args.pred) as f:
        pred = json.load(f)
    with open(args.truth) as f:
        truth = json.load(f)
    print(json.dumps(score(pred, truth)))


if __name__ == "__main__":
    main()
