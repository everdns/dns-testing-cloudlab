#!/bin/sh
# Start the source-built BIND in /opt/dnsbuild/bind/prefix (optimization/servers/build_source.sh)
# with the package's /etc/bind config and the same OPTIONS (/etc/default/named).
P=/opt/dnsbuild/bind/prefix
[ -x "$P/sbin/named" ] || { echo "no source build of BIND in $P"; exit 1; }
OPTIONS="-u bind"
[ -f /etc/default/named ] && . /etc/default/named
sudo systemctl stop named 2>/dev/null
sudo mkdir -p /run/named && sudo chown bind:bind /run/named
# shellcheck disable=SC2086
sudo "$P/sbin/named" -c /etc/bind/named.conf $OPTIONS
