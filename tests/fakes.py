"""Fakes shared by the engine tests: a clipboard, a keyboard sender, a
sounddevice module, a recorder and a chime module. Nothing here touches the
real clipboard, keyboard, microphone or speakers.

Importing this module first points HOLDTOTALK_HOME at a temporary folder
(like test_00_home.py), so a test file run on its own cannot reach the real
data folder either."""

import atexit
import os
import shutil
import tempfile

if "htt-test-home-" not in os.environ.get("HOLDTOTALK_HOME", ""):
    _home = tempfile.mkdtemp(prefix="htt-test-home-")
    os.environ["HOLDTOTALK_HOME"] = _home
    atexit.register(shutil.rmtree, _home, True)

import json       # noqa: E402
import threading  # noqa: E402
import types      # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402


def accept_terms_in(folder, version=None, how="app"):
    """The test helpers' way past the Terms of Use: the same small record the
    installer and the app write (terms.py), made in `folder`, which must be a
    temporary data folder. Real users have no other way through."""
    import settings
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    rec = {"version": version or settings.TERMS_VERSION,
           "accepted_at": "2026-01-01T00:00:00Z", "how": how}
    (folder / "terms-accepted.json").write_text(json.dumps(rec),
                                                encoding="utf-8")
    return rec


# Every test that builds an App gets a data folder that already holds a
# record of accepting the current terms (tests of the terms themselves
# remove or replace it).
if not (Path(os.environ["HOLDTOTALK_HOME"]) / "terms-accepted.json").exists():
    accept_terms_in(os.environ["HOLDTOTALK_HOME"])


class FakeClipboard:
    """keys.paste_text's clipboard backend, in memory. `items` is what the
    clipboard holds: [(fmt, name, bytes or None)]."""

    def __init__(self, items=(), readable=True):
        self.items = list(items)
        self.seq = 1
        self.readable = readable
        self.writes = []                 # every write, in order
        self.on_write = None             # fn(clipboard) after each write

    def sequence(self):
        return self.seq

    def read_all(self, max_bytes=None):
        if not self.readable:
            raise OSError("clipboard busy")
        return list(self.items)

    def write(self, items):
        self.items = [(fmt, name, data) for fmt, name, data in items]
        self.seq += 1
        self.writes.append(list(self.items))
        if self.on_write:
            self.on_write(self)
        return self.seq

    def user_copies(self, items):
        """Something else (the user) puts new content on the clipboard."""
        self.items = list(items)
        self.seq += 1

    def names(self):
        return {name for _, name, _ in self.items}


class FakeSender:
    def __init__(self, on_send=None):
        self.calls = []
        self.on_send = on_send

    def ctrl_v(self):
        self.calls.append(("ctrl_v",))
        if self.on_send:
            self.on_send("ctrl_v")

    def type_text(self, text):
        self.calls.append(("type", text))
        if self.on_send:
            self.on_send("type")


# -- a fake sounddevice ---------------------------------------------------------------

class PortAudioError(Exception):
    pass


class FakeStream:
    def __init__(self, sd, samplerate, device, callback):
        self.sd, self.samplerate, self.device = sd, samplerate, device
        self.callback = callback
        self.active = False
        self.closed = False
        self.latency = sd.latency
        self.starts = 0

    def start(self):
        if self.closed:
            raise PortAudioError("stream closed")
        if self.sd.fail_starts:
            self.sd.fail_starts -= 1
            raise PortAudioError("Unanticipated host error")
        name = self.sd.snapshot[self.device]["name"] \
            if self.device is not None and self.device < len(self.sd.snapshot) \
            else self.sd.default_name()
        if name not in self.sd.present:
            raise PortAudioError("device unavailable")
        self.active = True
        self.starts += 1

    def stop(self):
        if self.sd.on_stop:
            self.sd.on_stop(self)        # e.g. buffers delivered while stopping
        self.active = False

    def close(self):
        self.active = False
        self.closed = True

    def feed(self, value, frames=160):
        block = np.full((frames, 1), value, dtype=np.float32)
        self.callback(block, frames, None, None)


class FakeSD:
    """Enough of sounddevice for audio.py. Like PortAudio, the device list
    (snapshot) only changes on _initialize(); `present` is what is really
    plugged in now."""

    PortAudioError = PortAudioError

    def __init__(self, names=("Desk mic", "Headset mic"), latency=0.05):
        self.present = list(names)
        self.default = types.SimpleNamespace(device=[-1, -1], hostapi=0)
        self.latency = latency
        self.fail_starts = 0
        self.on_stop = None
        self.streams = []
        self.inits = self.terms = 0
        self.lock = threading.Lock()
        self._initialize()
        self.inits = 0

    def default_name(self):
        return self.present[0] if self.present else None

    def _initialize(self):
        self.inits += 1
        self.snapshot = [{"index": i, "name": n, "hostapi": 0,
                          "max_input_channels": 1,
                          "default_samplerate": 48000.0}
                         for i, n in enumerate(self.present)]

    def _terminate(self):
        self.terms += 1

    def query_hostapis(self, index=None):
        apis = [{"name": "MME"}]
        return apis if index is None else apis[index]

    def query_devices(self, device=None, kind=None):
        if kind == "input" and device is None:
            if not self.snapshot:
                raise PortAudioError("no default input")
            return self.snapshot[0]
        if device is not None:
            return self.snapshot[device]
        return list(self.snapshot)

    def InputStream(self, samplerate, channels, dtype, device, callback):
        if not self.snapshot:
            raise PortAudioError("no input device")
        if device is not None and device >= len(self.snapshot):
            raise PortAudioError("invalid device")
        stream = FakeStream(self, samplerate, device, callback)
        self.streams.append(stream)
        return stream

    def signature(self):
        return (len(self.present), tuple(self.present))


class FakeRecorder:
    """A recorder for the app state machine: start/stop/cancel with fixed
    audio (1 s, not silent)."""

    def __init__(self, seconds=1.0):
        self.recording = False
        self.starts = 0
        self.level = 0.0
        self.problem = None
        self.samples = np.full(int(16000 * seconds), 0.1, dtype=np.float32)
        self.muted = None

    def start(self):
        self.recording = True
        self.starts += 1

    def stop(self):
        self.recording = False
        return self.samples

    def cancel(self):
        self.recording = False

    def mute_start(self, seconds):
        self.muted = seconds

    def close(self):
        pass

    def set_device(self, device):
        pass


class FakeSounds:
    """Stands in for sounds.py: records what would be played."""

    def __init__(self):
        self.played = []
        import sounds as real
        self._real = real

    def play(self, kind, cfg=None):
        self.played.append(kind)
        return True

    def settings_of(self, cfg):
        return self._real.settings_of(cfg)

    def wav_bytes(self, kind, volume):
        return self._real.wav_bytes(kind, volume)    # pure: no playback
