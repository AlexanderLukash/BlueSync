#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v pactl >/dev/null
/usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
install -d "$HOME/.local/share/bluesync/bluesync" "${XDG_CONFIG_HOME:-$HOME/.config}/bluesync" "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
# Stop the previous version before replacing its files.
if systemctl --user is-active --quiet bluesync.service; then
    systemctl --user stop bluesync.service
fi
install -m 644 bluesync/*.py "$HOME/.local/share/bluesync/bluesync/"
if [ ! -f "${XDG_CONFIG_HOME:-$HOME/.config}/bluesync/config.toml" ]; then
    install -m 644 config.toml "${XDG_CONFIG_HOME:-$HOME/.config}/bluesync/config.toml"
fi
install -m 644 systemd/bluesync.service "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/bluesync.service"
systemctl --user daemon-reload
systemctl --user enable --now bluesync.service
systemctl --user --no-pager status bluesync.service
