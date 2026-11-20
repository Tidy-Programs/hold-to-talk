"""The app window's server (api.py) over HTTP, against temporary config and
history files and a stand-in engine (no Tk, no Whisper, no windows)."""

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

import http.client
import json
import re
import threading
import time
import unittest
from pathlib import Path

import api
import app as app_mod
from fakes import FakeClipboard
import dictate
import learn
import overlay
import pipeline
import settings
import textrules
import tray
import uiwin

PROJECT = Path(__file__).resolve().parent.parent


class FakeApp:
    """The attributes api.Server uses, with reload_config() like App._reload
    against a temporary config file."""

    def __init__(self, path):
        self.path = path
        self.cfg = settings.load_config(path)
        self.status, self.status_detail = "ready", ""
        self.paused = False
        self.activity = "idle"
        self.capturing = False
        self.mouse_hook_state = "off"
        self.dry_run = True
        self.on_dictation, self.on_notice, self.on_reload = [], [], []
        self.recorder = type("R", (), {"level": 0.0})()
        self.engine = None
        self.previews = []
        self.capture_cb = None
        self.reloads = 0

    def reload_config(self):
        self.reloads += 1
        old, new = self.cfg, settings.load_config(self.path)
        result = {"applied": sorted(k for k in set(old) | set(new)
                                    if old.get(k) != new.get(k)),
                  "restart_required": [], "errors": []}
        if new.get("input_device") == "no such mic":
            result["errors"].append("input_device: No input device matches")
            new["input_device"] = old.get("input_device")
            result["applied"].remove("input_device")
        self.cfg = new
        for fn in self.on_reload:
            fn(old, new, result)
        return result

    def set_paused(self, paused):
        self.paused = bool(paused)

    def capture_hotkey(self, callback, timeout=10.0):
        self.capturing = True
        self.capture_cb = callback

    def cancel_capture(self):
        cb, self.capture_cb = self.capture_cb, None
        self.capturing = False
        if cb:
            cb(None)

    def preview_overlay(self, state, seconds=1.0):
        self.previews.append(state)


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="htt-api-"))
        self.cfg_path = self.dir / "config.json"
        self.cfg_path.write_text(json.dumps({
            "hotkey": "f23", "vocabulary": ["Kubernetes"],
            "dictionary": [{"heard": "jason", "wanted": "JSON"}],
            "snippets": [{"trigger": "my signature", "text": "Best,\nSam"}],
            "cleanup": {"enabled": False}}), encoding="utf-8")
        self.app = FakeApp(self.cfg_path)
        self.history = textrules.History(self.dir / "h.sqlite3",
                                         cfg=lambda: self.app.cfg)
        self.clipboard = FakeClipboard()    # the real one is never used
        self.server = api.Server(self.app, self.history,
                                 config_path=self.cfg_path,
                                 autostart_name=None,
                                 clipboard=self.clipboard).start()
        self.host = f"127.0.0.1:{self.server.port}"
        self.key = self.hello()

    def tearDown(self):
        self.server.stop()
        self.history.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- plumbing
    def request(self, method, path, body=None, headers=None, key=True,
                raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port,
                                          timeout=10)
        h = {"Host": self.host}
        if method == "POST":
            h["Origin"] = self.server.origin
            h["Content-Type"] = "application/json"
        if key:
            h[api.KEY_HEADER] = self.key
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        data = raw if raw is not None else (
            json.dumps(body or {}).encode() if method == "POST" else None)
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = payload
        return resp.status, parsed, dict(resp.getheaders())

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body, **kw)

    def ok(self, method, path, body=None):
        status, data, _ = self.request(method, path, body)
        self.assertEqual(status, 200, (path, data))
        return data

    def hello(self):
        url = self.server.launch_url()
        ticket = re.search(r"#t=([^&]+)", url).group(1)
        status, data, _ = self.request("POST", "/api/hello",
                                       {"ticket": ticket}, key=False)
        self.assertEqual(status, 200, data)
        return data["key"]

    def record(self, raw, final, ts=None):
        ts = time.time() if ts is None else ts
        rec = {"ts": ts, "raw": raw, "final": final, "pasted": final + " ",
               "audio_seconds": 2.0, "speech_seconds": 0.4,
               "stages": [{"name": "normalize", "text": final, "seconds": 0.0}],
               "cleanup": "disabled", "dry_run": True, "paste_error": None,
               "total_seconds": 0.5}
        self.history.record(raw, final, 2.0, ts=ts)
        for fn in self.app.on_dictation:
            fn(rec)
        self.history.flush()
        return ts


