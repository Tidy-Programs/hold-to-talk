"""audiomute.py and its use in app.py: muting every other program's sound
while the hold-to-talk key is held, and giving it back.

Everything here runs against a fake audio backend (tests/fakes.py: FakeAudio).
No real audio session of this PC is ever read or changed by the suite."""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import audiomute
import settings
import sounds
from audiomute import Muter, ident_of
from fakes import FakeAudio, FakeRecorder
from test_flow import ALT, CTRL, WIN, Base, cfg_with, wait_for  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


def entry_keys(audio, names):
    return {(ident_of(n), audio.sessions[n]["pid"]) for n in names}


class MuterCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="htt-audio-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = self.dir / audiomute.STATE_NAME
        self.audio = FakeAudio(own={os.getpid(), 4001})
        self.audio.add("browser", 100)
        self.audio.add("player", 200)
        self.audio.add("system", 0, system=True)
        self.muters = []

    def muter(self, **kw):
        kw.setdefault("retry_pause", 0)
        m = Muter(backend=kw.pop("backend", self.audio),
                  state_path=kw.pop("state_path", self.state), **kw)
        self.muters.append(m)
        self.addCleanup(m.close, 2)
        return m

    def hold(self, m):
        self.assertTrue(m.mute())
        self.assertTrue(m.flush())

    def let_go(self, m, why="test"):
        m.restore(why)
        self.assertTrue(m.flush())


class MuteAndRestore(MuterCase):
    def test_mutes_everything_else_and_restores_exactly_that(self):
        m = self.muter()
        self.hold(m)
        for name in ("browser", "player", "system"):
            self.assertTrue(self.audio.is_muted(name), name)
        self.let_go(m)
        for name in ("browser", "player", "system"):
            self.assertFalse(self.audio.is_muted(name), name)

    def test_a_session_the_person_muted_stays_muted(self):
        self.audio.add("music", 300, muted=True)
        m = self.muter()
        self.hold(m)
        self.assertTrue(self.audio.is_muted("music"))
        self.assertNotIn(entry_keys(self.audio, ["music"]).pop(),
                         self.audio.set_calls[0][0])
        self.let_go(m)
        self.assertTrue(self.audio.is_muted("music"))     # still the person's
        self.assertFalse(self.audio.is_muted("browser"))

    def test_the_apps_own_process_and_children_are_never_muted(self):
        self.audio.add("chime", os.getpid())
        self.audio.add("window", 4001)                     # a child process
        m = self.muter()
        self.hold(m)
        self.assertFalse(self.audio.is_muted("chime"))
        self.assertFalse(self.audio.is_muted("window"))
        self.assertTrue(self.audio.is_muted("browser"))
        self.let_go(m)

    def test_a_session_that_vanished_is_skipped_quietly(self):
        m = self.muter()
        self.hold(m)
        self.audio.sessions["browser"]["alive"] = False    # the program exited
        with self.assertNoLogs("holdtotalk.audiomute", "WARNING"):
            self.let_go(m)
        self.assertFalse(self.audio.is_muted("player"))
        self.assertFalse(self.state.exists())

    def test_a_new_process_with_the_same_session_name_is_not_unmuted(self):
        # The key is (session id, process id): the same id under another pid
        # is not what we muted.
        m = self.muter()
        self.hold(m)
        self.audio.sessions["browser"]["pid"] = 999
        self.let_go(m)
        self.assertTrue(self.audio.is_muted("browser"))
        self.assertFalse(self.audio.is_muted("player"))

    def test_asking_twice_is_one_mute(self):
        m = self.muter()
        self.assertTrue(m.mute())
        self.assertFalse(m.mute())
        self.assertTrue(m.flush())
        self.assertEqual(len([c for c in self.audio.set_calls if c[1]]), 1)

    def test_restoring_when_nothing_is_muted_does_nothing(self):
        m = self.muter()
        self.assertFalse(m.restore())
        self.assertIsNone(m._thread)

    def test_quick_press_and_release_leaves_nothing_muted(self):
        m = self.muter()
        for _ in range(20):
            m.mute()
            m.restore("quick")
        self.assertTrue(m.flush())
        for name in ("browser", "player", "system"):
            self.assertFalse(self.audio.is_muted(name))
        self.assertFalse(self.state.exists())

    def test_nothing_to_mute_writes_nothing(self):
        for s in self.audio.sessions.values():
            s["muted"] = True
        m = self.muter()
        self.hold(m)
        self.assertFalse(self.state.exists())
        self.let_go(m)
        self.assertTrue(all(s["muted"] for s in self.audio.sessions.values()))

    def test_close_restores_everything(self):
        m = self.muter()
        self.hold(m)
        self.assertTrue(m.close(2))
        self.assertFalse(self.audio.is_muted("browser"))
        self.assertFalse(self.state.exists())
        self.assertEqual((self.audio.begun, self.audio.ended), (1, 1))
        self.assertFalse(m.mute())                         # closed for good


