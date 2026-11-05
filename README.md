# Mic Music Control

**Talk to your PC without fighting your music.** Mic Music Control lowers (or pauses) Spotify and other media the moment any app starts using your microphone, then fades it back in when you stop. It also has built-in **hold-to-talk dictation**: hold a key, speak, let go, and your words are typed into whatever text box you clicked. Dictation runs **offline on your own PC** using Whisper. Nothing is sent to the cloud.

Made for people who dictate prompts to Claude, ChatGPT, or any text box while music is playing.

> Built by **Anu Lavanya** at **Webbit**. Free and open source (MIT).

---

## Features

- **Auto duck or pause** when any app uses the microphone (Claude, Chrome, Teams, Discord, games, anything).
- **Smooth fades.** Quick fade out so you can talk right away, gentle fade back in to your original volume.
- **Only touches what it changed.** It never starts music that was already paused.
- **Hold-to-talk dictation** powered by [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Pastes the text into the active window.
- **Any hotkey you like**, including left and right Ctrl, Alt, Shift, and Win keys. Default: hold **Left Ctrl + Right Ctrl**.
- **Settings window with sliders** for duck volume, fade speed, and delay. Pick lower or pause, and choose a speech model.
- **Tray icon** with a status colour: green is watching, red is mic on, grey is off.
- **Crash-safe volume.** Your real volume is saved to disk, so quitting or crashing mid-fade never leaves your music quiet.

## Download and use (no Python needed)

1. Go to the [**Releases**](../../releases) page and download `MicMusicControl-Windows.zip`.
2. Unzip it anywhere you like, for example `C:\Tools\MicMusicControl`.
3. Double-click `MicMusicControl.exe`. A microphone icon appears near the clock (it may be under the **^** arrow).
4. The **first time**, it downloads the speech model (about 140 MB) into a `models` folder next to the exe. It needs internet once, then works offline. Wait for the notification "Speech model ready".
5. **To dictate:** click into any text box, hold **Left Ctrl + Right Ctrl**, speak, and let go. The text appears after a second or two.

> Windows may show a blue "Windows protected your PC" screen the first time, because the app is not code-signed. Click **More info**, then **Run anyway**. Some antivirus tools also flag apps made with PyInstaller. The full source is in this repo so you can check it or build it yourself.

### Settings

Double-click the tray icon (or right-click it, then **Settings...**):

| Setting | What it does |
|---|---|
| Music volume while mic is on | How quiet the music gets (0 to 100%) |
| Fade back in over | How fast the music returns |
| Wait after mic stops | Pause before the music comes back |
| Lower or pause | Keep music playing quietly, or pause it fully |
| Hotkey | Pick a preset or press your own combination |
| Accuracy / speed | Fastest (tiny), Balanced (base, default), or Most accurate (small, about 480 MB) |

Settings are saved in `%APPDATA%\MicMusicControl\settings.json`.

### Start with Windows

1. Press `Win + R`, type `shell:startup`, press Enter.
2. Right-click in the folder, choose **New**, then **Shortcut**, and browse to `MicMusicControl.exe`.

### Quit

Right-click the tray icon and choose **Quit**.

## Run from source

Requires Windows 10/11 and Python 3.10 or newer.

```bash
pip install -r requirements.txt
python mic_music_control.py          # with a console, shows messages
pythonw mic_music_control.py         # silent
```

## Build the exe yourself

```bash
pip install pyinstaller
build.bat
```

The exe is created in `dist\`. Keep the `models` folder next to it, or let it download on first run.

## How it works

- **Mic detection:** Windows Core Audio capture sessions (through `pycaw`), plus the Windows microphone privacy log as a backup.
- **Music control:** Windows media session API (`winrt`) to pause and resume, and per-app volume control to fade.
- **Dictation:** records from the microphone while the hotkey is held, transcribes with faster-whisper on the CPU, then pastes with the clipboard (your previous clipboard text is restored).

## Troubleshooting

- **Nothing happens when I talk:** check Windows Settings, Privacy and security, Microphone, and make sure apps can use the microphone.
- **Dictation text lands in the wrong place:** click into the text box first and keep it focused while you hold the keys.
- **Dictation is slow:** choose "Fastest" or "Balanced" in Settings. The first sentence after startup can be slower.
- **The app stopped:** look for `error_log.txt` next to the exe.

Tested on Windows 11. Other Windows versions may work but are untested.

## Credits and license

Created by **Anu Lavanya**, **Webbit**. MIT license, see [LICENSE](LICENSE).

Uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [OpenAI Whisper](https://github.com/openai/whisper) models, [pycaw](https://github.com/AndreMiras/pycaw), [pystray](https://github.com/moses-palmer/pystray), and [keyboard](https://github.com/boppreh/keyboard).
