#!/bin/sh
# Stop the source-built Knot DNS (ns_knot_src)
if pgrep -f '^/opt/dnsbuild/knot/' >/dev/null 2>&1; then
    echo "Stopping source-built knotd..."
    sudo pkill -f '^/opt/dnsbuild/knot/'
    for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -f '^/opt/dnsbuild/knot/' >/dev/null || exit 0; sleep 1; done
    sudo pkill -9 -f '^/opt/dnsbuild/knot/'
fi
