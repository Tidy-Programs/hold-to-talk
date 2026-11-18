"""sounds.py: the audio is generated and inspected; nothing is ever played
(the playback call is replaced in every test that could reach it)."""

import array
import io
import threading
import time
import unittest
import wave
from unittest import mock

import sounds


def decode(data):
    with wave.open(io.BytesIO(data), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth()) == (1, 2)
        rate, frames = w.getframerate(), w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(frames)
    return rate, list(samples)


class Cache(unittest.TestCase):
    """P5: the chime WAV is synthesised once per (kind, volume)."""

    def test_synthesised_once_per_kind_and_volume(self):
        sounds._wav_cache.clear()
        with mock.patch.object(sounds, "_samples",
                               wraps=sounds._samples) as spy:
            first = sounds.wav_bytes("start", 0.5)
            for _ in range(5):
                self.assertEqual(sounds.wav_bytes("start", 0.5), first)
            self.assertEqual(spy.call_count, 1)
            sounds.wav_bytes("stop", 0.5)          # another kind
            sounds.wav_bytes("start", 0.25)        # another volume
            self.assertEqual(spy.call_count, 3)
            sounds.wav_bytes("start", 7.0)         # clamped to 1.0
            sounds.wav_bytes("start", 1.0)
            self.assertEqual(spy.call_count, 4)

    def test_cached_bytes_equal_fresh_bytes(self):
        sounds._wav_cache.clear()
        for kind in sounds.KINDS:
            for vol in (0, 0.3, 1.0):
                self.assertEqual(sounds.wav_bytes(kind, vol),
                                 sounds._build_wav(kind, vol))

    def test_the_cache_stays_small(self):
        sounds._wav_cache.clear()
        for i in range(200):
            sounds.wav_bytes("start", i / 200)
        self.assertLessEqual(len(sounds._wav_cache), 64)

    def test_second_call_is_fast(self):
        sounds._wav_cache.clear()
        sounds.wav_bytes("stop", 0.5)
        started = time.perf_counter()
        for _ in range(100):
            sounds.wav_bytes("stop", 0.5)
        self.assertLess((time.perf_counter() - started) / 100, 0.0005)

class Audio(unittest.TestCase):
    def test_duration_is_short(self):
        for kind in ("start", "stop"):
            rate, s = decode(sounds.wav_bytes(kind, 0.5))
            seconds = len(s) / rate
            self.assertLessEqual(seconds, 0.18, kind)
            self.assertGreaterEqual(seconds, 0.10, kind)

    def test_stays_within_16_bit_range_even_at_full_volume(self):
        for kind in ("start", "stop"):
            for vol in (1.0, 5.0, -3.0, 0.5):
                _, s = decode(sounds.wav_bytes(kind, vol))
                self.assertTrue(all(-32768 <= v <= 32767 for v in s))
                self.assertGreater(max(map(abs, s)), 0) if vol > 0 else None

    def test_starts_and_ends_near_zero_without_clicks(self):
        for kind in ("start", "stop"):
            _, s = decode(sounds.wav_bytes(kind, 1.0))
            peak = max(map(abs, s))
            self.assertLess(abs(s[0]), peak * 0.02)
            self.assertLess(abs(s[-1]), peak * 0.02)
            self.assertLess(max(map(abs, s[:5])), peak * 0.05)
            self.assertLess(max(map(abs, s[-5:])), peak * 0.05)
            # No jump between neighbouring samples anywhere (a click).
            step = max(abs(a - b) for a, b in zip(s, s[1:]))
            self.assertLess(step, peak * 0.5)
            # The gap between the two notes is smooth too: the envelope
            # reaches ~0 at the joint.
            mid = len(s) // 2
            self.assertLess(max(map(abs, s[mid - 3:mid + 3])), peak * 0.1)

    def test_the_two_chimes_differ(self):
        a = sounds.wav_bytes("start", 0.5)
        b = sounds.wav_bytes("stop", 0.5)
        self.assertNotEqual(a, b)
        _, sa = decode(a)
        _, sb = decode(b)
        self.assertEqual(len(sa), len(sb))
        diff = sum(abs(x - y) for x, y in zip(sa, sb)) / len(sa)
        self.assertGreater(diff, 1000)

    def test_start_rises_and_stop_falls(self):
        def pitches(kind):
            rate, s = decode(sounds.wav_bytes(kind, 1.0))
            half = len(s) // 2

            def freq(part):          # zero crossings per second
                n = sum(1 for x, y in zip(part, part[1:])
                        if (x < 0) != (y < 0))
                return n / 2 / (len(part) / rate)
            return freq(s[:half]), freq(s[half:])
        first, second = pitches("start")
        self.assertGreater(second, first * 1.2)
        first, second = pitches("stop")
        self.assertLess(second, first * 0.8)

    def test_volume_scales_amplitude(self):
        peaks = [max(map(abs, decode(sounds.wav_bytes("start", v))[1]))
                 for v in (0.25, 0.5, 1.0)]
        self.assertLess(peaks[0], peaks[1])
        self.assertLess(peaks[1], peaks[2])
        self.assertAlmostEqual(peaks[1] / peaks[0], 2.0, delta=0.05)
        self.assertAlmostEqual(peaks[2] / peaks[1], 2.0, delta=0.05)
        _, silent = decode(sounds.wav_bytes("start", 0))
        self.assertEqual(max(map(abs, silent)), 0)

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            sounds.wav_bytes("beep", 0.5)


