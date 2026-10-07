# Spotify Desktop Lyrics

**English** | [简体中文](README.zh-CN.md)

A desktop lyrics overlay for the Spotify desktop app on Windows.

![Preview](docs/preview.png)

## Features

- Transparent always-on-top lyrics with a line-by-line highlight and an outline that stays readable on any background
- Lyrics from QQ Music, NetEase Cloud Music and LRCLIB, automatically matched by title, artist and duration, skipping covers, live versions and karaoke tracks
- Shows a Chinese translation for foreign-language songs when the lyrics source has one
- **Follows Spotify's own lyrics button**: click the microphone icon in Spotify's playback bar to show the lyrics, click it again to hide them; closing Spotify hides them too
- Hover over the lyrics for buttons: previous / play-pause / next / lock / settings / hide
- Lock to make the lyrics click-through; hover and click the small lock to unlock
- Per-song timing offset; adjustable text size, font and colors; optional start with Windows
- English and Chinese interface (English by default; switch under **Language / 语言** in the menu)
- No Spotify login or Spotify API key needed

## Download

1. Download `SpotifyLyrics-x.y.z-win64.zip` from [Releases](../../releases) and unzip it anywhere
2. Run `SpotifyLyrics.exe`; a green microphone icon appears in the system tray
3. Play a song in Spotify and click the microphone icon in its playback bar

Windows may show "Windows protected your PC" the first time (the app isn't code-signed). Click **More info** → **Run anyway**.

Requirements: Windows 10 / 11 and the Spotify desktop app downloaded from spotify.com (the Microsoft Store version should work too, but hasn't been tested).

## Controls

| Action | Effect |
| --- | --- |
| Drag the lyrics | Move them |
| Drag the left / right edge | Change the width |
| Mouse wheel | Change the text size |
| Right-click, or the settings button | Open the menu |
| Click the tray icon | Show / hide the lyrics |

The menu also has: lyrics 0.5 s earlier / later (remembered per song), search lyrics again, show translation, show second line, follow Spotify's lyrics button, appearance, language, start with Windows.

## How it works

- **Playback info**: the current track and position come from Windows' media controls (SMTC), which are also used for the playback buttons
- **Lyrics button**: the state of the lyrics button in Spotify's playback bar is read through UI Automation (the API screen readers use). If a Spotify update ever makes the button impossible to find, the app falls back to "show while Spotify is open"
- **Lyrics**: songs with Chinese, Japanese or Korean titles are looked up on QQ Music first, everything else on NetEase first, with LRCLIB as the last resort. Results are cached, so a song only needs to be looked up once

## Data and uninstalling

Settings, the lyrics cache and the log live in `%APPDATA%\SpotifyLyrics`.

To uninstall: turn off **Start with Windows** in the menu, quit the app, then delete the app folder and the folder above.

## Running from source and building

Tested with Python 3.11.

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw app.py      # run; add --debug to log detailed playback timing

.venv\Scripts\pip install pyinstaller
.\build.ps1                        # build into dist\
```

## Disclaimer

This project isn't affiliated with Spotify, QQ Music or NetEase Cloud Music. Lyrics are copyrighted by their authors and the respective platforms; this tool only displays them on your own computer, for personal use. The QQ Music and NetEase endpoints aren't official public APIs and may stop working at any time.

## License

[MIT](LICENSE)
