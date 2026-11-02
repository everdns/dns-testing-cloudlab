#!/bin/sh
# Stop the source-built BIND (ns_bind_src)
if pgrep -f '^/opt/dnsbuild/bind/' >/dev/null 2>&1; then
    echo "Stopping source-built named..."
    sudo pkill -f '^/opt/dnsbuild/bind/'
    for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -f '^/opt/dnsbuild/bind/' >/dev/null || exit 0; sleep 1; done
    sudo pkill -9 -f '^/opt/dnsbuild/bind/'
fi
