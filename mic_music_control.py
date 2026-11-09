"""
mic_pause_media_v2.py
Lowers (ducks) or pauses music when ANY app opens the microphone, and brings it
back afterwards (only what this script changed).

Extras:
  * Tray icon (bottom-right, maybe under the ^ arrow): right-click for options.
  * Push-to-talk: HOLD the hotkey to dictate (Win+H voice typing), release to stop.

Install once (Command Prompt):
    pip install pycaw psutil comtypes pystray pillow keyboard winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections

Test (shows messages):   python mic_pause_media_v2.py
Silent:                  pythonw mic_pause_media_v2.py
"""

import asyncio
import sys
import threading
import time
import winreg
from ctypes import POINTER, cast

import comtypes
import keyboard
import psutil
import pystray
from comtypes import CLSCTX_ALL
from PIL import Image, ImageDraw

from pycaw.api.audioclient import ISimpleAudioVolume
from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
from pycaw.constants import CLSID_MMDeviceEnumerator

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)

# ---------------- SETTINGS ----------------
MODE = "duck"            # "duck" = lower the music while mic is on, "pause" = pause it
DUCK_LEVEL = 0.15        # duck mode: music volume while you talk (0.15 = 15% of your volume)
PTT_KEY = "lctrl+rctrl"  # hold these keys to dictate (change it in Settings)
PTT_ENABLED = True
POLL_SECONDS = 0.5
RESUME_DELAY = 1.0       # seconds the mic must be off before music returns
FADE_OUT_SECONDS = 0.3   # quick fade when the mic turns on
FADE_IN_SECONDS = 1.0    # fade back up to your original volume
IGNORE_MIC_APPS = []     # e.g. ["teams", "discord", "obs"]
ONLY_CONTROL_APPS = []   # e.g. ["spotify"]; empty = any playing media
DEBUG = True             # prints messages (visible only with `python`, not `pythonw`)
# ------------------------------------------

MIC_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion"
    r"\CapabilityAccessManager\ConsentStore\microphone"
)

state = {"enabled": True, "mode": MODE, "ptt": PTT_ENABLED, "duck": DUCK_LEVEL,
         "key": PTT_KEY, "model": "base.en", "engine": "local", "api_key": "",
         "cloud_model": "whisper-large-v3-turbo", "fade_in": FADE_IN_SECONDS, "resume": RESUME_DELAY, "quit": False, "mic": False}

# Your choices from the tray menu are remembered in this file.
import json
import os

_APPDATA_FILE = os.path.join(os.environ.get("APPDATA", "."), "MicMusicControl", "settings.json")
_APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))


def _pick_settings_file():
    """Prefer settings.json next to the app (always the same file, however the app was started).
    If that folder is read-only (e.g. Program Files), use the AppData folder instead."""
    local = os.path.join(_APP_DIR, "settings.json")
    try:
        if os.path.exists(local):
            return local
        with open(os.path.join(_APP_DIR, ".write_test"), "w") as t:
            t.write("x")
        os.remove(os.path.join(_APP_DIR, ".write_test"))
        return local
    except Exception:
        return _APPDATA_FILE


SETTINGS_FILE = _pick_settings_file()
SAVED_KEYS = ("enabled", "mode", "ptt", "duck", "key", "model", "engine", "api_key", "cloud_model", "fade_in", "resume")


def load_settings():
    try:
        path = SETTINGS_FILE if os.path.exists(SETTINGS_FILE) else _APPDATA_FILE   # migrate old settings
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for k in SAVED_KEYS:
            if k in data:
                state[k] = data[k]
    except Exception:
        pass


def save_settings():
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({k: state[k] for k in SAVED_KEYS}, f)
    except Exception:
        pass


load_settings()


# ---------- Detection 1: Core Audio capture sessions ----------
def capture_apps():
    apps = []
    try:
        enum = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL
        )
        devices = enum.EnumAudioEndpoints(1, 1)  # 1 = eCapture, 1 = active devices
        for d in range(devices.GetCount()):
            dev = devices.Item(d)
            iface = dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
            mgr = iface.QueryInterface(IAudioSessionManager2)
            sessions = mgr.GetSessionEnumerator()
            for i in range(sessions.GetCount()):
                ctl = sessions.GetSession(i)
                if ctl.GetState() != 1:      # 1 = active
                    continue
                ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                pid = ctl2.GetProcessId()
                if not pid:
                    continue
                try:
                    name = psutil.Process(pid).name()
                except Exception:
                    name = f"pid{pid}"
                apps.append(name)
    except Exception as e:
        if DEBUG:
            print(f"[capture check error] {e}")
    return apps