class OneWorkerThread(MuterCase):
    def test_every_audio_call_runs_on_one_thread_that_is_not_the_callers(self):
        m = self.muter()
        self.hold(m)
        self.let_go(m)
        m.close(2)
        self.assertEqual(len(self.audio.threads), 1)
        self.assertNotIn(threading.get_ident(), self.audio.threads)
        begin = [t for n, t in self.audio.calls if n == "begin"]
        end = [t for n, t in self.audio.calls if n == "end"]
        self.assertEqual(begin, end)                       # COM in and out
        self.assertEqual(set(begin), self.audio.threads)   # on that thread

    def test_the_callers_cost_is_a_queue_put_even_with_a_slow_backend(self):
        slow = FakeAudio(own={os.getpid()}, delay=0.15)
        slow.add("browser", 100)
        m = self.muter(backend=slow)
        started = time.perf_counter()
        for _ in range(50):
            m.mute()
            m.restore("timing")
        per_pair = (time.perf_counter() - started) / 50
        self.assertLess(per_pair, 0.005, f"{per_pair * 1000:.3f} ms")
        m.close(5)

    def test_importing_never_starts_anything(self):
        self.assertIsNone(self.muter()._thread)


class StateFile(MuterCase):
    def test_written_while_muted_removed_after_and_holds_no_names(self):
        self.audio.add("secret-app-name", 300, start=777)
        m = self.muter()
        self.hold(m)
        data = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"version", "muted"})
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["muted"]), 4)
        for row in data["muted"]:
            self.assertEqual(set(row), {"id", "pid", "started"})
            self.assertRegex(row["id"], r"^[0-9a-f]{32}$")
        ids = {(r["id"], r["pid"]): r["started"] for r in data["muted"]}
        self.assertEqual(ids[(ident_of("secret-app-name"), 300)], 777)
        self.assertNotIn("secret-app-name", self.state.read_text("utf-8"))
        self.let_go(m)
        self.assertFalse(self.state.exists())
        self.assertEqual([p.name for p in self.dir.iterdir()], [])

    def test_cannot_write_the_file_means_nothing_is_muted(self):
        blocked = self.dir / "in-the-way"
        blocked.mkdir()
        m = self.muter(state_path=blocked)                  # a folder, not a file
        with self.assertLogs("holdtotalk.audiomute", "WARNING"):
            self.hold(m)
        self.assertFalse(any(s["muted"] for s in self.audio.sessions.values()))

    def test_recovery_unmutes_only_the_sessions_that_still_match(self):
        first = self.muter()
        self.hold(first)
        # The app died here: no restore. After a restart one program exited,
        # one is a new process with the old session id, one is as it was.
        self.audio.sessions["browser"]["alive"] = False
        self.audio.starts[200] += 5                          # a new process
        self.audio.add("fresh", 500, muted=True, start=1)    # not ours
        second = self.muter()
        self.assertTrue(second.recover())
        self.assertTrue(second.flush())
        self.assertTrue(self.audio.is_muted("player"))       # start differs
        self.assertFalse(self.audio.is_muted("system"))      # still matches
        self.assertTrue(self.audio.is_muted("fresh"))        # never listed
        self.assertFalse(self.state.exists())

    def test_recovery_with_a_matching_session_unmutes_it(self):
        first = self.muter()
        self.hold(first)
        second = self.muter()
        self.assertTrue(second.recover())
        self.assertTrue(second.flush())
        for name in ("browser", "player", "system"):
            self.assertFalse(self.audio.is_muted(name), name)
        self.assertFalse(self.state.exists())

    def test_no_state_file_means_no_worker_and_no_audio_call(self):
        m = self.muter()
        self.assertFalse(m.recover())
        self.assertIsNone(m._thread)
        self.assertEqual(self.audio.calls, [])

    def test_an_unreadable_state_file_is_dropped(self):
        self.state.write_text("{not json", encoding="utf-8")
        m = self.muter()
        self.assertTrue(m.recover())
        self.assertTrue(m.flush())
        self.assertFalse(self.state.exists())

    def test_a_state_file_with_odd_rows_is_read_safely(self):
        self.state.write_text(json.dumps({"version": 1, "muted": [
            {"id": 5, "pid": "x"}, "text", {"id": "ab", "pid": True},
            {"id": ident_of("browser"), "pid": 100, "started": "no"}]}),
            encoding="utf-8")
        m = self.muter()
        self.assertTrue(m.recover())
        self.assertTrue(m.flush())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.audio.set_calls, [])

    def test_a_failed_recovery_keeps_the_file_for_the_next_start(self):
        first = self.muter()
        self.hold(first)
        self.audio.fail = 1
        second = self.muter()
        with self.assertLogs("holdtotalk.audiomute", "WARNING"):
            second.recover()
            self.assertTrue(second.flush())
        self.assertTrue(self.state.exists())
        third = self.muter()
        third.recover()
        self.assertTrue(third.flush())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.audio.is_muted("browser"))


