"""A speech-model folder left half-filled by an interrupted download must never
reach ctranslate2 ("Unable to open file 'model.bin'"): stt decides whether a
folder is complete, repairs it for real, or raises ONE plain error; the app
backs off instead of retrying at once; install.ps1's model.py applies the same
rule (and a test fails if the two drift apart).

Nothing here downloads a model or touches the real Hugging Face cache:
HF_HUB_CACHE points at a temporary folder, the network functions are fakes, and
the one real library call (the installer's check) runs offline."""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import atexit
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import huggingface_hub
from huggingface_hub import errors as hf_errors

import app as app_mod
import stt
from test_engine import bare, drain

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = Path(os.environ.get("LOCALAPPDATA", "")) / "HoldToTalk" / "venv" \
    / "Scripts" / "python.exe"
SYSROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
POWERSHELL = SYSROOT / "System32" / "WindowsPowerShell" / "v1.0" \
    / "powershell.exe"

# An account name that must never show up in a message or a log line. The
# Users folder is built from pieces so this file holds no real-looking path.
ACCOUNT = "tester" + "-zq9"
USERS_PATH = "C:" + "\\" + "Us" + "ers" + "\\" + ACCOUNT \
    + "\\.cache\\huggingface\\hub"


class ConnectError(Exception):
    """Named like httpx's ConnectError, which the classifier looks for."""


def new_cache():
    d = Path(tempfile.mkdtemp(prefix="htt-test-hfcache-"))
    atexit.register(shutil.rmtree, d, True)
    return d


class Net:
    """The Hugging Face functions, faked. `snapshot_ok`: download_model
    completes the folder; otherwise (the Hub is unreachable) it returns an
    existing folder AS IS, like huggingface_hub does, and raises when there is
    none. `files_ok`: hf_hub_download works; otherwise it raises the error the
    library raises without a connection."""

    def __init__(self, cache, snapshot_ok=True, files_ok=True):
        self.cache = cache
        self.snapshot_ok, self.files_ok = snapshot_ok, files_ok
        self.downloads = []        # (name, local_files_only, revision)
        self.files = []            # (repo, filename, revision, force)
        self.cpu_loads = []

    def folder(self, name):
        return fakes.make_model_folder(self.cache, name)

    def download_model(self, name, local_files_only=False, revision=None,
                       **kw):
        self.downloads.append((name, local_files_only, revision))
        repo = stt.MODEL_INFO[name][0] if name in stt.MODEL_INFO \
            else "Fake/" + name
        snap = (self.cache / ("models--" + repo.replace("/", "--"))
                / "snapshots" / (revision or "0" * 40))
        if local_files_only:
            if snap.is_dir():
                return str(snap)
            raise hf_errors.LocalEntryNotFoundError(
                "Cannot find an appropriate cached snapshot folder")
        if self.snapshot_ok:
            return str(fakes.make_model_folder(self.cache, name, revision))
        if snap.is_dir():
            return str(snap)             # the partial folder, as is
        raise hf_errors.LocalEntryNotFoundError(
            "An error happened while trying to locate the files on the disk "
            "and outgoing traffic has been disabled or a connection issue")

    def hf_hub_download(self, repo, filename, revision=None, cache_dir=None,
                        force_download=False, **kw):
        self.files.append((repo, filename, revision, force_download))
        if not self.files_ok:
            try:
                raise ConnectError("[WinError 10061] " + USERS_PATH + " "
                                   + str(self.cache))
            except ConnectError as exc:
                raise hf_errors.LocalEntryNotFoundError(
                    "An error happened while trying to locate the file on "
                    "the disk " + USERS_PATH) from exc
        snap = Path(cache_dir) / ("models--" + repo.replace("/", "--")) \
            / "snapshots" / revision
        snap.mkdir(parents=True, exist_ok=True)
        target = snap / filename
        if target.is_symlink() or target.exists():
            target.unlink()
        target.write_bytes(b"x" * (1_500_000 if filename == "model.bin"
                                   else 12))
        return str(target)

    def patched(self):
        """Context: the fake libraries in place, HF_HUB_CACHE at the cache."""
        stack = _Stack()
        stack.add(mock.patch.dict(os.environ,
                                  {"HF_HUB_CACHE": str(self.cache)}))
        stack.add(mock.patch.dict(sys.modules, {
            "faster_whisper": types.SimpleNamespace(
                download_model=self.download_model,
                WhisperModel=self.whisper_model)}))
        stack.add(mock.patch.object(huggingface_hub, "hf_hub_download",
                                    self.hf_hub_download))
        return stack

    def whisper_model(self, path, device="cpu", compute_type="int8", **kw):
        self.cpu_loads.append((path, device))
        return types.SimpleNamespace(
            transcribe=lambda *a, **k: ([], types.SimpleNamespace(
                language="en")),
            hf_tokenizer=None, is_multilingual=True)