class Security(ServerCase):
    def test_needs_the_secret(self):
        for path in ("/api/state", "/api/history", "/api/config", "/api/log",
                     "/api/feed", "/api/events"):
            status, data, _ = self.get(path, key=False)
            self.assertEqual(status, 401, path)
            status, _, _ = self.get(path, headers={api.KEY_HEADER: "x" * 43})
            self.assertEqual(status, 401, path)
        status, _, _ = self.post("/api/history/clear", {"confirm": True},
                                 key=False)
        self.assertEqual(status, 401)

    def test_ticket_is_single_use_and_secret_not_in_url(self):
        url = self.server.launch_url("history")
        self.assertNotIn(self.server.secret, url)
        self.assertIn("#t=", url)                       # fragment, not query
        self.assertTrue(url.endswith("&v=history"))
        ticket = re.search(r"#t=([^&]+)", url).group(1)
        first = self.post("/api/hello", {"ticket": ticket}, key=False)
        again = self.post("/api/hello", {"ticket": ticket}, key=False)
        self.assertEqual(first[0], 200)
        self.assertEqual(again[0], 401)
        self.assertEqual(self.post("/api/hello", {"ticket": "guess"},
                                   key=False)[0], 401)

    def test_expired_ticket(self):
        url = self.server.launch_url()
        ticket = re.search(r"#t=([^&]+)", url).group(1)
        self.server._tickets[ticket] = time.monotonic() - 1
        self.assertEqual(self.post("/api/hello", {"ticket": ticket},
                                   key=False)[0], 401)

    def test_wrong_host_rejected(self):
        for host in ("evil.example:%d" % self.server.port, "localhost:1",
                     "127.0.0.1", "localhost:%d" % self.server.port):
            status, _, _ = self.get("/api/state", headers={"Host": host})
            self.assertEqual(status, 403, host)
            status, _, _ = self.get("/", headers={"Host": host})
            self.assertEqual(status, 403, host)
        import socket                                   # no Host at all
        with socket.create_connection(("127.0.0.1", self.server.port), 5) as s:
            s.sendall(f"GET /api/state HTTP/1.1\r\n{api.KEY_HEADER}: "
                      f"{self.key}\r\nConnection: close\r\n\r\n".encode())
            self.assertIn(b" 403 ", s.recv(100))

    def test_foreign_origin_rejected(self):
        for origin in ("http://evil.example", "http://127.0.0.1:1",
                       "null"):
            status, _, _ = self.post("/api/pause", {"paused": True},
                                     headers={"Origin": origin})
            self.assertEqual(status, 403, origin)
            status, _, _ = self.get("/api/state", headers={"Origin": origin})
            self.assertEqual(status, 403, origin)
        self.assertFalse(self.app.paused)
        status, _, _ = self.post("/api/pause", {"paused": True},
                                 headers={"Origin": None})
        self.assertEqual(status, 403)                   # POST needs Origin
        status, _, _ = self.get("/api/state",
                                headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_post_must_be_json_object(self):
        status, _, _ = self.post("/api/pause", headers={
            "Content-Type": "application/x-www-form-urlencoded"},
            raw=b"paused=true")
        self.assertEqual(status, 415)
        self.assertEqual(self.post("/api/pause", raw=b"[1]")[0], 400)
        self.assertEqual(self.post("/api/pause", raw=b"{nope")[0], 400)
        big = b'{"x": "' + b"a" * (api.MAX_BODY + 10) + b'"}'
        self.assertEqual(self.post("/api/pause", raw=big)[0], 413)
        self.assertEqual(self.request("PUT", "/api/pause")[0], 501)
        self.assertEqual(self.request("OPTIONS", "/api/pause")[0], 501)

    def test_no_cors_and_safe_headers(self):
        status, _, headers = self.get("/api/state", headers={
            "Origin": self.server.origin})
        self.assertEqual(status, 200)
        self.assertFalse(any(k.lower().startswith("access-control")
                             for k in headers))
        status, body, headers = self.get("/", key=False)
        self.assertEqual(status, 200)
        self.assertIn(b"<title>" + settings.APP_NAME.encode(), body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_only_whitelisted_files(self):
        for path in ("/app.js", "/app.css", "/icon.png"):
            self.assertEqual(self.get(path, key=False)[0], 200, path)
        for path in ("/../config.json", "/ui/app.js", "/config.json",
                     "/%2e%2e/dictate.py", "/api/../dictate.py", "/api/nope"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_loopback_only(self):
        for host in ("0.0.0.0", "192.168.1.5", "example.com", "::"):
            with self.assertRaises(ValueError):
                api.Server(self.app, None, host=host)

    def test_secret_never_logged(self):
        with self.assertLogs("holdtotalk", "INFO") as logs:
            import logging
            logging.getLogger("holdtotalk.api").info("probe")
            self.ok("GET", "/api/state")
            self.server.launch_url()
        joined = "\n".join(logs.output) + "\n".join(self.server._logbuf.lines)
        self.assertNotIn(self.server.secret, joined)


class Endpoints(ServerCase):
    def test_state_feed_config_log(self):
        self.record("um send the jason file", "Send the JSON file.")
        state = self.ok("GET", "/api/state")
        self.assertEqual(state["info"]["name"], settings.APP_NAME)
        self.assertEqual(state["live"]["hotkey"]["plain"], "F23")
        self.assertEqual(state["stats"]["all"]["dictations"], 1)
        feed = self.ok("GET", "/api/feed")["items"]
        self.assertEqual(feed[0]["final"], "Send the JSON file.")
        self.assertIsNotNone(feed[0]["history_id"])
        self.assertEqual(feed[0]["stages"][0]["name"], "normalize")
        cfg = self.ok("GET", "/api/config")
        self.assertEqual(cfg["vocabulary"], ["Kubernetes"])
        self.assertEqual(cfg["dictionary"][0]["wanted"], "JSON")
        self.assertFalse(cfg["autostart"]["available"])
        self.assertIn({"code": "en", "name": "English"}, cfg["languages"])
        self.assertEqual(cfg["settings"]["overlay"], settings.OVERLAY_DEFAULTS)
        self.assertIn("lines", self.ok("GET", "/api/log"))

    def test_event_stream(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port,
                                          timeout=5)
        conn.request("GET", "/api/events", headers={
            "Host": self.host, api.KEY_HEADER: self.key})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith(
            "text/event-stream"))
        self.assertEqual(resp.fp.readline(), b"event: live\n")
        resp.fp.readline(); resp.fp.readline()
        threading.Timer(0.2, lambda: self.record("hi there", "Hi there.")
                        ).start()
        lines = []
        deadline = time.time() + 5
        while time.time() < deadline and b"event: dictation\n" not in lines:
            lines.append(resp.fp.readline())
        conn.close()
        self.assertIn(b"event: dictation\n", lines)

    def test_history_endpoints(self):
        ts = self.record("one", "One.")
        self.record("two", "Two.")
        items = self.ok("GET", "/api/history")["items"]
        self.assertEqual([i["final"] for i in items], ["Two.", "One."])
        self.assertEqual(self.ok("GET", "/api/history?q=one")["items"][0]
                         ["final"], "One.")
        row = self.ok("POST", "/api/history/find", {"ts": ts})["row"]
        self.assertEqual(row["final"], "One.")
        self.ok("POST", "/api/history/delete", {"id": row["id"]})
        self.assertEqual(self.post("/api/history/delete",
                                   {"id": row["id"]})[0], 404)
        self.assertEqual(self.post("/api/history/clear", {})[0], 400)
        self.ok("POST", "/api/history/clear", {"confirm": True})
        self.assertEqual(self.ok("GET", "/api/history")["count"], 0)
        self.assertEqual(self.post("/api/history/delete", {"id": "1"})[0], 400)

    def test_settings_apply_live(self):
        res = self.ok("POST", "/api/settings", {
            "hotkey": "Mouse Forward", "language": "de",
            "trailing_space": False, "cleanup_enabled": True,
            "cleanup_model": "qwen3:4b", "formatting_commands": True,
            "snippet_mode": "whole", "auto_learn": True,
            "save_history": False, "input_device": None})
        self.assertEqual(res["result"]["errors"], [])
        cfg = self.app.cfg
        self.assertEqual(cfg["hotkey"], "mouse5")
        self.assertEqual(cfg["language"], "de")
        self.assertFalse(cfg["trailing_space"])
        self.assertEqual(cfg["cleanup"]["model"], "qwen3:4b")
        self.assertTrue(cfg["cleanup"]["enabled"])
        self.assertTrue(cfg["textrules"]["formatting_commands"])
        self.assertTrue(learn.settings_of(cfg)["auto_apply"])
        data = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(data["cleanup"], {"enabled": True,
                                           "model": "qwen3:4b"})
        self.assertEqual(res["settings"]["snippet_mode"], "whole")
        for bad in ({"hotkey": "right click"}, {"language": "klingon"},
                    {"trailing_space": "yes"}, {"cleanup_model": "rm -rf /"},
                    {"snippet_mode": "x"}, {"nope": 1}, {},
                    {"input_device": ["x"]}, {"model": "tiny"}):
            self.assertEqual(self.post("/api/settings", bad)[0], 400, bad)
        status, res, _ = self.post("/api/settings",
                                   {"input_device": "no such mic"})
        self.assertEqual(status, 422)                    # refused: an error
        self.assertEqual(res["result"]["rejected"], ["input_device"])
        self.assertIsNone(json.loads(self.cfg_path.read_text(
            encoding="utf-8")).get("input_device"))

    def test_pause_and_autostart(self):
        self.assertTrue(self.ok("POST", "/api/pause", {"paused": True})
                        ["paused"])
        self.assertTrue(self.app.paused)
        self.ok("POST", "/api/pause", {"paused": False})
        self.assertFalse(self.app.paused)
        # A test instance has no Run entry to manage.
        self.assertEqual(self.post("/api/autostart", {"enabled": True})[0],
                         400)

    def test_hotkey_capture_with_fake_app(self):
        q = self.server.hub.subscribe()
        cap = self.ok("POST", "/api/hotkey/capture", {})["capture"]
        self.assertEqual(cap["state"], "waiting")
        self.assertTrue(self.app.capturing)
        self.app.capture_cb("mouse4")          # the user pressed Back
        state = self.ok("GET", "/api/state")["live"]["capture"]
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["plain"], "Mouse Back button")
        self.assertIn("XBUTTON1", state["technical"])
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        self.assertIn("capture", [e[0] for e in events if e])
        self.server.hub.unsubscribe(q)
        self.assertEqual(self.app.cfg["hotkey"], "f23")   # not applied yet
        self.ok("POST", "/api/hotkey/capture", {})
        self.ok("POST", "/api/hotkey/cancel", {})
        self.assertEqual(self.ok("GET", "/api/state")["live"]["capture"]
                         ["state"], "cancelled")

    def test_vocabulary(self):
        self.assertEqual(self.ok("POST", "/api/vocabulary/check",
                                 {"term": "table"})["warnings"][0][:1], "“")
        status, data, _ = self.post("/api/vocabulary/check",
                                    {"term": "kubernetes"})
        self.assertEqual(status, 400)
        self.assertIn("already", data["error"])
        res = self.ok("POST", "/api/vocabulary/add", {"term": "Zorvex"})
        self.assertEqual(res["config"]["vocabulary"], ["Kubernetes", "Zorvex"])
        self.ok("POST", "/api/vocabulary/edit", {"old": "Zorvex",
                                                 "new": "Zorvexa"})
        self.ok("POST", "/api/vocabulary/remove", {"term": "Kubernetes"})
        self.assertEqual(self.app.cfg["vocabulary"], ["Zorvexa"])
        self.assertEqual(self.post("/api/vocabulary/remove",
                                   {"term": "nope"})[0], 400)

    def test_dictionary(self):
        self.assertIn("ordinary", self.ok("POST", "/api/dictionary/check", {
            "heard": "table", "wanted": "Tabel"})["warnings"][0])
        self.assertEqual(self.post("/api/dictionary/check", {
            "heard": "Jason", "wanted": "J"})[0], 400)
        self.ok("POST", "/api/dictionary/add", {"heard": "pie torch",
                                                "wanted": "PyTorch"})
        prev = self.ok("POST", "/api/dictionary/preview", {
            "heard": "pie torch", "wanted": "PyTorch",
            "sample": "I like pie torch and jason."})
        self.assertEqual(prev["full"], "I like PyTorch and JSON.")
        self.assertEqual(prev["matches"], 1)
        prev = self.ok("POST", "/api/dictionary/preview", {
            "heard": "zork", "wanted": "Zork"})
        self.assertIn("Zork", prev["full"])                  # default sample
        self.ok("POST", "/api/dictionary/edit", {
            "old_heard": "pie torch", "heard": "pie torch",
            "wanted": "Pytorch"})
        self.ok("POST", "/api/dictionary/remove", {"heard": "jason"})
        self.assertEqual(self.app.cfg["dictionary"],
                         [{"heard": "pie torch", "wanted": "Pytorch"}])
        self.assertEqual(pipeline.run("Pie torch rocks.", dict(
            self.app.cfg, cleanup={"enabled": False})), "Pytorch rocks.")

    def test_snippets(self):
        self.assertEqual(self.post("/api/snippets/check", {
            "trigger": "My signature!", "text": "x"})[0], 400)
        self.ok("POST", "/api/snippets/add", {"trigger": "my address",
                                              "text": "1 Road\nTown"})
        self.ok("POST", "/api/snippets/edit", {
            "old_trigger": "my address", "trigger": "home address",
            "text": "2 Road\nTown"})
        self.ok("POST", "/api/snippets/remove", {"trigger": "my signature"})
        self.assertEqual(self.app.cfg["snippets"],
                         [{"trigger": "home address", "text": "2 Road\nTown"}])
        self.assertEqual(pipeline.run("Home address.", dict(
            self.app.cfg, cleanup={"enabled": False})), "2 Road\nTown")
        for bad in ({"trigger": "", "text": "x"}, {"trigger": "a b", "text": ""},
                    {"trigger": "a\nb", "text": "x"}, {"trigger": 5, "text": "x"}):
            self.assertEqual(self.post("/api/snippets/add", bad)[0], 400, bad)

    def test_mic_test_runs_off_thread(self):
        q = self.server.hub.subscribe()
        res = self.ok("POST", "/api/mic/test", {"device": 987654})
        self.assertEqual(res["seconds"], api.MIC_TEST_SECONDS)
        deadline, done = time.time() + 10, None
        while time.time() < deadline and done is None:
            try:
                item = q.get(timeout=0.5)
            except Exception:
                continue
            if item and item[0] == "mictest":
                done = json.loads(item[1])
        self.server.hub.unsubscribe(q)
        self.assertIsNotNone(done)
        self.assertFalse(done["ok"])            # no such device: reported
        self.assertEqual(self.post("/api/mic/test", {"device": True})[0], 400)


class Learning(ServerCase):
    def test_correction_suggestion_accept_undo(self):
        ts = self.record("lets meet at the zurik office",
                         "Let's meet at the zurik office.")
        row = self.ok("POST", "/api/history/find", {"ts": ts})["row"]
        res = self.ok("POST", "/api/history/correct", {
            "id": row["id"], "text": "Let's meet at the Zürich office."})
        self.assertEqual(res["row"]["corrected"],
                         "Let's meet at the Zürich office.")
        [sugg] = res["suggestions"]
        self.assertEqual(sugg["dictionary"], {"heard": "zurik",
                                              "wanted": "Zürich"})
        self.assertEqual(sugg["vocabulary"], "Zürich")
        acc = self.ok("POST", "/api/learn/accept", {
            "history_id": row["id"], "ids": [sugg["id"]]})
        [rec] = acc["applied"]
        self.assertIn({"heard": "zurik", "wanted": "Zürich"},
                      self.app.cfg["dictionary"])
        self.assertIn("Zürich", self.app.cfg["vocabulary"])
        self.assertEqual(acc["config"]["learned"][0]["id"], rec["id"])
        cfg = dict(self.app.cfg, cleanup={"enabled": False})
        self.assertEqual(pipeline.run("lets meet at the zurik office", cfg),
                         "Lets meet at the Zürich office")
        # Accepting again: nothing left to learn.
        self.assertEqual(self.post("/api/learn/accept", {
            "history_id": row["id"], "ids": [sugg["id"]]})[0], 400)
        stats = self.ok("GET", "/api/state")["stats"]
        self.assertEqual((stats["corrections"], stats["learned"]), (1, 1))
        undo = self.ok("POST", "/api/learned/undo", {"id": rec["id"]})
        self.assertEqual(undo["removed_entry"], {"heard": "zurik",
                                                 "wanted": "Zürich"})
        self.assertNotIn("Zürich", self.app.cfg["vocabulary"])
        self.assertEqual(self.app.cfg["learned"], [])
        self.assertEqual(self.post("/api/learned/undo", {"id": rec["id"]})[0],
                         400)
        # Reverting the correction.
        self.assertIsNone(self.ok("POST", "/api/history/revert",
                                  {"id": row["id"]})["row"]["corrected"])

    def test_auto_learning(self):
        self.ok("POST", "/api/settings", {"auto_learn": True})
        ts = self.record("push it to github", "Push it to github.")
        row = self.ok("POST", "/api/history/find", {"ts": ts})["row"]
        res = self.ok("POST", "/api/history/correct", {
            "id": row["id"], "text": "Push it to GitHub."})
        self.assertEqual(res["suggestions"], [])
        self.assertEqual(res["auto"][0]["wanted"], "GitHub")
        self.assertIn({"heard": "github", "wanted": "GitHub"},
                      self.app.cfg["dictionary"])

    def test_rejected_inputs(self):
        self.assertEqual(self.post("/api/history/correct", {
            "id": 999, "text": "x"})[0], 404)
        self.assertEqual(self.post("/api/learn/accept", {
            "history_id": 999, "ids": ["a"]})[0], 409)
        self.assertEqual(self.post("/api/learn/accept", {
            "history_id": 1, "ids": "a"})[0], 400)


class Overlay(ServerCase):
    def test_overlay_colours(self):
        res = self.ok("POST", "/api/settings",
                      {"overlay": {"color": "#FF4FA3"}})
        self.assertEqual(res["settings"]["overlay"],
                         {"color": "#ff4fa3", "background": "#17181c"})
        self.assertEqual(res["warnings"], [])
        self.assertEqual(json.loads(self.cfg_path.read_text(
            encoding="utf-8"))["overlay"], {"color": "#ff4fa3"})
        self.assertEqual(self.ok("GET", "/api/state")["live"]["overlay"]
                         ["color"], "#ff4fa3")
        low = self.ok("POST", "/api/overlay/check",
                      {"color": "#202020", "background": "#17181c"})
        self.assertIn("hard to tell apart", low["warnings"][0])
        res = self.ok("POST", "/api/settings", {"overlay": {
            "color": "#1a1b1f"}})                        # saved, but warned
        self.assertTrue(res["warnings"])
        for bad in ({"color": "pink"}, {"color": "#12345"}, {"x": "#000000"},
                    "#000000"):
            self.assertEqual(self.post("/api/settings",
                                       {"overlay": bad})[0], 400, bad)
        self.ok("POST", "/api/settings", {"overlay": dict(
            settings.OVERLAY_DEFAULTS)})
        self.assertEqual(settings.overlay_colors(self.app.cfg),
                         settings.OVERLAY_DEFAULTS)

    def test_preview_uses_preview_overlay(self):
        self.ok("POST", "/api/overlay/preview", {})
        self.assertEqual(self.app.previews, ["listening"])
        time.sleep(2.8)
        self.assertEqual(self.app.previews, ["listening", "working"])


class OverlayConfig(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="htt-ov-"))
        self.saved = settings.CONFIG_PATH
        settings.CONFIG_PATH = self.dir / "config.json"

    def tearDown(self):
        settings.CONFIG_PATH = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_defaults_merge_and_fallbacks(self):
        settings.CONFIG_PATH.write_text(json.dumps(
            {"overlay": {"color": "#FF4FA3"}}), encoding="utf-8")
        cfg = settings.load_config()
        self.assertEqual(cfg["overlay"], {"color": "#FF4FA3",
                                          "background": "#17181c"})
        self.assertEqual(settings.overlay_colors(cfg),
                         {"color": "#ff4fa3", "background": "#17181c"})
        for bad in ({"overlay": {"color": "pink", "background": 5}},
                    {"overlay": "nonsense"}, {"overlay": None}, {}):
            self.assertEqual(settings.overlay_colors(bad),
                             settings.OVERLAY_DEFAULTS, bad)
        settings.save_config({"overlay": {"background": "#000000"}})
        data = json.loads(settings.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["overlay"], {"color": "#FF4FA3",
                                           "background": "#000000"})
        self.assertGreater(settings.contrast_ratio("#ffffff", "#000000"), 20)
        self.assertEqual(settings.check_overlay("#f4f4f5", "#17181c"), [])
        with self.assertRaises(ValueError):
            settings.check_overlay("red", "#000000")

    def test_live_reload_recolours_the_pill(self):
        settings.CONFIG_PATH.write_text("{}", encoding="utf-8")
        app = app_mod.App.__new__(app_mod.App)
        app.cfg = settings.load_config()
        app.engine = None
        app.on_reload = []
        calls = []
        app.overlay = type("Ov", (), {"set_colors": lambda self, c, b:
                                      calls.append((c, b))})()
        settings.save_config({"overlay": {"color": "#ff4fa3"}})
        result = app._reload()
        self.assertEqual(result["errors"], [])
        self.assertIn("overlay", result["applied"])
        self.assertEqual(result["restart_required"], [])
        self.assertEqual(calls, [("#ff4fa3", "#17181c")])
        app._reload()                                    # unchanged: no call
        self.assertEqual(len(calls), 1)
        settings.save_config({"overlay": {"color": "garbage"}})
        app._reload()                                    # falls back
        self.assertEqual(calls[-1], tuple(settings.OVERLAY_DEFAULTS.values()))

    def test_overlay_colour_helpers(self):
        self.assertEqual(overlay.mix("#000000", "#ffffff", 0.5), "#808080")
        self.assertNotEqual(overlay.safe_background(overlay.TRANSPARENT),
                            overlay.TRANSPARENT)
        self.assertEqual(overlay.safe_background("#17181c"), "#17181c")


