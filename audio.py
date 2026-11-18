"""Microphone capture and WAV reading. Everything is float32 mono 16 kHz.

Devices: PortAudio only enumerates microphones when it is initialised, so a
microphone plugged in, unplugged or made the default later is invisible to
it. refresh() re-initialises PortAudio when Windows' device list or default
microphone has changed (a cheap check through winmm), and always after a
microphone failed to start. Every Recorder's stream is closed first (a
recording in progress postpones it); streams are reopened on the next
start(). list_devices() refreshes first, so it is always current.

Recording: stop() does not cut the stream off at the moment of the request:
audio already captured but still in the driver's buffers is delivered up to
one stream latency later, so the take stays open until that much more audio
has arrived (a drain), then it is complete. The stream itself is stopped
afterwards (Recorder.service()). A new start() during a drain reuses the
running stream, so nothing is lost between two quick dictations either.
"""

import ctypes
import logging
import math
import threading
import time
import wave
import weakref
from ctypes import wintypes

import numpy as np
import sounddevice as sd

log = logging.getLogger("holdtotalk.audio")

SAMPLE_RATE = 16000
TAIL_EXTRA = 0.02          # s of audio kept beyond the stream latency
DRAIN_GRACE = 0.3          # s past the latency before a drain gives up
STALL_SECONDS = 1.5        # recording but no audio for this long: a problem

PROBLEM_START = ("The microphone could not be started. Check that it is "
                 "plugged in and not used by another program.")
PROBLEM_MISSING = "The chosen microphone is not connected."
PROBLEM_NONE = "No microphone was found."
PROBLEM_SILENT = "No sound is arriving from the microphone."


class MicError(RuntimeError):
    """start() failed; str(exc) is a short plain sentence (the problem)."""


def resample(audio, rate, target=SAMPLE_RATE):
    """Linear resampling; good enough for speech going into Whisper."""
    if rate == target or len(audio) == 0:
        return audio.astype(np.float32, copy=False)
    n = max(1, int(round(len(audio) * target / rate)))
    return np.interp(np.linspace(0, len(audio) - 1, n),
                     np.arange(len(audio)), audio).astype(np.float32)


# -- the device list -----------------------------------------------------------------

class _WAVEINCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * 32),
                ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
                ("wReserved1", wintypes.WORD)]


def windows_signature():
    """(count, names, preferred) of Windows' capture devices right now, from
    winmm (cheap, opens nothing), or None if it cannot be asked."""
    try:
        winmm = ctypes.WinDLL("winmm")
        count = winmm.waveInGetNumDevs()
        names = []
        caps = _WAVEINCAPSW()
        for i in range(count):
            if winmm.waveInGetDevCapsW(ctypes.c_size_t(i), ctypes.byref(caps),
                                       ctypes.sizeof(caps)) == 0:
                names.append(caps.szPname)
        dev, flags = wintypes.DWORD(), wintypes.DWORD()
        winmm.waveInMessage.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                        ctypes.c_void_p, ctypes.c_void_p]
        preferred = None
        if winmm.waveInMessage(wintypes.HANDLE(0xFFFFFFFF), 0x2015,
                               ctypes.byref(dev), ctypes.byref(flags)) == 0:
            preferred = dev.value             # DRVM_MAPPER_PREFERRED_GET
        return count, tuple(names), preferred
    except Exception:
        return None


# Replaceable for tests (a fake sounddevice module and a fake signature).
device_signature = windows_signature
_pa_lock = threading.RLock()       # every PortAudio call goes through it
_recorders = weakref.WeakSet()
_UNSET = object()
_signature = _UNSET
generation = 0                     # +1 at every re-initialisation