class _Stack:
    def __init__(self):
        self.patches = []

    def add(self, p):
        self.patches.append(p)

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


def break_model_bin(folder, how):
    target = Path(folder) / "model.bin"
    if how == "missing":
        target.unlink()
    elif how == "empty":
        target.write_bytes(b"")
    elif how == "tiny":
        target.write_bytes(b"x" * 999)
    elif how == "directory":
        target.unlink()
        target.mkdir()


class Completeness(unittest.TestCase):
    def setUp(self):
        self.cache = new_cache()

    def test_a_complete_folder_is_complete_for_every_pinned_model(self):
        for name in stt.MODEL_REVISIONS:
            folder = fakes.make_model_folder(self.cache, name)
            self.assertEqual(stt.model_folder_problems(folder, name), [],
                             name)

    def test_each_missing_file_is_named(self):
        for name in ("small", "large-v3-turbo"):
            for fname in stt.required_files(name):
                skip = (fname,)
                folder = fakes.make_model_folder(
                    self.cache, name, revision=("%040x" % abs(hash(fname))),
                    skip=skip)
                self.assertEqual(stt.model_folder_problems(folder, name),
                                 [fname], (name, fname))

    def test_the_turbo_model_needs_its_preprocessor_config_small_does_not(self):
        self.assertIn("preprocessor_config.json",
                      stt.required_files("large-v3-turbo"))
        self.assertNotIn("preprocessor_config.json",
                         stt.required_files("small"))

    def test_either_vocabulary_file_will_do(self):
        for vocab in stt.VOCABULARY_FILES:
            folder = fakes.make_model_folder(self.cache, "small",
                                             vocabulary=vocab,
                                             revision="a" * 40)
            self.assertEqual(stt.model_folder_problems(folder, "small"), [])

    def test_a_zero_byte_small_or_unreadable_model_bin_is_incomplete(self):
        for i, how in enumerate(("missing", "empty", "tiny", "directory")):
            folder = fakes.make_model_folder(self.cache, "small",
                                             revision=f"{i:040x}")
            break_model_bin(folder, how)
            self.assertEqual(stt.model_folder_problems(folder, "small"),
                             ["model.bin"], how)

    def test_a_model_bin_that_cannot_be_opened_is_incomplete(self):
        folder = fakes.make_model_folder(self.cache, "small")
        real_open = open

        def refuse(path, *a, **k):
            if os.fspath(path).endswith("model.bin"):
                raise PermissionError("denied")
            return real_open(path, *a, **k)
        with mock.patch.object(stt, "open", refuse, create=True):
            self.assertEqual(stt.model_folder_problems(folder, "small"),
                             ["model.bin"])

    def test_a_dangling_link_is_incomplete_and_a_working_link_is_followed(self):
        folder = fakes.make_model_folder(self.cache, "small")
        blob = self.cache / "blob"
        blob.write_bytes(b"x" * 2_000_000)
        link = folder / "model.bin"
        link.unlink()
        try:
            os.symlink(blob, link)
        except OSError:
            self.skipTest("symbolic links are not allowed here")
        self.assertEqual(stt.model_folder_problems(folder, "small"), [])
        blob.unlink()                      # now the link points nowhere
        self.assertEqual(stt.model_folder_problems(folder, "small"),
                         ["model.bin"])

    def test_an_unpinned_model_needs_only_model_bin_and_config(self):
        self.assertEqual(stt.required_files("someone/their-model"),
                         ("model.bin", "config.json"))
        folder = fakes.make_model_folder(self.cache, "their-model")
        self.assertEqual(stt.model_folder_problems(folder, "their-model"), [])