class Wiring(unittest.TestCase):
    def test_tk_windows_are_gone(self):
        self.assertFalse(hasattr(textrules, "open_history_window"))
        for name in ("SettingsWindow", "RowEditor", "open_settings",
                     "apply_changes", "normalize_hotkey"):
            self.assertFalse(hasattr(tray, name), name)
        stale = re.compile(r"open_history_window|SettingsWindow|RowEditor|"
                           r"on_open_history|open_settings|_open_windows|"
                           r"tray\.apply_changes|tray\.cleanup_enabled")
        for path in list(PROJECT.glob("*.py")) + list(PROJECT.glob("tests/*.py")):
            if path.name == "test_api.py":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(stale.search(text), path.name)
            if path.name not in ("app.py", "overlay.py"):
                self.assertNotIn("import tkinter", text, path.name)

    def test_instances_coexist(self):
        self.assertEqual(dictate.instance_suffix(settings.APP_DIR
                                                 / "config.json"), "")
        other = dictate.instance_suffix(Path(tempfile.gettempdir()) / "c.json")
        self.assertRegex(other, r"^-[0-9a-f]{8}$")
        self.assertEqual(other, dictate.instance_suffix(
            str(Path(tempfile.gettempdir()) / "C.JSON")))
        self.assertEqual(dictate.names(""), (
            "Local\\HoldToTalkDictation", "Local\\HoldToTalkDictation.Quit",
            "Local\\HoldToTalkDictation.Open"))
        self.assertTrue(all(n.startswith("Local\\HoldToTalkDictation" + other)
                            for n in dictate.names(other)))
        self.assertNotEqual(uiwin.profile_dir(""), uiwin.profile_dir(other))

    def test_window_open_focuses_existing(self):
        win = uiwin.AppWindow(lambda: "http://127.0.0.1:1/#t=x", Path(
            tempfile.gettempdir()) / "htt-never-used")
        calls = []
        saved = uiwin.find_windows, uiwin.focus, uiwin.browser_candidates
        try:
            uiwin.find_windows = lambda title=None, owners=None: [(1234, 99)]
            uiwin.focus = calls.append
            uiwin.browser_candidates = lambda: (_ for _ in ()).throw(
                AssertionError("must not launch"))
            self.assertEqual(win.open(), "focused")
            self.assertEqual(calls, [1234])
        finally:
            uiwin.find_windows, uiwin.focus, uiwin.browser_candidates = saved

    def test_finds_an_installed_browser(self):
        found = list(uiwin.browser_candidates())
        self.assertTrue(found)
        self.assertTrue(found[0].lower().endswith(("msedge.exe",
                                                   "chrome.exe")))
        self.assertEqual(uiwin.find_windows("no such title 123", {}), [])


