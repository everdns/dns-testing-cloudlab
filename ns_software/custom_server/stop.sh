#!/bin/sh
# Stop the custom (source-built) DNS server (ns_custom_server): every process
# matching info.yaml's kill_pattern, whichever software it is.
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
val() { sed -n "s/^$1:[[:space:]]*//p" "$DIR/info.yaml" | head -1; }

PATTERN=$(val kill_pattern)
[ -n "$PATTERN" ] || exit 0
if pgrep -f "$PATTERN" >/dev/null 2>&1; then
    echo "Stopping custom $(val software) server..."
    sudo pkill -f "$PATTERN"
    for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "$PATTERN" >/dev/null || exit 0; sleep 1; done
    sudo pkill -9 -f "$PATTERN"
fi
