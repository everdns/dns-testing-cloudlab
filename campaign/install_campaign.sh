#!/bin/bash
# Install the campaign daemon on test_host_0 as a systemd user service.
#
#   bash /local/repository/campaign/install_campaign.sh <env-name>
#
# Writes ~/campaign/campaign.yaml (env set, other values from the repo copy) if
# it does not exist yet, enables lingering so the service survives logout, and
# starts dns-campaign. It restarts on failure, not after a clean exit or exit
# 3 (server could not be restored), so `python3 campaign.py stop` (the STOP
# file) ends it for good.
#
#   journalctl --user -u dns-campaign -f        # follow it
#   python3 /local/repository/campaign/campaign.py status
set -euo pipefail

ENV_NAME=${1:?usage: $0 <env-name, e.g. rs620>}
HERE=$(cd "$(dirname "$0")" && pwd)
STATE=$HOME/campaign
mkdir -p "$STATE"

if [ ! -f "$STATE/campaign.yaml" ]; then
    sed -e "s|^env: .*|env: $ENV_NAME|" \
        -e "s|^search_config: .*|search_config: $HERE/../optimization/agent_search.yaml|" \
        "$HERE/campaign.yaml" > "$STATE/campaign.yaml"
    echo "wrote $STATE/campaign.yaml"
fi

command -v claude >/dev/null || [ -x "$HOME/.local/bin/claude" ] || \
    echo "WARNING: claude not found; install Claude Code and log in (explore/source steps need it)"
python3 -c "import yaml" 2>/dev/null || sudo apt-get install -y python3-yaml

sudo loginctl enable-linger "$USER"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/dns-campaign.service" <<EOF
[Unit]
Description=DNS optimization campaign ($ENV_NAME)
After=network-online.target

[Service]
Environment=PATH=$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WorkingDirectory=$STATE
ExecStart=/usr/bin/python3 $HERE/campaign.py run --config $STATE/campaign.yaml
Restart=on-failure
RestartSec=300
# 3: the name server could not be restored; restarting would not help.
RestartPreventExitStatus=3

[Install]
WantedBy=default.target
EOF

export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
systemctl --user daemon-reload
systemctl --user enable --now dns-campaign
systemctl --user --no-pager status dns-campaign | head -5
