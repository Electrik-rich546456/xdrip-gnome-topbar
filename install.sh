#!/bin/sh
# Installs xDrip Top Bar for the current user (GNOME 46).
set -e
UUID="xdrip-topbar@local"
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/gnome-shell/extensions/$UUID"

echo "Checking requirements..."
missing=0
python3 -c "import cryptography" 2>/dev/null || { echo "  missing: python3 cryptography  (sudo apt install python3-cryptography)"; missing=1; }
command -v gjs >/dev/null || { echo "  missing: gjs  (sudo apt install gjs)"; missing=1; }
gjs -c "imports.gi.versions.Secret='1'; imports.gi.Secret" 2>/dev/null || { echo "  missing: libsecret typelib  (sudo apt install gir1.2-secret-1)"; missing=1; }
command -v zbarimg >/dev/null || echo "  optional: zbar-tools, to read the xDrip QR code from a screenshot  (sudo apt install zbar-tools)"
[ "$missing" = 0 ] || { echo "Install the missing packages, then run this again."; exit 1; }

# back up your settings first (they live in dconf and are kept anyway; this is just a safety net)
SETTINGS_PATH="/org/gnome/shell/extensions/xdrip-topbar/"
BACKUP="$HOME/.local/share/xdrip-topbar-settings-backup.ini"
if command -v dconf >/dev/null && [ -n "$(dconf dump "$SETTINGS_PATH" 2>/dev/null)" ]; then
    mkdir -p "$(dirname "$BACKUP")"
    dconf dump "$SETTINGS_PATH" > "$BACKUP"
    echo "Settings backed up to $BACKUP"
fi

# stop a collector left running by the previous version (the new one would also replace it itself)
if pkill -f "$UUID/collector/xdrip_gnome_collector.py" 2>/dev/null; then
    echo "Stopped the previous collector."
    sleep 1
fi

mkdir -p "$DEST"
cp -r "$SRC"/metadata.json "$SRC"/*.js "$SRC"/schemas "$SRC"/collector "$DEST"/
chmod +x "$DEST/collector/xdrip_gnome_collector.py"
glib-compile-schemas "$DEST/schemas"

echo "Collector self-test:"
python3 "$DEST/collector/xdrip_gnome_collector.py" --selftest | tail -n 1

echo
echo "Installed to $DEST"
echo "Next:"
echo "  X11:     press Alt+F2, type r, press Enter (restarts GNOME Shell, windows stay open)"
echo "  Wayland: log out and back in"
echo "  First install only: gnome-extensions enable $UUID, then gnome-extensions prefs $UUID"
echo "  (your data folder and settings are kept across updates)"
