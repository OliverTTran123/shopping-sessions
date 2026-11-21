#!/bin/sh
# Fail if scorer/ changed since `make freeze`.
set -eu
if [ ! -f scorer/.frozen.sha ]; then
  echo "verify-frozen: scorer is not frozen yet. Run 'make freeze' once the scorer is correct." >&2
  exit 1
fi
current=$(find scorer -type f ! -name '.frozen.sha' ! -path '*__pycache__*' | sort \
  | xargs sha256sum | sha256sum | cut -d' ' -f1)
if [ "$current" != "$(cat scorer/.frozen.sha)" ]; then
  echo "verify-frozen: scorer/ changed. Revert it, or have the operator re-freeze on purpose." >&2
  exit 1
fi
echo "verify-frozen: scorer unchanged"