# ---------- Detection 2: registry privacy log ----------
def _registry_apps(path):
    active = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        return active
    with root:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            if name == "NonPackaged":
                continue
            try:
                with winreg.OpenKey(root, name) as sub:
                    start = winreg.QueryValueEx(sub, "LastUsedTimeStart")[0]
                    stop = winreg.QueryValueEx(sub, "LastUsedTimeStop")[0]
            except OSError:
                continue
            if start > stop:
                active.append(name.split("#")[-1])
    return active


def mic_in_use():
    apps = capture_apps() + _registry_apps(MIC_KEY) + _registry_apps(MIC_KEY + r"\NonPackaged")
    ignore = [x.lower() for x in IGNORE_MIC_APPS]
    apps = [a for a in apps if not any(x in a.lower() for x in ignore)]
    return sorted(set(apps))


# ---------- Media control ----------
def _allowed(app_id):
    if not ONLY_CONTROL_APPS:
        return True
    return any(x.lower() in (app_id or "").lower() for x in ONLY_CONTROL_APPS)


def _key(app_id):
    return (app_id or "").lower().split("!")[-1].replace(".exe", "")


def volume_ifaces(app_id):
    """Per-app volume controls (ISimpleAudioVolume) for this app's playback sessions."""
    out, key = [], _key(app_id)
    try:
        enum = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL
        )
        dev = enum.GetDefaultAudioEndpoint(0, 1)  # render, multimedia
        iface = dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
        mgr = iface.QueryInterface(IAudioSessionManager2)
        sessions = mgr.GetSessionEnumerator()
        for i in range(sessions.GetCount()):
            ctl = sessions.GetSession(i)
            pid = ctl.QueryInterface(IAudioSessionControl2).GetProcessId()
            if not pid:
                continue
            try:
                name = psutil.Process(pid).name().lower()
            except Exception:
                continue
            if key and key in name:
                out.append(ctl.QueryInterface(ISimpleAudioVolume))
    except Exception as e:
        if DEBUG:
            print(f"[volume lookup error] {e}")
    return out


async def ramp(app_id, start, end, seconds):
    vols = volume_ifaces(app_id)
    if not vols:
        return
    steps = max(1, int(seconds / 0.05))
    for n in range(1, steps + 1):
        level = start + (end - start) * n / steps
        for v in vols:
            v.SetMasterVolume(level, None)
        await asyncio.sleep(seconds / steps)


def current_volume(app_id):
    vols = volume_ifaces(app_id)
    return vols[0].GetMasterVolume() if vols else None


saved_volume = {}   # app_id -> the volume you had set before we touched it
RECOVER_FILE = os.path.join(os.path.dirname(SETTINGS_FILE), "volume_recovery.json")


def persist_volumes():
    """Remember your real volume on disk, so a crash/quit mid-duck can never leave music quiet."""
    try:
        os.makedirs(os.path.dirname(RECOVER_FILE), exist_ok=True)
        if saved_volume:
            with open(RECOVER_FILE, "w", encoding="utf-8") as f:
                json.dump(saved_volume, f)
        elif os.path.exists(RECOVER_FILE):
            os.remove(RECOVER_FILE)
    except Exception:
        pass


def recover_volumes():
    try:
        with open(RECOVER_FILE, encoding="utf-8") as f:
            old = json.load(f)
        for app_id, vol in old.items():
            for v in volume_ifaces(app_id):
                v.SetMasterVolume(vol, None)
        os.remove(RECOVER_FILE)
    except Exception:
        pass

fade_task = None