class Resolving(unittest.TestCase):
    def setUp(self):
        self.cache = new_cache()

    def test_a_complete_folder_loads_without_any_network_call(self):
        net = Net(self.cache, snapshot_ok=False, files_ok=False)
        folder = net.folder("small")
        with net.patched():
            got = stt.resolve_model("small")
        self.assertEqual(Path(got), folder)
        self.assertEqual([d[1] for d in net.downloads], [True])
        self.assertEqual(net.files, [])

    def test_a_partial_folder_is_repaired_and_loads(self):
        net = Net(self.cache, snapshot_ok=False, files_ok=True)
        folder = net.folder("small")
        break_model_bin(folder, "missing")
        with net.patched():
            got = stt.resolve_model("small")
        self.assertEqual(Path(got), folder)
        self.assertEqual(stt.model_folder_problems(folder, "small"), [])
        # Only the missing file was fetched, at the pinned commit.
        self.assertEqual(net.files, [("Systran/faster-whisper-small",
                                      "model.bin",
                                      stt.MODEL_REVISIONS["small"], False)])
        self.assertEqual([d[1] for d in net.downloads], [True, False])

    def test_a_broken_model_bin_is_fetched_again_by_force(self):
        net = Net(self.cache, snapshot_ok=False, files_ok=True)
        folder = net.folder("small")
        break_model_bin(folder, "empty")
        with net.patched():
            stt.resolve_model("small")
        self.assertEqual(net.files[0][1:], (
            "model.bin", stt.MODEL_REVISIONS["small"], True))
        self.assertEqual(stt.model_folder_problems(folder, "small"), [])

    def test_a_missing_vocabulary_is_fetched_as_json_then_txt(self):
        net = Net(self.cache, snapshot_ok=False, files_ok=True)
        folder = net.folder("small")
        for v in stt.VOCABULARY_FILES:
            (folder / v).unlink(missing_ok=True)
        absent = hf_errors.RemoteEntryNotFoundError.__new__(
            hf_errors.RemoteEntryNotFoundError)
        real = net.hf_hub_download

        def only_txt(repo, filename, **kw):
            if filename == "vocabulary.json":
                net.files.append((repo, filename, kw.get("revision"), False))
                raise absent
            return real(repo, filename, **kw)
        with net.patched(), mock.patch.object(huggingface_hub,
                                              "hf_hub_download", only_txt):
            stt.resolve_model("small")
        self.assertEqual([f[1] for f in net.files],
                         ["vocabulary.json", "vocabulary.txt"])
        self.assertEqual(stt.model_folder_problems(folder, "small"), [])

    def test_a_missing_model_is_downloaded_when_the_hub_answers(self):
        net = Net(self.cache, snapshot_ok=True)
        with net.patched():
            got = stt.resolve_model("large-v3-turbo")
        self.assertEqual(stt.model_folder_problems(got, "large-v3-turbo"), [])
        self.assertEqual([d[1] for d in net.downloads], [True, False])
        self.assertEqual(net.downloads[1][2],
                         stt.MODEL_REVISIONS["large-v3-turbo"])

    def test_a_local_folder_path_is_used_as_it_is(self):
        net = Net(self.cache)
        with net.patched():
            self.assertEqual(stt.resolve_model(str(self.cache)),
                             str(self.cache))
        self.assertEqual(net.downloads, [])

    def test_an_unpinned_name_is_resolved_unpinned(self):
        net = Net(self.cache, snapshot_ok=True)
        with net.patched():
            got = stt.resolve_model("someone/their-model")
        self.assertTrue(Path(got).is_dir())
        self.assertEqual([d[2] for d in net.downloads], [None, None])

    def test_other_local_errors_are_not_hidden_as_a_missing_model(self):
        net = Net(self.cache)
        with net.patched(), mock.patch.object(
                net, "download_model", side_effect=ValueError("bad name")):
            sys.modules["faster_whisper"].download_model = net.download_model
            with self.assertRaises(ValueError):
                stt.resolve_model("small")

    def test_progress_is_reported_only_during_a_download(self):
        net = Net(self.cache, snapshot_ok=True)
        seen = []
        real = net.download_model

        def spy(name, **kw):
            seen.append(stt.download_progress())
            return real(name, **kw)
        with net.patched():
            sys.modules["faster_whisper"].download_model = spy
            self.assertIsNone(stt.download_progress())
            stt.resolve_model("small")
        self.assertIsNone(stt.download_progress())
        # The first (local-only) call is not a download; the second is.
        self.assertIsNone(seen[0])
        self.assertEqual(seen[1]["name"], "small")
        self.assertEqual(seen[1]["size"], "about 0.5 GB")
        self.assertEqual(seen[1]["percent"], 0)

    def test_the_percentage_follows_the_size_of_the_cache_folder(self):
        blobs = self.cache / "models--Systran--faster-whisper-small" / "blobs"
        blobs.mkdir(parents=True)
        (blobs / "abc.incomplete").write_bytes(b"x" * 10)
        info = {"small": ("Systran/faster-whisper-small", 1000)}
        with mock.patch.dict(os.environ, {"HF_HUB_CACHE": str(self.cache)}), \
                mock.patch.dict(stt.MODEL_INFO, info), \
                mock.patch.object(stt, "_dl_active", "small"):
            self.assertEqual(stt.download_progress()["percent"], 1)
            (blobs / "big").write_bytes(b"x" * 490)
            self.assertEqual(stt.download_progress()["percent"], 50)
            (blobs / "huge").write_bytes(b"x" * 5000)
            self.assertEqual(stt.download_progress()["percent"], 99)


