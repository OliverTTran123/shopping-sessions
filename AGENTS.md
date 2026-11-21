# Track: shopping-sessions (Databricks — "Shopping Sessions")

Read `../AGENTS.md` first. Its rules apply here. This file holds only what is specific to this track.

## Problem
Turn 60 million messy click-stream CSV events (page views, add-to-carts, purchases, ad clicks) into two exact daily Parquet tables: `/output/user_daily/` and `/output/campaign_daily/`. Input logs have duplicates, malformed lines, bad timestamps, late events, and one shopper can have many user IDs (a third linkage file ties them across chains and shared devices). Must: clean logs, join each person's IDs, build sessions, credit each session to the ad click that brought the shopper in, count assisting campaigns from the week before. There is exactly one right answer per the rules — a fast wrong answer scores nothing.

Full rules are authoritative in `rules.md` (download from the submission page). Where a starter and `rules.md` disagree, `rules.md` wins.

## Scoring rule
- Metric: container run time in seconds, **lower is better**.
- Scorer version: `shopping-sessions-4`.
- Correctness is a gate, not part of the score: a wrong table, a crash, or hitting memory/disk/stdout/time limits stops the submission entirely (no score).
- Practice: run on hidden practice data (~20M events). A run under 20s is timed 3 times and scored on the **median**; a run ≥20s is timed once. Practice run cap: min(2× baseline, 5 min) = ~5 min effectively, baseline 172.00s (PySpark starter) so 2x = 344s capped at 300s.
- Final: ~60M events (≈3× practice), each run capped at **3 minutes**. Over 3 min = no final score. **Aim for well under 60s in practice** to have margin.
- At least the top 10 final entries get retimed 7× alone on an idle machine; that median replaces their first score.
- Practice times can vary ~20% across scoring machines — small gaps on the practice board are noise, don't chase them.
- Clock runs container start to container exit, including engine startup and CSV reads.

## Commands
- Build/run: see starter's Dockerfile and `make` targets once downloaded (PySpark / DuckDB / Polars starters all available — **recommend starting from the DuckDB starter**: lowest cold-start overhead of the three for a single container, native Parquet, handles out-of-core spills, fits the 8-vCPU/24GB box well, and most directly matches what we researched for this kind of exact+fast session pipeline).
- Local dev check: `make check-dev` against the 1M-event dev set (generate with `generator.zip`, expected output in `dev-answer.zip`).
- Full local check: `make check` here in the kit (wraps our own scorer/tests on top of the starter's dev-set check).

## Data
- Input: `/data` (read-only at runtime), CSV: page views, add-to-carts, purchases, ad clicks, plus an ID-linkage file.
- Dev set: 1M events, seed 1, via `generator.zip`; expected tables in `dev-answer.zip`.
- Practice (hidden): ~20M events. Final (hidden): ~60M events, generated fresh after submissions close.
- Output: exactly two Parquet tables, `/output/user_daily/` (one row per person per day) and `/output/campaign_daily/` (one row per campaign per day), written with the exact columns `rules.md` specifies.

## Limits
- Image: linux/amd64, ≤ 8 GB.
- Build: network access, ≤ 10 min.
- Run: **no network**, 8 pinned vCPUs (hyperthreads), 24 GB RAM, no swap.
- `/data` read-only; `/scratch` writable for spills; `/output` + `/scratch` ≤ 50 GB total.
- Root filesystem read-only except `/tmp` (64 MB, in-memory); runs as uid 1000.
- Env vars set at runtime: `DATA_DIR=/data`, `OUTPUT_DIR=/output`, `SCRATCH_DIR=/scratch`, `HOME=/scratch`, `NCPU=8`, `MEM_GB=24`.
- ≤ 4,096 processes/threads; ≤ 50 MB on stdout (log to stderr instead).
- **Submission limit: 2/hour.** Rejected submissions don't change your best score, but still read the local dev-set check output before submitting — don't burn a slot on a dev-check failure.
- Repo: public GitHub, Dockerfile at root, ≤ 50 MB, no data checked in (dataset is mounted at runtime). Keep every submitted commit in the repo until results are published.

## Submission format
Public GitHub repository URL + the commit hash to score, submitted on the track page.

## Current best
Baseline to beat: **172.00s** (PySpark starter, local mode). Target: comfortably under 60s in practice.
Not yet attempted — no commit scored yet.

## Notes
Attempts live in `decisions.tsv`. Read it before proposing an idea so you do not repeat a failed one.

Priority order for the first valid submission:
1. Get the DuckDB starter building and passing its own 36 edge-case fixtures.
2. Get `make check-dev` passing exactly (zero diff rows) on the 1M dev set — correctness first, speed second, since a fast wrong answer scores 0.
3. Only then optimize: push toward <60s via columnar/vectorized ops, avoiding repeated parses, batching I/O, and using `/scratch` for spills instead of failing on memory.
4. Submit early once `check-dev` passes cleanly, even if slow — get a real score on the board, then iterate within the 2/hour budget.
