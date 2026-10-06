#!/usr/bin/env python3
"""Run the toolkit and hot-reload the server when the code changes.

Why this exists: so the app can be edited while you keep working, without the
server being yanked out from under you.

  · **HTML, CSS and JavaScript** are served fresh on every request, so those
    changes need nothing here — just reload the page (⌘R) when you want them.
  · **Python** lives inside the running process, so it needs a restart to take
    effect. This does that automatically, and ONLY when the code compiles, so a
    half-written edit never takes your server down.

Your tagging queue is on disk (`~/.config/rekordbox-toolkit/queue.json`), so a
reload loses nothing: the browser just retries its next request, usually
without you noticing. The restart is sub-second and rebinds the same port.

Run this instead of convertidor.py:

    ./.venv/bin/python3 watch.py            (or the .command launcher)
"""

import glob
import os
import py_compile
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# ⚠ Launch WITH the venv python on purpose: convertidor.py re-execs itself into
# the venv otherwise, and the re-exec'd child would outlive terminate(),
# leaving an orphan holding the port. Started already-in-venv, it does not
# re-exec, so terminate() kills the real server.
VENV_PY = (os.path.join(HERE, '.venv', 'Scripts', 'python.exe') if os.name == 'nt'
           else os.path.join(HERE, '.venv', 'bin', 'python3'))
SERVER = os.path.join(HERE, 'convertidor.py')


def watched():
    return {f: os.path.getmtime(f)
            for f in glob.glob(os.path.join(HERE, '*.py'))
            if os.path.basename(f) != 'watch.py'}


def compiles():
    """True only if every .py compiles. Protects the running server from a
    mid-edit syntax error: we keep serving the old code until the new code is
    at least parseable."""
    ok = True
    for f in watched():
        try:
            py_compile.compile(f, doraise=True)
        except py_compile.PyCompileError as e:
            first = str(e).splitlines()[0] if str(e) else 'syntax error'
            print('  ⚠ %s: %s' % (os.path.basename(f), first))
            ok = False
    return ok


def start():
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    return subprocess.Popen([py, SERVER] + sys.argv[1:])


def main():
    # ⚠ The menu bar is started by convertidor.py itself, which is the only
    # thing that knows the port it actually bound. Doing it here as well would
    # race with that one for the single-instance lock.
    proc = start()
    seen = watched()
    try:
        while True:
            time.sleep(0.7)
            if proc.poll() is not None:      # server exited on its own; restart
                proc = start()
                seen = watched()
                continue
            now = watched()
            if now == seen:
                continue
            seen = now
            time.sleep(0.4)                  # let a burst of saves settle
            seen = watched()
            if not compiles():
                print('  (kept the running server; fix the error above)')
                continue
            print('↻ code changed — reloading the server…')
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            proc = start()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == '__main__':
    main()