class WhenTheHubIsUnreachable(unittest.TestCase):
    def setUp(self):
        self.cache = new_cache()
        self.net = Net(self.cache, snapshot_ok=False, files_ok=False)
        self.folder = self.net.folder("small")
        break_model_bin(self.folder, "missing")

    def capture_logs(self):
        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        h = H(logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(h)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, h)
        self.addCleanup(root.setLevel, old)
        return records

    def test_one_plain_error_without_paths_or_the_account_name(self):
        logs = self.capture_logs()
        with self.net.patched():
            with self.assertRaises(stt.ModelDownloadError) as cm:
                stt.resolve_model("small")
        err = cm.exception
        self.assertEqual(str(err), "The speech model could not be "
                         "downloaded: no connection to huggingface.co.")
        self.assertEqual(err.kind, "connection")
        self.assertEqual(err.advice, "Check your internet connection.")
        self.assertIsNone(err.__cause__)
        self.assertTrue(err.__suppress_context__)   # no raw text in a traceback
        for text in [str(err), err.reason, err.advice] + logs:
            low = text.lower()
            self.assertNotIn(ACCOUNT, low, text)
            self.assertNotIn("\\", text)
            self.assertNotIn(".cache", low)
            self.assertNotIn(str(self.cache).lower(), low)
            user = os.environ.get("USERNAME", "").lower()
            if user:
                self.assertNotIn(user, low, text)
        # The technical detail IS in the log, scrubbed, with the folder
        # relative to the cache.
        joined = "\n".join(logs)
        self.assertIn("LocalEntryNotFoundError", joined)
        self.assertIn("<user path>", joined)
        self.assertIn("models--Systran--faster-whisper-small/snapshots/",
                      joined)

    def test_the_engine_raises_that_same_error_and_app_backs_off(self):
        a = bare(engine=None)
        cfg = dict(device="cpu", cpu_model="small", model="small",
                   language=None, vocabulary=[], dictionary=[])
        a.cfg.update(cfg)
        waits = []
        with self.net.patched(), \
                mock.patch.object(stt, "add_cuda_dll_dirs", return_value=[]):
            for expected in (5, 15, 60, 300, 300):
                a.load_engine(lambda: stt.Engine(a.cfg))
                a.loader_thread.join(10)
                drain(a)
                self.assertEqual(a.status, "failed")
                self.assertEqual(a.status_detail,
                                 "The speech model could not be downloaded: "
                                 "no connection to huggingface.co.")
                self.assertEqual(a.failure["kind"], "connection")
                waits.append(round(a.auto_retry_in()))
                self.assertEqual(waits[-1], expected)
                a._auto_retry_at = 0.0          # let the next one start
        self.assertEqual(waits, [5, 15, 60, 300, 300])
        self.assertEqual(self.net.cpu_loads, [])   # ctranslate2 never saw it

    def test_a_graphics_card_download_failure_falls_back_to_the_cached_cpu_model(self):
        net = Net(self.cache, snapshot_ok=False, files_ok=False)
        net.folder("small")                       # complete, in the cache
        cfg = dict(device="auto", model="large-v3-turbo", cpu_model="small",
                   language=None, vocabulary=[], dictionary=[],
                   compute_type="auto")
        modules = {"ctranslate2": types.SimpleNamespace(
            get_cuda_device_count=lambda: 1,
            get_supported_compute_types=lambda d: {"float16", "int8"})}
        with net.patched(), mock.patch.dict(sys.modules, modules), \
                mock.patch.object(stt, "add_cuda_dll_dirs", return_value=[]), \
                mock.patch.object(stt, "cuda_runtime_installed",
                                  return_value=True), \
                mock.patch.object(stt.Engine, "_warm_up_like_a_dictation"):
            engine = stt.Engine(cfg)
        self.assertEqual(engine.device, "cpu")
        self.assertIn("could not be downloaded", engine.fallback_reason)
        self.assertNotIn("large-v3-turbo", engine.fallback_reason)


