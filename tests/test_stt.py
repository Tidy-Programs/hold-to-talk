"""stt.Engine with a fake speech model and a fake ctranslate2: nothing is
loaded, downloaded or run on the GPU."""

import atexit
import os
import shutil
import tempfile

if "htt-test-home-" not in os.environ.get("HOLDTOTALK_HOME", ""):
    _home = tempfile.mkdtemp(prefix="htt-test-home-")
    os.environ["HOLDTOTALK_HOME"] = _home
    atexit.register(shutil.rmtree, _home, True)

import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import stt

CFG = {"model": "big-model", "cpu_model": "small-model", "device": "auto",
       "language": None, "vocabulary": [], "dictionary": []}


class Seg:
    def __init__(self, text):
        self.text = text


class FakeModel:
    def __init__(self, language="de"):
        self.language = language
        self.probability = None
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        info = types.SimpleNamespace(language=self.language)
        if self.probability is not None:
            info.language_probability = self.probability
        return [Seg(" Hallo "), Seg("Welt ")], info


class World:
    """Fake faster_whisper + ctranslate2 modules and their behaviour."""

    def __init__(self, gpus=1, supported=("float16", "int8_float16", "int8"),
                 cached=True, gpu_error=None):
        self.gpus, self.supported = gpus, set(supported)
        self.cached, self.gpu_error = cached, gpu_error
        self.loads = []                  # (name, device, compute, local_only)
        self.ct_calls = []
        self.model = FakeModel()
        self.runtime = True              # the NVIDIA runtime is installed
        self.kwargs = []                 # extra keywords of each load
        world = self

        class WhisperModel:
            def __new__(cls, name, device="auto", compute_type="default",
                        local_files_only=False, **kw):
                world.loads.append((name, device, compute_type,
                                    local_files_only))
                world.kwargs.append(kw)
                if local_files_only and not world.cached:
                    raise FileNotFoundError("Cannot find an appropriate "
                                            "cached snapshot folder ... "
                                            "outgoing traffic has been "
                                            "disabled")
                if device == "cuda" and world.gpu_error:
                    raise world.gpu_error
                return world.model

        def count():
            world.ct_calls.append("count")
            return world.gpus

        def types_(device):
            world.ct_calls.append(("types", device))
            return world.supported

        self.modules = {
            "faster_whisper": types.SimpleNamespace(WhisperModel=WhisperModel),
            "ctranslate2": types.SimpleNamespace(
                get_cuda_device_count=count,
                get_supported_compute_types=types_)}

    def engine(self, **cfg):
        with mock.patch.dict(sys.modules, self.modules), \
                mock.patch.object(stt, "add_cuda_dll_dirs", return_value=[]), \
                mock.patch.object(stt, "cuda_runtime_installed",
                                  return_value=self.runtime):
            return stt.Engine(dict(CFG, **cfg))


class Loading(unittest.TestCase):
    def test_a_cached_model_is_loaded_without_the_network(self):
        w = World()
        e = w.engine()
        self.assertEqual(w.loads, [("big-model", "cuda", "float16", True)])
        self.assertEqual((e.name, e.device, e.compute_type),
                         ("big-model", "cuda", "float16"))
        self.assertIsNone(e.fallback_reason)

    def test_a_missing_model_is_downloaded_only_then(self):
        w = World(cached=False)
        w.cached = False
        # Cached after the first download so the order is visible.
        e = w.engine(device="cpu")
        self.assertEqual(w.loads, [("small-model", "cpu", "int8", True),
                                   ("small-model", "cpu", "int8", False)])
        self.assertEqual(e.device, "cpu")

    def test_other_load_errors_do_not_trigger_a_download(self):
        w = World(gpu_error=RuntimeError("Library cublas64_12.dll is not "
                                         "found or cannot be loaded"))
        e = w.engine()
        self.assertEqual([(l[1], l[3]) for l in w.loads],
                         [("cuda", True), ("cpu", True)])
        self.assertEqual(e.device, "cpu")

    def test_telemetry_is_disabled_before_the_library_is_imported(self):
        env = {k: v for k, v in os.environ.items()
               if k != "HF_HUB_DISABLE_TELEMETRY"}
        code = ("import os, sys; import stt; "
                "print(os.environ.get('HF_HUB_DISABLE_TELEMETRY'), "
                "'faster_whisper' in sys.modules)")
        out = subprocess.run([sys.executable, "-B", "-c", code], env=env,
                             cwd=Path(stt.__file__).parent, text=True,
                             capture_output=True, timeout=120)
        self.assertEqual(out.stdout.split(), ["1", "False"], out.stderr)


