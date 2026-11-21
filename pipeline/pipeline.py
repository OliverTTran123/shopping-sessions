#!/usr/bin/env python3
"""Shopping Sessions pipeline. rules.md wins over the starter skeleton.

Cleaning (CSV dialect, dedup, validation, late events, linked-id names) is done
here. DuckDB sessionizes, attributes, and aggregates.
"""

import json
import os
import re
import sys
import tempfile
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
_PRICE = re.compile(r"^[0-9]+$")
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


def price_value(raw: str | None) -> int | None:
    if raw is None or _PRICE.fullmatch(raw) is None:
        return None
    number = int(raw)
    if 1 <= number <= _PRICE_MAX:
        return number
    return None


def self_check() -> None:
    assert ts_to_ms("1970-01-01T00:00:00Z") == 0
    assert ts_to_ms("1970-01-02T00:00:00Z") == _DAY_MS
    assert ts_to_ms("86400000") == _DAY_MS
    same = ts_to_ms("2026-10-03T23:58:10Z")
    assert same == 1_791_071_890_000
    assert ts_to_ms("2026-10-03T19:58:10-04:00") == same
    assert ts_to_ms("0001791071890000") == ts_to_ms("1791071890000")
    assert ts_to_ms("2026-10-03T10:00:00-00:00") == ts_to_ms("2026-10-03T10:00:00Z")
    assert ts_to_ms("2026-10-03T10:00:00+23:59") is not None
    assert ts_to_ms("2028-02-29T00:00:00Z") is not None
    for bad in (
        "2026-02-29T00:00:00Z",
        "2026-10-03T24:00:00Z",
        "2026-10-03T10:00:60Z",
        "2026-10-03T10:00:00z",
        "2026-10-03T10:00:00+24:00",
        "2026-10-03T10:00:00+05:60",
        "2026-10-03T10:00:00+0200",
        "1791071890000.0",
        "١٧٩١٠٢١٦٠٠٠٠٠",
        "２026-10-03T10:00:00Z",
        "",
    ):
        assert ts_to_ms(bad) is None, bad
    assert price_value("0250") == 250
    assert price_value("10000000") == _PRICE_MAX
    assert price_value("10000001") is None
    assert price_value("０250") is None
    assert price_value("٢٥٠") is None


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


def file_lines(path: Path) -> list[str]:
    parts = path.read_bytes().decode("utf-8").split("\n")
    return [line for line in parts[1:] if line != ""]


def load_lookup(path: Path) -> dict[str, str]:
    table: dict[str, str] = {}
    for line in file_lines(path):
        fields = parse_fields(line, 2)
        if fields is None or fields[0] is None or fields[1] is None:
            continue
        table[fields[0]] = fields[1]
    return table


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


def surviving_events(
    folder: Path, window_start: int, window_end: int
) -> list[tuple[str, str, int, str, str, int | None]]:
    # ingest_ms, ts_ms, user_id, event_type, product_id, price_raw
    best: dict[
        str, tuple[int | None, int | None, str | None, str | None, str | None, str | None]
    ] = {}
    for line in data_lines(folder):
        fields = parse_fields(line, 7)
        if fields is None or fields[0] is None:
            continue
        event_id, user_id, ts, ingest, event_type, product_id, price_raw = fields
        ingest_ms = ts_to_ms(ingest)
        current = (ingest_ms, ts_to_ms(ts), user_id, event_type, product_id, price_raw)
        previous = best.get(event_id)
        if previous is None or (
            ingest_ms is not None and (previous[0] is None or ingest_ms < previous[0])
        ):
            best[event_id] = current
    rows: list[tuple[str, str, int, str, str, int | None]] = []
    for event_id, (ingest_ms, ts_ms, user_id, event_type, product_id, price_raw) in best.items():
        if not user_id or ts_ms is None or ingest_ms is None or not product_id:
            continue
        if event_type not in ("view", "cart", "purchase"):
            continue
        if not (window_start <= ts_ms < window_end):
            continue
        if ingest_ms - ts_ms > _DAY_MS:
            continue
        price_cents = None
        if event_type == "purchase":
            price_cents = price_value(price_raw)
            if price_cents is None:
                continue
        rows.append((event_id, user_id, ts_ms, event_type, product_id, price_cents))
    return rows