class Classification(unittest.TestCase):
    def kind(self, exc, name="large-v3-turbo"):
        return stt.classify_download_error(exc, name)

    def test_the_common_causes(self):
        class Resp:
            def __init__(self, code):
                self.status_code = code

        def http(code):
            exc = OSError("boom")
            exc.response = Resp(code)
            return exc

        def named(cls_name, base=Exception):
            return type(cls_name, (base,), {})("raw text " + USERS_PATH)

        cases = [
            (named("ConnectError"), "connection", "no connection to "
             "huggingface.co"),
            (named("ConnectTimeout"), "timeout", "huggingface.co did not "
             "answer in time"),
            (named("ReadTimeout"), "timeout", None),
            (named("ProxyError"), "proxy", None),
            (named("SSLError"), "ssl", None),
            (named("RemoteProtocolError"), "interrupted", "the download "
             "was interrupted"),
            (hf_errors.LocalEntryNotFoundError("x"), "connection", None),
            (http(401), "http", "huggingface.co refused the request "
             "(error 401)"),
            (http(403), "http", None),
            (http(429), "http", "huggingface.co is asking for fewer "
             "requests (error 429)"),
            (http(503), "http", "huggingface.co has a problem (error 503)"),
            (OSError(28, "No space left on device"), "disk",
             "there is not enough free disk space (about 1.5 GB is needed)"),
            (PermissionError(13, "denied"), "permission", None),
            (OSError("Consistency check failed: file should be of size 5"),
             "interrupted", None),
            (RuntimeError("something odd " + USERS_PATH), "other",
             "the download failed (RuntimeError)"),
            (None, "interrupted", None),
        ]
        for exc, kind, reason in cases:
            err = self.kind(exc)
            self.assertEqual(err.kind, kind, repr(exc))
            if reason:
                self.assertEqual(err.reason, reason, repr(exc))
            self.assertNotIn(ACCOUNT, str(err))
            self.assertNotIn("\\", str(err))
            self.assertTrue(err.advice.endswith("."), err.advice)

    def test_a_windows_disk_full_code_and_a_chained_cause(self):
        e = OSError(0, "x")
        e.winerror = 112
        self.assertEqual(self.kind(e, "small").reason,
                         "there is not enough free disk space (about 0.5 GB "
                         "is needed)")
        try:
            try:
                raise ConnectError("refused")
            except ConnectError as inner:
                raise RuntimeError("wrapper") from inner
        except RuntimeError as outer:
            self.assertEqual(self.kind(outer).kind, "connection")

    def test_file_in_use_on_windows(self):
        e = OSError(0, "x")
        e.winerror = 32
        self.assertEqual(self.kind(e).kind, "permission")

    def test_scrub_removes_the_cache_and_any_users_folder(self):
        cache = new_cache()
        with mock.patch.dict(os.environ, {"HF_HUB_CACHE": str(cache)}):
            out = stt.scrub(f"open {cache}\\x and {USERS_PATH}\\y failed")
        self.assertNotIn(str(cache), out)
        self.assertNotIn(ACCOUNT, out)


class ScrubNames(unittest.TestCase):
    """An account name with a space, and the profile folder in either slash
    style, never reach the log. Names are built from pieces."""
    FIRST, SECOND = "Zed" + "q", "Quxv" + "x"
    USERS = "C:" + "\\" + "Us" + "ers" + "\\"

    def test_a_two_word_account_name_is_removed_whole(self):
        name = f"{self.FIRST} {self.SECOND}"
        for sep in ("\\", "/"):
            path = (self.USERS + name + "\\AppData\\Local\\x").replace(
                "\\", sep)
            out = stt.scrub(f"cannot open {path} now")
            self.assertNotIn(self.FIRST, out)
            self.assertNotIn(self.SECOND, out)
            self.assertIn("<user path>", out)

    def test_a_bare_profile_folder_ends_at_the_colon(self):
        out = stt.scrub(f"{self.USERS}{self.FIRST} {self.SECOND}: not found")
        self.assertNotIn(self.SECOND, out)
        self.assertTrue(out.endswith(": not found"), out)

    def test_the_profile_folder_is_replaced_in_both_slash_styles(self):
        home = ("D:" + "\\" + "profiles" + "\\"
                + f"{self.FIRST} {self.SECOND}")
        for sep in ("\\", "/"):
            for case in (str.lower, str.upper, str):
                spelled = case(home.replace("\\", sep))
                with mock.patch.dict(os.environ, {"USERPROFILE": home}), \
                        mock.patch("os.path.expanduser", return_value=home):
                    out = stt.scrub(f"x {spelled}{sep}cache y")
                self.assertNotIn(self.SECOND.lower(), out.lower())
                self.assertEqual(out, "x <user path>" + sep + "cache y")