class SafetyTimer(MuterCase):
    def test_restores_after_the_longest_hold(self):
        m = self.muter(max_hold=0.25)
        self.hold(m)
        self.assertTrue(self.audio.is_muted("browser"))
        self.assertTrue(wait_for(lambda: not self.audio.is_muted("browser"), 3))
        self.assertTrue(m.flush())
        self.assertFalse(self.state.exists())
        self.assertFalse(m.active)

    def test_the_default_is_five_minutes(self):
        self.assertEqual(audiomute.MAX_HOLD_SECONDS, 300.0)
        self.assertEqual(self.muter().max_hold, 300.0)

    def test_a_later_press_works_after_the_timer_fired(self):
        m = self.muter(max_hold=0.2)
        self.hold(m)
        self.assertTrue(wait_for(lambda: not self.audio.is_muted("browser"), 3))
        self.assertTrue(m.flush())
        self.hold(m)
        self.assertTrue(self.audio.is_muted("browser"))
        self.let_go(m)


class QuietFailure(MuterCase):
    def test_a_failing_backend_never_raises_and_mutes_nothing(self):
        self.audio.fail = 100
        m = self.muter()
        with self.assertLogs("holdtotalk.audiomute", "WARNING") as logs:
            self.assertTrue(m.mute())
            self.assertTrue(m.flush())
            m.restore("x")
            self.assertTrue(m.flush())
        self.assertEqual(len(logs.output), 1)               # one line
        self.assertIn("AudioError", logs.output[0])
        self.assertFalse(any(s["muted"] for s in self.audio.sessions.values()))
        self.assertFalse(self.state.exists())

    def test_a_backend_that_cannot_start_is_quiet(self):
        class Broken(FakeAudio):
            def begin(self):
                raise OSError("no audio service")
        m = self.muter(backend=Broken())
        with self.assertLogs("holdtotalk.audiomute", "WARNING"):
            m.mute()
            self.assertTrue(m.flush())
        m.restore()
        self.assertTrue(m.flush())

    def test_a_failure_halfway_through_muting_is_undone(self):
        real = self.audio.set_mute
        calls = []

        def flaky(keys, muted):
            calls.append(muted)
            if muted and len(calls) == 1:
                real(keys[:1], True)                        # one got muted ...
                raise audiomute.AudioError("0x80004005")    # ... then it broke
            return real(keys, muted)
        self.audio.set_mute = flaky
        m = self.muter()
        with self.assertLogs("holdtotalk.audiomute", "WARNING"):
            self.hold(m)
        self.assertFalse(any(s["muted"] for s in self.audio.sessions.values()))
        self.assertFalse(self.state.exists())

    def test_a_restore_that_fails_is_retried_and_the_file_kept(self):
        m = self.muter(retry_seconds=0.1)
        self.hold(m)
        self.audio.fail = 3                                 # all three tries
        with self.assertLogs("holdtotalk.audiomute", "WARNING"):
            m.restore("test")
            self.assertTrue(m.flush())
        self.assertTrue(self.state.exists())                # still our way back
        self.assertTrue(wait_for(lambda: not self.audio.is_muted("browser"), 3))
        self.assertTrue(wait_for(lambda: not self.state.exists(), 3))

    def test_a_refusing_session_is_skipped(self):
        self.audio.sessions["player"]["refuse"] = True      # e.g. elevated
        m = self.muter()
        self.hold(m)
        self.assertTrue(self.audio.is_muted("browser"))
        self.assertFalse(self.audio.is_muted("player"))
        rows = json.loads(self.state.read_text("utf-8"))["muted"]
        self.assertNotIn(200, [r["pid"] for r in rows])     # not promised
        self.let_go(m)
        self.assertFalse(self.audio.is_muted("browser"))

    def test_the_real_backend_imports_and_fails_quietly_off_windows_paths(self):
        # No Windows call is made by importing, and an unusable backend is a
        # log line, not an exception.
        with mock.patch.object(audiomute.sys, "platform", "linux"):
            audiomute._dlls.clear()
            try:
                with self.assertRaises(audiomute.AudioError):
                    audiomute.WasapiBackend().begin()
            finally:
                audiomute._dlls.clear()


