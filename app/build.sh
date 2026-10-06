#!/bin/zsh
# Builds "Backspins.app" and puts it in ~/Applications.
#
#   app/build.sh            build and install
#   app/build.sh --no-install   build into app/build only
#
# No Xcode project: one Swift file (main.swift), the icon in assets/
# (tools/make_brand.py draws it), and an Info.plist that remembers where this toolkit lives —
# the app starts the server from here, so if you move the folder, build again.
#
# ⚠ Rebuild only when main.swift changes: the screens are served by the Python
# server and change without it. Every build is a new ad-hoc signature, and
# macOS asks again for access to the Documents folder the toolkit lives in.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
TOOL="$(cd "$HERE/.." && pwd)"
NAME="Backspins"
OUT="$HERE/build"
APP="$OUT/$NAME.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "· compiling"
swiftc -O -swift-version 5 -target arm64-apple-macosx13.0 \
  -framework Cocoa -framework WebKit \
  -o "$APP/Contents/MacOS/Backspins" "$HERE/main.swift"

echo "· icon"
cp "$TOOL/assets/backspins.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>app.backspins.mac</string>
  <key>CFBundleExecutable</key><string>Backspins</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>$(git -C "$TOOL" rev-list --count HEAD 2>/dev/null || echo 1)</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.music</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSSupportsAutomaticTermination</key><false/>
  <key>NSDesktopFolderUsageDescription</key><string>Backspins reads your music files here to draw their waveforms, play them and check their quality. They never leave this computer.</string>
  <key>NSDocumentsFolderUsageDescription</key><string>Backspins reads your music files here to draw their waveforms, play them and check their quality. They never leave this computer.</string>
  <key>NSDownloadsFolderUsageDescription</key><string>Backspins reads your music files here to draw their waveforms, play them and check their quality. They never leave this computer.</string>
  <key>NSRemovableVolumesUsageDescription</key><string>Backspins reads your music files here to draw their waveforms, play them and check their quality. They never leave this computer.</string>
  <key>NSNetworkVolumesUsageDescription</key><string>Backspins reads your music files here to draw their waveforms, play them and check their quality. They never leave this computer.</string>
  <key>NSAppleEventsUsageDescription</key><string>Backspins asks Finder to move files you delete to the Bin, so they can always be put back.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>ToolkitDir</key><string>$TOOL</string>
</dict>
</plist>
PLIST

# Ad-hoc signature: enough for an app built on this Mac to run on it.
codesign --force --sign - "$APP" >/dev/null 2>&1

if [ "$1" != "--no-install" ]; then
  DEST="$HOME/Applications"
  mkdir -p "$DEST"
  rm -rf "$DEST/$NAME.app"
  cp -R "$APP" "$DEST/"
  # Tell Finder, the Dock and Spotlight about the new version and its icon.
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST/$NAME.app" >/dev/null 2>&1 || true
  touch "$DEST/$NAME.app"
  echo "· installed: $DEST/$NAME.app"
else
  echo "· built: $APP"
fi
