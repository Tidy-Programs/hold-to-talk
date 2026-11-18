"""Two short chimes so dictation start and stop can be heard without looking.

    start: a rising two-note chime     stop: a falling two-note chime

The audio is generated here (no files, no dependencies) and played from
memory with winsound on a short-lived daemon thread, so the caller never
blocks. play() never raises.

    sounds.play("start", cfg)       # cfg["sounds"] = {"enabled", "volume"}
"""

import array
import io
import logging
import math
import threading
import wave

try:                                   # Windows only
    import winsound
except ImportError:                    # pragma: no cover
    winsound = None

log = logging.getLogger("holdtotalk.sounds")

# mute_start: replace the first chime-length of each recording with silence so
# the start chime is not captured by the microphone (app.py); off by default
# because it can clip the first syllable.
DEFAULTS = {"enabled": True, "volume": 0.5, "mute_start": False}

RATE = 22050
NOTE_SECONDS = 0.085                 # two notes: 170 ms in all
FADE_IN = 0.010                      # s, raised cosine
FADE_OUT = 0.030
PEAK = 0.8                           # of full scale at volume 1.0
NOTES = {                            # Hz
    "start": (659.25, 880.00),       # E5 -> A5, rising
    "stop": (880.00, 587.33),        # A5 -> D5, falling
}
KINDS = tuple(NOTES)


def settings_of(cfg):
    """cfg["sounds"] over DEFAULTS: {"enabled": bool, "volume": 0.0-1.0,
    "mute_start": bool}.
    A malformed value falls back to its default; volume is clamped."""
    out = dict(DEFAULTS)
    sub = cfg.get("sounds") if isinstance(cfg, dict) else None
    if isinstance(sub, dict):
        if isinstance(sub.get("enabled"), bool):
            out["enabled"] = sub["enabled"]
        if isinstance(sub.get("mute_start"), bool):
            out["mute_start"] = sub["mute_start"]
        out["volume"] = _volume(sub.get("volume"), DEFAULTS["volume"])
    return out


def _volume(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value != value:                 # NaN
        return default
    return min(1.0, max(0.0, float(value)))


def _samples(kind, volume):
    amp = PEAK * 32767 * _volume(volume, DEFAULTS["volume"])
    n_note = int(RATE * NOTE_SECONDS)
    n_in, n_out = int(RATE * FADE_IN), int(RATE * FADE_OUT)
    out = array.array("h")
    for freq in NOTES[kind]:
        for i in range(n_note):
            env = 1.0
            if i < n_in:
                env = 0.5 - 0.5 * math.cos(math.pi * i / n_in)
            elif i >= n_note - n_out:
                env = 0.5 - 0.5 * math.cos(math.pi * (n_note - 1 - i) / n_out)
            value = amp * env * math.sin(2 * math.pi * freq * i / RATE)
            out.append(max(-32768, min(32767, int(round(value)))))
    return out


_wav_cache = {}        # (kind, clamped volume) -> bytes; a handful at most


def wav_bytes(kind, volume):
    """A complete 16-bit mono WAV file for the "start" or "stop" chime.
    No playback; the bytes are built once per (kind, volume) and then reused
    (synthesising them in Python took about 2.6 ms per chime). ValueError
    for an unknown kind."""
    if kind not in NOTES:
        raise ValueError(f"unknown chime {kind!r}")
    key = (kind, _volume(volume, DEFAULTS["volume"]))
    data = _wav_cache.get(key)
    if data is None:
        if len(_wav_cache) >= 64:          # volumes are few; never unbounded
            _wav_cache.clear()
        data = _wav_cache[key] = _build_wav(kind, volume)
    return data


def _build_wav(kind, volume):
    samples = _samples(kind, volume)
    if samples.itemsize != 2:          # pragma: no cover
        raise RuntimeError("unexpected sample size")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes() if _LITTLE else _swapped(samples))
    return buf.getvalue()


_LITTLE = array.array("h", [1]).tobytes() == b"\x01\x00"


def _swapped(samples):             # pragma: no cover (big-endian hosts)
    copy = array.array("h", samples)
    copy.byteswap()
    return copy.tobytes()


# -- playing --------------------------------------------------------------------

_lock = threading.Lock()
_busy = False          # a worker thread is running
_pending = None        # the newest request that arrived meanwhile: (kind, vol)


def _play_bytes(data):
    """Play a WAV held in memory and wait for it to finish (on the worker
    thread). Tests replace this; it is the only place that makes sound."""
    winsound.PlaySound(data, winsound.SND_MEMORY)


def _worker(request):
    global _busy, _pending
    try:
        while request is not None:
            try:
                _play_bytes(wav_bytes(*request))
            except Exception as exc:
                log.debug("chime not played: %s", type(exc).__name__)
            with _lock:
                request, _pending = _pending, None
                if request is None:
                    _busy = False
    except BaseException:
        with _lock:
            _busy = False
            _pending = None
        raise


def play(kind, cfg=None):
    """Play the "start" or "stop" chime in the background. True if one was
    started or queued; False when sounds are off, the volume is 0, `kind` is
    unknown or playback is unavailable. Never raises, never blocks.

    At most one chime waits behind the one playing (the newest), so rapid
    calls cannot build a backlog."""
    global _busy, _pending
    try:
        if kind not in NOTES or winsound is None:
            return False
        s = settings_of(cfg)
        if not s["enabled"] or s["volume"] <= 0:
            return False
        request = (kind, s["volume"])
        with _lock:
            if _busy:
                _pending = request
                return True
            _busy = True
        try:
            threading.Thread(target=_worker, args=(request,), daemon=True,
                             name="chime").start()
        except Exception:
            with _lock:
                _busy = False
            return False
        return True
    except Exception:
        return False
