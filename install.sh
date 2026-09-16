#!/usr/bin/env bash
# Install / uninstall waydroid-tray.
#   ./install.sh            system-wide (/usr/local/bin + /etc/xdg/autostart)
#   ./install.sh --user     current user (~/.local/bin + ~/.config/autostart)
#   ./install.sh --uninstall [system|--user]
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=waydroid-tray

install_system() {
    install -Dm755 "$SELF_DIR/$APP.py" "/usr/local/bin/$APP"
    install -Dm644 "$SELF_DIR/packaging/$APP.desktop" "/etc/xdg/autostart/$APP.desktop"
    echo "Installed system-wide. The tray icon starts on your next login (or run '$APP' now)."
}

install_user() {
    install -Dm755 "$SELF_DIR/$APP.py" "$HOME/.local/bin/$APP"
    install -Dm644 "$SELF_DIR/packaging/$APP.desktop" "$HOME/.config/autostart/$APP.desktop"
    echo "Installed for current user. The tray icon starts on your next login (or run '$APP' now)."
}

uninstall_system() {
    rm -f "/usr/local/bin/$APP" "/etc/xdg/autostart/$APP.desktop"
    echo "Removed system-wide installation."
}

uninstall_user() {
    rm -f "$HOME/.local/bin/$APP" "$HOME/.config/autostart/$APP.desktop"
    echo "Removed user installation."
}

case "${1:-}" in
    --user)     install_user ;;
    --uninstall)
        if [[ "${2:-}" == "--user" ]]; then uninstall_user; else uninstall_system; fi ;;
    "")         install_system ;;
    *)          echo "usage: $0 [--user] | [--uninstall [--user]]"; exit 2 ;;
esac

# dependency hint
if ! python3 -c "import PyQt6" 2>/dev/null; then
    echo "NOTE: python-pyqt6 does not seem to be installed (pacman -S python-pyqt6)."
fi