class RefusedUnmute(MuterCase):
    """A session that is found but refuses SetMute(0) is retried, with its
    entry kept in the state file, and is not forgotten."""

    def rows(self):
        return json.loads(self.state.read_text("utf-8"))["muted"]

    def test_refuses_once_then_succeeds(self):
        m = self.muter(retry_seconds=0.1)
        self.hold(m)
        self.audio.sessions["player"]["refuse"] = 1           # refuses once
        m.restore("test")
        self.assertTrue(m.flush())
        self.assertFalse(self.audio.is_muted("browser"))      # others done
        self.assertTrue(self.audio.is_muted("player"))
        if self.state.exists():                               # not yet retried
            self.assertEqual([r["pid"] for r in self.rows()], [200])
        self.assertTrue(wait_for(lambda: not self.audio.is_muted("player"), 3))
        self.assertTrue(wait_for(lambda: not self.state.exists(), 3))

    def test_the_file_names_exactly_the_sessions_still_muted(self):
        m = self.muter(retry_seconds=30)                       # no retry yet
        self.hold(m)
        self.audio.sessions["player"]["refuse"] = True
        with self.assertLogs("holdtotalk.audiomute", "WARNING"):
            m.restore("test")
            self.assertTrue(m.flush())
        self.assertEqual([r["pid"] for r in self.rows()], [200])

    def test_refuses_always_is_dropped_after_the_give_up_time(self):
        m = self.muter(retry_seconds=0.05, give_up=0.3)
        self.hold(m)
        self.audio.sessions["player"]["refuse"] = True
        with self.assertLogs("holdtotalk.audiomute", "WARNING") as logs:
            m.restore("test")
            self.assertTrue(wait_for(lambda: not self.state.exists(), 5))
        self.assertTrue(any("giving up" in line for line in logs.output))
        self.assertTrue(self.audio.is_muted("player"))
        self.assertFalse(self.audio.is_muted("browser"))
        self.assertTrue(m.flush())
        seen = len(self.audio.set_calls)
        time.sleep(0.3)
        self.assertEqual(len(self.audio.set_calls), seen)      # no more tries
        self.assertFalse(m._dirty)

    def test_a_vanished_session_is_not_a_failure(self):
        m = self.muter(retry_seconds=0.05)
        self.hold(m)
        self.audio.sessions["player"]["alive"] = False
        m.restore("test")
        self.assertTrue(m.flush())
        self.assertFalse(self.state.exists())


class WorkerSurvival(MuterCase):
    def test_a_failure_in_the_safety_timer_does_not_kill_the_worker(self):
        m = self.muter(max_hold=0.1)
        self.hold(m)
        real = m._do_restore
        calls = []

        def flaky(why):
            calls.append(why)
            if len(calls) == 1:
                raise RuntimeError("unexpected")
            return real(why)
        with mock.patch.object(m, "_do_restore", flaky):
            with self.assertLogs("holdtotalk.audiomute", "WARNING"):
                self.assertTrue(wait_for(
                    lambda: not self.audio.is_muted("browser"), 3))
        self.assertTrue(m._thread.is_alive())
        self.assertFalse(self.state.exists())

    def test_a_dead_worker_is_replaced_by_the_public_methods(self):
        m = self.muter()
        self.hold(m)
        m._q.put(("stop", threading.Event()))                  # worker ends
        self.assertTrue(wait_for(lambda: not m._thread.is_alive(), 3))
        old = m._thread
        self.assertTrue(m.restore("test"))                     # new worker
        self.assertTrue(m.flush())
        self.assertIsNot(m._thread, old)
        self.assertFalse(self.audio.is_muted("browser"))
        old = m._thread
        m._q.put(("stop", threading.Event()))
        self.assertTrue(wait_for(lambda: not old.is_alive(), 3))
        self.assertTrue(m.mute())
        self.assertTrue(m.flush())
        self.assertTrue(self.audio.is_muted("browser"))


