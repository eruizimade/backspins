@echo off
rem Double-click to start Backspin from this folder (Windows, from source).
rem The packaged app (Backspin.exe, from the Releases page) needs none of this.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 convertidor.py & goto :eof)
where python >nul 2>nul && (python convertidor.py & goto :eof)
echo Python 3.9 or newer is needed: https://www.python.org/downloads/
pause
