"""The app window's back end: the UI's static files (ui/) and a small JSON
API with a live event stream, served on the loopback interface from
background threads of the dictation process.

    server = api.Server(app, history)
    server.start()
    url = server.launch_url()       # one-time URL for the window
    server.stop()

Security (the history holds everything the user ever dictated)
- Listens on 127.0.0.1 only; any other address is refused at construction.
- Every API request must carry the per-run secret in the X-HTT-Key header,
  compared in constant time, and it is checked before a request body is
  parsed (only the ticket exchange reads a body without it). The secret is
  never logged and never put in a URL. The window gets it like this:
  launch_url() makes a one-time ticket (random, single use, valid
  TICKET_SECONDS) and puts it in the URL *fragment*, which browsers never
  send to a server or in a Referer; the page POSTs it to /api/hello and
  receives the secret, keeps it in sessionStorage (per origin, per window)
  and removes the fragment. A ticket that leaks afterwards (browser
  history, command line) is useless. No cookies: a browser would send a
  127.0.0.1 cookie to every local port.
- Host must be exactly 127.0.0.1:<port> (DNS rebinding). An Origin header,
  when present, must be this server's origin; POST requires it, plus
  Content-Type application/json (so no plain HTML form can post), and a
  JSON object body of at most MAX_BODY bytes. Any body that does not parse
  is a 400 (never a 500 with a traceback in the log). Sec-Fetch-Site, when
  sent, must be same-origin or none. No CORS headers are ever sent, so
  other sites cannot read responses; other methods get 501.
- At most MAX_CONNECTIONS requests are read or handled at once (further
  connections are closed at once). Each connection has a real deadline,
  not a per-read timeout: its request line and headers must be complete
  within HEADER_TIMEOUT and its body within BODY_TIMEOUT of the headers,
  however slowly bytes trickle in, so a local program cannot hold the
  slots (or threads) by sending a byte now and then. Event streams are not
  counted once they are authenticated.
- Nothing a request contains can raise out of the handler: lone surrogate
  characters in a JSON body are refused with a 400, answers are always
  encodable, and an unexpected exception is logged by class and traceback
  only (never its message, which may hold the user's text) - also the
  server's own error hook, so nothing goes to standard error.
- Static files come from a fixed whitelist (no path is ever joined from a
  request) and are the repository's own UI code; they hold no user data,
  so they are served without the secret (a page load cannot carry a
  header). All data goes through /api/ and needs the secret.
- The Terms of Use text is not a static file: GET /api/terms needs the
  secret and reads the one fixed file TERMS.md (terms.py), never a path from
  a request. POST /api/terms/accept writes the acceptance record (only for
  the version this app was built with, and only when TERMS.md matches it);
  POST /api/quit works only while the terms are not accepted (the terms
  screen's Quit button).
- No endpoint reads or writes a file chosen by the request, or runs a
  command. Config changes go through settings.apply_changes, history
  changes through textrules.History. The log viewer's lines never contain
  the secret or a ticket, and "Clear history" also clears them.

Never blocking the engine
- Requests run on their own daemon threads. The Tk thread never waits for
  them: changes hop to it only inside app.reload_config() (milliseconds),
  history access uses the History object's own lock, and Ollama / device
  checks run on a separate "ui-health" thread.
- Live events go to per-client bounded queues; a client that stops reading
  is dropped, and nothing the dictation path calls ever waits on a client.

Errors the user sees are plain sentences (what happened, what to do); the
technical detail goes to the log only.
"""

import collections
import hmac
import http.server
import io
import ipaddress
import json
import logging
import queue
import re
import secrets
import socket
import socketserver
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import audio
import cleanup
import keys
import learn
import settings
import stt
import terms
import textrules
import tray

log = logging.getLogger("holdtotalk.api")

UI_DIR = Path(__file__).resolve().with_name("ui")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")
KEY_HEADER = "X-HTT-Key"
TICKET_SECONDS = 120
MAX_BODY = 256 * 1024
MAX_CONNECTIONS = 48            # requests being read / handled at once
MAX_STREAMS = 16                # live event streams at once
HEADER_TIMEOUT = 5.0            # s, in all, for a request's line and headers to
                                # arrive (also an idle keep-alive connection)
BODY_TIMEOUT = 10.0             # s, in all, for the body once the headers are in
WRITE_TIMEOUT = 10.0            # s a client may take to accept an answer
PRESSURE_SECONDS = 1.0          # headers deadline while 3/4 of the slots are used
DRAIN_LIMIT = 4 * 1024 * 1024   # a refused body up to this size is read away
DRAIN_SECONDS = 2.0
CONNECTED_GRACE = 15.0          # s a page may be reconnecting and still count
FEED_SIZE = 50
LOG_LINES = 400
HEALTH_SECONDS = 10.0
LEVEL_SECONDS = 0.06
STATE_SECONDS = 0.2
MIC_TEST_SECONDS = 3.0
CAPTURE_SECONDS = 10.0
HISTORY_MAX_LIMIT = 100000
CLIPBOARD_MAX = 200000          # characters the Copy button may place
SAMPLE_SENTENCE = "I sent the {heard} notes to everyone this morning."
SOUND_DEFAULTS = {"enabled": True, "volume": 0.5, "mute_others": False,
                  **{k: v for k, v in (settings.DEFAULTS.get("sounds") or {})
                     .items() if k in ("enabled", "volume",
                                       "mute_others")}}
_MODEL_NAME = re.compile(r"[A-Za-z0-9._:/\-]{1,100}")

# Request fields as the user knows them (for messages about them).
FIELD_LABELS = {
    "heard": "“When it hears”", "wanted": "“Write”", "old_heard": "The entry",
    "term": "The word", "old": "The word", "new": "The new spelling",
    "editing": "The item being edited", "trigger": "“When you say”",
    "old_trigger": "The snippet", "text": "The text",
    "sample": "The sample sentence", "cleanup_model": "The model name",
    "color": "The colour", "background": "The colour", "id": "That item",
    "history_id": "That dictation", "ticket": "The window link",
}

# What to tell the user when the engine refused a setting (the raw reason
# goes to the log).
_REFUSED = {
    "input_device": "That microphone is not available right now. Plug it in, "
                    "or choose another one.",
    "hotkey": "That key cannot be used to dictate. Choose another one.",
    "pause_hotkey": "That key cannot be used as the pause shortcut. Choose "
                    "another one.",
}


class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


def _bad(message):
    return ApiError(400, message)


def log_failure(what, exc):
    """Log an unexpected exception by class and traceback (code lines only),
    never its message: that may contain what the user dictated or typed."""
    try:
        frames = "".join(traceback.format_list(
            traceback.extract_tb(exc.__traceback__)))
    except Exception:
        frames = "(no traceback)\n"
    log.error("%s: %s\n%s", what, type(exc).__name__, frames.rstrip())


_SURROGATE = re.compile("[\ud800-\udfff]")