def _deep_json(depth):
    return b"[" * depth + b"]" * depth


class Robustness(ServerCase):
    """S1, S2, W6: the server under bad or hostile local requests."""

    def test_oversized_body_gets_413_not_a_reset(self):
        # W6: the server used to answer and close before the client had
        # sent the body, so the client saw a reset about one run in three.
        big = b'{"x": "' + b"a" * (api.MAX_BODY + 10) + b'"}'
        for _ in range(10):
            self.assertEqual(self.post("/api/pause", raw=big)[0], 413)
        # Refused before the body was read (no key): also an answer.
        for _ in range(5):
            self.assertEqual(self.post("/api/pause", raw=big, key=False)[0],
                             401)

    def test_body_is_not_parsed_without_the_key(self):
        # S1: deeply nested JSON used to be parsed before the key check and
        # end in a 500 with a RecursionError traceback in the log.
        deep = _deep_json(100000)
        with self.assertLogs("holdtotalk", "INFO") as logs:
            import logging
            logging.getLogger("holdtotalk.api").info("probe")
            status, data, _ = self.post("/api/pause", raw=deep, key=False)
            self.assertEqual(status, 401)
            status, data, _ = self.post("/api/pause", raw=deep)
            self.assertEqual(status, 400)           # with the key: bad JSON
            status, _, _ = self.post("/api/hello", raw=deep, key=False)
            self.assertEqual(status, 400)           # the ticket exchange too
        text = "\n".join(logs.output)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("request failed", text)
        self.assertFalse(self.app.paused)

    def test_connection_flood_is_capped(self):
        # S2: one thread per connection with no cap; 1,500 half-sent
        # requests made 1,514 threads.
        import socket
        self.server.stop()
        self.server = api.Server(self.app, self.history,
                                 config_path=self.cfg_path,
                                 autostart_name=None, max_connections=8,
                                 header_timeout=1.0).start()
        self.host = f"127.0.0.1:{self.server.port}"
        self.key = self.hello()
        # A live stream connected before the flood keeps working.
        stream = http.client.HTTPConnection("127.0.0.1", self.server.port,
                                            timeout=10)
        stream.request("GET", "/api/events", headers={
            "Host": self.host, api.KEY_HEADER: self.key})
        resp = stream.getresponse()
        self.assertEqual(resp.fp.readline(), b"event: live\n")
        before = threading.active_count()
        socks = []
        try:
            for _ in range(200):
                s = socket.create_connection(("127.0.0.1", self.server.port),
                                             5)
                s.sendall(b"GET /api/state HTTP/1.1\r\nHost: ")  # half-sent
                socks.append(s)
            time.sleep(0.3)
            self.assertLessEqual(threading.active_count() - before, 10)
            self.assertLessEqual(self.server.httpd.in_progress, 8)
            self.assertGreater(self.server.httpd.refused, 150)
            threading.Timer(0.1, lambda: self.record("still", "Still.")).start()
            lines = []
            deadline = time.time() + 5
            while time.time() < deadline and b"event: dictation\n" not in lines:
                lines.append(resp.fp.readline())
            self.assertIn(b"event: dictation\n", lines)
            # Half-sent requests are dropped after the header timeout, so
            # a real request gets through again.
            time.sleep(1.6)
            self.assertEqual(self.get("/api/state")[0], 200)
        finally:
            for s in socks:
                s.close()
            stream.close()