class PinnedModels(unittest.TestCase):
    def test_the_revision_is_passed_to_the_loader_and_to_the_download(self):
        w = World(cached=False)
        w.engine(cpu_model="small", device="cpu")
        rev = stt.MODEL_REVISIONS["small"]
        self.assertRegex(rev, r"^[0-9a-f]{40}$")
        self.assertEqual(w.kwargs, [{"revision": rev}, {"revision": rev}])
        self.assertEqual([l[3] for l in w.loads], [True, False])

    def test_the_gpu_model_is_pinned_too(self):
        w = World()
        w.engine(model="large-v3-turbo")
        self.assertEqual(w.kwargs[0],
                         {"revision": stt.MODEL_REVISIONS["large-v3-turbo"]})

    def test_an_unlisted_model_is_loaded_unpinned(self):
        w = World()
        w.engine()                                  # "big-model"
        self.assertEqual(w.kwargs, [{}])
        self.assertIsNone(stt.model_revision("some/other-repo"))
        self.assertIsNone(stt.model_revision(None))

    def test_every_pin_is_a_full_commit_hash(self):
        for name, rev in stt.MODEL_REVISIONS.items():
            self.assertRegex(rev, r"^[0-9a-f]{40}$", name)

    def test_the_installer_reads_the_same_table(self):
        text = (Path(stt.__file__).parent / "install.ps1").read_text(
            encoding="ascii")
        self.assertIn("MODEL_REVISIONS in stt.py", text)
        src = Path(stt.__file__).read_text(encoding="utf-8")
        for name, rev in stt.MODEL_REVISIONS.items():
            self.assertRegex(src, rf'"{name}":\s*"{rev}"')
        self.assertIn("revision=rev", text)


