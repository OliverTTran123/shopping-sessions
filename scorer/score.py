"""Local gate for Shopping Sessions.

The published score is container run time in seconds, and lower is better. A wrong
table is not scored. This checks the two output tables against the dev answer and
sets "score" to the number of differing rows, so 0 means an exact match.
"""

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from compare import load  # noqa: E402
from run_fixtures import SCHEMAS  # noqa: E402


def score(pred: Path, truth: Path) -> dict:
    diff_rows = 0
    details: dict[str, int] = {}
    for table, schema in SCHEMAS.items():
        actual, expected = load(pred, table), load(truth, table)
        keys = [column for column in ("user_id", "campaign_id", "day") if column in schema]
        duplicate_pred = actual.height - actual.n_unique(subset=keys)
        duplicate_truth = expected.height - expected.n_unique(subset=keys)
        only_pred = actual.join(expected, on=list(schema), how="anti", nulls_equal=True).height
        only_truth = expected.join(actual, on=list(schema), how="anti", nulls_equal=True).height
        table_diff = only_pred + only_truth + duplicate_pred + duplicate_truth
        diff_rows += table_diff
        details[f"{table}_diff"] = table_diff
    return {"score": diff_rows, **details}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pred", required=True, help="output directory, or a folder of csv answers"
    )
    parser.add_argument("--truth", required=True)
    args = parser.parse_args()
    result = score(Path(args.pred), Path(args.truth))
    print(json.dumps(result))
    return 0 if result["score"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