class SaveFailures(ServerCase):
    """W1: a change that was not saved must fail, keep the real state and
    say so in plain words."""

    def setUp(self):
        super().setUp()
        import os
        import stat
        os.chmod(self.cfg_path, stat.S_IREAD)

    def tearDown(self):
        import os
        import stat
        os.chmod(self.cfg_path, stat.S_IREAD | stat.S_IWRITE)
        super().tearDown()

    def assert_failed_plainly(self, status, data):
        self.assertEqual(status, 503, data)
        self.assertIn("could not be saved", data["error"])
        self.assertNotIn("WinError", data["error"])
        self.assertNotIn(str(self.dir), data["error"])
        self.assertNotIn("config.json", data["error"])
        self.assertNotIn("WinError", json.dumps(data))

    def test_list_changes(self):
        status, data, _ = self.post("/api/dictionary/add", {
            "heard": "kwerty", "wanted": "QWERTY"})
        self.assert_failed_plainly(status, data)
        self.assertNotIn("kwerty", [e["heard"] for e in
                                    data["config"]["dictionary"]])
        status, data, _ = self.post("/api/dictionary/remove",
                                    {"heard": "jason"})
        self.assert_failed_plainly(status, data)
        self.assertIn("jason", [e["heard"] for e in
                                data["config"]["dictionary"]])
        status, data, _ = self.post("/api/vocabulary/add", {"term": "Zorvex"})
        self.assert_failed_plainly(status, data)
        self.assertEqual(data["config"]["vocabulary"], ["Kubernetes"])
        status, data, _ = self.post("/api/snippets/add", {
            "trigger": "my phone", "text": "555 0100"})
        self.assert_failed_plainly(status, data)
        self.assertEqual(len(data["config"]["snippets"]), 1)

    def test_setting_shows_the_engine_state(self):
        self.assertTrue(self.app.cfg["remove_fillers"])
        status, data, _ = self.post("/api/settings",
                                    {"remove_fillers": False})
        self.assert_failed_plainly(status, data)
        self.assertTrue(data["settings"]["remove_fillers"])
        self.assertTrue(self.app.cfg["remove_fillers"])
        with self.assertLogs("holdtotalk.api", "WARNING") as logs:
            self.post("/api/settings", {"remove_fillers": False})
        self.assertIn("Could not save", "\n".join(logs.output))  # raw: log

    def test_refused_value_fails_with_the_kept_value(self):
        import os
        import stat
        os.chmod(self.cfg_path, stat.S_IREAD | stat.S_IWRITE)
        status, data, _ = self.post("/api/settings",
                                    {"input_device": "no such mic"})
        self.assertEqual(status, 422, data)
        self.assertIn("microphone is not available", data["error"])
        self.assertIsNone(data["settings"]["input_device"])