class GraphicsCard(unittest.TestCase):
    def test_without_the_nvidia_runtime_the_card_is_not_tried(self):
        # The installer lets the user decline the NVIDIA runtime: the app must
        # then use the processor and never download the large GPU model.
        w = World()
        w.runtime = False
        e = w.engine()
        self.assertEqual([l[1] for l in w.loads], ["cpu"])
        self.assertEqual(e.device, "cpu")
        self.assertIn("NVIDIA runtime is not installed", e.fallback_reason)
        self.assertNotIn("big-model", e.fallback_reason)

    def test_no_gpu_means_the_gpu_model_is_never_requested(self):
        w = World(gpus=0)
        e = w.engine()
        self.assertEqual([l[1] for l in w.loads], ["cpu"])
        self.assertNotIn(("types", "cuda"), w.ct_calls)
        self.assertEqual(e.device, "cpu")
        self.assertIn("no NVIDIA", e.fallback_reason)
        self.assertNotIn("big-model", e.fallback_reason)

    def test_the_runtime_check_looks_for_cublas(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "nvidia"
            (root / "cublas" / "bin").mkdir(parents=True)
            with mock.patch.object(stt, "_nvidia_roots", return_value=[root]), \
                    mock.patch.dict(os.environ, {"PATH": ""}):
                self.assertFalse(stt.cuda_runtime_installed())
                (root / "cublas" / "bin" / "cublas64_12.dll").write_bytes(b"")
                self.assertTrue(stt.cuda_runtime_installed())

    def test_cuda_only_without_a_gpu_is_a_clear_error(self):
        w = World(gpus=0)
        with self.assertRaises(RuntimeError) as cm:
            w.engine(device="cuda")
        self.assertIn("no NVIDIA", str(cm.exception))
        self.assertEqual(w.loads, [])

    def test_compute_type_is_the_best_the_card_supports(self):
        for supported, want in ((("float16", "int8"), "float16"),
                                (("int8_float16", "int8", "float32"),
                                 "int8_float16"),
                                (("int8", "float32"), "int8")):
            with self.subTest(supported=supported):
                w = World(supported=supported)
                e = w.engine()
                self.assertEqual(e.compute_type, want)
                self.assertEqual(w.loads[0][2], want)
                self.assertIn(("types", "cuda"), w.ct_calls)

    def test_pick_compute_type(self):
        self.assertEqual(stt.pick_compute_type(
            {"int8", "float16", "bfloat16"}), "float16")
        self.assertIsNone(stt.pick_compute_type({"float32", "bfloat16"}))
        self.assertIsNone(stt.pick_compute_type(None))

    def test_a_card_with_no_usable_mode_falls_back_with_a_reason(self):
        w = World(supported=("float32",))
        e = w.engine()
        self.assertEqual(e.device, "cpu")
        self.assertEqual([l[1] for l in w.loads], ["cpu"])
        self.assertTrue(e.fallback_reason)

    def test_a_failed_gpu_start_records_a_short_plain_reason(self):
        for error, word in ((RuntimeError("CUDA failed: out of memory"),
                             "memory"),
                            (RuntimeError("cudnn64_9.dll missing"),
                             "libraries"),
                            (ValueError("weird"), "could not be used")):
            with self.subTest(error=str(error)):
                w = World(gpu_error=error)
                e = w.engine()
                self.assertEqual(e.device, "cpu")
                self.assertIn(word, e.fallback_reason)
                for secret in ("big-model", "weird", "cudnn64"):
                    self.assertNotIn(secret, e.fallback_reason)
                self.assertLess(len(e.fallback_reason), 90)

    def test_the_processor_by_choice_has_no_fallback_reason(self):
        w = World()
        e = w.engine(device="cpu")
        self.assertIsNone(e.fallback_reason)
        self.assertEqual(w.ct_calls, [])        # the card was not even asked

    def test_name_is_not_claimed_before_a_model_is_loaded(self):
        w = World(gpus=0, cached=True)
        with self.assertRaises(RuntimeError):
            w.engine(device="cuda")


class WarmUp(unittest.TestCase):
    """P1: the warm-up asks for exactly what a dictation asks for."""

    def test_warm_up_uses_a_dictations_options(self):
        w = World()
        e = w.engine(vocabulary=["Zorvex"])
        warm = w.model.calls[-1]                  # the last warm-up call
        e.transcribe(np.zeros(1600, dtype=np.float32))
        real = w.model.calls[-1]
        self.assertTrue(warm["vad_filter"])
        self.assertEqual(warm, real)              # every option, prompt too
        self.assertIn("Zorvex", warm["initial_prompt"])

    def test_one_call_when_the_language_is_automatic(self):
        # Language detection runs the speech model, so this one call is the
        # DLL check too; a second call would only cost start-up time.
        w = World()
        w.engine()
        self.assertEqual(len(w.model.calls), 1)
        self.assertTrue(w.model.calls[0]["vad_filter"])
        self.assertIsNone(w.model.calls[0]["language"])

    def test_a_chosen_language_still_gets_the_plain_encoder_check(self):
        # With a language given, silence never reaches the model after VAD.
        w = World()
        w.engine(language="fr")
        self.assertEqual([c["beam_size"] for c in w.model.calls], [1, 5])
        self.assertFalse(w.model.calls[0].get("vad_filter", False))
        self.assertTrue(w.model.calls[1]["vad_filter"])

    def test_a_language_setting_is_part_of_the_warm_up(self):
        w = World()
        w.engine(language="fr")
        self.assertEqual(w.model.calls[-1]["language"], "fr")

    def test_a_failing_warm_up_is_a_failed_load_not_a_half_loaded_model(self):
        w = World()
        real = w.model.transcribe

        def broken(audio, **kw):
            if kw.get("vad_filter"):
                raise RuntimeError("vad model missing")
            return real(audio, **kw)
        w.model.transcribe = broken
        with self.assertRaises(RuntimeError):
            w.engine(device="cuda")


class ComputeType(unittest.TestCase):
    """P6: cfg["compute_type"]."""

    def test_auto_is_todays_behaviour(self):
        w = World()
        self.assertEqual(w.engine(compute_type="auto").compute_type, "float16")
        w = World()
        self.assertEqual(w.engine().compute_type, "float16")   # key absent

    def test_an_explicit_supported_type_is_used(self):
        w = World()
        e = w.engine(compute_type="int8_float16")
        self.assertEqual((e.compute_type, w.loads[0][2]),
                         ("int8_float16", "int8_float16"))

    def test_an_unsupported_type_falls_back_to_auto_with_a_warning(self):
        w = World(supported=("float16", "int8"))
        with self.assertLogs("holdtotalk.stt", "WARNING") as logs:
            e = w.engine(compute_type="bfloat16")
        self.assertEqual(e.compute_type, "float16")
        self.assertIn("bfloat16", "\n".join(logs.output))
        self.assertIn("auto", "\n".join(logs.output))

    def test_the_processor_uses_it_when_supported(self):
        w = World(supported=("int8", "float32"))
        e = w.engine(device="cpu", compute_type="float32")
        self.assertEqual(e.compute_type, "float32")
        w = World(supported=("int8", "float32"))
        e = w.engine(device="cpu", compute_type="float16")   # a GPU type
        self.assertEqual(e.compute_type, "int8")

    def test_pick_compute_type_with_a_request(self):
        sup = {"float16", "int8_float16", "int8"}
        self.assertEqual(stt.pick_compute_type(sup, "int8"), "int8")
        self.assertEqual(stt.pick_compute_type(sup, " INT8_float16 "),
                         "int8_float16")
        self.assertEqual(stt.pick_compute_type(sup, "auto"), "float16")
        self.assertEqual(stt.pick_compute_type(sup, None), "float16")
        with self.assertLogs("holdtotalk.stt", "WARNING"):
            self.assertEqual(stt.pick_compute_type(sup, "float32"), "float16")

class Language(unittest.TestCase):
    def test_the_detected_language_is_recorded(self):
        w = World()
        e = w.engine()
        self.assertIsNone(e.last_language)
        text = e.transcribe(np.zeros(1600, dtype=np.float32))
        self.assertEqual(text, "Hallo Welt")            # still a plain str
        self.assertEqual(e.last_language, "de")
        self.assertIsNone(w.model.calls[-1]["language"])

    def test_the_configured_language_is_what_was_used(self):
        w = World()
        e = w.engine(language="fr")
        e.transcribe(np.zeros(1600, dtype=np.float32))
        self.assertEqual(e.last_language, "fr")
        self.assertEqual(w.model.calls[-1]["language"], "fr")
        e.cfg["language"] = None                       # read on every call
        e.transcribe(np.zeros(1600, dtype=np.float32))
        self.assertEqual(e.last_language, "de")

    def test_unknown_when_the_model_does_not_say(self):
        w = World()
        w.model.language = None
        e = w.engine()
        e.transcribe(np.zeros(1600, dtype=np.float32))
        self.assertIsNone(e.last_language)

    def test_the_probability_is_kept_only_for_a_detected_language(self):
        w = World()
        e = w.engine()
        self.assertIsNone(e.last_language_probability)
        w.model.probability = 0.97
        e.transcribe(np.zeros(1600, dtype=np.float32))
        self.assertEqual((e.last_language, e.last_language_probability),
                         ("de", 0.97))
        e2 = w.engine(language="fr")             # chosen, not detected
        e2.transcribe(np.zeros(1600, dtype=np.float32))
        self.assertEqual((e2.last_language, e2.last_language_probability),
                         ("fr", None))

if __name__ == "__main__":
    unittest.main()