class StateFileKept(MuterCase):
    def test_a_later_mute_keeps_the_rows_of_a_failed_recovery(self):
        first = self.muter()
        self.hold(first)                                       # then it "dies"
        self.audio.add("newcomer", 600)
        self.audio.fail = 2                  # recover at start, and again
        second = self.muter()
        with self.assertLogs("holdtotalk.audiomute", "WARNING"):
            second.recover()
            self.assertTrue(second.flush())
            self.assertTrue(self.state.exists())
            self.hold(second)
        rows = json.loads(self.state.read_text("utf-8"))["muted"]
        self.assertEqual(sorted(r["pid"] for r in rows), [0, 100, 200, 600])
        self.let_go(second)
        for name in ("browser", "player", "system", "newcomer"):
            self.assertFalse(self.audio.is_muted(name), name)
        self.assertFalse(self.state.exists())

    def test_a_later_mute_tries_the_recovery_again_first(self):
        first = self.muter()
        self.hold(first)
        self.audio.fail = 1
        second = self.muter()
        with self.assertLogs("holdtotalk.audiomute", "WARNING"):
            second.recover()
            self.assertTrue(second.flush())
        self.hold(second)
        self.assertEqual(
            set(), {n for n in ("browser", "player", "system")
                    if not self.audio.is_muted(n)})      # still muted: held
        self.let_go(second)
        self.assertFalse(self.state.exists())


class StaleStateFile(MuterCase):
    def age(self, hours):
        t = time.time() - hours * 3600
        os.utime(self.state, (t, t))

    def test_a_file_older_than_a_day_is_dropped_without_unmuting(self):
        first = self.muter()
        self.hold(first)
        self.age(25)
        second = self.muter()
        with self.assertLogs("holdtotalk.audiomute", "INFO") as logs:
            self.assertTrue(second.recover())
            self.assertTrue(second.flush())
        self.assertFalse(self.state.exists())
        for name in ("browser", "player", "system"):
            self.assertTrue(self.audio.is_muted(name), name)   # left alone
        self.assertTrue(any("old list" in line for line in logs.output))

    def test_a_file_younger_than_a_day_is_acted_on(self):
        first = self.muter()
        self.hold(first)
        self.age(23)
        second = self.muter()
        self.assertTrue(second.recover())
        self.assertTrue(second.flush())
        self.assertFalse(self.state.exists())
        for name in ("browser", "player", "system"):
            self.assertFalse(self.audio.is_muted(name), name)

    def test_the_limit_is_24_hours(self):
        self.assertEqual(audiomute.MAX_STATE_AGE, 24 * 3600.0)