class BrokenConfig(ServerCase):
    def test_unparsable_file_is_a_plain_save_failure(self):
        # settings.save_config refuses to overwrite a file it cannot parse
        # (settings.ConfigError): the window must say so, not "Added".
        self.cfg_path.write_text('{"vocabulary": ["Zorvex",', encoding="utf-8")
        status, data, _ = self.post("/api/vocabulary/add", {"term": "Quillon"})
        self.assertEqual(status, 503, data)
        self.assertIn("mistake", data["error"])
        self.assertNotIn("Expecting", data["error"])      # no parser detail
        self.assertNotIn("Quillon", data["config"]["vocabulary"])
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"),
                         '{"vocabulary": ["Zorvex",')       # left as it was


class NewSettings(ServerCase):
    """The pause shortcut, chimes, retry and where AI cleanup runs."""

    def test_pause_hotkey(self):
        res = self.ok("POST", "/api/settings", {"pause_hotkey": "Ctrl + F9"})
        self.assertEqual(res["settings"]["pause_hotkey"], "ctrl+f9")
        self.assertEqual(res["settings"]["pause_hotkey_plain"], "Ctrl + F9")
        self.assertEqual(json.loads(self.cfg_path.read_text(
            encoding="utf-8"))["pause_hotkey"], "ctrl+f9")
        status, data, _ = self.post("/api/settings", {"pause_hotkey": "F23"})
        self.assertEqual(status, 400)
        self.assertIn("already your hold-to-talk key", data["error"])
        status, data, _ = self.post("/api/settings", {"hotkey": "f9+ctrl"})
        self.assertEqual(status, 400)
        self.assertIn("pause shortcut", data["error"])
        status, data, _ = self.post("/api/settings", {"pause_hotkey": "hyper"})
        self.assertEqual(status, 400)
        self.assertNotIn("hyper", data["error"])
        res = self.ok("POST", "/api/settings", {"pause_hotkey": None})
        self.assertIsNone(res["settings"]["pause_hotkey"])
        self.assertIsNone(self.app.cfg["pause_hotkey"])

    def test_sounds(self):
        cfg = self.ok("GET", "/api/config")
        self.assertEqual(cfg["settings"]["sounds"], api.SOUND_DEFAULTS)
        res = self.ok("POST", "/api/settings", {"sounds": {"volume": 0.25}})
        self.assertEqual(res["settings"]["sounds"],
                         {"enabled": True, "volume": 0.25,
                          "mute_others": False})
        self.ok("POST", "/api/settings", {"sounds": {"enabled": False}})
        self.assertEqual(json.loads(self.cfg_path.read_text(
            encoding="utf-8"))["sounds"], {"enabled": False, "volume": 0.25})
        # Mute other sound while dictating: off by default, a bool, saved
        # and applied live (the running app reads it from its reloaded cfg).
        res = self.ok("POST", "/api/settings",
                      {"sounds": {"mute_others": True}})
        self.assertIs(res["settings"]["sounds"]["mute_others"], True)
        self.assertIs(json.loads(self.cfg_path.read_text(
            encoding="utf-8"))["sounds"]["mute_others"], True)
        self.assertIs(self.app.cfg["sounds"]["mute_others"], True)
        self.assertIs(self.ok("GET", "/api/config")["settings"]["sounds"]
                      ["mute_others"], True)
        res = self.ok("POST", "/api/settings",
                      {"sounds": {"mute_others": False}})
        self.assertIs(self.app.cfg["sounds"]["mute_others"], False)
        for bad in ({"volume": True}, {"volume": "loud"}, {"enabled": "yes"},
                    {"mute_others": "yes"}, {"mute_others": 1},
                    {"loud": 1}, {}, "on"):
            self.assertEqual(self.post("/api/settings", {"sounds": bad})[0],
                             400, bad)
        # Out of range is clamped by the engine's rule (validate_change).
        res = self.ok("POST", "/api/settings", {"sounds": {"volume": 2}})
        self.assertEqual(res["settings"]["sounds"]["volume"], 1.0)

    def test_play_sound_and_retry(self):
        status, data, _ = self.post("/api/sounds/play", {"which": "start"})
        self.assertEqual(status, 501)
        self.assertIn("not available", data["error"])
        played = []
        self.app.play_sound = played.append
        self.ok("POST", "/api/sounds/play", {"which": "stop"})
        self.assertEqual(played, ["stop"])
        self.assertEqual(self.post("/api/sounds/play", {"which": "x"})[0], 400)
        self.assertEqual(self.post("/api/engine/retry", {})[0], 501)
        retried = []
        self.app.retry_engine = lambda: retried.append(1)
        self.assertEqual(self.post("/api/engine/retry", {})[0], 409)  # ready
        self.app.status = "failed"
        self.ok("POST", "/api/engine/retry", {})
        self.assertEqual(retried, [1])
        self.assertTrue(self.ok("GET", "/api/state")["can"]["retry"])

    def test_engine_answers_are_reported(self):
        self.app.status = "failed"
        self.app.retry_engine = lambda: False          # a load is running
        status, data, _ = self.post("/api/engine/retry", {})
        self.assertEqual(status, 409)
        self.assertIn("already trying", data["error"])
        self.app.play_sound = lambda kind: False        # e.g. dry-run
        status, data, _ = self.post("/api/sounds/play", {"which": "start"})
        self.assertEqual(status, 409)
        self.assertIn("test mode", data["error"])

    def test_pause_shortcut_inside_the_main_key_is_refused(self):
        self.ok("POST", "/api/settings", {"hotkey": "ctrl+windows"})
        status, data, _ = self.post("/api/settings", {"pause_hotkey": "ctrl"})
        self.assertEqual(status, 400)
        self.assertIn("part of your hold-to-talk key", data["error"])
        self.ok("POST", "/api/settings", {"pause_hotkey": "ctrl+f9"})
        status, data, _ = self.post("/api/settings", {"hotkey": "ctrl+f9+shift"})
        self.assertEqual(status, 400)
        self.assertIn("includes your pause shortcut", data["error"])

    def test_microphone_problem_stays_until_it_is_over(self):
        self.app.mic_problem = "The chosen microphone is not connected."
        for fn in self.app.on_notice:
            fn("mic", self.app.mic_problem)
        self.record("zorvex", "Zorvex.")               # a dictation works
        state = self.ok("GET", "/api/state")
        self.assertEqual(state["live"]["mic_problem"], self.app.mic_problem)
        self.assertIn("mic", [n["kind"] for n in state["notices"]])
        self.app.mic_problem = None
        for fn in self.app.on_notice:
            fn("mic", "The microphone works again.")
        state = self.ok("GET", "/api/state")
        self.assertIsNone(state["live"]["mic_problem"])
        self.assertEqual([n["message"] for n in state["notices"]
                          if n["kind"] == "mic"], ["The microphone works again."])

    def test_cleanup_host_shown(self):
        view = self.ok("GET", "/api/config")["settings"]
        self.assertTrue(view["cleanup_local"])
        self.cfg_path.write_text(json.dumps({"cleanup": {
            "url": "http://gpu-box.example:11434"}}), encoding="utf-8")
        self.app.reload_config()
        view = self.ok("GET", "/api/config")["settings"]
        self.assertFalse(view["cleanup_local"])
        self.assertEqual(view["cleanup_host"], "gpu-box.example")
        state = self.ok("GET", "/api/state")["cleanup"]
        self.assertEqual((state["host"], state["local"], state["allowed"]),
                         ("gpu-box.example", False, False))   # refused
        self.assertFalse(view["cleanup_allowed"])
        self.cfg_path.write_text(json.dumps({"cleanup": {
            "url": "http://gpu-box.example:11434", "allow_remote": True}}),
            encoding="utf-8")
        self.app.reload_config()                        # the removed key: ignored
        view = self.ok("GET", "/api/config")["settings"]
        self.assertFalse(view["cleanup_allowed"])
        js = (Path(api.__file__).parent / "ui" / "app.js").read_text(
            encoding="utf-8")
        self.assertNotIn("allow_remote", js)
        self.assertNotIn("your words leave this computer", js)
        for url in ("http://localhost:11434", "http://[::1]:11434",
                    "http://127.0.0.1:9"):
            self.assertTrue(api.cleanup_where(
                {"cleanup": {"url": url}})["local"], url)

    def test_capture_says_why_it_ended(self):
        self.ok("POST", "/api/hotkey/capture", {})
        self.ok("POST", "/api/hotkey/cancel", {})
        cap = self.ok("GET", "/api/state")["live"]["capture"]
        self.assertEqual((cap["state"], cap["reason"]), ("cancelled", "user"))
        self.ok("POST", "/api/hotkey/capture", {})
        self.app.capture_cb(None)                      # nothing pressed
        cap = self.ok("GET", "/api/state")["live"]["capture"]
        self.assertEqual((cap["reason"], cap["seconds"]), ("timeout", 10.0))