class Backoff(unittest.TestCase):
    def failure(self):
        return {"kind": "connection", "reason": "no connection",
                "advice": "Check your internet connection."}

    def test_the_schedule_and_a_manual_retry_starting_it_over(self):
        self.assertEqual(app_mod.DOWNLOAD_RETRY_SECONDS,
                         (5.0, 15.0, 60.0, 300.0))
        a = bare(engine=None)
        a.status = "failed"
        a._factory = mock.Mock(side_effect=stt.ModelDownloadError(
            "connection", "no connection", "x"))
        got = []
        for _ in range(3):
            a._load_failed("d", self.failure())
            got.append(round(a.auto_retry_in()))
        self.assertEqual(got, [5, 15, 60])
        a.status = "failed"
        a._factory = lambda: types.SimpleNamespace(cfg=None)
        self.assertTrue(a.retry_engine())            # manual: at once
        a.loader_thread.join(5)
        drain(a)
        self.assertEqual(a.status, "ready")
        self.assertIsNone(a.failure)
        self.assertIsNone(a.auto_retry_in())
        a._load_failed("d", self.failure())
        self.assertEqual(round(a.auto_retry_in()), 5)

    def test_a_manual_try_again_resets_the_wait(self):
        a = bare(engine=None)
        for _ in range(4):
            a._load_failed("d", self.failure())
        self.assertEqual(round(a.auto_retry_in()), 300)
        a.status = "failed"
        a._factory = mock.Mock(side_effect=stt.ModelDownloadError(
            "connection", "no connection", "x"))
        with self.assertLogs("holdtotalk.app", "ERROR"):
            self.assertTrue(a.retry_engine())
            a.loader_thread.join(5)
        drain(a)
        self.assertEqual(round(a.auto_retry_in()), 5)

    def test_nothing_is_retried_before_it_is_due(self):
        a = bare(engine=None)
        a.status = "failed"
        a._factory = mock.Mock()
        a._load_failed("d", self.failure())
        for _ in range(3):
            a._maybe_auto_retry()
        a._factory.assert_not_called()

    def test_when_due_the_retry_starts_by_itself_and_can_succeed(self):
        a = bare(engine=None)
        a._factory = lambda: types.SimpleNamespace(cfg=None)
        a._load_failed("d", self.failure())
        a._auto_retry_at = 0.0
        a._maybe_auto_retry()
        a.loader_thread.join(5)
        drain(a)
        self.assertEqual(a.status, "ready")
        self.assertIsNone(a._auto_retry_at)
        self.assertEqual(a._download_failures, 0)

    def test_other_failures_do_not_retry_by_themselves(self):
        a = bare(engine=None)
        a._load_failed("model files are missing", None)
        self.assertIsNone(a.failure)
        self.assertIsNone(a.auto_retry_in())

    def test_a_plain_download_error_is_logged_without_a_traceback(self):
        a = bare(engine=None)

        def factory():
            raise stt.ModelDownloadError("connection", "no connection to "
                                         "huggingface.co", "x")
        with self.assertLogs("holdtotalk.app", "ERROR") as logs:
            a.load_engine(factory)
            a.loader_thread.join(5)
        drain(a)
        self.assertEqual(len(logs.records), 1)
        self.assertIsNone(logs.records[0].exc_info)
        self.assertEqual(a.failure["kind"], "connection")

    def test_the_window_gets_the_reason_and_the_progress(self):
        import api
        a = bare(engine=None)
        a.status = "failed"
        a._load_failed("The speech model could not be downloaded: no "
                       "connection to huggingface.co.", self.failure())
        server = api.Server.__new__(api.Server)
        server.app = a
        got = api.Server._load_failure(server)
        self.assertEqual(got["kind"], "connection")
        self.assertTrue(got["auto_retry"])
        self.assertIsNone(api.Server._download_state(server))
        a.status = "loading"
        a.progress_source = lambda: {"size": "about 1.5 GB", "percent": 42}
        self.assertEqual(api.Server._download_state(server),
                         {"size": "about 1.5 GB", "percent": 42})
        self.assertIsNone(api.Server._load_failure(server))


