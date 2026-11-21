"""The SQL parsing must match the Python reference and rules.md on dirty input."""

import random
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import pipeline as p  # noqa: E402

HEADER = "click_id,user_id,campaign_id,ts"


def random_timestamps(count: int) -> list[str]:
    rng = random.Random(7)
    out = []
    for _ in range(count):
        clock = (
            f"{rng.choice([0, 1999, 2024, 2025, 2026, 2027, 2028, 2100, 9999]):04d}"
            f"-{rng.randint(0, 13):02d}-{rng.randint(0, 32):02d}"
            f"T{rng.randint(0, 24):02d}:{rng.randint(0, 60):02d}:{rng.randint(0, 60):02d}"
        )
        kind = rng.randrange(4)
        if kind == 0:
            value = clock + "Z"
        elif kind == 1:
            value = f"{clock}{rng.choice('+-')}{rng.randint(0, 24):02d}:{rng.randint(0, 60):02d}"
        elif kind == 2:
            value = "0" * rng.randint(0, 3) + str(rng.randint(0, 2**63 + 9))
        else:
            value = rng.choice([clock + "Z", clock + "+05:30", str(rng.randint(0, 10**13))])
            at = rng.randrange(len(value) + 1)
            value = value[:at] + rng.choice("zZtT +-.:9０٢\r") + value[at + rng.randrange(2) :]
        out.append(value)
    return out


def test_ts_ms_matches_python_reference():
    con = p.connect()
    values = random_timestamps(20000)
    got = p.sql_map(con, values, "ts_ms(v)")
    want = [p.ts_to_ms(value) for value in values]
    assert sum(ms is not None for ms in want) > 2000
    assert got == want


def order(row):
    return tuple("" if value is None else value for value in row)


def rows(con, files, fast):
    return sorted(con.execute(p.parsed_sql(files, HEADER, fast=fast)).fetchall(), key=order)


def test_fast_and_text_readers_agree(tmp_path):
    body = (
        'k1,u1,c_1,5\n"k2","u 2",,"6"\nshort,1,2\nlong,1,2,3,4\n\n,,,\n'
        'bad"q,u,c,1\n"bad,u,c,1\n"b"ad",u,c,1\n",u,c,1\n"",u,c,1\n'
        f"{HEADER}\n k4 ,٢,c,1\nlast,u,c,9"
    )
    (tmp_path / "a.csv").write_bytes(f"{HEADER}\n{body}".encode())
    (tmp_path / "b.csv").write_bytes(f"{HEADER}\n".encode())
    files = p.csv_files(tmp_path)
    con = p.connect()
    assert p.plain_headers(files, HEADER)
    want = [
        (" k4 ", "٢", "c", "1"),
        ("click_id", "user_id", "campaign_id", "ts"),
        ("k1", "u1", "c_1", "5"),
        ("k2", "u 2", None, "6"),
        ("last", "u", "c", "9"),
        (None, "u", "c", "1"),
        (None, None, None, None),
    ]
    assert rows(con, files, fast=False) == sorted(want, key=order)
    assert rows(con, files, fast=True) == sorted(want, key=order)


@pytest.mark.parametrize(
    "body",
    ["k1,u1,c_1,5\r\nk2,u2,c_2,6\n", "k1,u\r1,c_1,5\nk2,u2,c_2,6\n", "k1,u1,c_1,5\nk2,u2,c_2,6\r"],
)
def test_carriage_return_stays_in_its_field(tmp_path, body):
    (tmp_path / "a.csv").write_bytes(f"{HEADER}\n{body}".encode())
    files = p.csv_files(tmp_path)
    con = p.connect()
    with pytest.raises(duckdb.InvalidInputException):
        con.execute(p.parsed_sql(files, HEADER, fast=True)).fetchall()
    p.create_from_csv(con, files, HEADER, "CREATE TABLE t AS SELECT * FROM ({parsed})")
    lines = con.execute(
        "SELECT concat_ws(',', click_id, user_id, campaign_id, ts) FROM t ORDER BY click_id"
    ).fetchall()
    assert "\n".join(line for (line,) in lines) == body.removesuffix("\n")


def test_components_names_each_group_by_its_smallest_node():
    rng = random.Random(11)
    count = 3000
    # Long chains in random order, stars, repeats, and nodes with no edge at all.
    order = list(range(count))
    rng.shuffle(order)
    edges = [(order[i], order[i + 1]) for i in range(0, 1200) if i % 400 != 399]
    edges += [(order[1500], order[i]) for i in range(1501, 1700)]
    edges += [(rng.randrange(2000, 2600), rng.randrange(2000, 2600)) for _ in range(250)]
    edges += [(b, a) for a, b in edges[::7]]
    rng.shuffle(edges)
    parent = list(range(count))

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for a, b in edges:
        parent[find(a)] = find(b)
    smallest = {}
    for node in range(count):
        smallest[find(node)] = min(smallest.get(find(node), node), node)
    want = [smallest[find(node)] for node in range(count)]
    left = p.np.array([a for a, _ in edges], dtype=p.np.int64)
    right = p.np.array([b for _, b in edges], dtype=p.np.int64)
    assert p.components(count, left, right).tolist() == want
    assert p.components(4, left[:0], right[:0]).tolist() == [0, 1, 2, 3]


def test_odd_header_takes_the_text_reader(tmp_path):
    (tmp_path / "a.csv").write_bytes(b"x\r\nk1,u1,c_1,5\n")
    assert not p.plain_headers(p.csv_files(tmp_path), HEADER)
    con = p.connect()
    p.create_from_csv(
        con, p.csv_files(tmp_path), HEADER, "CREATE TABLE t AS SELECT * FROM ({parsed})"
    )
    assert con.execute("SELECT * FROM t").fetchall() == [("k1", "u1", "c_1", "5")]
