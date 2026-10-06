# PyInstaller recipe for the desktop app:   pyinstaller backspins.spec
#
# One program that is every program Backspins runs (see desktop.py): the
# window, the server, the audio analysis workers, the menu bar icon.
#
# ⚠ The server imports most of its modules lazily, inside the request that
# needs them, so PyInstaller cannot see them — every module in this folder is
# listed as a hidden import, or the packaged app would fail on the first click
# that reaches one.
import glob
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

HERE = os.path.abspath(SPECPATH)
LOCAL = sorted(os.path.splitext(os.path.basename(p))[0]
               for p in glob.glob(os.path.join(HERE, '*.py'))
               if os.path.basename(p) not in ('desktop.py',))

datas = [(os.path.join(HERE, 'convertidor.html'), '.'),
         (os.path.join(HERE, 'menubar.html'), '.'),
         (os.path.join(HERE, 'i18n.js'), '.')]
binaries, hidden = [], list(LOCAL)
for pkg in ('pyrekordbox', 'sqlcipher3', 'webview'):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
hidden += collect_submodules('mutagen') + ['send2trash', 'tkinter', 'tkinter.filedialog']

mac = sys.platform == 'darwin'
MUSIC = ('Backspins reads your music files here to draw their waveforms, play them and check their quality. They never leave this computer.')
icon = os.path.join(HERE, 'assets', 'backspins.icns' if mac else 'backspins.ico')

a = Analysis([os.path.join(HERE, 'desktop.py')], pathex=[HERE], binaries=binaries,
             datas=datas, hiddenimports=hidden, excludes=['matplotlib', 'IPython'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Backspins',
          console=False, icon=icon, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name='Backspins')
if mac:
    app = BUNDLE(coll, name='Backspins.app', icon=icon, bundle_identifier='app.backspins.mac',
                 info_plist={'CFBundleShortVersionString': os.environ.get('BACKSPINS_VERSION', '0.1.0'),
                             'NSHighResolutionCapable': True,
                             'LSApplicationCategoryType': 'public.app-category.music',
                             'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
                             # What macOS shows when it asks. Said plainly,
                             # because "Don't allow" here makes the library
                             # look broken (access.py explains it after).
                             'NSDesktopFolderUsageDescription': MUSIC,
                             'NSDocumentsFolderUsageDescription': MUSIC,
                             'NSDownloadsFolderUsageDescription': MUSIC,
                             'NSRemovableVolumesUsageDescription': MUSIC,
                             'NSNetworkVolumesUsageDescription': MUSIC,
                             'NSAppleEventsUsageDescription':
                                 'Backspins asks Finder to move files you delete to the Bin, '
                                 'so they can always be put back.'})