def installer_code():
    text = (ROOT / "install.ps1").read_text(encoding="ascii")
    m = re.search(r"\$ModelCode = @'\r?\n(.*?)\r?\n'@", text, re.S)
    assert m, "$ModelCode not found in install.ps1"
    return m.group(1)


def installer_module():
    """The installer's model.py with its functions available (it is only
    executed as a script when __name__ is __main__)."""
    ns = {"__name__": "htt_model_py"}
    exec(compile(installer_code(), "model.py", "exec"), ns)
    return ns


def run_installer(argv, net):
    """Run model.py the way the installer does ("check" or "get"), with the
    faked libraries; (exit code, stderr text)."""
    ns = {"__name__": "__main__"}
    err = io.StringIO()
    with net.patched(), mock.patch.object(sys, "argv", ["model.py"] + argv), \
            redirect_stderr(err):
        try:
            exec(compile(installer_code(), "model.py", "exec"), ns)
        except SystemExit as exc:
            return exc.code, err.getvalue()
    raise AssertionError("model.py did not exit")


class InstallerModelCheck(unittest.TestCase):
    def setUp(self):
        self.cache = new_cache()

    def test_the_installer_is_plain_ascii_python(self):
        code = installer_code()
        code.encode("ascii")
        compile(code, "model.py", "exec")
        self.assertNotIn("\n'@", code)

    def test_check_says_not_present_for_a_partial_folder(self):
        net = Net(self.cache)
        folder = net.folder("small")
        rev = stt.MODEL_REVISIONS["small"]
        self.assertEqual(run_installer(["small", "check", rev], net)[0], 0)
        break_model_bin(folder, "missing")
        self.assertEqual(run_installer(["small", "check", rev], net)[0], 3)
        net.folder("small")
        break_model_bin(folder, "empty")
        self.assertEqual(run_installer(["small", "check", rev], net)[0], 3)

    def test_check_says_not_present_when_the_model_is_not_there_at_all(self):
        net = Net(self.cache)
        self.assertEqual(run_installer(["small", "check",
                                        stt.MODEL_REVISIONS["small"]],
                                       net)[0], 3)

    def test_get_repairs_a_partial_folder(self):
        net = Net(self.cache, snapshot_ok=False, files_ok=True)
        folder = net.folder("small")
        break_model_bin(folder, "missing")
        code, err = run_installer(["small", "get",
                                   stt.MODEL_REVISIONS["small"]], net)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(stt.model_folder_problems(folder, "small"), [])
        self.assertEqual([f[1] for f in net.files], ["model.bin"])

    def test_get_fails_with_one_plain_line_when_it_cannot_finish(self):
        net = Net(self.cache, snapshot_ok=False, files_ok=False)
        folder = net.folder("small")
        break_model_bin(folder, "missing")
        code, err = run_installer(["small", "get",
                                   stt.MODEL_REVISIONS["small"]], net)
        self.assertEqual(code, 4)
        self.assertEqual(err, "The speech model could not be downloaded: "
                         "no connection to huggingface.co.\n")
        self.assertNotIn(ACCOUNT, err)

    def test_get_with_a_working_hub_downloads_everything(self):
        net = Net(self.cache, snapshot_ok=True)
        self.assertEqual(run_installer(
            ["large-v3-turbo", "get", stt.MODEL_REVISIONS["large-v3-turbo"]],
            net), (0, ""))

    def test_the_two_completeness_rules_agree(self):
        ns = installer_module()
        # The tables themselves.
        self.assertEqual(ns["MIN_BYTES"], stt.MODEL_MIN_BYTES)
        self.assertEqual(tuple(ns["CORE"]), stt.CORE_FILES)
        self.assertEqual(tuple(ns["EXTRA"]), stt.EXTRA_FILES)
        self.assertEqual(ns["EXTRA_BY_MODEL"], stt.EXTRA_BY_MODEL)
        self.assertEqual(tuple(ns["VOCAB"]), stt.VOCABULARY_FILES)
        # And what they decide, over broken folders of every kind.
        names = list(stt.MODEL_REVISIONS) + ["someone/their-model", "x"]
        n = 0
        for name in names:
            pinned = stt.model_revision(name) is not None
            files = stt.required_files(name) + ("preprocessor_config.json",)
            variants = [()]
            variants += [(f,) for f in files]
            variants += [("model.bin", "config.json"),
                         ("tokenizer.json", "vocabulary.*")]
            for skip in variants:
                for vocab in stt.VOCABULARY_FILES:
                    for bin_how in (None, "empty", "tiny", "directory"):
                        n += 1
                        folder = fakes.make_model_folder(
                            self.cache, name, revision="%040x" % n,
                            skip=skip, vocabulary=vocab)
                        if bin_how and (folder / "model.bin").exists():
                            break_model_bin(folder, bin_how)
                        self.assertEqual(
                            ns["folder_problems"](str(folder), name, pinned),
                            stt.model_folder_problems(folder, name),
                            (name, skip, vocab, bin_how))
            # A folder with no vocabulary file at all.
            folder = fakes.make_model_folder(
                self.cache, name, revision="%040x" % (n + 1))
            for v in stt.VOCABULARY_FILES:
                (folder / v).unlink(missing_ok=True)
            self.assertEqual(
                ns["folder_problems"](str(folder), name, pinned),
                stt.model_folder_problems(folder, name))
        # The installer says "pinned" when it was given a revision, stt when
        # the name is in MODEL_REVISIONS: the installer reads the revision out
        # of stt.py by name, so the two agree for every pinned model.
        text = (ROOT / "install.ps1").read_text(encoding="ascii")
        self.assertIn("MODEL_REVISIONS in stt.py", text)

    def test_both_sides_say_they_must_be_kept_in_step(self):
        self.assertIn("install.ps1", (ROOT / "stt.py").read_text(
            encoding="utf-8"))
        self.assertIn("same rule", installer_code().lower())
        self.assertIn("test_model_complete", installer_code())
        self.assertIn("test_model_complete", (ROOT / "stt.py").read_text(
            encoding="utf-8"))


