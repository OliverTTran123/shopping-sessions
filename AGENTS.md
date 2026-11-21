# Track: shopping-sessions

Read `../AGENTS.md` first. Its rules apply here. This file holds only what is specific to this track.

## Problem
<2 to 3 sentences from the kit, in plain words.>

## Scoring rule
<Exact rule from the kit. Direction (higher or lower wins). Tie-breaks. Hidden test set size if stated.>

## Commands
- Produce predictions: see `RUN` in `Makefile`.
- Local score: see `SCORE` in `Makefile`. It approximates the hidden score, it does not equal it.
- Everything: `make check`. It prints one `CHECK` summary line.

## Data
<Paths, formats, what is labeled, what is held out. Never tune on the held-out split.>

## Limits
- Runtime limit: <seconds, or "none stated">. Set `LIMIT_S` in `Makefile`.
- Allowed tools or libraries: <from the kit, or "not stated, ask">.
- Submission limits: <count, cooldown, whether a worse score overwrites a better one>.

## Submission format
<Exact file name and shape.>

## Current best
<Score, date, commit. Update when a change is kept.>

## Notes
Attempts live in `decisions.tsv`. Read it before proposing an idea so you do not repeat a failed one.