class Lists(ServerCase):
    def test_history_paging_and_match_count(self):
        # W11: only the newest 300 were shown and the header said "N saved"
        # whatever the search matched.
        base = time.time() - 100
        for i in range(7):
            self.record(f"zorvex note {i}" if i % 2 else f"plain {i}",
                        f"Note {i}.", ts=base + i)
        res = self.ok("GET", "/api/history?limit=3")
        self.assertEqual((len(res["items"]), res["matches"], res["count"]),
                         (3, 7, 7))
        res = self.ok("GET", "/api/history?limit=2&q=zorvex")
        self.assertEqual((len(res["items"]), res["matches"]), (2, 3))
        res = self.ok("GET", "/api/history?limit=1000&q=zorvex")
        self.assertEqual(len(res["items"]), 3)

    def test_glossary_names_ignored_terms(self):
        # W12: "44 of 75 hints fit" without saying which.
        words = [f"Zorvexterm{i:02d}" for i in range(80)]
        self.cfg_path.write_text(json.dumps({"vocabulary": words}),
                                 encoding="utf-8")
        self.app.reload_config()
        g = self.ok("GET", "/api/config")["glossary"]
        self.assertLess(g["used"], g["total"])
        self.assertEqual(g["ignored"], words[g["used"]:])

    def test_delete_drops_the_dictation_from_home(self):
        # W17: a dictation deleted in History stayed on Home.
        ts = self.record("zorvex", "Zorvex.")
        row = self.ok("POST", "/api/history/find", {"ts": ts})["row"]
        self.assertEqual(len(self.ok("GET", "/api/feed")["items"]), 1)
        q = self.server.hub.subscribe()
        self.ok("POST", "/api/history/delete", {"id": row["id"]})
        self.assertEqual(self.ok("GET", "/api/feed")["items"], [])
        events = [q.get_nowait() for _ in range(q.qsize())]
        self.assertIn("history", [e[0] for e in events if e])
        self.server.hub.unsubscribe(q)

    def test_messages_name_fields_as_the_user_sees_them(self):
        # W9: "'heard' is too long."
        status, data, _ = self.post("/api/dictionary/add", {
            "heard": "x" * 300, "wanted": "y"})
        self.assertEqual(status, 400)
        self.assertNotIn("'heard'", data["error"])
        self.assertIn("When it hears", data["error"])

    def test_dictionary_write_is_trimmed(self):
        self.ok("POST", "/api/dictionary/add", {"heard": "zorvecks",
                                                "wanted": "  Zorvex  "})
        self.assertIn({"heard": "zorvecks", "wanted": "Zorvex"},
                      self.app.cfg["dictionary"])


