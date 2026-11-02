#!/bin/sh
# Stop the source-built NSD (ns_nsd_src)
if pgrep -f '^/opt/dnsbuild/nsd/' >/dev/null 2>&1; then
    echo "Stopping source-built nsd..."
    sudo pkill -f '^/opt/dnsbuild/nsd/'
    for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -f '^/opt/dnsbuild/nsd/' >/dev/null || exit 0; sleep 1; done
    sudo pkill -9 -f '^/opt/dnsbuild/nsd/'
fi
