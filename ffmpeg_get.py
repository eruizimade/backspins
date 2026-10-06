"""FFmpeg, installed from inside the app — no terminal, no Homebrew, no admin.

Converting (FLAC → AIFF, the Import tab) and some playback need ffmpeg and
ffprobe. Most DJs do not have them and should not have to learn a package
manager to get them, so the app offers to fetch them itself.

Where from: Backspins' own GitHub release "ffmpeg-9", which holds builds from
the distributors ffmpeg.org itself links to (gyan.dev for Windows,
osxexperts.net for Apple Silicon). Every download is checked against the
SHA-256 written HERE, in the code — a file that does not match is thrown
away, whatever the server said.

Where to: APP_DIR/bin, which settings.py puts first on the PATH — so every
part of the app that runs "ffmpeg" finds this one, and nothing outside the
app's own folder is touched.
"""

import hashlib
import os
import platform
import shutil
import stat
import tempfile
import threading
import urllib.request
import zipfile

import settings

BASE = 'https://github.com/eruizimade/backspins/releases/download/ffmpeg-9/'
BUILDS = {
    ('Darwin', 'arm64'): {
        'file': 'ffmpeg-9-macos-arm64.zip', 'size': 45108946, 'version': '9.0',
        'sha256': '3b15548097ae9262ca3748040723ccac82a71065b6fe6d36ab19c58738d4f321'},
    ('Windows', 'AMD64'): {
        'file': 'ffmpeg-9-windows-x64.zip', 'size': 77378584, 'version': '9.0.2',
        'sha256': '6a23519a85594b674c191343940353f0b277a19e099fe9c42a957a1e161a70b4'},
}
EXE = '.exe' if os.name == 'nt' else ''
STATE = {'phase': 'idle', 'done': 0, 'total': 0, 'error': None}
_LOCK = threading.Lock()


def build():
    """The build for this computer, or None (Intel Macs, Linux: install it yourself)."""
    return BUILDS.get((platform.system(), platform.machine()))


def where():
    """Which ffmpeg the app would use: (path or None, 'app' | 'system' | None)."""
    mine = os.path.join(settings.TOOLS_DIR, 'ffmpeg' + EXE)
    if os.path.isfile(mine):
        return mine, 'app'
    found = shutil.which('ffmpeg')
    return (found, 'system') if found else (None, None)


def status():
    path, kind = where()
    b = build()
    with _LOCK:
        st = dict(STATE)
    return {'installed': bool(path) and bool(shutil.which('ffprobe')), 'path': path, 'kind': kind,
            'offer': b is not None, 'size': b['size'] if b else None,
            'version': b['version'] if b else None, 'job': st}


def _set(**kw):
    with _LOCK:
        STATE.update(kw)


def _install():
    b = build()
    tmpdir = tempfile.mkdtemp(prefix='bs-ffmpeg-')
    try:
        _set(phase='downloading', done=0, total=b['size'], error=None)
        part = os.path.join(tmpdir, b['file'])
        h = hashlib.sha256()
        req = urllib.request.Request(BASE + b['file'], headers={'User-Agent': 'Backspins'})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, 'wb') as fh:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
                _set(done=STATE['done'] + len(chunk))
        # ⚠ The check that matters: the bytes are the ones this code expects.
        if h.hexdigest() != b['sha256']:
            raise RuntimeError('The download did not match what was expected, so it was not used. '
                               'Try again later.')
        _set(phase='installing')
        staging = os.path.join(tmpdir, 'x')
        with zipfile.ZipFile(part) as z:
            for name in z.namelist():
                # Flat archive, known names only: nothing can land outside.
                if '/' in name or '\\' in name or name.startswith('.'):
                    continue
                z.extract(name, staging)
        for tool in ('ffmpeg', 'ffprobe'):
            if not os.path.isfile(os.path.join(staging, tool + EXE)):
                raise RuntimeError('The download is missing %s.' % tool)
        os.makedirs(settings.TOOLS_DIR, exist_ok=True)
        for name in os.listdir(staging):
            dst = os.path.join(settings.TOOLS_DIR, name)
            shutil.move(os.path.join(staging, name), dst + '.new')
            os.replace(dst + '.new', dst)
            if name.startswith(('ffmpeg', 'ffprobe')):
                os.chmod(dst, os.stat(dst).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _set(phase='done')
    except Exception as e:
        _set(phase='error', error=str(e)[:200])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def install():
    """Start fetching it in the background. Returns status()."""
    if build() is None:
        raise RuntimeError('No ready-made ffmpeg for this computer: install it yourself '
                           '(https://ffmpeg.org/download.html).')
    with _LOCK:
        if STATE['phase'] in ('downloading', 'installing'):
            return status()
        STATE.update(phase='downloading', done=0, error=None)
    threading.Thread(target=_install, daemon=True).start()
    return status()


def remove():
    """Take the app's own copy away (a system ffmpeg, if any, stays)."""
    for tool in ('ffmpeg', 'ffprobe'):
        p = os.path.join(settings.TOOLS_DIR, tool + EXE)
        if os.path.exists(p):
            os.remove(p)
    _set(phase='idle', done=0, error=None)
    return status()
