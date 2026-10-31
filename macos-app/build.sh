#!/bin/bash
# Build iCourseNotes.app into the project root (next to work/ and outputs/).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
APP="$ROOT/iCourseNotes.app"
BUILD="$HERE/.build"

cd "$HERE"
swift build -c release --arch arm64
BIN="$(swift build -c release --arch arm64 --show-bin-path)/ICourseNotes"

ICONSET="$BUILD/AppIcon.iconset"
rm -rf "$ICONSET"
swiftc -O tools/make_icon.swift -o "$BUILD/make_icon"
"$BUILD/make_icon" "$ICONSET"
iconutil -c icns "$ICONSET" -o "$BUILD/AppIcon.icns"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/ICourseNotes"
cp "$BUILD/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp -R "$HERE/web" "$APP/Contents/Resources/web"
mkdir -p "$APP/Contents/Resources/bridge"
cp "$HERE/bridge/app_bridge.py" "$APP/Contents/Resources/bridge/"
cp -R "$HERE/bridge/templates" "$APP/Contents/Resources/bridge/templates"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>iCourseNotes</string>
  <key>CFBundleDisplayName</key><string>iCourseNotes</string>
  <key>CFBundleIdentifier</key><string>io.github.lucasuiii.icoursenotes</string>
  <key>CFBundleExecutable</key><string>ICourseNotes</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.education</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>iCourseNotes 本机识别与笔记</string>
</dict>
</plist>
PLIST
codesign --force --deep --sign - "$APP" >/dev/null
echo "Built: $APP"