def surviving_clicks(
    folder: Path, click_start: int, window_end: int, campaigns: dict[str, str]
) -> list[tuple[str, str, str, int]]:
    first: dict[str, tuple[str | None, str | None, int | None]] = {}
    for line in data_lines(folder):
        fields = parse_fields(line, 4)
        if fields is None or fields[0] is None:
            continue
        click_id, user_id, campaign_id, ts = fields
        if click_id not in first:
            first[click_id] = (user_id, campaign_id, ts_to_ms(ts))
    rows: list[tuple[str, str, str, int]] = []
    for click_id, (user_id, campaign_id, ts_ms) in first.items():
        if not user_id or campaign_id is None or ts_ms is None:
            continue
        if campaign_id not in campaigns:
            continue
        if click_start <= ts_ms < window_end:
            rows.append((click_id, user_id, campaign_id, ts_ms))
    return rows


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
    return con


def build(con: duckdb.DuckDBPyConnection) -> None:
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
    con.execute("""
        CREATE TABLE clicks_p AS
        SELECT coalesce(n.person, c.user_id) AS person,
               c.click_id,
               c.campaign_id,
               c.ts_ms
        FROM clicks c
        LEFT JOIN person_name n ON c.user_id = n.user_id
    """)
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
    con.execute(f"""
        CREATE TABLE assisted AS
        SELECT DISTINCT s.person, s.sess_idx, s.day, c.campaign_id
        FROM sessions s
        JOIN clicks_p c
          ON c.person = s.person
         AND c.ts_ms <= s.session_start
         AND s.session_start - c.ts_ms <= {_WEEK_MS}
    """)
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
    self_check()
    root = Path(DATA)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    window_start = ts_to_ms(manifest["window_start"])
    window_end = ts_to_ms(manifest["window_end"])
    if window_start is None or window_end is None:
        raise SystemExit("manifest timestamps are not valid F1")
    products = load_lookup(root / "products.csv")
    campaigns = load_lookup(root / "campaigns.csv")
    events = surviving_events(root / "events", window_start, window_end)
    clicks = surviving_clicks(root / "clicks", window_start - _WEEK_MS, window_end, campaigns)
    names = person_names(link_pairs(root / "links"))
    print(
        f"window {manifest['window_start']} {manifest['window_end']} "
        f"events {len(events)} clicks {len(clicks)} links {len(names)}",
        file=sys.stderr,
    )
    con = connect()
    insert_rows(
        con,
        "events",
        [
            ("event_id", "VARCHAR"),
            ("user_id", "VARCHAR"),
            ("ts_ms", "BIGINT"),
            ("event_type", "VARCHAR"),
            ("product_id", "VARCHAR"),
            ("price_cents", "BIGINT"),
        ],
        events,
    )
    insert_rows(
        con,
        "clicks",
        [
            ("click_id", "VARCHAR"),
            ("user_id", "VARCHAR"),
            ("campaign_id", "VARCHAR"),
            ("ts_ms", "BIGINT"),
        ],
        clicks,
    )
    insert_rows(
        con,
        "person_name",
        [("user_id", "VARCHAR"), ("person", "VARCHAR")],
        names,
    )
    insert_rows(
        con,
        "products",
        [("product_id", "VARCHAR"), ("category", "VARCHAR")],
        list(products.items()),
    )
    insert_rows(
        con,
        "campaigns",
        [("campaign_id", "VARCHAR"), ("channel", "VARCHAR")],
        list(campaigns.items()),
    )
    build(con)
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
    con.close()


if __name__ == "__main__":
    main()
