#!/usr/bin/env python3
"""Shopping Sessions pipeline. rules.md wins over the starter skeleton.

DuckDB reads the files as raw text and applies the CSV dialect, dedup and
validation in SQL, then sessionizes, attributes, and aggregates. Linked-id names
still come from a Python union-find.
"""

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import duckdb

DATA = os.environ.get("DATA_DIR", "/data")
OUT = os.environ.get("OUTPUT_DIR", "/output")
NCPU = int(os.environ.get("NCPU", os.cpu_count() or 1))
MEM_GB = int(os.environ.get("MEM_GB", "8"))

_WEEK_MS = 7 * 24 * 60 * 60 * 1000
_DAY_MS = 24 * 60 * 60 * 1000
_GAP_MS = 30 * 60 * 1000
_INT64_MAX = 9223372036854775807
_PRICE_MAX = 10_000_000

_F1 = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z$")
_F2 = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"([+-])([0-9]{2}):([0-9]{2})$"
)
_F3 = re.compile(r"^[0-9]+$")
_MONTH_DAYS = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _valid_clock(year: int, month: int, day: int, hour: int, minute: int, second: int) -> bool:
    if not (1 <= month <= 12 and 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return False
    limit = 29 if month == 2 and _leap(year) else _MONTH_DAYS[month]
    return 1 <= day <= limit


def _epoch_ms(year: int, month: int, day: int, hour: int, minute: int, second: int) -> int:
    # Howard Hinnant, days from civil. Integer only: float epoch math drifts by 1 ms.
    year -= month <= 2
    era = (year if year >= 0 else year - 399) // 400
    yoe = year - era * 400
    month_adj = month + (-3 if month > 2 else 9)
    doy = (153 * month_adj + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    days = era * 146097 + doe - 719468
    return (days * 86400 + hour * 3600 + minute * 60 + second) * 1000


def ts_to_ms(value: str | None) -> int | None:
    """UTC epoch milliseconds, or None if the timestamp is bad. Digits are ASCII only."""
    if value is None:
        return None
    match = _F1.fullmatch(value)
    if match:
        parts = [int(piece) for piece in match.groups()]
        if not _valid_clock(*parts):
            return None
        return _epoch_ms(*parts)
    match = _F2.fullmatch(value)
    if match:
        year, month, day, hour, minute, second, sign, off_h, off_m = match.groups()
        parts = [int(year), int(month), int(day), int(hour), int(minute), int(second)]
        off_h, off_m = int(off_h), int(off_m)
        if not _valid_clock(*parts) or not (0 <= off_h <= 23 and 0 <= off_m <= 59):
            return None
        offset = (off_h * 3600 + off_m * 60) * 1000
        if sign == "-":
            offset = -offset
        return _epoch_ms(*parts) - offset
    if _F3.fullmatch(value):
        number = int(value)
        if number > _INT64_MAX:
            return None
        return number
    return None


_BAD_TS = (
    "2026-02-29T00:00:00Z",
    "2026-10-03T24:00:00Z",
    "2026-10-03T10:00:60Z",
    "2026-10-03T10:00:00z",
    "2026-10-03T10:00:00+24:00",
    "2026-10-03T10:00:00+05:60",
    "2026-10-03T10:00:00+0200",
    "2026-13-03T10:00:00Z",
    "2026-04-31T10:00:00Z",
    "2026-10-03 10:00:00Z",
    "2026-10-03T10:00:00",
    "2026-10-03T10:00:00.5Z",
    "2026-10-03T10:00:00Z\r",
    "1791071890000.0",
    "1.79e12",
    "-5",
    "9223372036854775808",
    "١٧٩١٠٢١٦٠٠٠٠٠",
    "２026-10-03T10:00:00Z",
    "",
)
_GOOD_TS = (
    "1970-01-01T00:00:00Z",
    "1970-01-02T00:00:00Z",
    "86400000",
    "0",
    "000",
    "2026-10-03T23:58:10Z",
    "2026-10-03T19:58:10-04:00",
    "2026-10-04T11:58:10+12:00",
    "0001791071890000",
    "1791071890000",
    "2026-10-03T10:00:00-00:00",
    "2026-10-03T10:00:00+23:59",
    "2028-02-29T00:00:00Z",
    "2028-02-29T23:59:59-10:00",
    "2024-12-31T23:59:59Z",
    "2025-01-01T00:00:00Z",
    "2027-03-01T00:00:00+00:00",
    "2000-02-29T12:00:00Z",
    "0000-01-01T00:00:00Z",
    "9999-12-31T23:59:59Z",
    "9223372036854775807",
    "00009223372036854775807",
)
_PRICES = (
    ("0250", 250),
    ("1", 1),
    ("10000000", _PRICE_MAX),
    ("10000001", None),
    ("0", None),
    ("000", None),
    ("", None),
    ("12.5", None),
    ("-5", None),
    ("+5", None),
    ("99999999999999999999999", None),
    ("1000\r", None),
    ("０250", None),
    ("٢٥٠", None),
)


def sql_map(con: duckdb.DuckDBPyConnection, values: list[str], expr: str) -> list:
    """expr applied to each value v, in order. Literals: binding a Python list is slow."""
    literal = "[" + ", ".join("'" + value.replace("'", "''") + "'" for value in values) + "]"
    return con.execute(f"SELECT list_transform({literal}, v -> {expr})").fetchone()[0]


def self_check(con: duckdb.DuckDBPyConnection) -> None:
    """The SQL macros must agree with the Python reference on every edge form."""
    assert ts_to_ms("1970-01-02T00:00:00Z") == _DAY_MS
    same = ts_to_ms("2026-10-03T23:58:10Z")
    assert same == 1_791_071_890_000
    assert ts_to_ms("2026-10-03T19:58:10-04:00") == same
    for bad in _BAD_TS:
        assert ts_to_ms(bad) is None, bad
    samples = list(_GOOD_TS + _BAD_TS)
    for value, sql_ms in zip(samples, sql_map(con, samples, "ts_ms(v)"), strict=True):
        assert sql_ms == ts_to_ms(value), (value, sql_ms, ts_to_ms(value))
    got = sql_map(con, [value for value, _ in _PRICES], "price_val(v)")
    assert got == [want for _, want in _PRICES], got
    fields = ["", '""', '"', '"a"', 'a"', '"a', '"a"b"', " a ", '" a "', "a", '"""']
    got = sql_map(con, fields, "CASE WHEN fld_ok(v) THEN coalesce(fld(v), '<null>') END")
    want = ["<null>", "<null>", None, "a", None, None, None, " a ", " a ", "a", None]
    assert got == want, got


def parse_fields(line: str, width: int) -> list[str | None] | None:
    """CSV dialect from rules.md. None means the whole line is malformed."""
    parts = line.split(",")
    if len(parts) != width:
        return None
    values: list[str | None] = []
    for field in parts:
        if field == "" or field == '""':
            values.append(None)
            continue
        if field.startswith('"'):
            if len(field) < 2 or not field.endswith('"') or '"' in field[1:-1]:
                return None
            values.append(field[1:-1])
            continue
        if '"' in field:
            return None
        values.append(field)
    return values


def data_lines(folder: Path) -> list[str]:
    lines: list[str] = []
    for path in sorted(folder.glob("*.csv")):
        parts = path.read_bytes().decode("utf-8").split("\n")
        lines.extend(line for line in parts[1:] if line != "")
    return lines


def person_names(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(left: str, right: str) -> None:
        ra, rb = find(left), find(right)
        if ra != rb:
            parent[rb] = ra

    for left, right in pairs:
        if left == right:
            find(left)
        else:
            union(left, right)
    groups: dict[str, list[str]] = {}
    for node in list(parent):
        groups.setdefault(find(node), []).append(node)
    rows: list[tuple[str, str]] = []
    for members in groups.values():
        name = min(members)
        rows.extend((member, name) for member in members)
    return rows


_CLOCK = (
    "[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
)
# The CSV dialect and the three timestamp forms of rules.md, as SQL macros.
_MACROS = (
    # A quote is legal only as the first and last character of a whole field.
    """
    CREATE MACRO fld_ok(x) AS (
        x IS NULL
        OR NOT contains(x, '"')
        OR (
            length(x) >= 2 AND starts_with(x, '"') AND ends_with(x, '"')
            AND NOT contains(substr(x, 2, length(x) - 2), '"')
        )
    )
    """,
    # Only for a field that passed fld_ok: a quote in it means it is wrapped in quotes.
    """
    CREATE MACRO fld(x) AS (
        nullif(CASE WHEN contains(x, '"') THEN substr(x, 2, length(x) - 2) ELSE x END, '')
    )
    """,
    # The patterns allow only ASCII digits and in-range clock fields; the cast then
    # rejects impossible dates such as February 30. The cast ignores an offset, so
    # F2 subtracts it by hand.
    f"""
    CREATE MACRO ts_ms(v) AS (
        CASE
        WHEN regexp_full_match(v, '{_CLOCK}Z') THEN epoch_ms(TRY_CAST(v AS TIMESTAMP))
        WHEN regexp_full_match(v, '[0-9]+') THEN TRY_CAST(v AS BIGINT)
        WHEN regexp_full_match(v, '{_CLOCK}[+-]([01][0-9]|2[0-3]):[0-5][0-9]') THEN
            epoch_ms(TRY_CAST(v[1:19] AS TIMESTAMP))
            - (CASE WHEN v[20:20] = '-' THEN -1 ELSE 1 END)
              * (TRY_CAST(v[21:22] AS BIGINT) * 3600 + TRY_CAST(v[24:25] AS BIGINT) * 60)
              * 1000
        END
    )
    """,
    f"""
    CREATE MACRO price_val(v) AS (
        CASE WHEN regexp_full_match(v, '[0-9]+')
              AND TRY_CAST(v AS BIGINT) BETWEEN 1 AND {_PRICE_MAX}
        THEN TRY_CAST(v AS BIGINT)
        END
    )
    """,
)
_EVENT_HEADER = "event_id,user_id,ts,ingest_ts,event_type,product_id,price_cents"
_CLICK_HEADER = "click_id,user_id,campaign_id,ts"


def csv_files(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.csv"))


def plain_headers(files: list[Path], header: str) -> bool:
    want = header.encode() + b"\n"
    for path in files:
        with path.open("rb") as handle:
            if handle.read(len(want)) != want:
                return False
    return True


def parsed_sql(files: list[Path], header: str, fast: bool) -> str:
    """Well-formed data lines of the files as unquoted, nullable fields named by header.

    fast: read_csv splits lines and commas on all threads, with quoting off so the
    fields arrive raw, and drops lines of the wrong width. It also ends a line at a
    carriage return, which rules.md does not; in strict mode it raises on one instead.
    Not fast: each file is split as text on newlines only, on one thread.
    """
    names = header.split(",")
    raw = [f"f{i}" for i in range(len(names))]
    cols = ", ".join(f"fld({field}) AS {name}" for field, name in zip(raw, names, strict=True))
    ok = " AND ".join(f"fld_ok({field})" for field in raw)
    paths = ", ".join("'" + str(path).replace("'", "''") + "'" for path in files)
    if not files:
        source = "SELECT " + ", ".join(f"NULL::VARCHAR AS {field}" for field in raw)
        ok = "false"
    elif fast:
        columns = ", ".join(f"'{field}': 'VARCHAR'" for field in raw)
        source = f"""
            SELECT * FROM read_csv(
                [{paths}], columns = {{{columns}}}, auto_detect = false,
                delim = ',', quote = '', escape = '', new_line = '\\n',
                header = false, skip = 1, strict_mode = true, ignore_errors = true
            )
        """
    else:
        fields = ", ".join(f"p[{i}] AS {field}" for i, field in enumerate(raw, 1))
        source = f"""
            SELECT {fields}
            FROM (
                SELECT string_split(line, ',') AS p
                FROM (
                    SELECT unnest(string_split(content, chr(10))[2:]) AS line
                    FROM read_text([{paths}])
                )
            )
            WHERE len(p) = {len(raw)}
        """
    return f"SELECT {cols} FROM ({source}) WHERE {ok}"


def create_from_csv(
    con: duckdb.DuckDBPyConnection, files: list[Path], header: str, statement: str
) -> None:
    """Run a statement whose {parsed} is the parsed lines, on the fast reader when it is exact."""
    if plain_headers(files, header):
        try:
            con.execute(statement.format(parsed=parsed_sql(files, header, fast=True)))
            return
        except duckdb.InvalidInputException:
            pass  # a carriage return in the data: only the text reader keeps it in its field
    con.execute(statement.format(parsed=parsed_sql(files, header, fast=False)))


def load_lookup(con: duckdb.DuckDBPyConnection, table: str, path: Path, key: str, val: str) -> None:
    statement = f"""
        CREATE TABLE {table} AS
        SELECT {key}, min({val}) AS {val}
        FROM ({{parsed}})
        WHERE {key} IS NOT NULL AND {val} IS NOT NULL
        GROUP BY {key}
    """
    create_from_csv(con, [path], f"{key},{val}", statement)


def load_events(
    con: duckdb.DuckDBPyConnection, files: list[Path], window_start: int, window_end: int
) -> None:
    # Dedup first: the copy with the smallest ingest_ts is kept even if it is invalid.
    statement = f"""
        CREATE TABLE events AS
        SELECT event_id,
               k.user_id AS user_id,
               k.ts_ms AS ts_ms,
               k.event_type AS event_type,
               k.product_id AS product_id,
               k.price_cents AS price_cents
        FROM (
            SELECT event_id,
                   arg_min(
                       {{{{
                           'user_id': user_id,
                           'ts_ms': ts_ms(ts),
                           'ingest_ms': ts_ms(ingest_ts),
                           'event_type': event_type,
                           'product_id': product_id,
                           'price_cents':
                               CASE WHEN event_type = 'purchase' THEN price_val(price_cents) END
                       }}}},
                       ts_ms(ingest_ts)
                   ) AS k
            FROM ({{parsed}})
            WHERE event_id IS NOT NULL
            GROUP BY event_id
        )
        WHERE k.user_id IS NOT NULL
          AND k.product_id IS NOT NULL
          AND CASE WHEN k.ts_ms >= {window_start} AND k.ts_ms < {window_end}
                   THEN k.ingest_ms - k.ts_ms <= {_DAY_MS}
                   ELSE false
              END
          AND (
              k.event_type IN ('view', 'cart')
              OR (k.event_type = 'purchase' AND k.price_cents IS NOT NULL)
          )
    """
    create_from_csv(con, files, _EVENT_HEADER, statement)


def load_clicks(
    con: duckdb.DuckDBPyConnection, files: list[Path], click_start: int, window_end: int
) -> None:
    # Copies of a click hold identical values, so min() just picks that value.
    statement = f"""
        CREATE TABLE clicks AS
        SELECT click_id, user_id, campaign_id, ts_ms
        FROM (
            SELECT click_id,
                   min(user_id) AS user_id,
                   min(campaign_id) AS campaign_id,
                   min(ts_ms(ts)) AS ts_ms
            FROM ({{parsed}})
            WHERE click_id IS NOT NULL
            GROUP BY click_id
        )
        WHERE user_id IS NOT NULL
          AND ts_ms >= {click_start} AND ts_ms < {window_end}
          AND campaign_id IN (SELECT campaign_id FROM campaigns)
    """
    create_from_csv(con, files, _CLICK_HEADER, statement)


def link_pairs(folder: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in data_lines(folder):
        fields = parse_fields(line, 2)
        if fields is None or fields[0] is None or fields[1] is None:
            continue
        pairs.append((fields[0], fields[1]))
    return pairs


def insert_rows(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[tuple[str, str]],
    rows: list[tuple],
) -> None:
    decls = ", ".join(f"{name} {kind}" for name, kind in columns)
    con.execute(f"CREATE TABLE {table} ({decls})")
    if not rows:
        return
    select = ", ".join(f"unnest(?)::{kind} AS {name}" for name, kind in columns)
    step = 100_000
    for start in range(0, len(rows), step):
        chunk = rows[start : start + step]
        payload = [list(column) for column in zip(*chunk, strict=True)]
        con.execute(f"INSERT INTO {table} SELECT {select}", payload)


def scratch_dir() -> str:
    scratch = os.environ.get("SCRATCH_DIR", "/scratch")
    try:
        os.makedirs(os.path.join(scratch, "duckdb_tmp"), exist_ok=True)
    except OSError:
        scratch = tempfile.mkdtemp(prefix="ss-scratch-")
        os.makedirs(os.path.join(scratch, "duckdb_tmp"), exist_ok=True)
    return scratch


def connect() -> duckdb.DuckDBPyConnection:
    scratch = scratch_dir()
    con = duckdb.connect()
    con.execute(f"SET threads = {NCPU}")
    con.execute(f"SET memory_limit = '{max(1, int(MEM_GB * 0.8))}GB'")
    con.execute(f"SET temp_directory = '{scratch}/duckdb_tmp'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET TimeZone = 'UTC'")
    for macro in _MACROS:
        con.execute(macro)
    return con


class Stages:
    """Seconds per stage on stderr, so every run is also a profile."""

    def __init__(self) -> None:
        self.start = self.last = time.perf_counter()

    def done(self, label: str) -> None:
        now = time.perf_counter()
        print(f"stage {now - self.last:8.3f}s  {label}", file=sys.stderr)
        self.last = now

    def total(self) -> None:
        print(f"stage {time.perf_counter() - self.start:8.3f}s  total", file=sys.stderr)


def build(con: duckdb.DuckDBPyConnection, stages: Stages) -> None:
    con.execute("""
        CREATE TABLE events_p AS
        SELECT coalesce(n.person, e.user_id) AS person,
               e.event_id,
               e.ts_ms,
               e.event_type,
               coalesce(p.category, 'unknown') AS category,
               e.price_cents
        FROM events e
        LEFT JOIN person_name n ON e.user_id = n.user_id
        LEFT JOIN products p ON e.product_id = p.product_id
    """)
    stages.done("apply person names to events")
    con.execute("""
        CREATE TABLE clicks_p AS
        SELECT coalesce(n.person, c.user_id) AS person,
               c.click_id,
               c.campaign_id,
               c.ts_ms
        FROM clicks c
        LEFT JOIN person_name n ON c.user_id = n.user_id
    """)
    stages.done("apply person names to clicks")
    con.execute(f"""
        CREATE TABLE marked AS
        SELECT person, event_id, ts_ms, event_type, category, price_cents,
               CASE
                   WHEN prev_ts IS NULL OR ts_ms - prev_ts > {_GAP_MS} THEN 1
                   ELSE 0
               END AS brk
        FROM (
            SELECT *,
                   lag(ts_ms) OVER (PARTITION BY person ORDER BY ts_ms, event_id) AS prev_ts
            FROM events_p
        )
    """)
    stages.done("sessions: gap flags")
    con.execute("""
        CREATE TABLE in_session AS
        SELECT person, event_id, ts_ms, event_type, category, price_cents,
               sum(brk) OVER (
                   PARTITION BY person
                   ORDER BY ts_ms, event_id
                   ROWS UNBOUNDED PRECEDING
               ) AS sess_idx
        FROM marked
    """)
    stages.done("sessions: running index")
    con.execute("""
        CREATE TABLE sessions AS
        SELECT person,
               sess_idx,
               min(ts_ms) AS session_start,
               DATE '1970-01-01'
                   + CAST(min(ts_ms) // 86400000 AS INTEGER) AS day,
               count(*) AS n_events,
               coalesce(sum(price_cents), 0) AS revenue,
               count(price_cents) AS n_purchases
        FROM in_session
        GROUP BY person, sess_idx
    """)
    stages.done("sessions: aggregate")
    con.execute(f"""
        CREATE TABLE credited AS
        SELECT person, sess_idx, day, session_start, n_events, revenue, n_purchases,
               coalesce(campaign_id, 'organic') AS campaign_id
        FROM (
            SELECT s.person, s.sess_idx, s.day, s.session_start, s.n_events, s.revenue,
                   s.n_purchases, c.campaign_id, c.click_id,
                   row_number() OVER (
                       PARTITION BY s.person, s.sess_idx
                       ORDER BY (c.click_id IS NULL), c.ts_ms DESC, c.click_id
                   ) AS rn
            FROM sessions s
            LEFT JOIN clicks_p c
              ON c.person = s.person
             AND c.ts_ms <= s.session_start
             AND s.session_start - c.ts_ms <= {_WEEK_MS}
        )
        WHERE rn = 1
    """)
    stages.done("attribution")
    con.execute(f"""
        CREATE TABLE assisted AS
        SELECT DISTINCT s.person, s.sess_idx, s.day, c.campaign_id
        FROM sessions s
        JOIN clicks_p c
          ON c.person = s.person
         AND c.ts_ms <= s.session_start
         AND s.session_start - c.ts_ms <= {_WEEK_MS}
    """)
    stages.done("assists")
    con.execute("""
        CREATE TABLE top_category AS
        SELECT person, day, category AS top_category
        FROM (
            SELECT person, day, category, rev,
                   row_number() OVER (
                       PARTITION BY person, day
                       ORDER BY rev DESC, category
                   ) AS rn
            FROM (
                SELECT s.person, s.day, e.category, sum(e.price_cents) AS rev
                FROM in_session e
                JOIN sessions s ON s.person = e.person AND s.sess_idx = e.sess_idx
                WHERE e.event_type = 'purchase'
                GROUP BY s.person, s.day, e.category
            )
        )
        WHERE rn = 1
    """)
    stages.done("top category")
    con.execute("""
        CREATE TABLE user_daily AS
        SELECT s.person AS user_id,
               s.day,
               count(*)::BIGINT AS sessions,
               sum(s.n_events)::BIGINT AS events,
               sum(s.revenue)::BIGINT AS revenue_cents,
               t.top_category
        FROM sessions s
        LEFT JOIN top_category t ON t.person = s.person AND t.day = s.day
        GROUP BY s.person, s.day, t.top_category
    """)
    stages.done("user_daily table")
    con.execute("""
        CREATE TABLE campaign_daily AS
        SELECT x.campaign_id,
               CASE WHEN x.campaign_id = 'organic' THEN 'organic' ELSE cam.channel END AS channel,
               x.day,
               x.sessions,
               x.purchasing_sessions,
               x.revenue_cents,
               x.assisted_sessions
        FROM (
            SELECT coalesce(c.campaign_id, a.campaign_id) AS campaign_id,
                   coalesce(c.day, a.day) AS day,
                   coalesce(c.sessions, 0)::BIGINT AS sessions,
                   coalesce(c.purchasing_sessions, 0)::BIGINT AS purchasing_sessions,
                   coalesce(c.revenue_cents, 0)::BIGINT AS revenue_cents,
                   CASE
                       WHEN coalesce(c.campaign_id, a.campaign_id) = 'organic' THEN 0
                       ELSE coalesce(a.assisted_sessions, 0)
                   END::BIGINT AS assisted_sessions
            FROM (
                SELECT campaign_id, day,
                       count(*)::BIGINT AS sessions,
                       count(*) FILTER (WHERE n_purchases > 0)::BIGINT AS purchasing_sessions,
                       sum(revenue)::BIGINT AS revenue_cents
                FROM credited
                GROUP BY campaign_id, day
            ) c
            FULL OUTER JOIN (
                SELECT campaign_id, day, count(*)::BIGINT AS assisted_sessions
                FROM assisted
                GROUP BY campaign_id, day
            ) a USING (campaign_id, day)
        ) x
        LEFT JOIN campaigns cam ON cam.campaign_id = x.campaign_id
    """)
    stages.done("campaign_daily table")


def write_table(con: duckdb.DuckDBPyConnection, table: str, columns: str, order_by: str) -> None:
    folder = Path(OUT) / table
    folder.mkdir(parents=True, exist_ok=True)
    for path in folder.iterdir():
        if path.is_file() or path.is_symlink():
            path.unlink()
    dest = str(folder / "part-0.parquet").replace("'", "''")
    # One thread and a total order. Parallel COPY splits row groups in
    # completion order, so two correct runs were not byte-identical.
    con.execute("SET threads = 1")
    con.execute(
        f"COPY (SELECT {columns} FROM {table} ORDER BY {order_by}) TO '{dest}' (FORMAT PARQUET)"
    )
    con.execute(f"SET threads = {NCPU}")


def main() -> None:
    stages = Stages()
    root = Path(DATA)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    window_start = ts_to_ms(manifest["window_start"])
    window_end = ts_to_ms(manifest["window_end"])
    if window_start is None or window_end is None:
        raise SystemExit("manifest timestamps are not valid F1")
    con = connect()
    self_check(con)
    stages.done("startup and self-check")
    load_lookup(con, "products", root / "products.csv", "product_id", "category")
    load_lookup(con, "campaigns", root / "campaigns.csv", "campaign_id", "channel")
    stages.done("products and campaigns")
    load_events(con, csv_files(root / "events"), window_start, window_end)
    stages.done("events: parse, dedup, validate")
    load_clicks(con, csv_files(root / "clicks"), window_start - _WEEK_MS, window_end)
    stages.done("clicks: parse, dedup, validate")
    pairs = link_pairs(root / "links")
    stages.done("links: parse")
    names = person_names(pairs)
    stages.done("id linking: union-find")
    insert_rows(
        con,
        "person_name",
        [("user_id", "VARCHAR"), ("person", "VARCHAR")],
        names,
    )
    stages.done("id linking: insert names")
    build(con, stages)
    write_table(
        con,
        "user_daily",
        "user_id, day, sessions, events, revenue_cents, top_category",
        "user_id, day",
    )
    write_table(
        con,
        "campaign_daily",
        "campaign_id, channel, day, sessions, purchasing_sessions, "
        "revenue_cents, assisted_sessions",
        "campaign_id, day",
    )
    stages.done("write parquet")
    counts = con.execute(
        "SELECT (SELECT count(*) FROM events), (SELECT count(*) FROM clicks)"
    ).fetchone()
    con.close()
    print(
        f"window {manifest['window_start']} {manifest['window_end']} "
        f"events {counts[0]} clicks {counts[1]} links {len(names)}",
        file=sys.stderr,
    )
    stages.total()


if __name__ == "__main__":
    main()
