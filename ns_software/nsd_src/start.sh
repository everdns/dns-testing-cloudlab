#!/bin/sh
# Start the source-built NSD in /opt/dnsbuild/nsd/prefix with the installed /etc/nsd/nsd.conf.
P=/opt/dnsbuild/nsd/prefix
[ -x "$P/sbin/nsd" ] || { echo "no source build of NSD in $P"; exit 1; }
sudo pkill -x nsd 2>/dev/null
sudo mkdir -p /var/db/nsd
sudo "$P/sbin/nsd" -c /etc/nsd/nsd.conf
