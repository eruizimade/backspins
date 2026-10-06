#!/bin/zsh
# Double-click to start Backspins from this folder (macOS).
#
# If the Mac app is installed (app/build.sh), this just opens it: the app
# starts the server itself and stops it again when you quit it. Otherwise it
# runs the server here and opens a browser.
if open -a "Backspins" 2>/dev/null; then
  exit 0
fi
# Tries several python3 binaries and uses the first that really works: some
# macOS installs have a python3 on the PATH that exits 0 without running
# anything, so it cannot be trusted.
cd "$(dirname "$0")"
for PY in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python3; do
  if [ "$("$PY" -c 'print(42)' 2>/dev/null)" = "42" ]; then
    exec "$PY" convertidor.py
  fi
done
echo "No working Python found on this Mac. Install Python 3.9 or newer from python.org."
read -sk 1