def refresh(force=False):
    """Re-initialise PortAudio if the microphones or the default one changed
    since the last time (or force=True). Returns True if it did. Postponed
    (False) while any Recorder is recording or draining. Any thread."""
    global _signature, generation
    with _pa_lock:
        sig = device_signature()
        if _signature is _UNSET:
            _signature = sig              # PortAudio was set up at import
            if not force:
                return False
        if not force and sig == _signature:
            return False
        if any(r.busy for r in list(_recorders)):
            return False
        for r in list(_recorders):
            r._drop_stream()
        try:
            sd._terminate()
        except Exception as exc:
            log.debug("PortAudio terminate: %s", type(exc).__name__)
        try:
            sd._initialize()
        except Exception as exc:
            log.warning("Could not re-initialise the audio system: %s", exc)
            return False
        _signature = sig
        generation += 1
        log.info("Microphone list refreshed (devices or default changed)"
                 if not force else "Audio system re-initialised")
        return True


def list_devices():
    """Input devices as dicts: index, name, hostapi, channels, default_rate,
    is_default. Always current (see refresh)."""
    with _pa_lock:
        refresh()
        try:
            default_in = sd.default.device[0]
            if default_in is None or default_in < 0:
                default_in = sd.query_devices(kind="input")["index"]
        except Exception:
            default_in = -1
        hostapis = sd.query_hostapis()
        out = []
        for dev in sd.query_devices():
            if dev["max_input_channels"] < 1:
                continue
            out.append({
                "index": dev["index"],
                "name": dev["name"],
                "hostapi": hostapis[dev["hostapi"]]["name"],
                "channels": dev["max_input_channels"],
                "default_rate": dev["default_samplerate"],
                "is_default": dev["index"] == default_in,
            })
        return out


def resolve_device(spec):
    """Config value -> sounddevice device index (None = system default).

    Accepts an index or a (case-insensitive, partial) name. The same mic is
    listed once per host API; sounddevice rejects such ambiguous names, so we
    pick the match on the default host API (MME), else the first match.
    """
    if spec is None or spec == "":
        return None
    if isinstance(spec, int) or (isinstance(spec, str) and spec.isdigit()):
        return int(spec)
    want = str(spec).lower()
    with _pa_lock:
        matches = [d for d in list_devices() if want in d["name"].lower()]
        if not matches:
            raise ValueError(f"No input device matches {spec!r}")
        default_api = sd.query_hostapis(sd.default.hostapi)["name"]
    exact = [d for d in matches if d["name"].lower() == want]
    for pool in (exact, matches):
        for d in pool:
            if d["hostapi"] == default_api:
                return d["index"]
    return (exact or matches)[0]["index"]


# -- recording ----------------------------------------------------------------------

class Take:
    """One recording. Filled by the audio callback; complete once the
    stream's buffered tail has arrived after stop (or the drain timed out).
    result()/wait() give float32 mono 16 kHz audio."""

    def __init__(self, rate, mute_seconds=0.0, clock=time.monotonic):
        self.rate = rate
        self.frames = []
        self.need = None            # frames still wanted after stop()
        self.deadline = None
        self.timed_out = False
        self.mute = max(0, int(round(float(mute_seconds) * rate)))
        self.clock = clock
        self._done = threading.Event()

    @property
    def done(self):
        return self._done.is_set()

    def finish(self, timed_out=False):
        if not self._done.is_set():
            self.timed_out = timed_out
            self._done.set()

    def wait(self, timeout=None):
        """Wait for the drain (at most `timeout`, default: until its own
        deadline) and return the audio. Any thread."""
        if timeout is None and self.deadline is not None:
            timeout = max(0.0, self.deadline - self.clock()) + 0.05
        if not self._done.wait(timeout):
            self.finish(timed_out=True)
        return self.samples()

    result = wait

    def samples(self):
        frames = list(self.frames)
        if not frames:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(frames).astype(np.float32, copy=True)
        if self.mute:
            audio[:self.mute] = 0.0       # the start chime, see App
        return resample(audio, self.rate)


