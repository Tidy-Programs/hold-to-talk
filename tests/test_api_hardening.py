"""Second-pass hardening of the window's server (api.py): a real deadline per
connection (W1), requests that cannot raise (W2, W6), the Copy button's
clipboard route (W3). Synthetic data; temporary files; the clipboard is the
in-memory fake and the microphone a stub."""

import contextlib
import http.client
import io
import json
import logging
import socket
import threading
import time
import unittest
from unittest import mock

import api
import keys
import test_api


def _closed_by_server(sock, until, send=b"E", every=0.25):
    """Trickle `send` every `every` s until `until` (monotonic); return the
    time the server closed the connection, or None if it never did."""
    start = time.monotonic()
    sock.settimeout(0.05)
    while time.monotonic() < until:
        try:
            sock.sendall(send)
        except OSError:
            return time.monotonic() - start
        try:
            if sock.recv(4096) == b"":
                return time.monotonic() - start
        except socket.timeout:
            pass
        except OSError:
            return time.monotonic() - start
        time.sleep(every)
    return None


class Deadlines(test_api.ServerCase):
    """W1: slow connections are closed even while bytes keep arriving."""

    def serve(self, **kw):
        self.server.stop()
        self.server = api.Server(self.app, self.history,
                                 config_path=self.cfg_path, autostart_name=None,
                                 clipboard=self.clipboard, **kw).start()
        self.host = f"127.0.0.1:{self.server.port}"
        self.key = self.hello()

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.server.port), 5)

    def test_a_trickling_request_is_closed_at_the_deadline(self):
        # One byte every 0.25 s never lets a per-read timeout (1.0 s) fire.
        self.serve(header_timeout=1.0)
        s = self.connect()
        self.addCleanup(s.close)
        s.sendall(b"G")
        closed = _closed_by_server(s, time.monotonic() + 4)
        self.assertIsNotNone(closed, "the server kept a trickling request")
        self.assertLess(closed, 2.5)

    def test_headers_trickling_in_are_closed_too(self):
        self.serve(header_timeout=1.0)
        s = self.connect()
        self.addCleanup(s.close)
        s.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n")
        closed = _closed_by_server(s, time.monotonic() + 4, b"X")
        self.assertIsNotNone(closed)
        self.assertLess(closed, 2.5)

    def test_a_trickling_body_is_cut_off(self):
        self.serve(header_timeout=1.0, body_timeout=1.0)
        s = self.connect()
        self.addCleanup(s.close)
        s.sendall((f"POST /api/pause HTTP/1.1\r\nHost: {self.host}\r\n"
                   f"Origin: {self.server.origin}\r\n"
                   "Content-Type: application/json\r\n"
                   f"{api.KEY_HEADER}: {self.key}\r\n"
                   "Content-Length: 200\r\n\r\n{").encode())
        closed = _closed_by_server(s, time.monotonic() + 5, b" ")
        self.assertIsNotNone(closed, "a slow body was never cut off")
        self.assertLess(closed, 3.5)
        self.assertFalse(self.app.paused)

    def test_a_normal_request_and_a_kept_alive_one_are_unaffected(self):
        self.serve(header_timeout=1.0, body_timeout=1.0)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port,
                                          timeout=5)
        for _ in range(3):                      # keep-alive reuse
            conn.request("GET", "/api/state", headers={
                "Host": self.host, api.KEY_HEADER: self.key})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 200)
            time.sleep(0.3)
        conn.close()

    def test_slots_recycle_and_legitimate_requests_get_through(self):
        # The reviewer's attack, scaled down: every slot is held by a
        # connection sending a byte every 0.25 s (under the 1.5 s timeout).
        self.serve(max_connections=8, header_timeout=1.5)
        socks = []
        stop = threading.Event()
        try:
            for _ in range(12):
                s = self.connect()
                s.sendall(b"G")
                socks.append(s)

            def drip():
                while not stop.is_set():
                    for s in list(socks):
                        try:
                            s.sendall(b"E")
                        except OSError:
                            pass
                    stop.wait(0.25)
            threading.Thread(target=drip, daemon=True).start()
            time.sleep(0.2)
            self.assertEqual(self.server.httpd.in_progress, 8)
            ok_at = None
            t0 = time.monotonic()
            while time.monotonic() - t0 < 5 and ok_at is None:
                try:
                    if self.get("/api/state")[0] == 200:
                        ok_at = time.monotonic() - t0
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(0.2)
            self.assertIsNotNone(ok_at, "a real request never got through")
            self.assertLess(ok_at, 3.0)
            # and the slow connections are gone, not just outnumbered
            time.sleep(0.5)
            self.assertLessEqual(self.server.httpd.in_progress, 1)
        finally:
            stop.set()
            for s in socks:
                s.close()

    def test_a_live_stream_outlives_the_deadlines(self):
        self.serve(header_timeout=0.6, body_timeout=0.6)
        stream = http.client.HTTPConnection("127.0.0.1", self.server.port,
                                            timeout=10)
        self.addCleanup(stream.close)
        stream.request("GET", "/api/events", headers={
            "Host": self.host, api.KEY_HEADER: self.key})
        resp = stream.getresponse()
        self.assertEqual(resp.fp.readline(), b"event: live\n")
        time.sleep(2.0)                         # 3 x the header deadline
        threading.Timer(0.1, lambda: self.record("later", "Later.")).start()
        lines, end = [], time.time() + 5
        while time.time() < end and b"event: dictation\n" not in lines:
            lines.append(resp.fp.readline())
        self.assertIn(b"event: dictation\n", lines)


