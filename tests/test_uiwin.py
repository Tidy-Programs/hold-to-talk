"""Which window is the app's (uiwin.AppWindow), against a simulated set of
processes and windows: no browser is started and no real window is
touched."""

import atexit
import os
import shutil
import tempfile

# Never touch real user data: the data folder is a temporary one (shared
# with test_00_home.py) before any project module is imported.
if "htt-test-home-" not in os.environ.get("HOLDTOTALK_HOME", ""):
    _home = tempfile.mkdtemp(prefix="htt-test-home-")
    os.environ["HOLDTOTALK_HOME"] = _home
    atexit.register(shutil.rmtree, _home, True)

import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import settings
import uiwin

EXE = r"C:\Fake\Browser\msedge.exe"


class World:
    """Processes and top-level windows as uiwin sees them."""

    def __init__(self):
        self.procs = {}             # pid -> (image, created)
        self.windows = []           # (hwnd, pid, title)
        self.closed, self.ended, self.focused = [], [], []
        self.next_pid, self.next_hwnd = 1000, 5000
        self.parents = {}           # pid -> parent pid
        self.no_window_yet = False  # popen() starts browsers with no window

    def add(self, image, title=settings.APP_NAME, pid=None, created=None,
            window=True, parent=None):
        pid = pid or self.next_pid
        self.next_pid += 1
        self.procs[pid] = (image, created or pid * 7)
        if parent is not None:
            self.parents[pid] = parent
        self.next_hwnd += 1
        if window:
            self.windows.append((self.next_hwnd, pid, title))
        return pid, self.next_hwnd

    def end(self, pid, expected, timeout):
        if uiwin._same(self.procs.get(pid), expected):
            self.ended.append(pid)
            del self.procs[pid]
            self.windows = [w for w in self.windows if w[1] != pid]
        return True

    def popen(self, cmd, **kw):
        world = self
        pid, _ = self.add(cmd[0], window=not self.no_window_yet)

        class Proc:
            def __init__(self):
                self.pid = pid

            def poll(self):
                return None if self.pid in world.procs else 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                world.procs.pop(self.pid, None)
        return Proc()