class Settings(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(sounds.DEFAULTS, {"enabled": True, "volume": 0.5,
                                           "mute_start": False})
        self.assertEqual(sounds.settings_of({}), sounds.DEFAULTS)
        self.assertEqual(sounds.settings_of(None), sounds.DEFAULTS)

    def test_merge_and_clamp(self):
        base = {"enabled": True, "volume": 0.5, "mute_start": False}
        self.assertEqual(sounds.settings_of({"sounds": {"volume": 2}}),
                         {**base, "volume": 1.0})
        self.assertEqual(sounds.settings_of({"sounds": {"volume": -1}}),
                         {**base, "volume": 0.0})
        self.assertEqual(sounds.settings_of({"sounds": {"enabled": False}}),
                         {**base, "enabled": False})

    def test_mute_start_setting(self):
        self.assertIs(sounds.settings_of({})["mute_start"], False)
        self.assertIs(sounds.settings_of(
            {"sounds": {"mute_start": True}})["mute_start"], True)
        self.assertIs(sounds.settings_of(
            {"sounds": {"mute_start": False}})["mute_start"], False)
        for bad in ("yes", 1, None, [True]):      # not a bool: the default
            self.assertIs(sounds.settings_of(
                {"sounds": {"mute_start": bad}})["mute_start"], False, bad)

    def test_malformed_values_fall_back(self):
        for sub in ("loud", 3, [1], {"enabled": "yes", "volume": "x"},
                    {"volume": True}, {"volume": float("nan")}):
            self.assertEqual(sounds.settings_of({"sounds": sub}),
                             sounds.DEFAULTS, sub)


class Playing(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(sounds, "_play_bytes")
        self.play_bytes = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.wait_idle)

    def wait_idle(self):
        deadline = time.time() + 5
        while sounds._busy and time.time() < deadline:
            time.sleep(0.005)

    def test_plays_the_right_chime_off_the_calling_thread(self):
        seen = {}
        done = threading.Event()

        def fake(data):
            seen["thread"] = threading.current_thread()
            seen["data"] = data
            done.set()
        self.play_bytes.side_effect = fake
        self.assertTrue(sounds.play("start", {}))
        self.assertTrue(done.wait(5))
        self.assertIsNot(seen["thread"], threading.main_thread())
        self.assertTrue(seen["thread"].daemon)
        self.assertEqual(seen["data"], sounds.wav_bytes("start", 0.5))
        self.wait_idle()
        done.clear()
        self.assertTrue(sounds.play("stop", {"sounds": {"volume": 0.2}}))
        self.assertTrue(done.wait(5))
        self.assertEqual(seen["data"], sounds.wav_bytes("stop", 0.2))

    def test_the_caller_never_blocks(self):
        release = threading.Event()
        self.play_bytes.side_effect = lambda data: release.wait(5)
        started = time.perf_counter()
        self.assertTrue(sounds.play("start", {}))
        self.assertLess(time.perf_counter() - started, 0.2)
        release.set()

    def test_false_when_disabled_silent_or_unknown(self):
        self.assertFalse(sounds.play("start", {"sounds": {"enabled": False}}))
        self.assertFalse(sounds.play("start", {"sounds": {"volume": 0}}))
        self.assertFalse(sounds.play("start", {"sounds": {"volume": -2}}))
        self.assertFalse(sounds.play("beep", {}))
        self.assertFalse(sounds.play(None, {}))
        self.play_bytes.assert_not_called()

    def test_false_when_playback_is_unavailable(self):
        with mock.patch.object(sounds, "winsound", None):
            self.assertFalse(sounds.play("start", {}))
        self.play_bytes.assert_not_called()

    def test_never_raises(self):
        self.play_bytes.side_effect = OSError("no sound card")
        self.assertTrue(sounds.play("start", {}))       # error stays inside
        self.wait_idle()
        self.assertFalse(sounds._busy)
        for cfg in (None, 5, "x", {"sounds": object()}):
            sounds.play("stop", cfg)
        with mock.patch.object(threading, "Thread",
                               side_effect=RuntimeError("no threads")):
            self.assertFalse(sounds.play("start", {}))
        self.assertFalse(sounds._busy)                  # and nothing is stuck
        self.wait_idle()

    def test_rapid_calls_never_build_a_backlog(self):
        release = threading.Event()
        played = []

        def slow(data):
            played.append(data)
            release.wait(5)
        self.play_bytes.side_effect = slow
        results = [sounds.play("start" if i % 2 == 0 else "stop", {})
                   for i in range(50)]
        self.assertTrue(all(results))
        release.set()
        self.wait_idle()
        # One playing, plus at most the newest one that arrived meanwhile.
        self.assertLessEqual(len(played), 2)
        self.assertEqual(played[-1], sounds.wav_bytes("stop", 0.5))
        # And it is usable again afterwards.
        self.play_bytes.side_effect = None
        self.assertTrue(sounds.play("start", {}))


if __name__ == "__main__":
    unittest.main()