def has_surrogate(value):
    """True if any text in a parsed JSON value (or any key) holds a lone
    surrogate code unit (a JSON "\\ud800"): it cannot be stored, shown or
    sent back as UTF-8. Iterative: the value may be deeply nested."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _SURROGATE.search(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _label(name):
    return FIELD_LABELS.get(name, "That value")


def _text(body, name, limit, required=True):
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise _bad(f"{_label(name)} must be text.")
    if len(value) > limit:
        raise _bad(f"{_label(name)} is too long (at most {limit:,} "
                   "characters).")
    return value


def _bool(body, name):
    value = body.get(name)
    if not isinstance(value, bool):
        raise _bad("That setting can only be switched on or off.")
    return value


def _int(body, name):
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(f"{_label(name)} was not recognised. Reload the window "
                   "and try again.")
    return value


def plain_change_error(errors):
    """One sentence for the user about why a config change failed (the raw
    errors from settings.apply_changes go to the log)."""
    errors = [str(e) for e in errors]
    if any(e.startswith("Could not save") and "has an error" in e
           for e in errors):                    # settings.ConfigError
        return ("Your change could not be saved: the settings file has a "
                "mistake in it, so it was left as it is (a copy was kept next "
                "to it). Fix the file, or delete it to start again, then try "
                "again. Details are in the log.")
    if any(e.startswith("Could not save") and "could not be read" in e
           for e in errors):
        return ("Your change could not be saved: the settings file could not "
                "be read, so it was left as it is. Details are in the log.")
    if any(e.startswith("Could not save") for e in errors):
        return ("Your change could not be saved: the settings file is "
                "read-only or in use by another program. Nothing was "
                "changed. Details are in the log.")
    if any(e.startswith("Saved, but") for e in errors):
        return ("Your change was saved, but the app could not apply it just "
                "now. Restart the app to use it.")
    for e in errors:
        key = e.split(":", 1)[0].strip()
        if key in _REFUSED:
            return _REFUSED[key]
    return ("That change could not be applied, so nothing was changed. "
            "Details are in the log.")


def plain_hotkey_error(exc, pause=False):
    what = "the pause shortcut" if pause else "your hold-to-talk key"
    if "left and right mouse" in str(exc):
        return ("The left and right mouse buttons cannot be used. Use the "
                "middle button or a side button.")
    return f"That key cannot be used as {what}. Choose another one."


def _hotkey_groups(spec):
    """Comparable form of a hotkey spec, or None if it is not valid."""
    try:
        return frozenset(tuple(g) for g in keys.parse_hotkey(spec))
    except (ValueError, TypeError):
        return None


def _fallback_change(key, value):
    """The partial config for a key settings.validate_change did not handle
    (already checked by the caller)."""
    if key == "hotkey":
        return {"hotkey": settings.normalize_hotkey(value)}
    if key == "pause_hotkey":
        return {"pause_hotkey": settings.normalize_hotkey(value)
                if value not in (None, "") else None}
    if key == "language":
        return {"language": settings.normalize_language(value)}
    return {key: dict(value)}


def cleanup_where(cfg):
    """Where AI cleanup sends dictations: {"host", "local", "allowed"}
    (cleanup.endpoint_info; a non-local address is always refused)."""
    try:
        info = cleanup.endpoint_info(cfg)
        return {"host": str(info.get("host") or ""),
                "local": bool(info.get("local")),
                "allowed": bool(info.get("allowed")),
                # Which Windows account answers on the port (cleanup.py; the
                # keys are absent from an older cleanup.py).
                "listener": str(info.get("listener") or ""),
                "listener_blocked": bool(info.get("listener_blocked")),
                "listener_detail": str(info.get("listener_detail") or ""),
                "trust_listener": bool(info.get("trust_listener"))}
    except Exception as exc:
        log_failure("Reading the AI cleanup address failed", exc)
        return {"host": "", "local": False, "allowed": False, "listener": "",
                "listener_blocked": False, "listener_detail": "",
                "trust_listener": False}


LESS_GPU_MEMORY = "int8_float16"    # weights in 8 bits: about half the memory


def _compute_choice_available():
    """The "Use less graphics memory" switch exists only if this version of
    the engine has the compute_type setting and accepts that value."""
    return ("compute_type" in settings.DEFAULTS
            and LESS_GPU_MEMORY in getattr(settings, "COMPUTE_TYPES", ()))


def sounds_of(cfg):
    """The "sounds" section: {"enabled", "volume", "mute_others"}
    (sounds.settings_of)."""
    try:
        import sounds
        out = sounds.settings_of(cfg)
        return {"enabled": bool(out["enabled"]),
                "volume": float(out["volume"]),
                "mute_others": bool(out.get("mute_others", False))}
    except Exception:
        out = dict(SOUND_DEFAULTS)
        sub = cfg.get("sounds") if isinstance(cfg, dict) else None
        if isinstance(sub, dict):
            if isinstance(sub.get("enabled"), bool):
                out["enabled"] = sub["enabled"]
            if isinstance(sub.get("mute_others"), bool):
                out["mute_others"] = sub["mute_others"]
            v = sub.get("volume")
            if (isinstance(v, (int, float)) and not isinstance(v, bool)
                    and 0.0 <= v <= 1.0):
                out["volume"] = float(v)
        return out


# -- the live event hub -----------------------------------------------------------

class Hub:
    """Fan-out of (event, json) to connected windows. publish() never
    blocks: a client whose queue is full is disconnected."""

    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=400)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    @property
    def count(self):
        with self._lock:
            return len(self._subs)

    def publish(self, event, data):
        payload = json.dumps(data, separators=(",", ":"))
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait((event, payload))
            except queue.Full:
                self.unsubscribe(q)
                try:
                    q.get_nowait()
                    q.put_nowait(None)          # tells the stream to end
                except (queue.Empty, queue.Full):
                    pass

    def close(self):
        with self._lock:
            subs, self._subs = list(self._subs), set()
        for q in subs:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass


class _LogBuffer(logging.Handler):
    """The last LOG_LINES log lines, for the window's log viewer. redact(line)
    removes what must never be shown (the secret, tickets)."""

    def __init__(self, redact=None):
        super().__init__(logging.INFO)
        self.lines = collections.deque(maxlen=LOG_LINES)
        self.redact = redact
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            line = self.format(record)
            if self.redact is not None:
                line = self.redact(line)
            self.lines.append(line)
        except Exception:
            pass


# -- HTTP plumbing --------------------------------------------------------------------

class _HTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded, but with at most max_connections requests in progress:
    connections beyond that are closed straight away (a local program
    cannot pile up threads). A live event stream gives its slot back once
    it is authenticated (release_slot), so it never waits for one and never
    keeps others out."""
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    api = None

    def __init__(self, address, handler, max_connections=MAX_CONNECTIONS,
                 header_timeout=HEADER_TIMEOUT, body_timeout=BODY_TIMEOUT):
        super().__init__(address, handler)
        self.header_timeout = header_timeout
        self.body_timeout = body_timeout
        self._max_connections = max_connections
        self._slots = threading.BoundedSemaphore(max_connections)
        self._holding = set()
        self._holding_lock = threading.Lock()
        self.refused = 0

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.refused += 1
            self.shutdown_request(request)
            return
        with self._holding_lock:
            self._holding.add(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            self.release_slot(request)
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.release_slot(request)

    def release_slot(self, request):
        with self._holding_lock:
            if request not in self._holding:
                return
            self._holding.discard(request)
        self._slots.release()

    @property
    def in_progress(self):
        with self._holding_lock:
            return len(self._holding)

    def pressure(self):
        """True while three quarters of the slots or more are taken."""
        return self.in_progress * 4 >= self._max_connections * 3

    def handle_error(self, request, client_address):
        """socketserver's default prints the traceback, with the exception's
        message, on standard error: log the class and traceback only."""
        exc = sys.exc_info()[1]
        if exc is None or isinstance(exc, (ConnectionError, TimeoutError)):
            return                      # a client that went away
        log_failure("App window connection failed", exc)


class _DeadlineReader(io.RawIOBase):
    """The request side of a connection, with one deadline for everything
    read until it is moved: a per-read timeout lets a client that sends a
    byte every few seconds hold the connection for ever."""

    def __init__(self, sock, pressure=None):
        super().__init__()
        self._sock = sock
        self.started = self.deadline = time.monotonic()
        self.waiting_for_request = False
        self._pressure = pressure       # fn() -> True while slots run short

    def readable(self):
        return True

    def readinto(self, buf):
        while True:
            now = time.monotonic()
            end = self.deadline
            watch = self.waiting_for_request and self._pressure is not None
            if watch and self._pressure():
                # Nearly all slots are taken: a request that has not
                # arrived after PRESSURE_SECONDS is not a real one (a
                # browser sends its headers at once on this machine).
                end = min(end, self.started + PRESSURE_SECONDS)
            if end - now <= 0:
                raise TimeoutError("the request did not arrive in time")
            # While watching for pressure, wake up now and then to look again.
            self._sock.settimeout(min(end - now, 0.25) if watch
                                  else end - now)
            try:
                return self._sock.recv_into(buf)
            except TimeoutError:
                continue


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HoldToTalk"
    sys_version = ""

    def setup(self):
        super().setup()
        # Replace the plain socket file by one with a deadline (see above).
        self.rfile.close()
        self._reader = _DeadlineReader(self.connection, self.server.pressure)
        self.rfile = io.BufferedReader(self._reader)

    def set_deadline(self, seconds, waiting_for_request=False):
        """The request side must finish what it is reading within `seconds`
        from now, however slowly it trickles in."""
        self._reader.started = time.monotonic()
        self._reader.deadline = self._reader.started + seconds
        self._reader.waiting_for_request = waiting_for_request

    def handle_one_request(self):
        # A request (and an idle keep-alive wait) gets header_timeout in all.
        self.set_deadline(self.server.header_timeout, True)
        try:
            super().handle_one_request()
        except OSError:                 # reset / aborted by the client
            self.close_connection = True

    def log_message(self, *args):       # requests are not logged
        pass

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def _serve(self, method):
        try:
            self.server.api._handle(self, method)
        except OSError:
            self.close_connection = True
        except Exception as exc:        # nothing may reach the server's hook
            log_failure("App window request failed", exc)
            self.close_connection = True

    def do_HEAD(self):
        self.send_error(405)


def _is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# -- the server -----------------------------------------------------------------------

class Server:
    """See the module docstring. app: app.App (or a stand-in with the same
    attributes); history: textrules.History or None."""

    def __init__(self, app, history, host="127.0.0.1", port=0,
                 config_path=None, log_path=None,
                 autostart_name=tray.AUTOSTART_NAME, window=None,
                 max_connections=MAX_CONNECTIONS,
                 header_timeout=HEADER_TIMEOUT, body_timeout=BODY_TIMEOUT,
                 clipboard=None):
        if not _is_loopback(host):
            raise ValueError(f"The app window server only listens on the "
                             f"loopback interface, not {host!r}")
        self.app, self.history = app, history
        self.config_path = config_path
        self.log_path = log_path
        self.autostart_name = autostart_name
        self.window = window
        self.secret = secrets.token_urlsafe(32)
        self.hub = Hub()
        self._tickets = {}
        self._tickets_lock = threading.Lock()
        self._feed = collections.deque(maxlen=FEED_SIZE)
        self._notices = collections.deque(maxlen=20)
        self._lock = threading.Lock()
        self._health = {"cleanup": None, "mic": None, "autostart": None,
                        "checked": None}
        self._health_wake = threading.Event()
        self._capture = None
        self._cancel_token = None
        self._mic_test = None
        self._stopping = threading.Event()
        self._threads = []
        self._last_stream = 0.0
        self._logbuf = _LogBuffer(self._redact)
        self.opened = None              # (ticket time, first state request)
        self.httpd = _HTTPServer((host, port), _Handler,
                                 max_connections=max_connections,
                                 header_timeout=header_timeout,
                                 body_timeout=body_timeout)
        self.httpd.api = self
        self.clipboard = clipboard      # None: the real one, made on use
        self.host = host
        self.port = self.httpd.server_address[1]
        hostport = f"[{host}]" if ":" in host else host
        self.origin = f"http://{hostport}:{self.port}"
        self._host_header = f"{hostport}:{self.port}"
        self._static = {}
        for path, (name, ctype) in STATIC.items():
            data = (UI_DIR / name).read_bytes()
            if name == "index.html":
                data = data.replace(b"{{APP_NAME}}", _html_escape(
                    settings.APP_NAME).encode("utf-8"))
            self._static[path] = (data, ctype)
        buf = io.BytesIO()
        tray.make_icon("ready", 64).save(buf, "PNG")
        self._static["/icon.png"] = (buf.getvalue(), "image/png")

    # -- lifecycle
    def start(self):
        for target, name in ((self.httpd.serve_forever, "ui-server"),
                             (self._sample_loop, "ui-live"),
                             (self._health_loop, "ui-health")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        logging.getLogger().addHandler(self._logbuf)
        for name, fn in (("on_dictation", self._on_dictation),
                         ("on_notice", self._on_notice),
                         ("on_reload", self._on_reload)):
            hooks = getattr(self.app, name, None)
            if isinstance(hooks, list):
                hooks.append(fn)
        tray.CHANGE_HOOKS.append(self._health_wake.set)
        if self.window is not None:
            # The window replaces a page that is not connected to this run.
            try:
                self.window.is_connected = self.page_connected
            except AttributeError:
                pass
        log.info("App window server on %s", self.origin)
        return self

    def stop(self):
        self._stopping.set()
        self._health_wake.set()
        self.hub.close()
        logging.getLogger().removeHandler(self._logbuf)
        try:
            tray.CHANGE_HOOKS.remove(self._health_wake.set)
        except ValueError:
            pass
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
        for t in self._threads:
            t.join(2)

    def page_connected(self):
        """True while a page of this run is connected to the live stream
        (or was, moments ago: it may be reconnecting)."""
        return (self.hub.count > 0 or time.monotonic() - self._last_stream
                < CONNECTED_GRACE)

    # -- tickets
    def launch_url(self, view=None):
        """A URL that opens the window: one-time ticket in the fragment."""
        ticket = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._tickets_lock:
            self._tickets = {t: exp for t, exp in self._tickets.items()
                             if exp > now}
            self._tickets[ticket] = now + TICKET_SECONDS
        self.opened = [time.monotonic(), None]
        frag = f"t={ticket}"
        if view in VIEWS:
            frag += f"&v={view}"
        return f"{self.origin}/#{frag}"

    def _redeem(self, ticket):
        now = time.monotonic()
        with self._tickets_lock:
            expires = self._tickets.pop(ticket, None)
        return expires is not None and expires > now

    def _redact(self, line):
        if self.secret in line:
            line = line.replace(self.secret, "[hidden]")
        with self._tickets_lock:
            tickets = list(self._tickets)
        for t in tickets:
            if t in line:
                line = line.replace(t, "[hidden]")
        return line

    # -- request handling
    def _handle(self, h, method):
        h.htt_unread = 0
        try:
            status, body, ctype, extra = self._route(h, method)
        except ApiError as exc:
            status, body, ctype, extra = (exc.status,
                                          {"error": str(exc), **exc.extra},
                                          None, {})
        except Exception as exc:
            log_failure("App window request failed", exc)
            status, body, ctype, extra = (500, {"error": "Something went "
                                                "wrong; details are in the "
                                                "log."}, None, {})
        if status is None:
            return                              # the event stream answered
        self._drain(h)
        try:
            if ctype is None:
                # "backslashreplace": whatever the answer holds always
                # encodes (a lone surrogate becomes the JSON escape \udXXX).
                data = json.dumps(body, ensure_ascii=False).encode(
                    "utf-8", "backslashreplace")
                ctype = "application/json; charset=utf-8"
            else:
                data = body
        except Exception as exc:                # e.g. a value JSON cannot hold
            log_failure("App window answer could not be built", exc)
            status, extra = 500, {}
            data = json.dumps({"error": "Something went wrong; details are "
                               "in the log."}).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        try:
            h.connection.settimeout(WRITE_TIMEOUT)
            h.send_response(status)
            self._security_headers(h, ctype)
            for k, v in extra.items():
                h.send_header(k, v)
            if h.close_connection:
                h.send_header("Connection", "close")
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
        except OSError:
            h.close_connection = True
            return
        if h.htt_unread:
            self._linger(h)

    @staticmethod
    def _drain(h):
        """Read away a request body that was refused before it was read, so
        the client gets the answer instead of a reset connection (a client
        is still sending while it waits for the answer)."""
        left = h.htt_unread
        if left <= 0:
            return
        h.close_connection = True
        if left > DRAIN_LIMIT:
            return                  # answer, then linger (see _linger)
        deadline = time.monotonic() + DRAIN_SECONDS
        h.set_deadline(DRAIN_SECONDS)
        try:
            while left > 0 and time.monotonic() < deadline:
                chunk = h.rfile.read1(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:                 # incl. the drain deadline
            pass
        h.htt_unread = left

    @staticmethod
    def _linger(h):
        """After answering a request whose body was not read: stop sending,
        and read (and discard) for a moment, so the answer is not lost to a
        reset while the client is still sending."""
        try:
            h.wfile.flush()
            h.connection.shutdown(socket.SHUT_WR)
            h.connection.settimeout(0.5)
            deadline = time.monotonic() + DRAIN_SECONDS
            while time.monotonic() < deadline:
                if not h.connection.recv(65536):
                    break
        except OSError:
            pass

    @staticmethod
    def _security_headers(h, ctype):
        h.send_header("Content-Type", ctype)
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Referrer-Policy", "no-referrer")
        h.send_header("X-Frame-Options", "DENY")
        h.send_header("Cross-Origin-Resource-Policy", "same-origin")
        h.send_header("Cross-Origin-Opener-Policy", "same-origin")
        if ctype.startswith("text/html"):
            h.send_header("Content-Security-Policy", CSP)

    def _check_key(self, h):
        given = h.headers.get(KEY_HEADER) or ""
        if not hmac.compare_digest(given.encode("utf-8", "replace"),
                                   self.secret.encode("ascii")):
            raise ApiError(401, "This window is not connected to the app. "
                           "Open the app again from its icon in the "
                           "notification area.")

    def _route(self, h, method):
        parts = urlsplit(h.path)
        path = parts.path
        length = 0
        if method == "POST":
            try:
                length = int(h.headers.get("Content-Length") or 0)
            except ValueError:
                h.close_connection = True
                raise ApiError(400, "Bad Content-Length.")
            if length < 0:
                h.close_connection = True
                raise ApiError(400, "Bad Content-Length.")
            h.htt_unread = length
        if h.headers.get("Host") != self._host_header:
            raise ApiError(403, "Wrong host.")
        origin = h.headers.get("Origin")
        if origin is not None and origin != self.origin:
            raise ApiError(403, "Requests from other sites are not allowed.")
        site = h.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            raise ApiError(403, "Requests from other sites are not allowed.")

        if method == "GET" and path in self._static:
            data, ctype = self._static[path]
            return 200, data, ctype, {}
        if not path.startswith("/api/"):
            raise ApiError(404, "Not found.")
        name = path[len("/api/"):]

        body = {}
        if method == "POST":
            if origin is None:
                raise ApiError(403, "Missing Origin header.")
            ctype = (h.headers.get("Content-Type") or "").split(";")[0]
            if ctype.strip().lower() != "application/json":
                raise ApiError(415, "Send JSON.")
            if name != "hello":
                self._check_key(h)          # before the body is even read
            if length > MAX_BODY:
                raise ApiError(413, "Request too large.")
            h.set_deadline(self.httpd.body_timeout)
            raw = h.rfile.read(length) if length else b""
            h.htt_unread = 0
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:               # incl. RecursionError (nesting)
                raise ApiError(400, "Bad JSON.")
            if not isinstance(body, dict):
                raise ApiError(400, "Send a JSON object.")
            if has_surrogate(body):
                raise ApiError(400, "The text contains a character that "
                               "cannot be used. Remove it and try again.")
            if name == "hello":
                ticket = body.get("ticket")
                if not isinstance(ticket, str) or not self._redeem(ticket):
                    raise ApiError(401, "This window link has expired. Open "
                                   "the app again from its icon in the "
                                   "notification area.")
                return 200, {"key": self.secret}, None, {}
        else:
            self._check_key(h)

        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        routes = GET_ROUTES if method == "GET" else POST_ROUTES
        fn = routes.get(name)
        if fn is None:
            raise ApiError(404, "Not found.")
        if name == "events":
            if self.hub.count >= MAX_STREAMS:
                raise ApiError(503, "Too many windows are open.")
            self._stream(h)
            return None, None, None, None
        result = fn(self, body if method == "POST" else query)
        return 200, result, None, {}

    # -- the event stream
    def _stream(self, h):
        q = self.hub.subscribe()
        self.httpd.release_slot(h.request)   # long-lived: not a "request"
        self._health_wake.set()
        try:
            h.connection.settimeout(WRITE_TIMEOUT)
            h.send_response(200)
            self._security_headers(h, "text/event-stream; charset=utf-8")
            h.send_header("Connection", "close")
            h.end_headers()
            h.close_connection = True
            first = json.dumps(self._live(), separators=(",", ":"))
            h.wfile.write(f"event: live\ndata: {first}\n\n".encode())
            h.wfile.flush()
            while not self._stopping.is_set():
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    h.wfile.write(b": ping\n\n")
                    h.wfile.flush()
                    continue
                if item is None:
                    break
                event, payload = item
                h.wfile.write(f"event: {event}\ndata: {payload}\n\n"
                              .encode("utf-8"))
                h.wfile.flush()
        except OSError:
            pass
        finally:
            self.hub.unsubscribe(q)
            self._last_stream = time.monotonic()

    # -- engine callbacks (Tk thread: only quick work here)
    def _on_dictation(self, record):
        item = dict(record)
        # A successful recording clears "only silence"; a microphone problem
        # stays until the app says it is gone (app.mic_problem is None).
        gone = {"silence"} if self._mic_problem() else {"mic", "silence"}
        with self._lock:
            self._feed.appendleft(item)
            self._notices = collections.deque(
                (n for n in self._notices if n["kind"] not in gone),
                maxlen=20)
        self.hub.publish("dictation", self._feed_item(item, resolve=False))

    def _on_notice(self, kind, message):
        notice = {"kind": kind, "message": message, "ts": time.time()}
        with self._lock:
            if kind == "mic" and not self._mic_problem():
                # The problem is over: drop what was said about it.
                self._notices = collections.deque(
                    (n for n in self._notices if n["kind"] != "mic"),
                    maxlen=20)
            self._notices.appendleft(notice)
        self.hub.publish("notice", notice)

    def _mic_problem(self):
        problem = getattr(self.app, "mic_problem", None)
        return problem if isinstance(problem, str) and problem else None

    def _on_reload(self, old, new, result):
        self.hub.publish("config", {"applied": result.get("applied", [])})
        self._health_wake.set()

    # -- background samplers
    def _live(self):
        app = self.app
        cfg = app.cfg
        hotkey = cfg.get("hotkey")
        cap = self._capture
        return {
            "status": getattr(app, "status", "ready"),
            "detail": getattr(app, "status_detail", ""),
            "paused": bool(getattr(app, "paused", False)),
            "terms": {"pending": bool(getattr(app, "terms_pending", False)),
                      "version": settings.TERMS_VERSION},
            "activity": getattr(app, "activity", "idle"),
            "capturing": bool(getattr(app, "capturing", False)),
            "hook": getattr(app, "mouse_hook_state", "off"),
            "hotkey": {"spec": hotkey, "plain": keys.plain_hotkey(hotkey),
                       "technical": keys.describe_hotkey(hotkey),
                       "mouse": bool(keys.MOUSE_NAMES.keys()
                                     & set(str(hotkey).split("+")))},
            "capture": dict(cap) if cap else None,
            "overlay": settings.overlay_colors(cfg),
            "mic_problem": self._mic_problem(),
            "language_hint": self._language_hint(),
            "held_seconds": self._held_seconds(),
            "failure": self._load_failure(),
            "download": self._download_state(),
        }

    def _load_failure(self):
        """{"kind", "reason", "advice", "auto_retry"} while the speech model
        failed because it could not be DOWNLOADED (the window shows the plain
        reason and what to do; auto_retry: the app tries again by itself),
        else None."""
        app = self.app
        try:
            failure = getattr(app, "failure", None)
            if getattr(app, "status", None) != "failed" or not isinstance(
                    failure, dict):
                return None
            retry_in = getattr(app, "auto_retry_in", None)
            return {"kind": str(failure.get("kind", "")),
                    "reason": str(failure.get("reason", "")),
                    "advice": str(failure.get("advice", "")),
                    "auto_retry": callable(retry_in)
                    and retry_in() is not None}
        except Exception:
            return None

    def _download_state(self):
        """{"size", "percent"} while the app downloads the speech model
        (percent is None when it cannot be told), else None."""
        try:
            if getattr(self.app, "status", None) != "loading":
                return None
            get = getattr(self.app, "download_progress", None)
            state = get() if callable(get) else None
            if not isinstance(state, dict):
                return None
            percent = state.get("percent")
            return {"size": state.get("size") or None,
                    "percent": percent if isinstance(percent, int)
                    and not isinstance(percent, bool) else None}
        except Exception:
            return None

    def _language_hint(self):
        """{"code", "name"} when the app suggests a language (app.
        language_hint: the last 10 dictations were all one language and the
        language setting is automatic), else None. Needs an engine with that
        attribute; without it there is never a suggestion."""
        try:
            code = getattr(self.app, "language_hint", None)
            if not isinstance(code, str) or not code:
                return None
            names = dict(settings.languages())
            if code not in names or self.app.cfg.get("language"):
                return None
            return {"code": code, "name": names[code]}
        except Exception:
            return None

    def _held_seconds(self):
        """Seconds of audio in the recording the app kept after a failed
        transcription (app.held_recording), or None."""
        try:
            held = getattr(self.app, "held_recording", None)
            if isinstance(held, dict) and callable(getattr(
                    self.app, "retry_held_recording", None)):
                return round(float(held.get("seconds") or 0.0), 1)
        except Exception:
            pass
        return None

    def _sample_loop(self):
        last = None
        next_level = 0.0
        while not self._stopping.is_set():
            if not self.hub.count:
                self._stopping.wait(0.25)
                last = None
                continue
            try:
                live = self._live()
            except Exception as exc:
                log_failure("Reading the app state failed", exc)
                self._stopping.wait(1)
                continue
            if live != last:
                last = live
                self.hub.publish("live", live)
            now = time.monotonic()
            if live["activity"] == "listening" and now >= next_level:
                rec = getattr(self.app, "recorder", None)
                level = float(getattr(rec, "level", 0.0) or 0.0)
                self.hub.publish("level", {"v": round(level, 4)})
                next_level = now + LEVEL_SECONDS
            self._stopping.wait(0.03 if live["activity"] == "listening"
                                else STATE_SECONDS)

    def _health_loop(self):
        while not self._stopping.is_set():
            self._health_wake.wait(HEALTH_SECONDS)
            self._health_wake.clear()
            if self._stopping.is_set():
                return
            if not self.hub.count and self._health["checked"] is not None:
                continue
            try:
                health = self._check_health()
            except Exception as exc:
                log_failure("Health check failed", exc)
                continue
            changed = {k: v for k, v in health.items() if k != "checked"} != {
                k: v for k, v in self._health.items() if k != "checked"}
            self._health = health
            if changed:
                self.hub.publish("health", health)

    def _check_health(self):
        cfg = self.app.cfg
        try:
            clean = cleanup.status(cfg)
        except Exception as exc:
            log.warning("AI cleanup status check failed: %s", exc)
            clean = {"state": "unreachable",
                     "model": str(cleanup.merged_settings(cfg)["model"])}
        mic = {"state": "ok"}
        try:
            devices = audio.list_devices()
            value = cfg.get("input_device")
            if not devices:
                mic = {"state": "none"}
            elif value not in (None, ""):
                try:
                    audio.resolve_device(value)
                except Exception:
                    mic = {"state": "missing", "device": str(value)}
        except Exception as exc:
            log.warning("Listing microphones failed: %s", exc)
            mic = {"state": "error"}
        return {"cleanup": clean, "mic": mic, "autostart": self._autostart(),
                "checked": time.time()}

    # -- helpers for routes
    def _feed_item(self, item, resolve=True):
        out = {k: item.get(k) for k in (
            "ts", "raw", "final", "pasted", "audio_seconds", "speech_seconds",
            "stages", "cleanup", "dry_run", "paste_error", "total_seconds",
            "paste_method", "language")}
        out["history_id"] = None
        if resolve and self.history is not None:
            row = self.history.at(item["ts"])
            if row:
                out["history_id"] = row["id"]
                out["corrected"] = row["corrected"]
        return out

    def _mutate(self, compute):
        """compute(cfg) -> (changes, extra dict); saved and applied under
        the config lock. ValueError (incl. learn.LearnError) -> 400. A
        change that was not saved or not applied fails the request (503:
        not saved, 422: refused by the engine) with a plain message and the
        engine's real state ("config", "settings") so the window can show
        it; the technical detail goes to the log."""
        with settings.CONFIG_LOCK:
            try:
                changes, extra = compute(self.app.cfg)
            except ValueError as exc:
                raise _bad(str(exc))
            result = settings.apply_changes(self.app, changes,
                                            self.config_path)
        out = dict(extra)
        out["result"] = {k: list(result.get(k, [])) for k in (
            "applied", "restart_required", "errors", "rejected")}
        out["config"] = self._config_lists()
        out["settings"] = self._settings_view()
        errors = out["result"]["errors"]
        if errors:
            log.warning("A change from the app window was not applied: %s",
                        "; ".join(map(str, errors)))
            message = plain_change_error(errors)
            out["result"]["errors"] = [message]
            saved = not any(str(e).startswith("Could not save")
                            for e in errors)
            raise ApiError(422 if saved else 503, message, {
                k: out[k] for k in ("result", "config", "settings")})
        return out

    def _config_lists(self):
        cfg = self.app.cfg
        learned = learn.learned_items(cfg)
        active_entries = {(r["added_entry"] or {}).get("heard", "").casefold()
                          for r in learned if r.get("active")
                          and isinstance(r.get("added_entry"), dict)}
        dictionary = [{"heard": e["heard"], "wanted": e["wanted"],
                       "learned": e["heard"].strip().casefold()
                       in active_entries}
                      for e in (cfg.get("dictionary") or [])
                      if isinstance(e, dict) and isinstance(e.get("heard"), str)
                      and isinstance(e.get("wanted"), str)]
        snippets = [{"trigger": s["trigger"], "text": s["text"]}
                    for s in (cfg.get("snippets") or [])
                    if isinstance(s, dict) and isinstance(s.get("trigger"), str)
                    and isinstance(s.get("text"), str)]
        vocab = [v for v in (cfg.get("vocabulary") or [])
                 if isinstance(v, str)]
        terms = textrules.vocabulary_terms(cfg)
        engine = getattr(self.app, "engine", None)
        counter = getattr(engine, "count_tokens", None) or stt.estimate_tokens
        try:
            prompt = stt.glossary_prompt(terms, counter) or ""
        except Exception:
            prompt = stt.glossary_prompt(terms) or ""
        used = 0
        for n in range(len(terms), 0, -1):
            if prompt == stt.PROMPT_PREFIX + ", ".join(terms[:n]) + ".":
                used = n
                break
        legacy = cfg.get("replacements")
        return {"vocabulary": vocab, "dictionary": dictionary,
                "snippets": snippets, "learned": learned,
                "legacy_replacements": len(legacy)
                if isinstance(legacy, dict) else 0,
                "glossary": {"used": used, "total": len(terms),
                             "ignored": terms[used:]}}

    def _settings_view(self):
        cfg = self.app.cfg
        rules = textrules.settings_of(cfg)
        clean = cleanup.merged_settings(cfg)
        where = cleanup_where(cfg)
        pause = cfg.get("pause_hotkey")
        pause = pause if isinstance(pause, str) and pause.strip() else None
        return {
            "hotkey": cfg.get("hotkey"),
            "pause_hotkey": pause,
            "pause_hotkey_plain": keys.plain_hotkey(pause) if pause else None,
            "input_device": cfg.get("input_device"),
            "language": cfg.get("language"),
            **{k: bool(cfg.get(k, settings.DEFAULTS.get(k))) for k in
               ("remove_fillers", "trailing_space", "restore_clipboard",
                "save_history")},
            "cleanup_enabled": bool(clean.get("enabled")),
            "cleanup_model": str(clean.get("model")),
            "cleanup_host": where["host"],
            "cleanup_local": where["local"],
            "cleanup_allowed": where["allowed"],
            "cleanup_trust_listener": where["trust_listener"],
            "cleanup_trust_available": "trust_listener" in cleanup.DEFAULTS,
            "less_graphics_memory_available": _compute_choice_available(),
            "less_graphics_memory": (
                str(cfg.get("compute_type", "auto")).lower()
                in ("int8_float16", "int8")),
            "formatting_commands": rules["formatting_commands"],
            "snippet_mode": rules["snippet_mode"],
            "auto_learn": learn.settings_of(cfg)["auto_apply"],
            "overlay": settings.overlay_colors(cfg),
            "sounds": sounds_of(cfg),
        }

    def _devices(self):
        try:
            groups = tray.dedupe_devices(audio.list_devices())
        except Exception as exc:
            log.warning("Listing microphones failed: %s", exc)
            return {"list": [], "error": "The microphones could not be "
                    "listed. Details are in the log.", "missing": None}
        value = self.app.cfg.get("input_device")
        current = tray.current_device_group(groups, value)
        out = []
        for g in groups:
            out.append({"value": tray.device_config_value(g),
                        "name": g["name"], "is_default": g["is_default"],
                        "selected": g is current})
        missing = (str(value) if value not in (None, "") and current is None
                   else None)
        return {"list": out, "error": None, "missing": missing}

    def _autostart(self):
        if self.autostart_name is None:
            return {"available": False, "enabled": False}
        try:
            return {"available": True,
                    "enabled": tray.autostart_enabled(self.autostart_name)}
        except OSError:
            return {"available": True, "enabled": False}

    # -- GET routes
    def r_state(self, q):
        if self.opened and self.opened[1] is None:
            self.opened[1] = time.monotonic()
        engine = getattr(self.app, "engine", None)
        cfg = self.app.cfg
        with self._lock:
            notices = list(self._notices)
        stats = self.history.stats() if self.history is not None else None
        clean = cleanup.merged_settings(cfg)
        where = cleanup_where(cfg)
        return {
            "info": {"name": settings.APP_NAME,
                     "version": settings.APP_VERSION,
                     "dry_run": bool(getattr(self.app, "dry_run", False)),
                     "log_path": str(self.log_path) if self.log_path else None,
                     "terms": self._terms_info()},
            "live": self._live(),
            "health": self._health,
            "speech": {"name": getattr(engine, "name", None),
                       "device": getattr(engine, "device", None),
                       "compute": getattr(engine, "compute_type", None),
                       "fallback": getattr(engine, "fallback_reason", None)},
            "cleanup": {"enabled": settings.cleanup_enabled(cfg),
                        "model": clean["model"], **where},
            "mic_problem": self._mic_problem(),
            "stats": stats,
            "history_enabled": bool(cfg.get("save_history", True)),
            "notices": notices,
            "window": self.window.mode if self.window is not None else None,
            "can": {"retry": callable(getattr(self.app, "retry_engine",
                                              None)),
                    "sounds": callable(getattr(self.app, "play_sound",
                                               None))},
        }

    def r_feed(self, q):
        with self._lock:
            items = list(self._feed)
        out = [self._feed_item(i) for i in items]
        seen = {i["history_id"] for i in out if i["history_id"]}
        if self.history is not None and len(out) < 15:
            for row in self.history.recent(15):
                if row["id"] in seen or any(abs(i["ts"] - row["ts"]) < 1e-6
                                            for i in out):
                    continue
                out.append({"ts": row["ts"], "raw": row["raw"],
                            "final": row["final"], "pasted": None,
                            "audio_seconds": row["seconds"],
                            "speech_seconds": None, "stages": None,
                            "cleanup": None, "dry_run": None,
                            "paste_error": None, "total_seconds": None,
                            "history_id": row["id"],
                            "corrected": row["corrected"]})
        out.sort(key=lambda i: i["ts"], reverse=True)
        return {"items": out[:FEED_SIZE]}

    def r_history(self, q):
        """?q=text&limit=N: the newest N rows (matching q), plus "matches"
        (how many match in all) and "count" (rows stored)."""
        if self.history is None:
            return {"items": [], "enabled": False, "count": 0, "matches": 0}
        query = q.get("q", "")[:200]
        try:
            limit = max(1, min(int(q.get("limit", 200)), HISTORY_MAX_LIMIT))
        except ValueError:
            limit = 200
        total = self.history.count()
        if query.strip():
            rows = self.history.search(query, max(total, limit))
            matches = len(rows)
            rows = rows[:limit]
        else:
            rows = self.history.recent(limit)
            matches = total
        return {"items": rows, "enabled": self.history.enabled,
                "count": total, "matches": matches}

    def r_config(self, q):
        return {"settings": self._settings_view(),
                "languages": [{"code": c, "name": n}
                              for c, n in settings.languages()],
                "devices": self._devices(),
                "autostart": self._autostart(),
                "overlay_defaults": settings.OVERLAY_DEFAULTS,
                "sound_defaults": SOUND_DEFAULTS,
                **self._config_lists()}

    def r_log(self, q):
        return {"lines": [self._redact(line) for line in
                          list(self._logbuf.lines)],
                "path": str(self.log_path) if self.log_path else None}

    def r_events(self, q):          # handled by _stream
        return None

    def _terms_info(self):
        """Which version of the Terms of Use this app has, whether they are
        still to be accepted, and the local record (when and how)."""
        pending = bool(getattr(self.app, "terms_pending", False))
        try:
            rec = getattr(self.app, "terms_record", None)
        except Exception:
            rec = None
        rec = rec if isinstance(rec, dict) else {}
        return {"version": settings.TERMS_VERSION, "pending": pending,
                "accepted_version": rec.get("version"),
                "accepted_at": rec.get("accepted_at"), "how": rec.get("how")}

    @staticmethod
    def _terms_text():
        """The text of TERMS.md, checked against this app's terms version, or
        a plain error."""
        try:
            text = terms.read_text()
        except (OSError, ValueError):
            log.warning("TERMS.md could not be read")
            raise ApiError(503, "The Terms of Use file (TERMS.md) could not "
                           "be read. Install the app again from the project's "
                           "page, or put TERMS.md back next to the program "
                           "files, and start the app again.")
        found = terms.version_line(text)
        if found is None or found[0] != settings.TERMS_VERSION:
            log.warning("TERMS.md does not match the app's terms version")
            raise ApiError(503, "The Terms of Use file (TERMS.md) is not the "
                           "one that belongs to this version of the app. "
                           "Install the app again from the project's page.")
        return text

    def r_terms(self, q):
        text = self._terms_text()
        return {**self._terms_info(), "text": text,
                "summary": terms.summary(text)}

    # -- POST routes: settings
    def p_settings(self, body):
        changes = {}
        engine_checked = {}     # key -> value for settings.validate_change
        for key, value in body.items():
            if key == "hotkey":
                try:
                    settings.normalize_hotkey(value)
                except ValueError as exc:
                    raise _bad(plain_hotkey_error(exc))
                engine_checked[key] = value
            elif key == "pause_hotkey":
                if value not in (None, ""):
                    try:
                        settings.normalize_hotkey(value)
                    except ValueError as exc:
                        raise _bad(plain_hotkey_error(exc, pause=True))
                engine_checked[key] = value
            elif key == "language":
                try:
                    settings.normalize_language(value)
                except ValueError:
                    raise _bad("Choose a language from the list.")
                engine_checked[key] = value
            elif key == "sounds":
                if not isinstance(value, dict) or not value or set(value) - {
                        "enabled", "volume", "mute_others"}:
                    raise _bad("Send the chimes as enabled/volume and the "
                               "muting as mute_others.")
                if "enabled" in value and not isinstance(value["enabled"],
                                                         bool):
                    raise _bad("Chimes can only be switched on or off.")
                if "mute_others" in value and not isinstance(
                        value["mute_others"], bool):
                    raise _bad("Muting other sound can only be switched on "
                               "or off.")
                if "volume" in value and (
                        isinstance(value["volume"], bool) or not isinstance(
                            value["volume"], (int, float))):
                    raise _bad("The volume must be between 0 and 100%.")
                engine_checked[key] = value
            elif key == "input_device":
                if not (value is None or (isinstance(value, int)
                                          and not isinstance(value, bool)
                                          and value >= 0)
                        or (isinstance(value, str) and len(value) <= 300)):
                    raise _bad("Choose a microphone from the list.")
                changes["input_device"] = value
            elif key in ("remove_fillers", "trailing_space",
                         "restore_clipboard", "save_history"):
                changes[key] = _bool(body, key)
            elif key == "cleanup_enabled":
                changes.setdefault("cleanup", {})["enabled"] = _bool(body, key)
            elif key == "cleanup_trust_listener":
                if "trust_listener" not in cleanup.DEFAULTS:
                    raise _bad("This version cannot change that.")
                changes.setdefault("cleanup", {})["trust_listener"] = _bool(
                    body, key)
            elif key == "less_graphics_memory":
                if not _compute_choice_available():
                    raise _bad("This version cannot change that.")
                changes["compute_type"] = (
                    LESS_GPU_MEMORY if _bool(body, key) else "auto")
            elif key == "cleanup_model":
                model = _text(body, key, 100).strip()
                if not _MODEL_NAME.fullmatch(model):
                    raise _bad("That does not look like an Ollama model name. "
                               "Model names look like gemma3:4b (letters, "
                               "digits, dots, dashes and colons).")
                changes.setdefault("cleanup", {})["model"] = model
            elif key == "formatting_commands":
                changes.setdefault("textrules", {})[key] = _bool(body, key)
            elif key == "snippet_mode":
                if value not in textrules.SNIPPET_MODES:
                    raise _bad("Choose when snippets fire from the list.")
                changes.setdefault("textrules", {})[key] = value
            elif key == "auto_learn":
                changes["learning"] = {"auto_apply": _bool(body, key)}
            elif key == "overlay":
                if not isinstance(value, dict) or set(value) - {"color",
                                                                "background"}:
                    raise _bad("Send the pill colours as color/background.")
                current = settings.overlay_colors(self.app.cfg)
                merged = {**current, **value}
                try:
                    settings.check_overlay(merged["color"],
                                           merged["background"])
                except ValueError:
                    raise _bad("Type a colour like #ff4fa3 (a # and six "
                               "digits or letters a–f).")
                changes["overlay"] = {k: v.strip().lower()
                                      for k, v in value.items()}
            else:
                raise _bad(f"Unknown setting '{key}'.")
        if not changes and not engine_checked:
            raise _bad("Nothing to change.")

        def compute(cfg):
            out = dict(changes)
            # The hold-to-talk key and the pause shortcut must differ (said
            # in the window's words; validate_change checks it again).
            main = engine_checked.get("hotkey", cfg.get("hotkey"))
            pause = engine_checked.get("pause_hotkey", cfg.get("pause_hotkey"))
            mg, pg = _hotkey_groups(main), _hotkey_groups(pause)
            if (("hotkey" in engine_checked or "pause_hotkey" in engine_checked)
                    and pause not in (None, "") and mg is not None
                    and pg is not None and pg <= mg):
                # Equal, or a part of it ("ctrl" in "ctrl+windows"): every
                # dictation would also pause. The app refuses it too.
                same = pg == mg
                if "pause_hotkey" not in engine_checked:
                    raise ValueError(
                        "That key includes your pause shortcut. Choose a "
                        "different hold-to-talk key, or clear the pause "
                        "shortcut first.")
                raise ValueError(
                    "That is already your hold-to-talk key. Choose a "
                    "different key for pausing." if same else
                    "That key is part of your hold-to-talk key, so it would "
                    "pause every time you dictate. Choose a key that is not "
                    "part of it.")
            # The engine's own rules for these keys (settings.validate_change);
            # each is checked against the config as the others leave it.
            view = dict(cfg)
            for key in ("hotkey", "pause_hotkey", "language", "sounds"):
                if key not in engine_checked:
                    continue
                part = settings.validate_change(key, engine_checked[key], view)
                if part is None:                # an engine without the rule
                    part = _fallback_change(key, engine_checked[key])
                out.update(part)
                view.update(part)
            return out, {}
        out = self._mutate(compute)
        if "overlay" in changes:
            colors = settings.overlay_colors(self.app.cfg)
            out["warnings"] = settings.check_overlay(colors["color"],
                                                     colors["background"])
        return out

    def p_overlay_check(self, body):
        color = _text(body, "color", 20)
        background = _text(body, "background", 20)
        try:
            return {"warnings": settings.check_overlay(color, background)}
        except ValueError:
            raise _bad("Type a colour like #ff4fa3.")

    def p_overlay_preview(self, body):
        """Show the real pill on screen for a moment (current colours)."""
        preview = getattr(self.app, "preview_overlay", None)
        if preview is None:
            raise _bad("The pill cannot be shown right now.")
        preview("listening", 2.5)       # thread-safe (hops to the Tk thread)
        timer = threading.Timer(2.5, preview, ("working", 1.5))
        timer.daemon = True
        timer.start()
        return {"ok": True, "seconds": 4.0}

    def p_pause(self, body):
        self.app.set_paused(_bool(body, "paused"))
        return {"paused": bool(self.app.paused)}

    def p_autostart(self, body):
        enabled = _bool(body, "enabled")
        if self.autostart_name is None:
            raise _bad("Start with Windows is only available for the main "
                       "installation.")
        try:
            tray.set_autostart(enabled, self.autostart_name)
        except OSError as exc:
            log.warning("Changing Start with Windows failed: %s", exc)
            raise _bad("Windows did not allow that change. Details are in "
                       "the log.")
        self._health_wake.set()
        return {"autostart": self._autostart()}

    def p_terms_accept(self, body):
        """The terms screen's "I agree": record the acceptance of the
        version the page showed, and let dictation start at once."""
        accept = getattr(self.app, "accept_terms", None)
        if not callable(accept):
            raise ApiError(501, "Accepting the terms from the window is not "
                           "available in this version.")
        if body.get("version") != settings.TERMS_VERSION:
            raise ApiError(409, "These terms are out of date. Close this "
                           "window and open it again from the icon in the "
                           "notification area.")
        self._terms_text()              # it must be readable to be accepted
        try:
            accept("app")
        except OSError as exc:
            log.warning("The acceptance could not be saved: %s",
                        type(exc).__name__)
            raise ApiError(503, "Your acceptance could not be saved: the "
                           "app's data folder is not writable. Details are "
                           "in the log.")
        self._health_wake.set()
        return {"ok": True, "terms": self._terms_info()}

    def p_quit(self, body):
        """The terms screen's "Quit". Only while the terms are pending;
        otherwise the tray icon's Quit is the way."""
        if not getattr(self.app, "terms_pending", False):
            raise ApiError(409, "Quit the app from its icon in the "
                           "notification area.")
        self.app.quit()
        return {"ok": True}

    def p_engine_retry(self, body):
        retry = getattr(self.app, "retry_engine", None)
        if not callable(retry):
            raise ApiError(501, "Trying again from the window is not "
                           "available in this version. Quit the app from "
                           "its icon in the notification area and start it "
                           "again.")
        if getattr(self.app, "status", None) != "failed":
            raise ApiError(409, "The speech model is not in a failed state, "
                           "so there is nothing to try again.")
        if retry() is False:
            raise ApiError(409, "It is already trying again. Wait a moment.")
        return {"ok": True}

    def p_recording_retry(self, body):
        retry = getattr(self.app, "retry_held_recording", None)
        if not callable(retry):
            raise ApiError(501, "Trying a kept recording again is not "
                           "available in this version.")
        if retry() is False:
            raise ApiError(409, "There is no kept recording to try again "
                            "(it is only kept for a few minutes), or the "
                            "speech model is not ready yet.")
        return {"ok": True}

    def p_sound_play(self, body):
        which = body.get("which")
        if which not in ("start", "stop"):
            raise _bad("Choose the start or the stop chime.")
        play = getattr(self.app, "play_sound", None)
        if not callable(play):
            raise ApiError(501, "Chimes are not available in this version "
                           "yet.")
        if play(which) is False:
            if getattr(self.app, "dry_run", False):
                raise ApiError(409, "Chimes are silent in test mode.")
            raise ApiError(409, "The chime could not be played. Check that "
                           "chimes are on and the volume is above 0%, and "
                           "that a speaker is connected.")
        return {"ok": True}

    def p_clipboard_copy(self, body):
        """Put dictated text on the clipboard the way the paste does: with
        the markers that keep it out of Windows clipboard history and cloud
        clipboard sync (page script cannot set those). The page falls back
        to the browser's own clipboard, and says so, if this fails."""
        text = _text(body, "text", CLIPBOARD_MAX)
        if not text.strip():
            raise _bad("There is nothing to copy.")
        try:
            board = self.clipboard or keys.Win32Clipboard()
            items = keys.text_items(text)
            private = getattr(keys, "_private", None)
            if callable(private):
                items = private(items)
            else:
                items = items + [(None, n, d) for n, d in
                                 getattr(keys, "PRIVATE_MARKERS", ())]
            board.write(items)
        except Exception as exc:
            log_failure("Copying to the clipboard failed", exc)
            raise ApiError(409, "The app could not use the Windows "
                           "clipboard just now (another program may be "
                           "using it).")
        return {"ok": True, "private": True}

    def p_health_check(self, body):
        """Check the microphone and AI cleanup again now (the answer
        arrives as a "health" event)."""
        self._health_wake.set()
        return {"health": self._health}

    def p_capture(self, body):
        token = secrets.token_hex(4)
        self._capture = {"id": token, "state": "waiting",
                         "seconds": CAPTURE_SECONDS}

        def done(spec):
            if not self._capture or self._capture.get("id") != token:
                return
            if spec:
                self._capture = {"id": token, "state": "done", "spec": spec,
                                 "plain": keys.plain_hotkey(spec),
                                 "technical": keys.describe_hotkey(spec)}
            else:
                reason = "user" if self._cancel_token == token else "timeout"
                self._capture = {"id": token, "state": "cancelled",
                                 "reason": reason,
                                 "seconds": CAPTURE_SECONDS}
            self.hub.publish("capture", self._capture)
        self.app.capture_hotkey(done, timeout=CAPTURE_SECONDS)
        self.hub.publish("capture", self._capture)
        return {"capture": self._capture}

    def p_capture_cancel(self, body):
        cap = self._capture
        self._cancel_token = cap.get("id") if cap else None
        self.app.cancel_capture()
        return {"ok": True}

    def p_mic_test(self, body):
        device = body.get("device")
        if not (device is None or isinstance(device, (str, int))) \
                or isinstance(device, bool):
            raise _bad("Choose a microphone from the list.")
        with self._lock:
            if self._mic_test is not None and self._mic_test.is_alive():
                raise ApiError(409, "A microphone test is already running.")
            self._mic_test = threading.Thread(
                target=self._run_mic_test, args=(device,), name="mic-test",
                daemon=True)
            self._mic_test.start()
        return {"seconds": MIC_TEST_SECONDS}

    def _run_mic_test(self, device):
        rec = None
        peak = 0.0
        try:
            rec = audio.Recorder(device)
            rec.start()
            end = time.monotonic() + MIC_TEST_SECONDS
            while time.monotonic() < end and not self._stopping.is_set():
                level = float(rec.level)
                peak = max(peak, level)
                self.hub.publish("miclevel", {"v": round(level, 4)})
                time.sleep(0.05)
            rec.stop()
            self.hub.publish("mictest", {"ok": True, "peak": round(peak, 4)})
        except Exception as exc:
            log.warning("Microphone test failed: %s", exc)
            self.hub.publish("mictest", {
                "ok": False, "error": "That microphone could not be opened. "
                "Check that it is plugged in and not in use by another "
                "program, or choose another one."})
        finally:
            if rec is not None:
                try:
                    rec.close()
                except Exception:
                    pass

    # -- POST routes: history and learning
    def _need_history(self):
        if self.history is None:
            raise _bad("History is not available.")
        return self.history

    def p_history_delete(self, body):
        history = self._need_history()
        entry_id = _int(body, "id")
        row = history.get(entry_id)
        if row is None or not history.delete(entry_id):
            raise ApiError(404, "That dictation is already gone.")
        with self._lock:
            self._feed = collections.deque(
                (i for i in self._feed
                 if abs(float(i.get("ts") or 0) - row["ts"]) >= 1e-6),
                maxlen=FEED_SIZE)
        self.hub.publish("history", {"deleted": entry_id, "ts": row["ts"]})
        return {"ok": True}

    def p_history_clear(self, body):
        if body.get("confirm") is not True:
            raise _bad("Confirm clearing the history.")
        if not self._need_history().clear():
            raise _bad("The history could not be cleared. Details are in "
                       "the log.")
        with self._lock:
            self._feed.clear()
        # The log viewer's lines can quote dictations too.
        self._logbuf.lines.clear()
        self.hub.publish("history", {"cleared": True})
        return {"ok": True}

    def p_history_find(self, body):
        ts = body.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            raise _bad("That dictation was not recognised.")
        row = self._need_history().at(ts)
        if row is None:
            raise ApiError(404, "That dictation is not in your history.")
        return {"row": row}

    def p_history_correct(self, body):
        history = self._need_history()
        entry_id = _int(body, "id")
        text = _text(body, "text", 20000)
        if not text.strip():
            raise _bad("The corrected text is empty.")
        if history.get(entry_id) is None:
            raise ApiError(404, "That dictation is no longer in your history.")
        row = history.correct(entry_id, text)
        if row is None:
            raise _bad("The correction could not be saved. Details are in "
                       "the log.")
        if row["corrected"] is None:            # back to what was pasted
            return {"row": row, "suggestions": [], "auto": []}
        learn_error = None
        with settings.CONFIG_LOCK:
            res = learn.learn_from_correction(self.app.cfg, row["final"], text,
                                              history_id=entry_id)
            auto = res["auto"]
            if auto:
                result = settings.apply_changes(self.app, auto["changes"],
                                                self.config_path)
                if result.get("errors"):
                    log.warning("Automatic learning was not saved: %s",
                                "; ".join(map(str, result["errors"])))
                    learn_error = plain_change_error(result["errors"])
                    auto = None
        return {"row": row, "suggestions": res["suggestions"],
                "auto": auto["applied"] if auto else [],
                "learn_error": learn_error}

    def p_history_revert(self, body):
        history = self._need_history()
        entry_id = _int(body, "id")
        history.uncorrect(entry_id)
        row = history.get(entry_id)
        if row is None:
            raise ApiError(404, "That dictation is no longer in your history.")
        return {"row": row}

    def p_learn_accept(self, body):
        history = self._need_history()
        entry_id = _int(body, "history_id")
        ids = body.get("ids")
        if (not isinstance(ids, list) or not ids
                or not all(isinstance(i, str) for i in ids)):
            raise _bad("Choose what to learn.")
        row = history.get(entry_id)
        if row is None or row["corrected"] is None:
            raise ApiError(409, "That correction is gone, so there is "
                           "nothing to learn from it.")

        def compute(cfg):
            sugg = learn.suggest(row["final"], row["corrected"], cfg,
                                 entry_id)
            chosen = [s for s in sugg if s["id"] in ids]
            if not chosen:
                raise ValueError("Already learned, or it no longer applies.")
            res = learn.apply_suggestions(cfg, chosen)
            return res["changes"], {"applied": res["applied"],
                                    "skipped": res["skipped"]}
        return self._mutate(compute)

    def p_learned_undo(self, body):
        learned_id = _text(body, "id", 64)

        def compute(cfg):
            res = learn.undo(cfg, learned_id)
            return res["changes"], {k: res[k] for k in (
                "removed_entry", "removed_term", "restored_entry",
                "warnings")}
        return self._mutate(compute)

    # -- POST routes: vocabulary, dictionary, snippets
    def p_vocab_check(self, body):
        term = _text(body, "term", 200)
        editing = _text(body, "editing", 200, required=False)
        try:
            return {"warnings": learn.check_term(self.app.cfg, term, editing)}
        except ValueError as exc:
            raise _bad(str(exc))

    def p_vocab_add(self, body):
        term = _text(body, "term", 200)
        return self._mutate(lambda cfg: _split(learn.add_term(cfg, term)))

    def p_vocab_edit(self, body):
        old, new = _text(body, "old", 200), _text(body, "new", 200)
        return self._mutate(lambda cfg: _split(learn.edit_term(cfg, old, new)))

    def p_vocab_remove(self, body):
        term = _text(body, "term", 200)
        return self._mutate(lambda cfg: _split(learn.remove_term(cfg, term)))

    def p_dict_check(self, body):
        heard, wanted = _text(body, "heard", 200), _text(body, "wanted", 1000)
        editing = _text(body, "editing", 200, required=False)
        try:
            return {"warnings": learn.check_entry(self.app.cfg, heard,
                                                  wanted.strip(), editing)}
        except ValueError as exc:
            raise _bad(str(exc))

    def p_dict_add(self, body):
        heard = _text(body, "heard", 200)
        wanted = _text(body, "wanted", 1000).strip()
        return self._mutate(lambda cfg: _split(
            learn.add_entry(cfg, heard, wanted)))

    def p_dict_edit(self, body):
        old = _text(body, "old_heard", 200)
        heard = _text(body, "heard", 200)
        wanted = _text(body, "wanted", 1000).strip()
        return self._mutate(lambda cfg: _split(
            learn.edit_entry(cfg, old, heard, wanted)))

    def p_dict_remove(self, body):
        heard = _text(body, "heard", 200)
        return self._mutate(lambda cfg: _split(learn.remove_entry(cfg, heard)))

    def p_dict_preview(self, body):
        heard = _text(body, "heard", 200)
        wanted = _text(body, "wanted", 1000).strip()
        sample = _text(body, "sample", 2000, required=False)
        if sample is None or not sample.strip():
            sample = SAMPLE_SENTENCE.format(heard=heard.strip() or "…")
        res = learn.preview_entry(heard, wanted, sample, self.app.cfg)
        return dict(res, sample=sample)

    def p_snip_check(self, body):
        trigger = _text(body, "trigger", 200)
        text = _text(body, "text", textrules.MAX_SNIPPET_CHARS + 1)
        editing = _text(body, "editing", 200, required=False)
        try:
            return {"warnings": textrules.check_snippet(self.app.cfg, trigger,
                                                        text, editing)}
        except ValueError as exc:
            raise _bad(str(exc))

    def p_snip_add(self, body):
        trigger = _text(body, "trigger", 200)
        text = _text(body, "text", textrules.MAX_SNIPPET_CHARS + 1)
        return self._mutate(lambda cfg: _split(
            textrules.add_snippet(cfg, trigger, text)))

    def p_snip_edit(self, body):
        old = _text(body, "old_trigger", 200)
        trigger = _text(body, "trigger", 200)
        text = _text(body, "text", textrules.MAX_SNIPPET_CHARS + 1)
        return self._mutate(lambda cfg: _split(
            textrules.edit_snippet(cfg, old, trigger, text)))

    def p_snip_remove(self, body):
        trigger = _text(body, "trigger", 200)
        return self._mutate(lambda cfg: _split(
            textrules.remove_snippet(cfg, trigger)))


def _split(res):
    return res["changes"], {"warnings": res.get("warnings", [])}


def _html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


VIEWS = ("home", "history", "dictionary", "vocabulary", "snippets",
         "settings")

GET_ROUTES = {
    "state": Server.r_state, "feed": Server.r_feed,
    "history": Server.r_history, "config": Server.r_config,
    "log": Server.r_log, "events": Server.r_events, "terms": Server.r_terms,
}
POST_ROUTES = {
    "settings": Server.p_settings, "pause": Server.p_pause,
    "terms/accept": Server.p_terms_accept, "quit": Server.p_quit,
    "autostart": Server.p_autostart,
    "engine/retry": Server.p_engine_retry,
    "recording/retry": Server.p_recording_retry,
    "sounds/play": Server.p_sound_play,
    "clipboard/copy": Server.p_clipboard_copy,
    "health/check": Server.p_health_check,
    "overlay/check": Server.p_overlay_check,
    "overlay/preview": Server.p_overlay_preview,
    "hotkey/capture": Server.p_capture,
    "hotkey/cancel": Server.p_capture_cancel,
    "mic/test": Server.p_mic_test,
    "history/delete": Server.p_history_delete,
    "history/clear": Server.p_history_clear,
    "history/find": Server.p_history_find,
    "history/correct": Server.p_history_correct,
    "history/revert": Server.p_history_revert,
    "learn/accept": Server.p_learn_accept,
    "learned/undo": Server.p_learned_undo,
    "vocabulary/check": Server.p_vocab_check,
    "vocabulary/add": Server.p_vocab_add,
    "vocabulary/edit": Server.p_vocab_edit,
    "vocabulary/remove": Server.p_vocab_remove,
    "dictionary/check": Server.p_dict_check,
    "dictionary/add": Server.p_dict_add,
    "dictionary/edit": Server.p_dict_edit,
    "dictionary/remove": Server.p_dict_remove,
    "dictionary/preview": Server.p_dict_preview,
    "snippets/check": Server.p_snip_check,
    "snippets/add": Server.p_snip_add,
    "snippets/edit": Server.p_snip_edit,
    "snippets/remove": Server.p_snip_remove,
}
