#!/usr/bin/env bash
# Install graphics-sync for the current user (no root needed).
#   ./install.sh              link the command into ~/.local/bin
#   ./install.sh --autostart  also check at every Plasma login and notify if out of sync
#   ./install.sh --uninstall  remove both
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="$HOME/.local/bin/graphics-sync"
autostart="$HOME/.config/autostart/graphics-sync.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$bin" "$autostart"
    echo "Removed $bin and $autostart"
    exit 0
fi

mkdir -p "$(dirname "$bin")"
chmod +x "$here/bin/graphics-sync"
ln -sf "$here/bin/graphics-sync" "$bin"
echo "Linked $bin -> $here/bin/graphics-sync"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "Note: ~/.local/bin is not on your PATH; open a new terminal or add it to ~/.profile." ;;
esac

if [[ "${1:-}" == "--autostart" ]]; then
    mkdir -p "$(dirname "$autostart")"
    cat > "$autostart" <<EOF
[Desktop Entry]
Type=Application
Name=graphics-sync check
Comment=Warn if the running kernel and NVIDIA driver are out of sync
Exec=$here/bin/graphics-sync --notify
X-KDE-autostart-phase=2
NoDisplay=true
EOF
    echo "Installed login check: $autostart"
fi
