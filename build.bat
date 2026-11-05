@echo off
rem Builds MicMusicControl.exe into the dist folder.
python -m PyInstaller --noconfirm --onefile --noconsole --name MicMusicControl ^
  --collect-all winrt --collect-all faster_whisper --collect-all ctranslate2 ^
  --collect-submodules pycaw --hidden-import comtypes.stream ^
  --hidden-import pystray._win32 --hidden-import tkinter mic_music_control.py
echo.
echo Done. Your exe is in the dist folder.