class Settings(unittest.TestCase):
    def test_off_by_default_and_a_bool(self):
        self.assertIs(sounds.DEFAULTS["mute_others"], False)
        self.assertIs(settings.DEFAULTS["sounds"]["mute_others"], False)
        self.assertIs(sounds.settings_of({})["mute_others"], False)
        self.assertIs(sounds.settings_of(
            {"sounds": {"mute_others": "yes"}})["mute_others"], False)
        self.assertIs(sounds.settings_of(
            {"sounds": {"mute_others": True}})["mute_others"], True)

    def test_validation_keeps_a_bool_and_replaces_anything_else(self):
        clean, problems = settings.validate(
            {"sounds": {"mute_others": True}})
        self.assertIs(clean["sounds"]["mute_others"], True)
        clean, problems = settings.validate(
            {"sounds": {"mute_others": "yes"}})
        self.assertIs(clean["sounds"]["mute_others"], False)   # the default
        self.assertTrue(any("mute_others" in p for p in problems))

    def test_validate_change_accepts_only_a_bool(self):
        v = settings.validate_change
        self.assertEqual(v("sounds", {"mute_others": True}),
                         {"sounds": {"mute_others": True}})
        for bad in ({"mute_others": "on"}, {"mute_others": 1},
                    {"mute_others": None}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    v("sounds", bad)

    def test_the_documents_name_the_setting_and_the_file(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("sounds.mute_others", readme)
        self.assertIn("Mute other sound while dictating", readme)
        self.assertIn("volume mixer", readme)
        self.assertIn(audiomute.STATE_NAME, readme)
        self.assertIn(audiomute.STATE_NAME,
                      (ROOT / "htt-common.ps1").read_text(encoding="utf-8")
                      .replace("\\", ""))
        self.assertIn(audiomute.STATE_NAME,
                      (ROOT / ".gitignore").read_text(encoding="utf-8"))
        ui = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Mute other sound while dictating", ui)

    def test_a_listed_module_with_no_new_package(self):
        listed = (ROOT / "install-files.txt").read_text(encoding="utf-8")
        self.assertIn("audiomute.py", listed.split())
        for name in ("requirements.txt", "requirements.lock.txt",
                     "requirements-cuda.txt", "requirements-cuda.lock.txt"):
            text = (ROOT / name).read_text(encoding="utf-8").lower()
            for word in ("pycaw", "comtypes", "pywin32"):
                self.assertNotIn(word, text, f"{name} has {word}")


class InTheApp(Base):
    """The App's use of the Muter: which events restore, live settings, and a
    dictation that is never broken by it."""

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(prefix="htt-audio-app-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = self.dir / audiomute.STATE_NAME
        self.audio = FakeAudio(own={os.getpid()})
        self.audio.add("browser", 100)
        self.audio.add("player", 200)
        self.turn(True)
        self.muter = Muter(backend=self.audio, state_path=self.state,
                           retry_pause=0, max_hold=60)
        self.app._muter = self.muter
        self.addCleanup(self.muter.close, 2)

    def turn(self, on):
        self.app.cfg["sounds"] = dict(self.app.cfg["sounds"], mute_others=on)

    def muted(self):
        self.assertTrue(self.muter.flush())
        return {n for n, s in self.audio.sessions.items() if s["muted"]}

    def assertSilentNowIsOver(self):
        self.assertEqual(self.muted(), set())
        self.assertFalse(self.state.exists())

    def test_key_down_mutes_and_key_up_restores(self):
        self.press(CTRL, WIN)
        self.assertEqual(self.app._state, "recording")
        self.assertEqual(self.muted(), {"browser", "player"})
        self.assertTrue(self.state.exists())
        self.release()
        self.assertSilentNowIsOver()

    def test_off_by_default_nothing_is_touched(self):
        self.turn(False)
        self.press(CTRL, WIN)
        self.assertEqual(self.muted(), set())
        self.assertEqual(self.audio.calls, [])
        self.release()
        self.assertIs(settings.DEFAULTS["sounds"]["mute_others"], False)

    def test_dry_run_never_mutes(self):
        self.app.dry_run = True
        self.press(CTRL, WIN)
        self.assertEqual(self.muted(), set())
        self.release()

    def test_restore_on_cancel_by_another_key(self):
        self.press(CTRL, WIN)
        self.assertEqual(self.muted(), {"browser", "player"})
        self.press(ord("C"))                                # a shortcut
        self.assertEqual(self.app._state, "idle")
        self.assertSilentNowIsOver()

    def test_restore_on_pause_while_recording(self):
        self.press(CTRL, WIN)
        self.assertEqual(self.muted(), {"browser", "player"})
        self.app.set_paused(True)
        self.assertSilentNowIsOver()

    def test_restore_on_the_pause_shortcut_path_and_the_tick(self):
        self.press(CTRL, WIN)
        self.app.paused = True                               # no set_paused
        self.step()                                          # _poll discards
        self.assertSilentNowIsOver()

    def test_restore_after_an_error_in_the_dictation(self):
        self.muter.mute()                                    # still muted
        self.assertEqual(self.muted(), {"browser", "player"})
        with mock.patch.object(self.app, "_process",
                               side_effect=RuntimeError("boom")):
            self.app.submit_transcript("some words here")
            self.assertTrue(wait_for(lambda: self.app._pending == 0))
        self.assertSilentNowIsOver()
        self.step()                                          # delivers notices
        self.assertIn("error", [k for k, _ in self.notices])

    def test_an_error_does_not_unmute_the_next_recording(self):
        self.press(CTRL, WIN)                                # holding again
        with mock.patch.object(self.app, "_process",
                               side_effect=RuntimeError("boom")):
            self.app.submit_transcript("some words here")
            self.assertTrue(wait_for(lambda: self.app._pending == 0))
        self.assertEqual(self.muted(), {"browser", "player"})
        self.release()
        self.assertSilentNowIsOver()

    def test_restore_on_shutdown(self):
        self.press(CTRL, WIN)
        self.assertEqual(self.muted(), {"browser", "player"})
        self.app.recorder.close = lambda: None
        self.app.overlay = mock.Mock()
        self.app.root = mock.Mock()
        self.app._shutdown()
        self.assertEqual({n for n, s in self.audio.sessions.items()
                          if s["muted"]}, set())
        self.assertFalse(self.state.exists())

    def test_turning_it_off_while_muted_restores_at_once(self):
        self.press(CTRL, WIN)
        self.assertEqual(self.muted(), {"browser", "player"})
        new = dict(self.app.cfg)
        new["sounds"] = dict(new["sounds"], mute_others=False)
        with mock.patch.object(settings, "load_config", return_value=new):
            self.app._reload()
        self.assertSilentNowIsOver()
        self.step()
        self.assertEqual(self.app._state, "recording")      # dictation goes on

    def test_turning_it_on_while_the_key_is_held_applies_now(self):
        self.turn(False)
        self.press(CTRL, WIN)
        self.assertEqual(self.muted(), set())
        new = dict(self.app.cfg)
        new["sounds"] = dict(new["sounds"], mute_others=True)
        with mock.patch.object(settings, "load_config", return_value=new):
            self.app._reload()
        self.assertEqual(self.muted(), {"browser", "player"})
        self.release()
        self.assertSilentNowIsOver()

    def test_a_reload_that_does_not_touch_it_changes_nothing(self):
        self.press(CTRL, WIN)
        self.assertEqual(self.muted(), {"browser", "player"})
        with mock.patch.object(settings, "load_config",
                               return_value=dict(self.app.cfg)):
            self.app._reload()
        self.assertEqual(self.muted(), {"browser", "player"})
        self.release()

    def test_a_failing_backend_never_breaks_a_dictation(self):
        self.audio.fail = 1000
        with self.assertLogs("holdtotalk.audiomute", "WARNING"):
            self.press(CTRL, WIN)
            self.assertEqual(self.app._state, "recording")
            self.assertTrue(self.muter.flush())
        self.release()
        self.assertTrue(wait_for(lambda: len(self.pastes) == 1))
        self.assertEqual(self.notices, [])

    def test_a_muter_that_cannot_be_made_never_breaks_a_dictation(self):
        self.app._muter = None
        with mock.patch.object(audiomute, "Muter",
                               side_effect=RuntimeError("x")):
            with self.assertLogs("holdtotalk.app", "WARNING"):
                self.press(CTRL, WIN)
            self.assertEqual(self.app._state, "recording")
            self.release()
        self.assertTrue(wait_for(lambda: len(self.pastes) == 1))

    def test_the_recorder_starts_before_and_regardless_of_the_mute(self):
        self.press(CTRL, WIN)
        self.assertEqual(self.app.recorder.starts, 1)
        self.release()

    def test_recovery_runs_at_start_when_a_file_is_left(self):
        crashed = Muter(backend=self.audio, state_path=self.state,
                        retry_pause=0)
        self.addCleanup(crashed.close, 2)
        crashed.mute()
        self.assertTrue(crashed.flush())
        self.assertEqual(self.muted(), {"browser", "player"})
        self.app._audio_recover()                           # the next start
        self.assertSilentNowIsOver()

    def test_start_with_the_feature_on_costs_the_caller_almost_nothing(self):
        slow = FakeAudio(own={os.getpid()}, delay=0.1)
        slow.add("browser", 100)
        self.muter.close(2)
        self.muter = Muter(backend=slow, state_path=self.state,
                           retry_pause=0)
        self.app._muter = self.muter
        self.addCleanup(self.muter.close, 3)
        spent = []
        for _ in range(30):
            self.down = {CTRL, WIN}
            started = time.perf_counter()
            self.app._start()
            spent.append(time.perf_counter() - started)
            self.app._discard("timing")
            self.down = set()
        spent.sort()
        median = spent[len(spent) // 2]
        print(f"\n  App._start with muting on: median "
              f"{median * 1000:.3f} ms, worst {spent[-1] * 1000:.3f} ms "
              f"(backend calls take 100 ms each)")
        self.assertLess(median, 0.01)


if __name__ == "__main__":
    unittest.main()
