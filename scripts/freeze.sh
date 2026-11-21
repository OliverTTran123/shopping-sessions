#!/bin/sh
# Record the hash of scorer/ so later edits are detected. Run once after the scorer is correct.
set -eu
hash=$(find scorer -type f ! -name '.frozen.sha' ! -path '*__pycache__*' | sort \
  | xargs sha256sum | sha256sum | cut -d' ' -f1)
printf '%s\n' "$hash" > scorer/.frozen.sha
echo "scorer frozen: $hash"
