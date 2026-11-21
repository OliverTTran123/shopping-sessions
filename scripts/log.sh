#!/bin/sh
# Append one attempt to decisions.tsv.
# Usage: scripts/log.sh "<hypothesis>" "<change>" <before> <after> <kept|reverted> "<note>"
set -eu
LOG="${LOG:-decisions.tsv}"
[ -f "$LOG" ] || printf 'ts\tid\thypothesis\tchange\tbefore\tafter\tverdict\tnote\n' > "$LOG"

clean() {
  # one line, no tabs, and neutralize spreadsheet formula starts
  v=$(printf '%s' "$1" | tr '\t\n' '  ')
  case "$v" in
    =*|+*|-*|@*) v="'$v" ;;
  esac
  printf '%s' "$v"
}

id=$(( $(wc -l < "$LOG") ))
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$ts" "$id" "$(clean "${1:-}")" "$(clean "${2:-}")" "$(clean "${3:-}")" \
  "$(clean "${4:-}")" "$(clean "${5:-}")" "$(clean "${6:-}")" >> "$LOG"
echo "logged attempt $id ($5)"
