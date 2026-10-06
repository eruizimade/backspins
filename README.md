<p align="center"><img src="assets/backspins-1024.png" width="128" alt=""></p>

# Backspins

**[eruizimade.github.io/backspins](https://eruizimade.github.io/backspins/)** · [Download](https://github.com/eruizimade/backspins/releases/latest)

**Look after your rekordbox library, and prepare your sets faster than rekordbox lets you.**

Backspins is a desktop app for DJs who use rekordbox 6 or 7. It reads your
library, finds what is wrong with it, and helps you tag, sort, listen and
build sets, then writes the results back into rekordbox, safely.

It runs entirely on your own computer. Your library is never uploaded.

> Backspins is an independent project. It is not made by, affiliated with or
> endorsed by AlphaTheta or Pioneer DJ. "rekordbox" is their trademark.

## Download

Get the latest build from the [Releases](../../releases) page:

- **Windows**: `Backspins-windows.zip`. Unzip it and run `Backspins.exe`.
- **macOS (Apple Silicon)**: `Backspins-macos-arm64.zip`. Unzip it and drag
  `Backspins.app` to Applications. The app is not notarised yet, so the first
  time you need to right-click it and choose **Open**.

For converting files you also need **ffmpeg**: `winget install ffmpeg` on
Windows, `brew install ffmpeg` on macOS. Without it everything else still
works. On macOS, conversion falls back to the system's own `afconvert`.

## What it does

The first time it opens, a short animated tour shows how it all fits together
(**How it works** in the sidebar plays it again).

| Tab | What it is for |
|---|---|
| **Inbox** | Your tagging queue. Listen, rate, set energy and tick your MyTags one track at a time, with the three-band waveform a CDJ shows. On macOS there is also a menu bar icon, so you can tag while another window is in front. |
| **Library** | Search and filter everything, play it, and edit tags and colours in place. |
| **Find** | Check a Spotify or SoundCloud playlist against your library: what you already own, what you don't, and what is almost a match. Pair songs by hand, then make a rekordbox playlist out of it, or link the streaming playlist to one you already have and update it later. |
| **Sets** | Build a set from your own MyTag vocabulary. It follows key and tempo, and shows what you have actually mixed after what. The result is saved back as a rekordbox playlist. |
| **Import** | Scan a folder of new music, rename it to `Title - Artist`, convert FLAC to AIFF, and add it to rekordbox under a colour that means "not looked at yet". |
| **Health** | Duplicates (it compares the real quality and length, keeps the better copy and merges your tags, playlists and cues into it), quality bands, **compatibility by CDJ generation** (which players your library plays on, and exactly which files hold you back), shop branding left in your tags, and missing artwork. |
| **Teach** | Teach the library a tag ("is this Acid?") or an ordering ("which is more intense?") by answering a few questions. It analyses the audio itself (notes, key, chords, timbre, groove), learns from your answers and your existing tags, tells you how often it is right, and ranks the whole library for you to confirm. |
| **Your data** | **Access**: whether Backspins can read your rekordbox library and every music file, and if macOS blocks a folder, which setting to change. **Storage**: everything it keeps on disk, in bytes, with where it lives. **Backups**: a copy of your library every hour it changes, kept by age within a size limit you choose, plus rekordbox XML and readable JSON versions that will outlive any change to rekordbox's encryption. |

The interface is in **English and Spanish** (it follows your system language;
switch with EN / ES at the bottom of the sidebar).

## Your words, not ours

Backspins works with your own rekordbox vocabulary. It works out which MyTag
group is the kind of night, which is the mood and which is the running order
from what is in them, whatever you call them.

Starting from nothing? The Inbox offers a **starter vocabulary**: 25 tags in
four groups, plus three colours for the tagging workflow (NEW → GRID → READY).
It was distilled from a library of 2,500 tracks tagged by hand for a year,
keeping only the tags that actually told tracks apart. It only adds, and you
can rename or delete any of it in rekordbox.

| Group | Tags |
|---|---|
| Type of set | Rave, After, Sunset, Bar, Party, Family |
| Mood | Dark, Hard, Acid, Psychedelic, Tribal, Groovy, Sexy, Happy, Euphoric, Emotional, Melancholic, Chill |
| Timing | Opener, Warm-up, Middle, Peak, Closer |
| Misc | Anthem, Tool |

## What it will never do

These are guarantees, not intentions. They are enforced in the code:

- **It never writes while rekordbox is open.** Edits wait in a queue and are
  written as soon as rekordbox is closed.
- **It backs up before every write.**
- **It never really deletes.** Files go to the recycle bin, and tracks are
  retired in rekordbox the same way rekordbox retires them.
- **Conversions are verified sample for sample** before an original is moved
  aside.
- **Only its own window can talk to it.** The app is a small server on
  `127.0.0.1`. It refuses any request that comes from another website open in
  your browser, so a page cannot reach your library through it.

## Your disk and your permissions

Nothing is hidden. **Your data → Storage** lists everything Backspins keeps,
with its size and folder:

- **Library backups** are the big one: a full copy of rekordbox's database
  (about 40 MB for a few thousand tracks) each time it changes, kept by age.
  They are limited to **2 GB** by default (oldest go first, the two newest
  always stay); choose 1, 2, 5, 10 GB or no limit. A backup is never taken,
  and so nothing is written, without the free space for it.
- **Caches** (audio analysis, energy, artwork found, playback previews, logs)
  can be cleared from the same screen; Backspins rebuilds them.
- **Your work** (tagging queue, lessons, pairings, settings) is listed and
  never offered for deletion.

On macOS the system asks before any app reads Desktop, Documents, Downloads,
external or network drives, and before it asks Finder to move a file to the
Bin. **Your data → Access** opens every one of your music files to check, tells
you which folders are blocked (and which are simply missing — a drive not
connected), and opens the right page of System Settings. Keep `Backspins.app`
in Applications: run straight from Downloads, macOS gives it a temporary copy
and forgets what you allowed.

## Backspins Cloud (coming)

The app is free and open source, and will stay that way. Backspins Cloud will
be an optional paid service with two parts:

- **A hosted backup of your music**: the audio files themselves, not just
  the library.
- **Your library on your phone**: tag, organise and listen from anywhere,
  with your computer switched off. Every change waits in the cloud and goes
  into rekordbox the next time Backspins runs on your computer.

## Run from source

Python 3.9 or newer.

```bash
git clone https://github.com/eruizimade/backspins.git
cd backspins
python3 convertidor.py        # Windows: py convertidor.py, or double-click Backspins.bat
```

On the first run a local `.venv` is created and the dependencies are installed
into it. Your browser then opens at `http://127.0.0.1:8765`. For the desktop
window instead of a browser tab:

```bash
.venv/bin/python desktop.py   # after `pip install pywebview` in that .venv
```

On macOS, `app/build.sh` builds a native window (`Backspins.app`) that runs this
folder.

To build the packaged app yourself: `pip install -r requirements.txt pyinstaller`,
then `pyinstaller backspins.spec`. The GitHub workflow in `.github/workflows`
does this for Windows and macOS on every push, and checks that the result
starts.

## Settings

Plain JSON in your app folder (`~/.config/backspins/settings.json`, or
`%APPDATA%\Backspins\settings.json` on Windows). Delete the file and every
default comes back.

| Setting | What it does |
|---|---|
| `library_db` | Where your rekordbox database is. Leave it empty and Backspins finds it, which also covers a library on an external drive. |
| `library_key` | Only needed if a future rekordbox changes its encryption key. |
| `ready_color` | The rekordbox colour that means "ready to play". Empty means every track counts. |
| `energy_in_comment` | Reads a leading number in the comment (`6 - dark roller`) as an Energy column. |
| `genre_fixes` | Your own genre spelling corrections, e.g. `{"Reggeaton": "Reggaeton"}`. |

## How it reads rekordbox

The library (`master.db`) is SQLCipher-encrypted with a passphrase that is
public and used by [pyrekordbox](https://github.com/dylanljones/pyrekordbox).
Every read happens on a temporary copy, so rekordbox is never locked by one.
Waveforms are not computed here: they are read from the analysis rekordbox
already stored next to every track (the `PWV6` section of the `.2EX` file),
which is why they match a CDJ exactly.

## Licence

[GPL-3.0](LICENSE). You may use, study, change and share it. If you
distribute a changed version, its source has to be available under the same
licence.
