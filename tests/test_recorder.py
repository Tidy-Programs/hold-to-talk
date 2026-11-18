"""E3 (the end of a dictation is kept) and E4 (microphones that come and
go are noticed) with a fake sounddevice module: no real microphone."""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import threading
import time
import types
import unittest
from unittest import mock

import numpy as np

import audio
import keys
from app import App
from fakes import FakeSD

SPEECH, TAIL = 0.1, 0.5


class Base(unittest.TestCase):
    def setUp(self):
        self.sd = FakeSD()
        self.sig = [self.sd.signature()]
        # PortAudio's list (the fake's snapshot) matches Windows' at start.
        patches = [mock.patch.object(audio, "sd", self.sd),
                   mock.patch.object(audio, "device_signature",
                                     lambda: self.sig[0], create=True),
                   mock.patch.object(audio, "_signature", self.sig[0],
                                     create=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def unplug(self, name):
        self.sd.present.remove(name)
        self.sig[0] = self.sd.signature()

    def plug(self, name):
        self.sd.present.append(name)
        self.sig[0] = self.sd.signature()

    def stream(self):
        return self.sd.streams[-1]


class Tail(Base):
    def test_audio_delivered_after_the_stop_request_is_kept(self):
        rec = audio.Recorder()
        rec.start()
        s = self.stream()
        for _ in range(20):
            s.feed(SPEECH)                    # 0.2 s of speech
        stop_called = threading.Event()

        def driver():
            # The last word, still in the driver's buffers when the key
            # comes up, arrives over the next stream latency (0.05 s).
            stop_called.wait(2)
            for _ in range(10):
                s.feed(TAIL)
                time.sleep(0.005)
        t = threading.Thread(target=driver)
        t.start()
        # Like PortAudio, the stream may also hand over a buffered block
        # while it is being stopped.
        self.sd.on_stop = lambda st: st.feed(TAIL)
        stop_called.set()
        samples = rec.stop()
        t.join()
        tail = int(np.sum(np.isclose(samples, TAIL)))
        self.assertGreaterEqual(tail, int(0.05 * 16000))     # >= latency
        self.assertEqual(int(np.sum(np.isclose(samples, SPEECH))), 3200)

    def test_stop_async_then_callbacks(self):
        rec = audio.Recorder()
        rec.start()
        s = self.stream()
        s.feed(SPEECH)
        take = rec.stop_async()
        self.assertFalse(take.done)
        self.assertTrue(s.active)             # not cut off
        for _ in range(8):                    # 0.08 s > 0.05 + 0.02
            s.feed(TAIL)
        self.assertTrue(take.done)
        out = take.wait(0)
        self.assertEqual(int(np.sum(np.isclose(out, TAIL))),
                         int((0.05 + audio.TAIL_EXTRA) * 16000))
        rec.service()
        self.assertFalse(s.active)            # stopped once drained

    def test_quick_second_press_keeps_both(self):
        rec = audio.Recorder()
        rec.start()
        s = self.stream()
        s.feed(SPEECH)
        first = rec.stop_async()
        rec.start()                           # pressed again at once
        self.assertEqual(len(self.sd.streams), 1)
        for _ in range(10):
            s.feed(TAIL)
        self.assertTrue(first.done)
        second = rec.stop_async()
        for _ in range(10):
            s.feed(0.2)
        a, b = first.wait(0), second.wait(0)
        self.assertEqual(len(a), 160 + 1120)
        self.assertGreater(len(b), 0)

    def test_drain_times_out_if_audio_stops(self):
        clock = [100.0]
        rec = audio.Recorder(clock=lambda: clock[0])
        rec.start()
        self.stream().feed(SPEECH)
        take = rec.stop_async()
        clock[0] += 1.0
        self.assertIs(rec.service(), take)
        self.assertTrue(take.done and take.timed_out)
        self.assertEqual(len(take.wait(0)), 160)

    def test_start_chime_muted(self):
        rec = audio.Recorder()
        rec.start()
        rec.mute_start(0.25)
        s = self.stream()
        for _ in range(50):
            s.feed(0.3)
        rec.service()
        out = rec.stop(timeout=0)
        self.assertTrue(np.all(out[:4000] == 0))
        self.assertTrue(np.all(out[4000:8000] > 0.29))


class Devices(Base):
    def test_unplug_and_replug_without_restart(self):
        rec = audio.Recorder("Headset")
        rec.start()
        rec.stop(timeout=0)
        self.assertEqual([d["name"] for d in audio.list_devices()],
                         ["Desk mic", "Headset mic"])
        self.unplug("Headset mic")
        # Listing is fresh: PortAudio is re-initialised.
        self.assertEqual([d["name"] for d in audio.list_devices()],
                         ["Desk mic"])
        self.assertGreaterEqual(self.sd.inits, 1)
        self.assertTrue(self.sd.streams[0].closed)
        with self.assertRaises(audio.MicError):
            rec.start()
        self.assertEqual(rec.problem, audio.PROBLEM_MISSING)
        self.plug("Headset mic")
        rec.check_devices()
        self.assertIsNone(rec.problem)
        rec.start()                           # works again
        self.assertTrue(rec.recording)
        rec.stop(timeout=0)

    def test_refresh_done_by_another_thread_still_sets_the_problem(self):
        # The window's health check lists devices on its own thread; that
        # refresh must not hide the change from the app's check.
        rec = audio.Recorder()
        self.unplug("Desk mic")
        self.unplug("Headset mic")
        t = threading.Thread(target=audio.list_devices)
        t.start()
        t.join(5)
        rec.check_devices()
        self.assertEqual(rec.problem, audio.PROBLEM_NONE)
        self.plug("Desk mic")
        t = threading.Thread(target=audio.list_devices)
        t.start()
        t.join(5)
        rec.check_devices()
        self.assertIsNone(rec.problem)

    def test_failed_start_reinitialises_and_retries(self):
        rec = audio.Recorder()
        self.sd.fail_starts = 1
        rec.start()
        self.assertTrue(rec.recording)
        self.assertEqual(self.sd.inits, 1)    # forced refresh
        self.assertTrue(self.sd.streams[0].closed)
        rec.stop(timeout=0)

    def test_new_default_device_is_noticed(self):
        rec = audio.Recorder()
        rec.start()
        rec.stop(timeout=0)
        self.plug("USB mic")                  # a new microphone
        self.assertTrue(rec.check_devices())
        self.assertIn("USB mic", [d["name"] for d in audio.list_devices()])

    def test_no_refresh_while_recording(self):
        rec = audio.Recorder()
        rec.start()
        self.plug("USB mic")
        self.assertFalse(audio.refresh())
        self.assertFalse(self.stream().closed)
        rec.stop(timeout=0)
        self.assertTrue(audio.refresh())

    def test_no_microphone_at_all(self):
        rec = audio.Recorder()
        self.unplug("Desk mic")
        self.unplug("Headset mic")
        with self.assertRaises(audio.MicError):
            rec.start()
        self.assertEqual(rec.problem, audio.PROBLEM_NONE)


class AppMic(Base):
    """App with the real Recorder on the fake device: E4 end to end."""

    def test_mic_problem_and_recovery(self):
        down = set()
        p = mock.patch.object(keys, "key_down", side_effect=lambda v: v in down)
        p.start()
        self.addCleanup(p.stop)
        app = App.__new__(App)
        app._init_state({"hotkey": "f9", "min_seconds": 0.1,
                         "input_device": "Headset"},
                        engine=types.SimpleNamespace(), dry_run=True)
        app.recorder = audio.Recorder("Headset")
        notices = []
        app.on_notice.append(lambda k, m: notices.append((k, m)))
        self.unplug("Headset mic")
        app._next_device_check = 0
        app._step()
        self.assertEqual(app.mic_problem, audio.PROBLEM_MISSING)
        self.assertIn(("mic", audio.PROBLEM_MISSING), notices)
        down.add(0x78)                        # press F9: fails, no crash
        app._step()
        self.assertEqual(app._state, "idle")
        down.clear()
        app._step()
        self.plug("Headset mic")
        app._next_device_check = 0
        app._step()
        self.assertIsNone(app.mic_problem)
        down.add(0x78)
        app._step()
        self.assertEqual(app._state, "recording")
        app._discard("test over")


if __name__ == "__main__":
    unittest.main()
