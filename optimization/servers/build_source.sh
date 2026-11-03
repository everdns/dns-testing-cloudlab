#!/bin/bash
# Build a DNS server from a source tree on the name server (source_tools.py copies
# this script and the tree over, then runs it).
#
#   build_source.sh build    <bind|knot|nsd> <src_dir> <build_id>
#   build_source.sh activate <bind|knot|nsd> <build_id>
#
# Every build of one software installs into the same prefix,
# /opt/dnsbuild/<sw>/prefix, which ns_software/custom_server/start.sh runs when
# its info.yaml names that software.
# A fixed prefix keeps libtool's baked-in library paths valid, and lets make
# rebuild only what a patch touched. Each installed build is kept as
# /opt/dnsbuild/<sw>/builds/<build_id>.tgz; "activate" puts one back.
#
# The config and state paths match the distro/package install (sysconfdir,
# localstatedir), so the source build reads the same /etc config and zone files
# as the package server it is compared with. Native build: no arch flags (nodes
# may be x86_64 or aarch64). CFLAGS default to -O2 -g -fno-omit-frame-pointer,
# for both the from-source baseline and every patch, so perf can unwind stacks
# and comparisons stay like for like.
set -euo pipefail

ACTION=$1
SW=$2
ROOT=/opt/dnsbuild/$SW
PREFIX=$ROOT/prefix
BUILDS=$ROOT/builds
export CFLAGS=${CFLAGS:--O2 -g -fno-omit-frame-pointer}
JOBS=$(nproc)

sudo mkdir -p "$BUILDS"

deps() {
    stamp=$ROOT/.deps-installed
    [ -f "$stamp" ] && return
    common="build-essential pkg-config autoconf automake libtool git"
    case $SW in
        bind) pkgs="$common meson ninja-build libuv1-dev libssl-dev libcap-dev libnghttp2-dev
                    libxml2-dev libjson-c-dev liburcu-dev libidn2-dev libjemalloc-dev perl python3-ply" ;;
        knot) pkgs="$common liburcu-dev libgnutls28-dev libedit-dev liblmdb-dev libbpf-dev
                    libmnl-dev libnghttp2-dev" ;;
        nsd)  pkgs="$common libssl-dev libevent-dev bison flex" ;;
        *) echo "unknown software $SW" >&2; exit 2 ;;
    esac
    sudo apt-get update -y
    # shellcheck disable=SC2086
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y $pkgs
    sudo touch "$stamp"
}

configure_args() {
    case $SW in
        bind) echo "--prefix=$PREFIX --sysconfdir=/etc/bind --localstatedir=/var" ;;
        knot) echo "--prefix=$PREFIX --sysconfdir=/etc --localstatedir=/var --with-rundir=/run/knot
                    --disable-documentation" ;;
        nsd)  echo "--prefix=$PREFIX --sysconfdir=/etc/nsd --localstatedir=/var --with-dbdir=/var/db/nsd" ;;
    esac
}

build() {
    SRC=$1
    ID=$2
    deps
    cd "$SRC"
    # Reconfigure only when the flags change; otherwise make rebuilds what changed.
    want="$(configure_args) CFLAGS=$CFLAGS"
    if [ -f meson.build ] && [ "$SW" = bind ]; then
        if [ ! -d build ] || [ "$(cat .campaign-configured 2>/dev/null)" != "$want" ]; then
            rm -rf build
            meson setup build --prefix="$PREFIX" --sysconfdir=/etc/bind --localstatedir=/var \
                --buildtype=plain -Dc_args="$CFLAGS"
            echo "$want" > .campaign-configured
        fi
        ninja -C build
        sudo rm -rf "$PREFIX"
        sudo ninja -C build install
    else
        if [ ! -f Makefile ] || [ "$(cat .campaign-configured 2>/dev/null)" != "$want" ]; then
            [ -x configure ] || autoreconf -fi
            # shellcheck disable=SC2046
            ./configure $(configure_args)
            echo "$want" > .campaign-configured
        fi
        make -j"$JOBS"
        sudo rm -rf "$PREFIX"
        sudo make install
    fi
    echo "$ID" | sudo tee "$PREFIX/BUILD_ID" >/dev/null
    sudo tar -czf "$BUILDS/$ID.tgz" -C "$PREFIX" .
    echo "installed $ID into $PREFIX"
}

activate() {
    ID=$1
    if [ "$(cat "$PREFIX/BUILD_ID" 2>/dev/null)" = "$ID" ]; then
        echo "$ID already active"
        return
    fi
    [ -f "$BUILDS/$ID.tgz" ] || { echo "no saved build $ID" >&2; exit 1; }
    sudo rm -rf "$PREFIX"
    sudo mkdir -p "$PREFIX"
    sudo tar -xzf "$BUILDS/$ID.tgz" -C "$PREFIX"
    echo "activated $ID"
}

case $ACTION in
    build) build "$3" "$4" ;;
    activate) activate "$3" ;;
    *) echo "usage: $0 build <sw> <src_dir> <build_id> | activate <sw> <build_id>" >&2; exit 2 ;;
esac