class LogViewer(ServerCase):
    """S5: the log viewer never shows the secret or a ticket, and Clear
    history also clears the lines it holds."""

    def test_no_secret_or_ticket(self):
        import logging
        url = self.server.launch_url()
        ticket = re.search(r"#t=([^&]+)", url).group(1)
        logging.getLogger("holdtotalk.test").warning(
            "oops %s %s", self.server.secret, ticket)
        lines = "\n".join(self.ok("GET", "/api/log")["lines"])
        self.assertIn("oops", lines)
        self.assertNotIn(self.server.secret, lines)
        self.assertNotIn(ticket, lines)

    def test_clear_history_clears_log_lines(self):
        import logging
        logging.getLogger("holdtotalk.test").warning("dictated: zorvex quux")
        self.assertTrue(self.ok("GET", "/api/log")["lines"])
        self.ok("POST", "/api/history/clear", {"confirm": True})
        lines = self.ok("GET", "/api/log")["lines"]
        self.assertFalse(any("zorvex" in line for line in lines))


class WindowLink(ServerCase):
    def test_server_tells_the_window_whether_a_page_is_connected(self):
        win = type("W", (), {"mode": "app", "is_connected": None})()
        self.server.stop()
        self.server = api.Server(self.app, self.history, autostart_name=None,
                                 config_path=self.cfg_path, window=win).start()
        self.host = f"127.0.0.1:{self.server.port}"
        self.key = self.hello()
        self.assertFalse(win.is_connected())
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port,
                                          timeout=5)
        conn.request("GET", "/api/events", headers={
            "Host": self.host, api.KEY_HEADER: self.key})
        resp = conn.getresponse()
        self.assertEqual(resp.fp.readline(), b"event: live\n")
        self.assertTrue(win.is_connected())
        conn.close()

    def test_tray_change_reaches_the_window(self):
        self.assertIn(self.server._health_wake.set, tray.CHANGE_HOOKS)
        self.server._health_wake.clear()
        tray._changed()
        self.assertTrue(self.server._health_wake.is_set())
        other = api.Server(FakeApp(self.cfg_path), None,
                           autostart_name=None).start()
        self.assertIn(other._health_wake.set, tray.CHANGE_HOOKS)
        other.stop()
        self.assertNotIn(other._health_wake.set, tray.CHANGE_HOOKS)


if __name__ == "__main__":
    unittest.main()
