#!/bin/sh
# Builds UnicornConnect.app into the directory given, or next to this
# script if none is (macOS only).
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$SRC}"
mkdir -p "$OUT"
cd "$OUT"
APP=UnicornConnect.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/UnicornConnect" "$SRC/UnicornConnect.swift" \
    -framework IOBluetooth
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>local.unicorn-bt.connect</string>
    <key>CFBundleName</key><string>UnicornConnect</string>
    <key>CFBundleExecutable</key><string>UnicornConnect</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSBackgroundOnly</key><true/>
    <key>NSBluetoothAlwaysUsageDescription</key>
    <string>Connects the Unicorn EEG headset so its data can be read.</string>
</dict>
</plist>
EOF
codesign --force --sign - "$APP"
echo "built $(pwd)/$APP"