class Recorder:
    """Push-to-talk recorder.

    start() opens (or reuses) an input stream and begins a Take; stop()
    returns everything captured since start() as float32 mono 16 kHz,
    including the buffered tail (it waits for it); stop_async() returns the
    Take at once and lets it fill in the background. service() must then be
    called regularly (the app's tick does) to stop the stream once the tail
    is in. `.level` is the RMS of the latest block, for the level meter;
    `.problem` is the last microphone problem as a short sentence, or None.
    Drive start/stop/service from one thread.
    """

    def __init__(self, device=None, clock=time.monotonic):
        self.device = device          # config value: None, index or name
        self.stream = None
        self.rate = SAMPLE_RATE
        self.level = 0.0
        self.recording = False
        self.problem = None
        self.clock = clock
        self._take = None
        self._draining = None
        self._last_audio = 0.0
        self._generation = generation
        self._lock = threading.Lock()
        _recorders.add(self)

    @property
    def busy(self):
        d = self._draining
        return self.recording or (d is not None and not d.done)

    # -- audio thread
    def _callback(self, indata, frames, time_info, status):
        block = indata[:, 0].copy()
        with self._lock:
            self._last_audio = self.clock()
            d = self._draining
            if d is not None and not d.done:
                n = min(len(block), max(d.need, 0))
                if n:
                    d.frames.append(block[:n])
                    d.need -= n
                block = block[n:]
                if d.need <= 0:
                    d.finish()
            take = self._take
            if take is not None and len(block):
                take.frames.append(block)
                self.level = float(np.sqrt(np.mean(np.square(block))))

    # -- stream handling (caller holds _pa_lock)
    def _open(self):
        refresh()
        index = resolve_device(self.device)
        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                    dtype="float32", device=index,
                                    callback=self._callback)
            self.rate = SAMPLE_RATE
        except sd.PortAudioError:
            # WASAPI/WDM-KS devices often only run at their native rate.
            rate = sd.query_devices(index, "input")["default_samplerate"]
            stream = sd.InputStream(samplerate=rate, channels=1,
                                    dtype="float32", device=index,
                                    callback=self._callback)
            self.rate = int(rate)
            log.info("Microphone runs at %d Hz; resampling to %d",
                     self.rate, SAMPLE_RATE)
        self.stream = stream

    def _drop_stream(self):
        """Close the stream (PortAudio is about to be re-initialised)."""
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    def _active(self):
        # Under the lock: another thread's refresh() may close the stream.
        with _pa_lock:
            try:
                return self.stream is not None and bool(self.stream.active)
            except Exception:
                return False

    def _latency(self):
        with _pa_lock:
            try:
                return float(self.stream.latency) if self.stream else 0.0
            except Exception:
                return 0.0

    def _start_stream(self):
        with _pa_lock:
            if self.stream is None:
                self._open()
            if not self._active():
                self.stream.start()

    def _why(self):
        """Why the microphone cannot be used, as a short sentence."""
        try:
            devices = list_devices()
        except Exception:
            return PROBLEM_START
        if not devices:
            return PROBLEM_NONE
        if self.device not in (None, ""):
            try:
                resolve_device(self.device)
            except Exception:
                return PROBLEM_MISSING
        return PROBLEM_START

    def set_device(self, device):
        """Switch microphone; takes effect on the next start()."""
        if device != self.device:
            self.device = device
            self.close()

    def start(self, mute_seconds=0.0):
        """Begin a take. A failure closes the stream, re-initialises the
        audio system (the device list may be stale) and tries once more;
        then MicError (with .problem set) is raised."""
        if self.recording:
            return
        take = Take(SAMPLE_RATE, mute_seconds, self.clock)
        for attempt in (1, 2):
            try:
                with _pa_lock:
                    if self.stream is None:
                        self._open()
                    take.rate = self.rate
                    take.mute = max(0, int(round(float(mute_seconds)
                                                 * self.rate)))
                    with self._lock:
                        self._take = take
                        self.level = 0.0
                        self.recording = True
                        self._last_audio = self.clock()
                    if not self._active():
                        self.stream.start()
                self.problem = None
                return
            except Exception as exc:
                with self._lock:
                    self._take = None
                    self.recording = False
                with _pa_lock:
                    self._drop_stream()
                    if attempt == 1:
                        log.warning("Microphone did not start (%s); "
                                    "refreshing the device list and trying "
                                    "again", exc)
                        refresh(force=True)
                        continue
                    self.problem = self._why()
                    raise MicError(self.problem) from exc

    def mute_start(self, seconds):
        """Replace the first `seconds` of the current take with silence
        (the start chime; see App._start)."""
        with self._lock:
            if self._take is not None:
                self._take.mute = max(0, int(round(float(seconds)
                                                   * self._take.rate)))

    def stop_async(self):
        """End the take now; returns it. It completes once one stream
        latency more audio has arrived (or after a timeout)."""
        with self._lock:
            take, self._take = self._take, None
            self.recording = False
            self.level = 0.0
        if take is None:
            take = Take(self.rate, 0, self.clock)
            take.finish()
            return take
        latency = self._latency()
        if not self._active():
            take.finish()
            return take
        with self._lock:
            old = self._draining
            if old is not None and not old.done:
                old.finish()
            take.need = int(math.ceil((latency + TAIL_EXTRA) * self.rate))
            take.deadline = self.clock() + latency + DRAIN_GRACE
            self._draining = take
        return take

    def stop(self, timeout=None):
        """End the take, wait for its tail and return the audio."""
        take = self.stop_async()
        samples = take.wait(timeout)
        self.service()
        return samples

    def cancel(self):
        """Throw the current take away (no drain)."""
        with self._lock:
            self._take = None
            self.recording = False
            self.level = 0.0
        self.service()

    def service(self):
        """Housekeeping, call often from the thread that calls start():
        times out a stuck drain, stops the stream once nothing needs it, and
        notices a microphone that went silent. Returns a take that was
        completed now (or None)."""
        finished = None
        d = self._draining
        if d is not None:
            if not d.done and d.deadline is not None \
                    and self.clock() >= d.deadline:
                d.finish(timed_out=True)
                log.debug("Recording tail timed out")
            if d.done:
                with self._lock:
                    if self._draining is d:
                        self._draining = None
                finished = d
        if self.recording:
            if (self._active() and self.clock() - self._last_audio
                    > STALL_SECONDS and self.problem is None):
                self.problem = PROBLEM_SILENT
                log.warning("No audio from the microphone for %.1f s",
                            STALL_SECONDS)
        elif self._draining is None and self._active():
            with _pa_lock:
                try:
                    if self.stream is not None:
                        self.stream.stop()
                except Exception:
                    self._drop_stream()
        return finished

    def check_devices(self):
        """Refresh the device list if it changed (when idle) and update
        .problem for a missing chosen microphone. Returns True if the audio
        system was re-initialised."""
        if self.busy:
            return False
        changed = refresh()
        # Another thread (the window's health check lists devices) may
        # have done the refresh: look at the generation, not only `changed`.
        seen, self._generation = self._generation, generation
        if (changed or seen != generation
                or self.problem in (PROBLEM_MISSING, PROBLEM_NONE)):
            why = self._why()
            self.problem = None if why == PROBLEM_START else why
        return changed

    def close(self):
        with self._lock:
            self._take = None
            self.recording = False
            d, self._draining = self._draining, None
        if d is not None:
            d.finish()
        with _pa_lock:
            if self.stream is not None:
                try:
                    self.stream.stop()
                except Exception:
                    pass
            self._drop_stream()


def read_wav(path):
    """PCM WAV (8/16/24/32-bit, any rate, any channels) -> float32 mono 16 kHz."""
    with wave.open(str(path), "rb") as wf:
        width, rate = wf.getsampwidth(), wf.getframerate()
        channels = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    if width == 1:
        audio = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128
    elif width == 2:
        audio = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        audio = ints.astype(np.float32) / 8388608
    elif width == 4:
        audio = (np.frombuffer(raw, "<i4").astype(np.float64)
                 / 2147483648).astype(np.float32)
    else:
        raise ValueError(f"Unsupported WAV sample width: {width} bytes")
    if channels > 1:
        audio = audio[: len(audio) // channels * channels]
        audio = audio.reshape(-1, channels).mean(axis=1)
    return resample(audio, rate)