@unittest.skipUnless(VENV_PY.is_file() and POWERSHELL.is_file(),
                     "needs the installed environment and Windows PowerShell")
class InstallerFactEndToEnd(unittest.TestCase):
    """install.ps1's own Test-ModelOnDisk (taken out of the script with the
    PowerShell parser), running the installer's real model.py with the
    environment's Python against a temporary cache, offline: the `present`
    fact of -Check is true only for a complete folder."""

    SCRIPT = r"""
param([string]$Install, [string]$Common, [string]$EnvPy, [string]$Dir, [string]$Name)
$ErrorActionPreference = 'Stop'
. $Common
$ast = [Management.Automation.Language.Parser]::ParseFile($Install, [ref]$null, [ref]$null)
foreach ($n in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and ($n.Name -eq 'Test-ModelOnDisk' -or $n.Name -eq 'Get-WorkDir') }, $true)) {
    . ([scriptblock]::Create($n.Extent.Text))
}
$assign = $ast.FindAll({ param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$ModelCode' }, $true)[0]
Invoke-Expression $assign.Extent.Text
$c = Test-ModelOnDisk $EnvPy $Dir $Name
Remove-Item -LiteralPath (Get-WorkDir) -Recurse -Force
$present = $null
if ($c.Code -eq 0) { $present = $true } elseif ($c.Code -eq 3) { $present = $false }
"code=$($c.Code) present=$present"
"""

    def run_case(self, cache):
        with tempfile.TemporaryDirectory(prefix="htt-m-") as d:
            script = Path(d) / "m.ps1"
            script.write_text(self.SCRIPT, encoding="ascii")
            env = dict(os.environ, HF_HUB_CACHE=str(cache),
                       HF_HUB_OFFLINE="1")
            env.pop("HF_HOME", None)
            out = subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script), str(ROOT / "install.ps1"),
                 str(ROOT / "htt-common.ps1"), str(VENV_PY), str(ROOT),
                 "small"], capture_output=True, text=True, timeout=240,
                env=env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out.stdout.strip().splitlines()[-1]

    def test_present_is_false_for_a_partial_folder_and_true_for_a_full_one(self):
        cache = new_cache()
        folder = fakes.make_model_folder(cache, "small")
        self.assertEqual(self.run_case(cache), "code=0 present=True")
        break_model_bin(folder, "missing")
        self.assertEqual(self.run_case(cache), "code=3 present=False")
        folder = fakes.make_model_folder(cache, "small")
        break_model_bin(folder, "empty")
        self.assertEqual(self.run_case(cache), "code=3 present=False")


if __name__ == "__main__":
    unittest.main()
