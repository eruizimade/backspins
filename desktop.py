#!/usr/bin/env python3
"""Backspins as a desktop app, on Windows and macOS: one window, no browser.

The same shape as the Swift app in app/: it owns nothing of the library. It
starts the server as its own process, opens a window on it, and stops the
server again when the window closes. The window is pywebview — WebView2
(Edge) on Windows, WebKit on macOS. Without pywebview it falls back to the
default browser and keeps the server running until Ctrl+C.

Packaged with PyInstaller (backspins.spec) this one program is every program
the app needs, chosen by its first switch:

    Backspins                 the window (and the server behind it)
    Backspins --server …      the server alone — what the window starts
    Backspins --musicdna …    the audio analysis worker the server starts
    Backspins --menubar       the macOS menu bar icon the server starts
    Backspins --smoke         start the server, check it answers, stop (CI)

From source the same file works: `python3 desktop.py`.
"""

import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

FROZEN = bool(getattr(sys, 'frozen', False))
HERE = os.path.dirname(os.path.abspath(__file__))
TITLE = 'Backspins'


def _quiet_streams(name):
    """A windowed app has no console: stdout/stderr are None and the first
    print() would kill the process. Send them to a log file instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    import settings
    os.makedirs(settings.APP_DIR, exist_ok=True)
    fh = open(os.path.join(settings.APP_DIR, name + '.log'), 'a', buffering=1,
              encoding='utf-8')
    sys.stdout = sys.stdout or fh
    sys.stderr = sys.stderr or fh


def _self(*args):
    return ([sys.executable] if FROZEN else [sys.executable, os.path.abspath(__file__)]) + list(args)


def start_server():
    """Start the server process; return (process, url) once it answers."""
    fd, port_file = tempfile.mkstemp(prefix='backspins-port-')
    os.close(fd)
    os.remove(port_file)
    flags = 0
    if os.name == 'nt':
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    # ⚠ From source the server is convertidor.py itself, not an import of it:
    # it may first build its .venv and re-exec into it, which needs its own
    # command line, not this one.
    cmd = (_self('--server') if FROZEN else [sys.executable, os.path.join(HERE, 'convertidor.py')])
    proc = subprocess.Popen(cmd + ['--no-browser', '--port-file', port_file],
                            creationflags=flags)
    deadline = time.time() + 120          # first run on a big library is slow
    url = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError('The server stopped while starting (exit %s).' % proc.returncode)
        if url is None and os.path.exists(port_file):
            try:
                port = int(open(port_file).read().strip() or 0)
            except (OSError, ValueError):
                port = 0
            if port:
                url = 'http://127.0.0.1:%d' % port
        if url:
            try:
                with urllib.request.urlopen(url + '/api/environment', timeout=5) as r:
                    if r.status == 200:
                        break
            except Exception:
                pass
        time.sleep(0.3)
    else:
        proc.terminate()
        raise RuntimeError('The server did not answer in time.')
    try:
        os.remove(port_file)
    except OSError:
        pass
    return proc, url


def stop_server(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_window():
    proc, url = start_server()
    try:
        try:
            import webview
        except ImportError:
            import webbrowser
            webbrowser.open(url)
            print('Backspins is running at %s — Ctrl+C to stop.' % url)
            try:
                proc.wait()
            except KeyboardInterrupt:
                pass
            return
        webview.create_window(TITLE, url, width=1400, height=900, min_size=(960, 620),
                              background_color='#121418', text_select=True)
        webview.start(private_mode=False)
    finally:
        stop_server(proc)


def smoke():
    """What CI runs on every platform: the packaged server starts and answers,
    and the guard refuses a page from somewhere else."""
    proc, url = start_server()
    try:
        with urllib.request.urlopen(url + '/api/environment', timeout=10) as r:
            print('environment:', r.status)
        req = urllib.request.Request(url + '/api/settings', headers={'Origin': 'https://example.com'})
        try:
            urllib.request.urlopen(req, timeout=10)
            print('guard: FAILED — a foreign origin was answered')
            return 1
        except urllib.error.HTTPError as e:
            print('guard:', e.code)
            if e.code != 403:
                return 1
        with urllib.request.urlopen(url + '/api/update', timeout=10) as r:
            print('version:', json.loads(r.read().decode('utf-8')).get('version'))
        with urllib.request.urlopen(url + '/', timeout=10) as r:
            page = r.read()
            print('page:', r.status, len(page), 'bytes')
            if b'Backspins' not in page:
                return 1
        return 0
    finally:
        stop_server(proc)


def main():
    multiprocessing.freeze_support()      # musicdna's worker pool, packaged
    if not FROZEN and HERE not in sys.path:
        sys.path.insert(0, HERE)
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode == '--server':
        _quiet_streams('server')
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        import convertidor
        convertidor.main()
    elif mode == '--musicdna':
        _quiet_streams('musicdna')
        import musicdna
        musicdna.main([sys.argv[0]] + sys.argv[2:])
    elif mode == '--menubar':
        _quiet_streams('menubar')
        import menubar
        menubar.main()
    elif mode == '--smoke':
        _quiet_streams('smoke')          # CI prints this log afterwards
        sys.exit(smoke())
    else:
        _quiet_streams('app')
        run_window()


if __name__ == '__main__':
    main()
