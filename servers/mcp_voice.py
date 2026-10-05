"""MCP server: voice / sound / media keys on the host.

TTS and sound playback go through PowerShell (System.Speech, System.Media);
media and volume keys go through user32 keybd_event. No third-party libraries.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

import ctypes  # noqa: E402
from ctypes import wintypes  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
PS = "powershell" if os.name == "nt" else "pwsh"
# non-ASCII (CJK) must survive the console round trip
PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
SPEAK_TIMEOUT_S = 120
PS_TIMEOUT_S = 60

srv = Server("voice")

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.keybd_event.restype = None
KEYEVENTF_KEYUP = 0x0002

MEDIA_VK = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2}
VOLUME_VK = {"up": 0xAF, "down": 0xAE, "mute": 0xAD}


def _ps(script, timeout_s=PS_TIMEOUT_S, input_text=None):
    """Run a PowerShell script; text goes through stdin so quotes/CJK are safe."""
    try:
        p = subprocess.run(
            [PS, "-NoProfile", "-NonInteractive", "-Command", PS_PREFIX + script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=float(timeout_s), input=input_text, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return "[timeout after %ss]" % timeout_s
    except Exception as exc:
        return "error: %s" % exc
    out = (p.stdout or "").strip()
    if p.stderr and p.stderr.strip():
        out += "\n[stderr] " + p.stderr.strip()
    if not out:
        out = "(no output)"
    return "exit=%s\n%s" % (p.returncode, out)


def _tap(vk, times=1):
    for _i in range(max(1, int(times))):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)


@srv.tool("speak",
          "Speak text out loud through the Windows SAPI voice. Text is passed via stdin, so CJK and quotes are safe. "
          "rate is -10..10 (0 is normal), volume is 0..100. Blocks until the sentence finishes (up to ~120s).",
          {"type": "object", "properties": {"text": {"type": "string"}, "rate": {"type": "integer", "default": 0},
                                            "volume": {"type": "integer", "default": 100}}, "required": ["text"]})
def speak(text, rate=0, volume=100):
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.Rate=[int]%d;$s.Volume=[int]%d;"
        "$t=[Console]::In.ReadToEnd();"
        "$s.Speak($t);$s.Dispose();'spoken ' + $t.Length + ' chars'"
    ) % (int(rate), int(volume))
    return _ps(script, SPEAK_TIMEOUT_S, input_text=str(text))


@srv.tool("list_voices", "List the installed SAPI text-to-speech voices with their name, gender, age and culture.",
          {"type": "object", "properties": {}, "required": []})
def list_voices():
    return _ps(
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.GetInstalledVoices() | ForEach-Object { $v=$_.VoiceInfo; "
        "'name=' + $v.Name + ' | gender=' + $v.Gender + ' | age=' + $v.Age + ' | culture=' + $v.Culture };"
        "$s.Dispose()"
    )


@srv.tool("play_sound",
          "Play a sound file without blocking. .wav files use System.Media.SoundPlayer synchronously in a hidden "
          "helper process; anything else is opened with the default Windows player.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def play_sound(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.exists(p):
        return "file not found: %s" % p
    if p.lower().endswith(".wav"):
        script = (
            "$p=New-Object System.Media.SoundPlayer('%s');$p.PlaySync();'played wav'" % p.replace("'", "''")
        )
        subprocess.Popen([PS, "-NoProfile", "-NonInteractive", "-Command", PS_PREFIX + script],
                         creationflags=NO_WINDOW, close_fds=True)
        return "playing wav (non-blocking): %s" % p
    script = "Start-Process -FilePath '%s'" % p.replace("'", "''")
    subprocess.Popen([PS, "-NoProfile", "-NonInteractive", "-Command", script],
                     creationflags=NO_WINDOW, close_fds=True)
    return "opened with default player (non-blocking): %s" % p


@srv.tool("media_key",
          "Send a media transport key to the current media player: play_pause | next | prev | stop. "
          "Sends real keyboard input (VK 179/176/177/178) to whatever has focus.",
          {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]})
def media_key(action):
    act = str(action).strip().lower()
    if act not in MEDIA_VK:
        return "unknown action: %s (play_pause|next|prev|stop)" % action
    _tap(MEDIA_VK[act], 1)
    return "sent media key: %s (vk=%d)" % (act, MEDIA_VK[act])


@srv.tool("volume_step",
          "Change the system volume or mute it: up | down | mute. Each step sends VK 175/174/173 'steps' times. "
          "Real system state change.",
          {"type": "object", "properties": {"action": {"type": "string"}, "steps": {"type": "integer", "default": 2}},
           "required": ["action"]})
def volume_step(action, steps=2):
    act = str(action).strip().lower()
    if act not in VOLUME_VK:
        return "unknown action: %s (up|down|mute)" % action
    n = 1 if act == "mute" else max(1, int(steps))
    _tap(VOLUME_VK[act], n)
    return "volume %s x%d (vk=%d)" % (act, n, VOLUME_VK[act])


@srv.tool("beep",
          "Play a console beep at freq Hz for duration_ms milliseconds through the hidden PowerShell console.",
          {"type": "object", "properties": {"freq": {"type": "integer", "default": 800},
                                            "duration_ms": {"type": "integer", "default": 300}}, "required": []})
def beep(freq=800, duration_ms=300):
    script = "[System.Console]::Beep([int]%d,[int]%d);'beeped %dHz %dms'" % (
        int(freq), int(duration_ms), int(freq), int(duration_ms))
    return _ps(script, 20)


@srv.tool("toast_speak",
          "Speak text out loud and show a balloon tip with the same text, then wait for the tip to expire. "
          "Two effects at once: sound + notification. Text goes through stdin so CJK is safe.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def toast_speak(text):
    script = (
        "Add-Type -AssemblyName System.Speech,System.Windows.Forms,System.Drawing;"
        "$t=[Console]::In.ReadToEnd();"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.Visible=$true;"
        "$n.BalloonTipTitle='Voice';"
        "$n.BalloonTipText=$t;"
        "$n.ShowBalloonTip(4000);"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.Speak($t);$s.Dispose();"
        "Start-Sleep -Milliseconds 800;$n.Dispose();'toast+speech done'"
    )
    return _ps(script, SPEAK_TIMEOUT_S, input_text=str(text))


def build():
    return srv


# Safe self-test samples. Short text so the run stays quick; media/volume keys are
# omitted because they change live system state.
SAMPLES = {
    "speak": {"text": "hello", "rate": 0, "volume": 30},
    "list_voices": {},
    "beep": {"freq": 440, "duration_ms": 80},
    "toast_speak": {"text": "test"},
}


if __name__ == "__main__":
    srv.run()