class AppWindowTest(unittest.TestCase):
    def setUp(self):
        self.world = w = World()
        self.dir = Path(tempfile.mkdtemp(prefix="htt-win-"))
        self.saved = {n: getattr(uiwin, n) for n in (
            "_top_windows", "_process_info", "_end_process", "_post_close",
            "_popen", "focus", "browser_candidates", "_window_rect",
            "_parent_table")}
        uiwin._parent_table = lambda: dict(w.parents)
        uiwin._top_windows = lambda: list(w.windows)
        uiwin._process_info = lambda pid: w.procs.get(pid)
        uiwin._end_process = w.end
        uiwin._post_close = w.closed.append
        uiwin._popen = w.popen
        uiwin.focus = w.focused.append
        uiwin.browser_candidates = lambda: iter([EXE])
        uiwin._window_rect = lambda: (800, 600, 0, 0)

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(uiwin, name, value)
        shutil.rmtree(self.dir, ignore_errors=True)

    def window(self):
        return uiwin.AppWindow(lambda: "http://127.0.0.1:1/#t=x", self.dir)

    def test_a_look_alike_is_never_focused_or_closed(self):
        # S3: any program could show a window with the app's title (even a
        # browser at the same path), and "Open" focused it; quit sent it
        # WM_CLOSE.
        other_pid, other_hwnd = self.world.add(r"C:\Elsewhere\msedge.exe")
        same_pid, same_hwnd = self.world.add(EXE)       # the user's own Edge
        win = self.window()
        self.assertEqual(win.windows(), [])
        self.assertEqual(win.open(), "launched")
        self.assertEqual(self.world.focused, [])
        win.close()
        self.assertNotIn(other_hwnd, self.world.closed)
        self.assertNotIn(same_hwnd, self.world.closed)
        self.assertNotIn(other_pid, self.world.ended)
        self.assertNotIn(same_pid, self.world.ended)
        self.assertEqual(len(self.world.ended), 1)       # only our browser

    def test_without_edge_or_chrome_the_fallback_is_logged_and_the_window_says_so(self):
        uiwin.browser_candidates = lambda: iter([])
        opened = []
        win = self.window()
        with mock.patch.object(uiwin.webbrowser, "open",
                               side_effect=lambda u: opened.append(u) or True), \
                self.assertLogs("holdtotalk.uiwin", "INFO") as lg:
            self.assertEqual(win.open(), "browser")
        self.assertEqual(len(opened), 1)
        self.assertEqual(win.mode, "browser")          # api.py reports this
        said = [m for m in lg.output if "default browser" in m]
        self.assertEqual(len(said), 1, lg.output)
        self.assertIn("extensions and settings", said[0])
        js = (Path(uiwin.__file__).parent / "ui" / "app.js").read_text(
            encoding="utf-8")
        html = (Path(uiwin.__file__).parent / "ui" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn("S.state?.window !== 'browser'", js)
        self.assertIn('id="browser-note"', html)
        self.assertIn("ordinary browser", html)
        self.assertIn("extensions and settings apply", html)

    def test_second_open_focuses_our_window(self):
        win = self.window()
        self.assertEqual(win.open(), "launched")
        [(hwnd, pid)] = win.windows()
        self.assertEqual(win.open(), "focused")
        self.assertEqual(self.world.focused, [hwnd])

    def test_window_left_by_an_earlier_run_is_replaced(self):
        # W2: after a restart the old window (connected to nothing) was
        # focused by Open and by starting the app again.
        first = self.window()
        first.open()
        [(old_hwnd, old_pid)] = first.windows()
        later = self.window()                       # the next run
        self.assertEqual(later.windows(), [])       # not this run's window
        self.assertEqual(later.open(), "replaced")
        self.assertIn(old_hwnd, self.world.closed)
        self.assertIn(old_pid, self.world.ended)
        [(new_hwnd, new_pid)] = later.windows()
        self.assertNotEqual(new_pid, old_pid)
        self.assertEqual(later.open(), "focused")

    def test_a_reused_process_id_is_left_alone(self):
        first = self.window()
        first.open()
        [(hwnd, pid)] = first.windows()
        # That browser ended and an unrelated process got its id.
        del self.world.procs[pid]
        self.world.windows = []
        self.world.add(r"C:\Unrelated\tool.exe", title="x", pid=pid,
                       created=1)
        self.assertEqual(self.window().open(), "launched")
        self.assertNotIn(pid, self.world.ended)

    def test_our_window_with_no_connected_page_is_replaced(self):
        win = self.window()
        connected = [True]
        win.is_connected = lambda: connected[0]
        win.open()
        win._started_at -= uiwin.START_GRACE + 1
        self.assertEqual(win.open(), "focused")
        connected[0] = False
        self.assertEqual(win.open(), "replaced")
        self.assertEqual(len(self.world.ended), 1)
        self.assertEqual(len(win.windows()), 1)

    def test_close_on_quit_ends_only_ours_and_forgets_them(self):
        win = self.window()
        win.open()
        win.close()
        self.assertEqual(win.windows(), [])
        self.assertFalse((self.dir / uiwin.OWNER_FILE).exists())
        self.assertEqual(self.window().open(), "launched")   # nothing left


class ForgedRecord(AppWindowTest):
    """W4: the owner record is a file any program of this user can write."""

    def forge(self, pid, image=None, created=None):
        image = image or self.world.procs[pid][0]
        created = self.world.procs[pid][1] if created is None else created
        (self.dir / uiwin.OWNER_FILE).write_text(json.dumps(
            [{"pid": pid, "image": image, "created": created}]),
            encoding="utf-8")

    def test_a_record_for_a_program_that_is_not_a_browser_is_ignored(self):
        victim, hwnd = self.world.add(r"C:\Tools\editor.exe", title="notes")
        self.forge(victim)                  # image and start time both true
        win = self.window()
        self.assertEqual(win._read_record(), {})
        self.assertEqual(win.open(), "launched")
        win.close()
        self.assertNotIn(victim, self.world.ended)
        self.assertNotIn(hwnd, self.world.closed)
        self.assertIn(victim, self.world.procs)

    def test_a_record_with_the_wrong_start_time_is_ignored(self):
        victim, hwnd = self.world.add(EXE)
        self.forge(victim, created=1)
        self.assertEqual(self.window().open(), "launched")
        self.assertNotIn(victim, self.world.ended)
        self.assertNotIn(hwnd, self.world.closed)

    def test_a_browser_showing_other_windows_is_never_ended(self):
        # A true image and start time (the user's own browsing): the record
        # alone does not make it ours.
        victim, hwnd = self.world.add(EXE, title="Some page - Edge")
        self.forge(victim)
        win = self.window()
        self.assertEqual(win._leftovers(), {})
        self.assertEqual(win.open(), "launched")
        win.close()
        self.assertNotIn(victim, self.world.ended)
        self.assertNotIn(hwnd, self.world.closed)

    def test_a_genuine_record_still_works(self):
        first = self.window()
        first.open()
        later = self.window()
        self.assertEqual(len(later._read_record()), 1)
        self.assertEqual(later.open(), "replaced")

    def test_a_record_of_a_candidate_browser_by_another_spelling(self):
        victim, _ = self.world.add(EXE.upper())
        self.forge(victim, image=EXE.lower())
        self.assertEqual(len(self.window()._read_record()), 1)


class Leftovers(AppWindowTest):
    """W5: quitting right after the window was started, and helper
    processes that outlive their browser."""

    def test_close_before_the_window_exists_waits_for_it(self):
        self.world.no_window_yet = True
        win = self.window()
        win.open()
        [pid] = list(win._owned)
        hwnd = [9001]

        def show():
            self.world.windows.append((hwnd[0], pid, settings.APP_NAME))
        threading.Timer(0.3, show).start()
        start = time.monotonic()
        win.close()
        self.assertIn(hwnd[0], self.world.closed)       # asked politely
        self.assertLess(time.monotonic() - start, uiwin.WINDOW_WAIT)

    def test_a_browser_that_never_shows_a_window_is_still_ended(self):
        self.world.no_window_yet = True
        win = self.window()
        win.open()
        [pid] = list(win._owned)
        start = time.monotonic()
        win.close(timeout=0.5)
        self.assertIn(pid, self.world.ended)
        self.assertLess(time.monotonic() - start, 2.0)

    def test_helper_processes_are_ended_with_their_browser(self):
        win = self.window()
        win.open()
        [root] = list(win._owned)
        born = self.world.procs[root][1]
        helpers = [self.world.add(EXE, window=False, parent=root,
                                  created=born + 5)[0]]
        helpers.append(self.world.add(EXE, window=False, parent=helpers[0],
                                      created=born + 6)[0])   # grandchild
        other_image, _ = self.world.add(r"C:\Elsewhere\tool.exe", window=False,
                                        parent=root, created=born + 5)
        older, _ = self.world.add(EXE, window=False, parent=root,
                                  created=born - 5)   # recycled parent id
        unrelated, _ = self.world.add(EXE, window=False, created=born + 9)
        win.close()
        for pid in [root] + helpers:
            self.assertIn(pid, self.world.ended)
        for pid in (other_image, older, unrelated):
            self.assertNotIn(pid, self.world.ended)

    def test_helpers_of_an_exited_browser_are_found_and_ended(self):
        win = self.window()
        win.open()
        [root] = list(win._owned)
        born = self.world.procs[root][1]
        helper, _ = self.world.add(EXE, window=False, parent=root,
                                   created=born + 5)
        del self.world.procs[root]              # the root is already gone
        self.world.windows = []
        win._end({root: (EXE, born)})
        self.assertNotIn(helper, self.world.procs)


class RealProcesses(unittest.TestCase):
    def test_identity_of_this_process(self):
        import os
        import sys
        image, created = uiwin._process_info(os.getpid())
        # (a venv's python.exe may start the base interpreter, so compare
        # the program name only)
        self.assertTrue(Path(image).is_absolute())
        self.assertTrue(Path(image).name.lower().startswith("python"),
                        Path(image).name)
        self.assertGreater(created, 0)
        self.assertIsNone(uiwin._process_info(0x7FFFFFF0))
        self.assertEqual(uiwin.find_windows(settings.APP_NAME, {}), [])


if __name__ == "__main__":
    unittest.main()
