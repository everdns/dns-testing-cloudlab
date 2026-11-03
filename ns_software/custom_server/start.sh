#!/bin/sh
# Start the custom (source-built) DNS server described by info.yaml:
# stop the package server, run prepare, then <prefix>/<binary> <args> $OPTIONS.
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
val() { sed -n "s/^$1:[[:space:]]*//p" "$DIR/info.yaml" | head -1; }

BIN="$(val prefix)/$(val binary)"
[ -x "$BIN" ] || { echo "no custom $(val software) build at $BIN"; exit 1; }

OPTIONS=$(val options_default)
OPTIONS_FILE=$(val options_file)
# shellcheck disable=SC1090
[ -n "$OPTIONS_FILE" ] && [ -f "$OPTIONS_FILE" ] && . "$OPTIONS_FILE"

STOP_PACKAGE=$(val stop_package)
[ -n "$STOP_PACKAGE" ] && sudo sh -c "$STOP_PACKAGE" 2>/dev/null
PREPARE=$(val prepare)
[ -n "$PREPARE" ] && sudo sh -c "$PREPARE"

# shellcheck disable=SC2046,SC2086
sudo "$BIN" $(val args) $OPTIONS
