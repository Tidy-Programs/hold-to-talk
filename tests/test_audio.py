import fakes  # noqa: F401  (HOLDTOTALK_HOME -> a temporary folder first)

import struct
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

import audio


def write_wav(path, frames, rate, channels, width):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(frames)


class ReadWav(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "a.wav"

    def tearDown(self):
        self.tmp.cleanup()

    def test_16bit_mono_16k_is_exact(self):
        ints = np.array([0, 16384, -16384, 32767, -32768], dtype="<i2")
        write_wav(self.path, ints.tobytes(), 16000, 1, 2)
        out = audio.read_wav(self.path)
        self.assertEqual(out.dtype, np.float32)
        np.testing.assert_allclose(out, [0, 0.5, -0.5, 32767 / 32768, -1])

    def test_stereo_is_mixed_down(self):
        ints = np.array([1000, 3000, -2000, 2000], dtype="<i2")  # L,R,L,R
        write_wav(self.path, ints.tobytes(), 16000, 2, 2)
        np.testing.assert_allclose(audio.read_wav(self.path),
                                   [2000 / 32768, 0], atol=1e-7)

    def test_resampled_to_16k(self):
        t = np.arange(44100) / 44100
        ints = (np.sin(2 * np.pi * 440 * t) * 20000).astype("<i2")
        write_wav(self.path, ints.tobytes(), 44100, 1, 2)
        out = audio.read_wav(self.path)
        self.assertEqual(len(out), 16000)
        self.assertAlmostEqual(float(np.max(np.abs(out))), 20000 / 32768,
                               places=2)

    def test_8_24_and_32_bit(self):
        write_wav(self.path, bytes([128, 255, 0]), 16000, 1, 1)
        np.testing.assert_allclose(audio.read_wav(self.path),
                                   [0, 127 / 128, -1])
        b24 = b"".join(struct.pack("<i", v)[:3]
                       for v in (0, 4194304, -8388608))
        write_wav(self.path, b24, 16000, 1, 3)
        np.testing.assert_allclose(audio.read_wav(self.path), [0, 0.5, -1])
        write_wav(self.path, np.array([0, 2 ** 30, -2 ** 31], "<i4").tobytes(),
                  16000, 1, 4)
        np.testing.assert_allclose(audio.read_wav(self.path), [0, 0.5, -1])

    def test_selftest_style_file_shape(self):
        write_wav(self.path, np.zeros(8000, "<i2").tobytes(), 16000, 1, 2)
        out = audio.read_wav(self.path)
        self.assertEqual(out.shape, (8000,))
        self.assertEqual(out.dtype, np.float32)


if __name__ == "__main__":
    unittest.main()