async def lower_media(manager):
    """Mic turned on. Returns (paused_ids, ducked_ids)."""
    paused, ducked = [], []
    mode = state["mode"]
    for session in manager.get_sessions():
        app_id = session.source_app_user_model_id
        if not _allowed(app_id):
            continue
        if session.get_playback_info().playback_status != PlaybackStatus.PLAYING:
            continue
        cur = current_volume(app_id)
        if app_id not in saved_volume and cur is not None:
            saved_volume[app_id] = cur
            persist_volumes()
        if mode == "duck" and cur is not None:
            await ramp(app_id, cur, saved_volume[app_id] * state["duck"], FADE_OUT_SECONDS)
            ducked.append(app_id)
            continue
        if cur is not None:
            await ramp(app_id, cur, 0.0, FADE_OUT_SECONDS)
        if await session.try_pause_async():
            paused.append(app_id)
        elif app_id in saved_volume:   # pause failed: put the volume back
            await ramp(app_id, 0.0, saved_volume.pop(app_id), 0.1)
            persist_volumes()
    return paused, ducked


async def restore_media(manager, paused, ducked):
    for session in manager.get_sessions():
        app_id = session.source_app_user_model_id
        if app_id not in paused and app_id not in ducked:
            continue
        cur = 0.0 if app_id in paused else current_volume(app_id)
        if app_id in paused:
            await session.try_play_async()
        target = saved_volume.get(app_id)
        if target is not None:
            await ramp(app_id, cur if cur is not None else 0.0, target, state["fade_in"])
            saved_volume.pop(app_id, None)
            persist_volumes()


def restore_volumes_now():
    """Safety net on quit: never leave an app muted/ducked."""
    for app_id, vol in list(saved_volume.items()):
        for v in volume_ifaces(app_id):
            v.SetMasterVolume(vol, None)
    saved_volume.clear()
    persist_volumes()


# ---------- Push-to-talk (hold key = Win+H voice typing) ----------
ptt_down = False

# ---------- Built-in dictation: Whisper (faster-whisper, runs on your PC) ----------
_BASE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
MODEL_DIR = os.path.join(_BASE, "models")   # speech models are stored in a "models" folder next to the app
whisper = {"model": None, "name": None, "lock": threading.Lock()}
rec = {"stream": None, "frames": []}


def notify(msg):
    try:
        if tray is not None:
            tray.notify(msg, "Mic music control")
    except Exception:
        pass


def load_model():
    from faster_whisper import WhisperModel
    with whisper["lock"]:
        name = state["model"]
        if whisper["model"] is not None and whisper["name"] == name:
            return whisper["model"]
        notify(f"Loading speech model '{name}' (first time downloads it, please wait)...")
        try:
            from faster_whisper.utils import download_model
            path = os.path.join(MODEL_DIR, name)
            if not os.path.exists(os.path.join(path, "model.bin")):
                os.makedirs(path, exist_ok=True)
                download_model(name, output_dir=path)
            whisper["model"] = WhisperModel(
                path, device="cpu", compute_type="int8", cpu_threads=4
            )
            whisper["name"] = name
            try:   # warm-up so your first real sentence is fast
                import numpy as np
                list(whisper["model"].transcribe(np.zeros(16000, dtype="float32"), language="en", vad_filter=True)[0])
            except Exception:
                pass
            notify("Speech model ready. Hold your hotkey and talk.")
        except Exception as e:
            whisper["model"] = None
            notify(f"Could not load speech model: {e}")
            if DEBUG:
                print(f"[model error] {e}")
        return whisper["model"]


def start_recording():
    import sounddevice as sd
    rec["frames"] = []

    def cb(indata, frames, t, status):
        rec["frames"].append(indata.copy())

    rec["stream"] = sd.InputStream(samplerate=16000, channels=1, dtype="float32", callback=cb)
    rec["stream"].start()


def stop_recording():
    import numpy as np
    s, rec["stream"] = rec["stream"], None
    if s is None:
        return
    s.stop()
    s.close()
    if not rec["frames"]:
        return
    audio = np.concatenate(rec["frames"])[:, 0]
    rec["frames"] = []
    if len(audio) < 16000 * 0.4:    # ignore accidental taps
        return
    threading.Thread(target=transcribe_and_paste, args=(audio,), daemon=True).start()


GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def cloud_transcribe(audio):
    """Send the recording to Groq's hosted Whisper (large-v3) and return the text."""
    import io
    import urllib.error
    import urllib.request
    import uuid
    import wave
    import numpy as np

    key = (state["api_key"] or os.environ.get("GROQ_API_KEY", "")).strip()
    if not key:
        raise RuntimeError("No API key. Open Settings and paste your Groq API key.")
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    boundary = uuid.uuid4().hex
    body = b""
    for name, value in (("model", state["cloud_model"]), ("language", "en"),
                        ("response_format", "text"), ("temperature", "0")):
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
             f'Content-Type: audio/wav\r\n\r\n').encode() + buf.getvalue() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(GROQ_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "MicMusicControl/1.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8").strip()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        if e.code == 401:
            raise RuntimeError("Groq rejected the API key (401). Check it in Settings.")
        raise RuntimeError(f"Groq error {e.code}: {detail}")


def transcribe_and_paste(audio):
    import pyperclip
    cloud = state["engine"] == "cloud"
    model = None if cloud else load_model()
    if not cloud and model is None:
        return
    try:
        if cloud:
            try:
                text = cloud_transcribe(audio)
            except Exception as e:
                notify(f"Cloud dictation failed: {e}")
                raise
        else:
            segments, _ = model.transcribe(
                audio, language="en", beam_size=1, vad_filter=True, condition_on_previous_text=False
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        if DEBUG:
            print(f"[dictation] {text!r}")
        if not text:
            return
        try:
            old = pyperclip.paste()
        except Exception:
            old = ""
        pyperclip.copy(text + " ")
        time.sleep(0.05)
        keyboard.send("ctrl+v")
        time.sleep(0.25)
        pyperclip.copy(old)
    except Exception as e:
        if DEBUG:
            print(f"[dictation error] {e}")


def _ptt_press(_event):
    global ptt_down
    if not state["ptt"] or ptt_down:   # ignore key auto-repeat
        return
    ptt_down = True
    try:
        start_recording()
    except Exception as e:
        ptt_down = False
        notify(f"Microphone error: {e}")


def _ptt_release(_event):
    global ptt_down
    if not ptt_down:
        return
    ptt_down = False
    try:
        stop_recording()
    except Exception as e:
        if DEBUG:
            print(f"[stop error] {e}")


# ---------- Hotkey: any keys, including left/right Ctrl, Alt, Shift, Win ----------
import ctypes

_VK = {"lctrl": 0xA2, "rctrl": 0xA3, "lalt": 0xA4, "ralt": 0xA5, "lshift": 0xA0, "rshift": 0xA1,
       "lwin": 0x5B, "rwin": 0x5C, "space": 0x20, "capslock": 0x14, "tab": 0x09, "menu": 0x5D}
_VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})
_VK.update({c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"})
_NAMES = {"lctrl": "Left Ctrl", "rctrl": "Right Ctrl", "lalt": "Left Alt", "ralt": "Right Alt",
          "lshift": "Left Shift", "rshift": "Right Shift", "lwin": "Left Win", "rwin": "Right Win",
          "space": "Space", "capslock": "Caps Lock", "tab": "Tab", "menu": "Menu key"}
_ORDER = list(_VK)
PRESETS = {
    "Left Ctrl + Right Ctrl": "lctrl+rctrl",
    "Right Ctrl": "rctrl",
    "Right Alt": "ralt",
    "Left Ctrl + Left Alt": "lctrl+lalt",
    "Left Ctrl + Space": "lctrl+space",
    "Right Ctrl + Right Alt": "rctrl+ralt",
    "F9": "f9",
}
capturing = {"on": False}


def pretty_key(spec):
    return " + ".join(_NAMES.get(k, k.upper()) for k in spec.split("+") if k)


def _vk_down(vk):
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def chord_active(spec):
    keys = [k for k in spec.split("+") if k in _VK]
    return bool(keys) and all(_vk_down(_VK[k]) for k in keys)


def hotkey_loop():
    """Watches your hotkey: hold = record, release = transcribe and paste."""
    down_since = None
    while not state["quit"]:
        try:
            active = state["ptt"] and not capturing["on"] and chord_active(state["key"])
            now = time.time()
            if active:
                if down_since is None:
                    down_since = now
                if not ptt_down and now - down_since >= 0.15:   # short taps of Ctrl etc. are ignored
                    _ptt_press(None)
            else:
                down_since = None
                if ptt_down:
                    _ptt_release(None)
        except Exception as e:
            if DEBUG:
                print(f"[hotkey error] {e}")
        time.sleep(0.02)


def capture_hotkey(on_done):
    """Hold the keys you want (any combination), then let go. Esc cancels."""
    capturing["on"] = True
    seen = []
    try:
        deadline = time.time() + 15
        started = False
        while time.time() < deadline:
            down = [k for k in _ORDER if _vk_down(_VK[k])]
            if _vk_down(0x1B):          # Esc
                seen = []
                break
            if down:
                started = True
                for k in down:
                    if k not in seen:
                        seen.append(k)
            elif started:
                break
            time.sleep(0.03)
    finally:
        capturing["on"] = False
    if seen:
        seen = seen[:3]
        state["key"] = "+".join(seen)
        save_settings()
    on_done()


# ---------- Settings window (sliders etc.) ----------
settings_win = {"open": False}


def open_settings():
    if settings_win["open"]:
        return
    settings_win["open"] = True
    threading.Thread(target=_settings_thread, daemon=True).start()


def _settings_thread():
    try:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title("Mic Music Control - Settings")
        root.resizable(False, False)
        try:
            root.attributes("-topmost", True)
            root.after(400, lambda: root.attributes("-topmost", False))
        except Exception:
            pass
        pad = {"padx": 14, "pady": 5}
        f = ttk.Frame(root, padding=10)
        f.grid()

        def section(text, row):
            ttk.Label(f, text=text, font=("Segoe UI", 10, "bold")).grid(
                row=row, column=0, columnspan=3, sticky="w", padx=14, pady=(12, 2))

        # ---- music ----
        enabled = tk.BooleanVar(value=state["enabled"])
        section("Music", 0)
        ttk.Checkbutton(f, text="Turn music control on", variable=enabled,
                        command=lambda: (state.update(enabled=enabled.get()), save_settings(), refresh_tray())
                        ).grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        mode = tk.StringVar(value=state["mode"])
        ttk.Label(f, text="When the mic is on:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Radiobutton(f, text="Lower the music", value="duck", variable=mode,
                        command=lambda: (state.update(mode=mode.get()), save_settings())
                        ).grid(row=2, column=1, sticky="w")
        ttk.Radiobutton(f, text="Pause the music", value="pause", variable=mode,
                        command=lambda: (state.update(mode=mode.get()), save_settings())
                        ).grid(row=2, column=2, sticky="w")

        def slider(row, label, lo, hi, key, to_state, from_state, fmt):
            var = tk.DoubleVar(value=from_state(state[key]))
            val = ttk.Label(f, text=fmt(var.get()), width=7)

            def changed(_=None):
                v = round(var.get(), 2)
                val.config(text=fmt(v))
                state[key] = to_state(v)
                save_settings()

            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", **pad)
            ttk.Scale(f, from_=lo, to=hi, variable=var, length=220, command=changed).grid(row=row, column=1, **pad)
            val.grid(row=row, column=2, sticky="w")

        slider(3, "Music volume while mic is on", 0, 100, "duck", lambda v: v / 100, lambda s: s * 100,
               lambda v: f"{round(v)}%")
        slider(4, "Fade back in over", 0.2, 3.0, "fade_in", lambda v: v, lambda s: s,
               lambda v: f"{v:.1f} s")
        slider(5, "Wait after mic stops", 0.3, 5.0, "resume", lambda v: v, lambda s: s,
               lambda v: f"{v:.1f} s")

        # ---- dictation ----
        section("Dictation (hold keys, talk, let go)", 6)
        ptt = tk.BooleanVar(value=state["ptt"])
        ttk.Checkbutton(f, text="Turn push-to-talk on", variable=ptt,
                        command=lambda: (state.update(ptt=ptt.get()), save_settings())
                        ).grid(row=7, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(f, text="Hotkey").grid(row=8, column=0, sticky="w", **pad)
        current = {v: k for k, v in PRESETS.items()}.get(state["key"], pretty_key(state["key"]))
        combo = ttk.Combobox(f, values=list(PRESETS), width=26, state="readonly")
        combo.set(current)
        combo.grid(row=8, column=1, **pad)

        hint = ttk.Label(f, text=f"Hold {pretty_key(state['key'])} and talk")
        hint.grid(row=9, column=0, columnspan=3, sticky="w", padx=14)

        def pick(_=None):
            spec = PRESETS.get(combo.get())
            if spec:
                state["key"] = spec
                save_settings()
                hint.config(text=f"Hold {pretty_key(spec)} and talk")

        combo.bind("<<ComboboxSelected>>", pick)

        def custom():
            btn.config(state="disabled", text="Hold your keys now... (Esc cancels)")

            def done():
                def ui():
                    btn.config(state="normal", text="Choose my own keys...")
                    combo.set(pretty_key(state["key"]))
                    hint.config(text=f"Hold {pretty_key(state['key'])} and talk")
                root.after(0, ui)

            threading.Thread(target=capture_hotkey, args=(done,), daemon=True).start()

        btn = ttk.Button(f, text="Choose my own keys...", command=custom)
        btn.grid(row=8, column=2, **pad)

        section("Speech engine", 10)
        engine = tk.StringVar(value=state["engine"])
        mnote = ttk.Label(f, text="")
        mnote.grid(row=15, column=0, columnspan=3, sticky="w", padx=14)

        def pick_engine():
            state["engine"] = engine.get()
            save_settings()
            if engine.get() == "local":
                mnote.config(text="Loading local model (first use downloads it)...")
                threading.Thread(target=load_model, daemon=True).start()
            else:
                mnote.config(text="Using Groq cloud. Needs internet and an API key.")

        ttk.Radiobutton(f, text="On this PC (private, free)", value="local", variable=engine,
                        command=pick_engine).grid(row=11, column=0, columnspan=2, sticky="w", **pad)
        ttk.Radiobutton(f, text="Groq cloud (faster, more accurate)", value="cloud", variable=engine,
                        command=pick_engine).grid(row=11, column=2, sticky="w", **pad)

        ttk.Label(f, text="Local model").grid(row=12, column=0, sticky="w", **pad)
        models = {"Fastest (tiny)": "tiny.en", "Balanced (base)": "base.en",
                  "Most accurate (small, slower)": "small.en"}
        mcombo = ttk.Combobox(f, values=list(models), width=26, state="readonly")
        mcombo.set({v: k for k, v in models.items()}.get(state["model"], "Balanced (base)"))
        mcombo.grid(row=12, column=1, **pad)

        def pick_model(_=None):
            state["model"] = models[mcombo.get()]
            save_settings()
            if engine.get() == "local":
                mnote.config(text="Loading model (first use downloads it)...")
                threading.Thread(target=load_model, daemon=True).start()

        mcombo.bind("<<ComboboxSelected>>", pick_model)

        ttk.Label(f, text="Cloud model").grid(row=13, column=0, sticky="w", **pad)
        cmodels = {"Fast (whisper-large-v3-turbo)": "whisper-large-v3-turbo",
                   "Most accurate (whisper-large-v3)": "whisper-large-v3"}
        ccombo = ttk.Combobox(f, values=list(cmodels), width=26, state="readonly")
        ccombo.set({v: k for k, v in cmodels.items()}.get(state["cloud_model"], "Fast (whisper-large-v3-turbo)"))
        ccombo.grid(row=13, column=1, **pad)
        ccombo.bind("<<ComboboxSelected>>",
                    lambda _=None: (state.update(cloud_model=cmodels[ccombo.get()]), save_settings()))

        ttk.Label(f, text="Groq API key").grid(row=14, column=0, sticky="w", **pad)
        keyvar = tk.StringVar(value=state["api_key"])
        kentry = ttk.Entry(f, textvariable=keyvar, show="*", width=29)
        kentry.grid(row=14, column=1, **pad)

        def save_key(*_):
            state["api_key"] = keyvar.get().strip()
            save_settings()

        keyvar.trace_add("write", save_key)

        def test_key():
            mnote.config(text="Testing key...")

            def run():
                import urllib.request
                try:
                    req = urllib.request.Request(
                        "https://api.groq.com/openai/v1/models",
                        headers={"Authorization": f"Bearer {state['api_key']}", "User-Agent": "MicMusicControl/1.1"})
                    urllib.request.urlopen(req, timeout=10).read()
                    msg = "Key works."
                except Exception as e:
                    msg = f"Key failed: {e}"
                root.after(0, lambda: mnote.config(text=msg))

            threading.Thread(target=run, daemon=True).start()

        ttk.Button(f, text="Test key", command=test_key).grid(row=14, column=2, **pad)

        ttk.Button(f, text="Close", command=root.destroy).grid(row=16, column=2, sticky="e", padx=14, pady=(14, 4))
        root.mainloop()
    except Exception as e:
        if DEBUG:
            print(f"[settings window error] {e}")
    finally:
        settings_win["open"] = False


# ---------- Tray icon ----------
def make_icon(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((22, 6, 42, 38), radius=10, fill=color)      # mic head
    d.arc((14, 20, 50, 50), 0, 180, fill=color, width=5)            # holder
    d.rectangle((30, 48, 34, 58), fill=color)                       # stem
    d.rectangle((22, 56, 42, 60), fill=color)                       # base
    return img


GREEN, GREY, RED = (46, 160, 67, 255), (130, 130, 130, 255), (220, 60, 60, 255)
tray = None


def refresh_tray():
    if tray is None:
        return
    if not state["enabled"]:
        tray.icon, tray.title = make_icon(GREY), "Mic music control: OFF"
    elif state["mic"]:
        tray.icon, tray.title = make_icon(RED), "Mic is ON - music lowered"
    else:
        tray.icon, tray.title = make_icon(GREEN), "Mic music control: watching"


def build_tray():
    def toggle_enabled(icon, item):
        state["enabled"] = not state["enabled"]
        save_settings()
        refresh_tray()

    def set_mode(m):
        def _f(icon, item):
            state["mode"] = m
            save_settings()
        return _f

    def open_(icon, item):
        open_settings()

    def quit_(icon, item):
        state["quit"] = True
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Settings...", open_, default=True),
        pystray.MenuItem("Enabled", toggle_enabled, checked=lambda i: state["enabled"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Lower the music", set_mode("duck"),
                         checked=lambda i: state["mode"] == "duck", radio=True),
        pystray.MenuItem("Pause the music", set_mode("pause"),
                         checked=lambda i: state["mode"] == "pause", radio=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda i: f"Dictation: hold {pretty_key(state['key'])}", open_),
        pystray.MenuItem("Quit", quit_),
    )
    return pystray.Icon("mic_music", make_icon(GREEN), "Mic music control: watching", menu)


async def main():
    global fade_task, tray
    import gc
    gc.disable()   # COM objects must only be freed on this thread (a crash otherwise)
    ticks = 0
    recover_volumes()
    manager = await MediaManager.request_async()
    paused, ducked = [], []
    mic_was_on, mic_off_since = False, None
    print("Watching microphone... (Ctrl+C to stop)")

    tray = build_tray()
    tray.run_detached()
    threading.Thread(target=hotkey_loop, daemon=True).start()
    if state["engine"] == "local":
        threading.Thread(target=load_model, daemon=True).start()   # get the speech model ready

    while not state["quit"]:
        try:
            active = mic_in_use() if state["enabled"] else []
            if active:
                mic_off_since = None
                if fade_task and not fade_task.done():   # mic came back mid fade-in
                    fade_task.cancel()
                    fade_task = None
                if not mic_was_on:
                    mic_was_on = True
                    state["mic"] = True
                    refresh_tray()
                    print(f"Mic ON: {active}")
                    p, d = await lower_media(manager)
                    paused = list(dict.fromkeys(paused + p))
                    ducked = list(dict.fromkeys(ducked + d))
                    print(f"Paused: {paused}  Ducked: {ducked}")
            elif mic_was_on:
                if mic_off_since is None:
                    mic_off_since = time.time()
                elif time.time() - mic_off_since >= state["resume"]:
                    mic_was_on, mic_off_since = False, None
                    state["mic"] = False
                    refresh_tray()
                    print("Mic OFF")
                    if paused or ducked:
                        p, d, paused, ducked = paused, ducked, [], []
                        fade_task = asyncio.ensure_future(restore_media(manager, p, d))
                        print(f"Restoring: {p + d}")
        except Exception as e:
            print(f"Error (still running): {e}")
        ticks += 1
        if ticks % 20 == 0:
            gc.collect()
        await asyncio.sleep(POLL_SECONDS)

    restore_volumes_now()
    try:
        keyboard.unhook_all()
    except Exception:
        pass


if __name__ == "__main__":
    import ctypes
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\MicPauseMediaV2")
    if ctypes.windll.kernel32.GetLastError() == 183:  # already running
        sys.exit(0)
    try:
        asyncio.run(main())
    except BaseException:
        import traceback
        with open(os.path.join(_BASE, "error_log.txt"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc() + "\n")
        raise
