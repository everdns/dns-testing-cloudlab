#!/bin/sh
# Start the source-built Knot DNS in /opt/dnsbuild/knot/prefix with the package's /etc/knot/knot.conf.
P=/opt/dnsbuild/knot/prefix
[ -x "$P/sbin/knotd" ] || { echo "no source build of Knot DNS in $P"; exit 1; }
sudo systemctl stop knot 2>/dev/null
sudo mkdir -p /run/knot && sudo chown knot:knot /run/knot
sudo "$P/sbin/knotd" -c /etc/knot/knot.conf -d