class Unraisable(test_api.ServerCase):
    """W2 and W6: nothing a request holds can raise out of the handler, and
    nothing the user typed is written to a log or to standard error."""

    SURROGATES = ('{"heard":"\\ud800","wanted":"x","term":"\\ud800",'
                  '"text":"\\ud800","trigger":"\\udfff","old":"\\ud800",'
                  '"new":"a","old_heard":"\\ud800","old_trigger":"\\ud800",'
                  '"editing":"\\ud800","sample":"\\ud800","id":1}').encode()

    def test_lone_surrogate_gets_a_400_on_every_route(self):
        skip = {"hotkey/capture", "mic/test", "history/clear"}
        for name in api.POST_ROUTES:
            if name in skip:
                continue
            status, data, _ = self.post("/api/" + name, raw=self.SURROGATES)
            self.assertEqual(status, 400, (name, data))
            self.assertIn("error", data)
        self.assertEqual(self.post("/api/hello", raw=self.SURROGATES,
                                   key=False)[0], 400)

    def test_surrogate_in_a_key_or_nested_value(self):
        for raw in (b'{"\\ud800": 1}', b'{"a": ["x", {"b": "\\ud83d"}]}'):
            self.assertEqual(self.post("/api/settings", raw=raw)[0], 400)

    def test_has_surrogate_handles_deep_nesting(self):
        value = "end"
        for _ in range(5000):
            value = [value]
        self.assertFalse(api.has_surrogate(value))
        self.assertTrue(api.has_surrogate([value, "\ud800"]))

    def test_an_answer_always_encodes(self):
        # Even a value that got past the door (here: stored text) is sent.
        with mock.patch.dict(api.GET_ROUTES, {
                "log": lambda srv, q: {"lines": ["bad \ud800 text"]}}):
            status, data, _ = self.get("/api/log")
        self.assertEqual(status, 200)
        self.assertEqual(data["lines"], ["bad \ud800 text"])   # the escape, parsed

    def test_an_unexpected_exception_logs_class_and_traceback_only(self):
        secret = "dictated-" + "words-" + "98765"

        def boom(srv, q):
            raise ValueError(secret)
        stderr = io.StringIO()
        with mock.patch.dict(api.GET_ROUTES, {"log": boom}), \
                contextlib.redirect_stderr(stderr), \
                self.assertLogs("holdtotalk.api", "ERROR") as logs:
            status, data, _ = self.get("/api/log")
        self.assertEqual(status, 500)
        text = "\n".join(logs.output)
        self.assertIn("ValueError", text)
        self.assertIn("boom", text)                 # the traceback is there
        self.assertNotIn(secret, text)
        self.assertNotIn(secret, json.dumps(data))
        self.assertEqual(stderr.getvalue(), "")

    def test_the_servers_error_hook_is_quiet_and_filtered(self):
        secret = "typed-" + "text-" + "13579"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                self.assertLogs("holdtotalk.api", "ERROR") as logs:
            try:
                raise RuntimeError(secret)
            except RuntimeError:
                self.server.httpd.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(stderr.getvalue(), "")
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(secret, text)

    def test_a_client_that_went_away_is_not_an_error(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            try:
                raise ConnectionResetError("gone")
            except ConnectionResetError:
                self.server.httpd.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(stderr.getvalue(), "")

    def test_exceptions_raised_while_answering_do_not_escape(self):
        class Odd:                                  # json cannot encode this
            pass
        secret = "odd-" + "value-" + "24680"
        stderr = io.StringIO()
        with mock.patch.dict(api.GET_ROUTES, {
                "log": lambda srv, q: {"x": Odd(), "s": secret}}), \
                contextlib.redirect_stderr(stderr), \
                self.assertLogs("holdtotalk.api", "ERROR") as logs:
            status, data, _ = self.get("/api/log")
        self.assertEqual(status, 500)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(secret, "\n".join(logs.output))


class Fuzz(test_api.ServerCase):
    """W2: every route, many kinds of wrong input: always an answer (never a
    dropped connection), never a 500, never anything on standard error, and
    nothing in the error log."""

    FIELDS = ("heard", "wanted", "old_heard", "term", "old", "new", "editing",
              "trigger", "old_trigger", "text", "sample", "cleanup_model",
              "color", "background", "id", "history_id", "ticket", "which",
              "enabled", "paused", "confirm", "device", "state", "seconds",
              "hotkey", "pause_hotkey", "language", "q", "limit", "key",
              "value", "settings", "overlay", "sounds", "cleanup", "items")
    SINGLE = ("heard", "text", "id", "enabled", "device", "settings", "limit",
              "confirm", "trigger", "state")
    VALUES = (
        None, True, False, 0, -1, 1, 2 ** 70, -2 ** 70, 1.5, 1e308,
        float("nan"), float("inf"), "", " ", "a", "x" * 5000, "x" * 70000,
        "\x00", "a\x00b", "‮ rtl", "\U0001F600 emoji", "́",
        "../../etc", "<script>", "{{APP_NAME}}", "%s %d {0}", "\r\nX: y",
        "f23", "ctrl+shift+a", "no such mic", "🙂" * 100,
        [], [1, 2], ["a", None], [[[]]], {}, {"a": 1}, {"a": {"b": [None]}},
        "1" * 5000,
    )

    def setUp(self):
        super().setUp()
        self.mic = mock.patch("audio.Recorder", side_effect=OSError("no mic"))
        self.mic.start()                            # never open a microphone
        self.addCleanup(self.mic.stop)
        self.record("hello there", "Hello there.")

    @staticmethod
    def dump(value):
        # json.dumps allows NaN/Infinity by default: sent as the bare words.
        return json.dumps(value)

    def bodies(self):
        yield b""
        yield b"[]"
        yield b"null"
        yield b"123"
        yield b'"text"'
        yield b"{"
        yield b"\xff\xfe\xfa"
        yield b'{"a": NaN, "b": Infinity, "c": -Infinity}'
        yield b'{"x": ' + b"[" * 3000 + b"]" * 3000 + b"}"
        for v in self.VALUES:
            yield self.dump({f: v for f in self.FIELDS}).encode()
        for f in self.SINGLE:
            for v in self.VALUES:
                yield self.dump({f: v}).encode()

    def test_every_post_route_answers_and_never_500s(self):
        stderr = io.StringIO()
        routes = [r for r in api.POST_ROUTES if r != "hello"]
        bad = []
        sent = 0
        headers = {"Host": self.host, "Origin": self.server.origin,
                   "Content-Type": "application/json",
                   api.KEY_HEADER: self.key}
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port,
                                          timeout=10)
        self.addCleanup(conn.close)
        with contextlib.redirect_stderr(stderr), \
                self.assertLogs("holdtotalk", "INFO") as logs:
            logging.getLogger("holdtotalk.api").info("probe")
            for name in routes:
                for raw in self.bodies():
                    for attempt in (1, 2):      # one connection, reused
                        try:
                            conn.request("POST", "/api/" + name, body=raw,
                                         headers=headers)
                            resp = conn.getresponse()
                            data = resp.read()
                            status = resp.status
                            break
                        except (OSError, http.client.HTTPException) as exc:
                            conn.close()        # the server closed it: retry
                            if attempt == 2:
                                bad.append((name, raw[:60],
                                            type(exc).__name__))
                                status = None
                    if status is None:
                        continue
                    sent += 1
                    if status >= 500 and status != 501:
                        bad.append((name, raw[:60], status, data[:80]))
        self.assertEqual(bad[:5], [], f"{len(bad)} bad of {sent}")
        self.assertGreater(sent, 5000)
        self.assertEqual(stderr.getvalue(), "")
        errors = [x for x in logs.output
                  if x.startswith("ERROR:holdtotalk.api")]
        self.assertEqual(errors, [])

    def test_every_get_route_answers_and_never_500s(self):
        queries = ["", "?q=%ED%A0%80", "?limit=-5", "?limit=" + "9" * 5000,
                   "?limit=abc&q=" + "x" * 5000, "?q=%00%FF", "?a=1&a=2",
                   "?q=%F0%9F%98%80", "?%FF=1", "?limit=1e3", "?limit=0"]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            for name in ("state", "feed", "history", "config", "log"):
                for qs in queries:
                    status, data, _ = self.get("/api/" + name + qs)
                    self.assertIn(status, (200, 400), (name, qs, data))
        self.assertEqual(stderr.getvalue(), "")

    def test_odd_requests_get_an_answer(self):
        raws = [
            b"GET /api/state HTTP/1.1\r\nHost: %s\r\n\r\n",
            b"GET /%ED%A0%80 HTTP/1.1\r\nHost: %s\r\n\r\n",
            b"GET /api/%00state HTTP/1.1\r\nHost: %s\r\n\r\n",
            b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\nHost: %s\r\n\r\n",
            b"BREW /api/state HTTP/1.1\r\nHost: %s\r\n\r\n",
            b"POST /api/pause HTTP/1.1\r\nHost: %s\r\nContent-Length: x\r\n\r\n",
            b"GET /api/state HTTP/1.1\r\nHost: %s\r\nX-HTT-Key: \xff\xfe\r\n\r\n",
            b"\x00\x01\x02\r\n\r\n",
            b"GET\r\n\r\n",
        ]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                self.assertLogs("holdtotalk", "INFO") as logs:
            logging.getLogger("holdtotalk.api").info("probe")
            for raw in raws:
                with socket.create_connection(
                        ("127.0.0.1", self.server.port), 5) as s:
                    s.sendall(raw.replace(b"%s", self.host.encode())
                              if raw.count(b"%s") == 1 else raw)
                    s.settimeout(3)
                    try:
                        got = s.recv(4096)
                    except OSError:
                        got = b""
                    self.assertFalse(got.startswith(b"HTTP/1.1 500"), raw[:40])
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual([x for x in logs.output
                          if x.startswith("ERROR:holdtotalk.api")], [])


