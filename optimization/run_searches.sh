#!/bin/bash
# Run three configuration searches one after another, resetting the name
# server to its default configuration after each.
#
#   1. agent_search.py --mode bounded      Claude picks values within the YAML bounds
#   2. grid_search.py --mode coordinate-descent
#   3. agent_search.py --mode open         Claude may change anything on the name server
#
# Run from /local/repository/optimization on the load generator:
#   nohup setsid bash run_searches.sh > ~/searches/run.log 2>&1 &
#
# Usage: run_searches.sh [OUTPUT_DIR]   (default ~/searches)
# Environment: MAX_CONFIGS (default 20), AGENT_BACKEND (default claude-code),
#              ENV_NAME (hardware type recorded in the ledger, default: hostname)
#
# Every measurement is validity-checked and appended to OUTPUT_DIR/ledger.jsonl.
# For continuous runs use campaign/campaign.py instead.
#
# A failing search does not stop the next one. Exit code 3 from a search or
# from the reset means the name server may be in an unknown state, so the run
# stops there. ``DONE`` in OUTPUT_DIR lists each search and its exit code.
export PATH="$HOME/.local/bin:$PATH"   # where the claude CLI installs
cd "$(dirname "$0")" || exit 2

OUT=${1:-$HOME/searches}
MAX_CONFIGS=${MAX_CONFIGS:-20}
BACKEND=${AGENT_BACKEND:-claude-code}
ENV_NAME=${ENV_NAME:-$(hostname -s)}
COMMON=(--ledger "$OUT/ledger.jsonl" --env "$ENV_NAME" --validate)
mkdir -p "$OUT"
: > "$OUT/DONE"

log() { echo "[$(date -u +%FT%TZ)] $*"; }

log "Snapshotting the name server"
python3 searches_reset.py snapshot --out "$OUT/pristine.json" || { log "snapshot failed"; exit 2; }

run_search() {
    local name=$1; shift
    log "START $name: $*"
    "$@"
    local rc=$?
    log "END $name (exit $rc)"
    echo "$name $rc" >> "$OUT/DONE"

    log "Resetting the name server after $name"
    python3 searches_reset.py reset --baseline "$OUT/pristine.json"
    local reset_rc=$?
    echo "reset_after_$name $reset_rc" >> "$OUT/DONE"
    if [ "$rc" -eq 3 ] || [ "$reset_rc" -eq 3 ]; then
        log "STOP: the name server could not be restored; fix it before running again"
        exit 3
    fi
}

run_search 1_agent_bounded \
    python3 agent_search.py --mode bounded --agent-backend "$BACKEND" \
        --max-configs "$MAX_CONFIGS" --output-dir "$OUT/1_agent_bounded" "${COMMON[@]}"

run_search 2_coordinate_descent \
    python3 grid_search.py --mode coordinate-descent --max-passes 3 \
        --output-dir "$OUT/2_coordinate_descent" "${COMMON[@]}"

run_search 3_agent_open \
    python3 agent_search.py --mode open --agent-backend "$BACKEND" \
        --max-configs "$MAX_CONFIGS" --output-dir "$OUT/3_agent_open" "${COMMON[@]}"

log "ALL DONE"
echo "finished $(date -u +%FT%TZ)" >> "$OUT/DONE"