class ClipboardCopy(test_api.ServerCase):
    """W3: the Copy button's text goes through the engine's clipboard class,
    with the markers that keep it out of history and cloud sync."""

    def test_text_is_placed_with_the_private_markers(self):
        status, data, _ = self.post("/api/clipboard/copy",
                                    {"text": "Dear Sam, héllo 🙂"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["private"])
        items = self.clipboard.items
        by_name = {name: (fmt, blob) for fmt, name, blob in items}
        self.assertEqual(items[0][0], keys.CF_UNICODETEXT)
        self.assertEqual(items[0][2].decode("utf-16-le").rstrip("\0"),
                         "Dear Sam, héllo 🙂")
        for name in ("ExcludeClipboardContentFromMonitorProcessing",
                     "CanIncludeInClipboardHistory",
                     "CanUploadToCloudClipboard"):
            self.assertIn(name, by_name)
            self.assertIsNone(by_name[name][0])        # registered by name
        self.assertEqual(len(self.clipboard.writes), 1)

    def test_needs_the_key_and_valid_text(self):
        self.assertEqual(self.post("/api/clipboard/copy", {"text": "x"},
                                   key=False)[0], 401)
        self.assertEqual(self.clipboard.writes, [])
        for body in ({}, {"text": 5}, {"text": None}, {"text": "  \n"},
                     {"text": ""}, {"text": "x" * (api.CLIPBOARD_MAX + 1)}):
            self.assertEqual(self.post("/api/clipboard/copy", body)[0], 400,
                             body)
        self.assertEqual(self.clipboard.writes, [])

    def test_a_busy_clipboard_is_a_409_with_a_plain_sentence(self):
        def busy(_):
            raise OSError("Could not open the clipboard (error 5)")
        self.clipboard.on_write = busy
        status, data, _ = self.post("/api/clipboard/copy", {"text": "words"})
        self.assertEqual(status, 409)
        self.assertIn("clipboard", data["error"])
        self.assertNotIn("error 5", data["error"])

    def test_the_page_falls_back_and_says_so(self):
        js = (api.UI_DIR / "app.js").read_text(encoding="utf-8")
        start = js.index("async copy(text)")
        body = js[start:js.index("remove(r, el)", start)]
        self.assertIn("/api/clipboard/copy", body)
        self.assertLess(body.index("/api/clipboard/copy"),
                        body.index("navigator.clipboard"))
        self.assertIn("could not keep it out of Windows clipboard history",
                      body)


class NewStates(test_api.ServerCase):
    """W9: what the engine now reports, and the controls for it."""

    def live(self):
        return self.ok("GET", "/api/state")["live"]

    def test_language_hint_is_shown_only_while_the_language_is_automatic(self):
        self.assertIsNone(self.live()["language_hint"])
        self.app.language_hint = "de"
        hint = self.live()["language_hint"]
        self.assertEqual(hint["code"], "de")
        self.assertTrue(hint["name"])
        self.ok("POST", "/api/settings", {"language": "de"})
        self.assertIsNone(self.live()["language_hint"])     # already chosen
        self.ok("POST", "/api/settings", {"language": None})
        self.app.language_hint = "zz-not-a-language"
        self.assertIsNone(self.live()["language_hint"])

    def test_a_held_recording_can_be_tried_again(self):
        self.assertIsNone(self.live()["held_seconds"])
        self.assertEqual(self.post("/api/recording/retry")[0], 501)
        calls = []
        self.app.retry_held_recording = lambda: calls.append(1) or True
        self.assertEqual(self.post("/api/recording/retry")[0], 200)
        self.assertEqual(calls, [1])
        self.app.held_recording = {"seconds": 3.24, "expires_in": 250.0}
        self.assertEqual(self.live()["held_seconds"], 3.2)
        self.app.retry_held_recording = lambda: False
        status, data, _ = self.post("/api/recording/retry")
        self.assertEqual(status, 409)
        self.assertIn("speech model", data["error"])

    def test_listener_state_reaches_the_window(self):
        info = {"host": "127.0.0.1", "local": True, "allowed": True,
                "listener": "unknown", "listener_blocked": True,
                "listener_detail": "the Windows account answering on the AI "
                                   "cleanup port could not be determined",
                "trust_listener": False}
        with mock.patch("cleanup.endpoint_info", return_value=info):
            cleanup_state = self.ok("GET", "/api/state")["cleanup"]
        self.assertEqual(cleanup_state["listener"], "unknown")
        self.assertTrue(cleanup_state["listener_blocked"])
        self.assertIn("could not be determined",
                      cleanup_state["listener_detail"])
        # an older cleanup.py without the keys: plain defaults
        with mock.patch("cleanup.endpoint_info", return_value={
                "host": "127.0.0.1", "local": True, "allowed": True}):
            cleanup_state = self.ok("GET", "/api/state")["cleanup"]
        self.assertFalse(cleanup_state["listener_blocked"])

    def test_trust_switch(self):
        import cleanup
        if "trust_listener" not in cleanup.DEFAULTS:
            self.skipTest("cleanup.trust_listener has not landed")
        view = self.ok("GET", "/api/config")["settings"]
        self.assertTrue(view["cleanup_trust_available"])
        self.assertFalse(view["cleanup_trust_listener"])
        self.ok("POST", "/api/settings", {"cleanup_trust_listener": True})
        saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertIs(saved["cleanup"]["trust_listener"], True)
        self.assertTrue(self.ok("GET", "/api/config")["settings"]
                        ["cleanup_trust_listener"])
        self.assertEqual(self.post("/api/settings",
                                   {"cleanup_trust_listener": "yes"})[0], 400)

    def test_less_graphics_memory_switch(self):
        import settings
        view = self.ok("GET", "/api/config")["settings"]
        if not view["less_graphics_memory_available"]:
            self.skipTest("compute_type is not in settings")
        self.assertFalse(view["less_graphics_memory"])
        self.ok("POST", "/api/settings", {"less_graphics_memory": True})
        saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertIn(saved["compute_type"], settings.COMPUTE_TYPES)
        self.assertTrue(self.ok("GET", "/api/config")["settings"]
                        ["less_graphics_memory"])
        self.ok("POST", "/api/settings", {"less_graphics_memory": False})
        saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["compute_type"], "auto")
        self.assertEqual(self.post("/api/settings",
                                   {"less_graphics_memory": 1})[0], 400)

    def test_page_words_for_the_new_states(self):
        js = (api.UI_DIR / "app.js").read_text(encoding="utf-8")
        # a refused listener is checked before "blocked", in both places
        self.assertLess(js.index("cl?.listener_blocked"),
                        js.index("if (blocked) return"))
        self.assertLess(js.index("refused) {"), js.index("blocked) {"))
        for needle in ("another Windows account is answering",
                       "could not be determined",
                       "untrusted listener", "not ready",
                       "your own words were pasted",
                       "Try that recording again", "You always dictate in",
                       "Choosing it makes each dictation a little faster",
                       "Use less graphics memory", "AI cleanup is unavailable",
                       "Allow this program"):
            self.assertIn(needle, js)


if __name__ == "__main__":
    unittest.main()



