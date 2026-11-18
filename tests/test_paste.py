"""E2: pasting must leave the clipboard as it was, in every format (or not
touch it at all and type the text), keep the temporary text out of
clipboard history, and never restore over a newer copy. A fake clipboard
and a fake keyboard: nothing real is touched."""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import unittest

import keys
from fakes import FakeClipboard, FakeSender

TEXT = "Invented dictation, line one."
HTML = b"Version:0.9\r\nStartHTML:0\r\n<b>Zorvex</b> cells"
RTF = b"{\\rtf1 Zorvex}"
BIFF = b"\x09\x08\x10\x00\x00\x06\x05\x00" * 40
DIB = b"BM-ish header" + bytes(range(256)) * 4
UTEXT = "a\tb\r\n1\t2\0".encode("utf-16-le")


def reg(fmt, name, data):
    return (fmt, name, data)


class Base(unittest.TestCase):
    def setUp(self):
        keys.restore_pending_clipboard()      # nothing left over from a test

    def paste(self, cb, text=TEXT, restore=True, **kw):
        self.sender = kw.pop("sender", None) or FakeSender()
        kw.setdefault("ready", lambda: True)
        kw.setdefault("defer", False)         # the restore inline, as before
        return keys.paste_text(text, restore, clipboard=cb,
                               sender=self.sender, sleep=lambda s: None, **kw)

    def assertPrivate(self, items):
        names = {n for _, n, _ in items}
        self.assertIn("ExcludeClipboardContentFromMonitorProcessing", names)
        self.assertIn("CanIncludeInClipboardHistory", names)

    def data(self, items):
        return [(f, n, d) for f, n, d in items
                if n not in dict(keys.PRIVATE_MARKERS)]


class Restore(Base):
    def test_rich_spreadsheet_content_comes_back_in_every_format(self):
        before = [(keys.CF_UNICODETEXT, "", UTEXT),
                  reg(0xC100, "HTML Format", HTML),
                  reg(0xC101, "Rich Text Format", RTF),
                  reg(0xC102, "Biff12", BIFF),
                  (keys.CF_LOCALE, "", b"\x09\x04\x00\x00")]
        cb = FakeClipboard(before)
        self.assertEqual(self.paste(cb), "pasted")
        self.assertEqual(self.sender.calls, [("ctrl_v",)])
        temp = cb.writes[0]
        self.assertEqual(temp[0], (keys.CF_UNICODETEXT, "",
                                   (TEXT + "\0").encode("utf-16-le")))
        self.assertPrivate(temp)               # not in clipboard history
        self.assertEqual(self.data(cb.items), before)   # byte for byte

    def test_image_comes_back(self):
        # A screenshot: CF_BITMAP is a GDI handle (cannot be copied) but
        # Windows offers CF_DIB / CF_DIBV5 too and re-creates the bitmap.
        before = [(keys.CF_BITMAP, "", None), (keys.CF_DIB, "", DIB),
                  (keys.CF_DIBV5, "", DIB + b"v5")]
        cb = FakeClipboard(before)
        self.assertEqual(self.paste(cb), "pasted")
        self.assertEqual(self.data(cb.items), before[1:])

    def test_copied_files_come_back(self):
        drop = b"\x14\x00\x00\x00" + "C:\\fake\\a.txt\0\0".encode("utf-16-le")
        before = [(keys.CF_HDROP, "", drop),
                  reg(0xC103, "Preferred DropEffect", b"\x05\0\0\0"),
                  reg(0xC104, "Shell IDList Array", b"\x01" * 64)]
        cb = FakeClipboard(before)
        self.paste(cb)
        self.assertEqual(self.data(cb.items), before)

    def test_ole_plumbing_is_not_copied(self):
        before = [(keys.CF_UNICODETEXT, "", UTEXT),
                  reg(0xC105, "DataObject", b"\x10\x20"),
                  reg(0xC106, "Ole Private Data", b"\x00" * 8)]
        cb = FakeClipboard(before)
        self.paste(cb)
        self.assertEqual(self.data(cb.items), before[:1])

    def test_empty_clipboard_is_empty_again(self):
        cb = FakeClipboard([])
        self.paste(cb)
        self.assertEqual(cb.items, [])

    def test_a_copy_made_meanwhile_is_left_alone(self):
        before = [(keys.CF_UNICODETEXT, "", UTEXT)]
        newer = [(keys.CF_UNICODETEXT, "", "newer\0".encode("utf-16-le"))]
        cb = FakeClipboard(before)
        sender = FakeSender(on_send=lambda what: cb.user_copies(newer))
        self.paste(cb, sender=sender)
        self.assertEqual(cb.items, newer)

    def test_excluded_original_stays_excluded(self):
        # e.g. a password manager's copy: its own markers are kept.
        before = [(keys.CF_UNICODETEXT, "", UTEXT),
                  reg(0xC107, "ExcludeClipboardContentFromMonitorProcessing",
                      b"\0")]
        cb = FakeClipboard(before)
        self.paste(cb)
        self.assertIn((0xC107, "ExcludeClipboardContentFromMonitorProcessing",
                       b"\0"), cb.items)

    def test_restore_off_leaves_the_text(self):
        cb = FakeClipboard([(keys.CF_UNICODETEXT, "", UTEXT)])
        self.assertEqual(self.paste(cb, restore=False), "pasted")
        self.assertEqual(self.data(cb.items), keys.text_items(TEXT))

    def test_restore_off_still_keeps_the_text_out_of_clipboard_history(self):
        # The text stays on the clipboard, so it carries the same markers as
        # the temporary text of the restoring path.
        cb = FakeClipboard([(keys.CF_UNICODETEXT, "", UTEXT)])
        self.assertEqual(self.paste(cb, restore=False), "pasted")
        self.assertPrivate(cb.items)
        self.assertPrivate(cb.writes[0])
        self.assertEqual(len(cb.writes), 1)
        self.assertEqual(self.sender.calls, [("ctrl_v",)])

class FakeTimers:
    """keys.paste_text's schedule(delay, fn): nothing runs by itself."""

    class Handle:
        def __init__(self, delay, fn):
            self.delay, self.fn, self.cancelled = delay, fn, False

        def cancel(self):
            self.cancelled = True

    def __init__(self):
        self.handles = []

    def __call__(self, delay, fn):
        handle = self.Handle(delay, fn)
        self.handles.append(handle)
        return handle

    def fire_live(self):
        """Run the timers that were not cancelled (what the clock would do)."""
        for handle in self.handles:
            if not handle.cancelled:
                handle.fn()


class DeferredRestore(Base):
    """P2: the clipboard is put back on a timer, not on the worker thread."""

    BEFORE = [(keys.CF_UNICODETEXT, "", UTEXT),
              reg(0xC100, "HTML Format", HTML)]

    def go(self, cb, timers, text=TEXT, sleeps=None):
        self.sender = FakeSender()
        slept = [] if sleeps is None else sleeps
        return keys.paste_text(text, True, clipboard=cb, sender=self.sender,
                               ready=lambda: True, sleep=slept.append,
                               schedule=timers)

    def test_the_worker_does_not_wait_for_the_restore(self):
        cb, timers, slept = FakeClipboard(self.BEFORE), FakeTimers(), []
        self.assertEqual(self.go(cb, timers, sleeps=slept), "pasted")
        self.assertNotIn(0.5, slept)              # no linger on this thread
        self.assertEqual(self.sender.calls, [("ctrl_v",)])
        self.assertEqual([h.delay for h in timers.handles], [0.5])
        self.assertIn(keys.text_items(TEXT)[0], cb.items)    # still ours
        timers.fire_live()
        self.assertEqual(self.data(cb.items), self.BEFORE)

    def test_inline_mode_still_waits_and_restores(self):
        cb, slept = FakeClipboard(self.BEFORE), []
        keys.paste_text(TEXT, True, clipboard=cb, sender=FakeSender(),
                        ready=lambda: True, sleep=slept.append, defer=False)
        self.assertIn(0.5, slept)
        self.assertEqual(self.data(cb.items), self.BEFORE)

    def test_a_run_of_quick_dictations_restores_once_at_the_end(self):
        cb, timers = FakeClipboard(self.BEFORE), FakeTimers()
        for n in range(3):
            self.go(cb, timers, text=f"{TEXT} {n}")
        # Three temporary texts so far; the earlier timers were cancelled.
        self.assertEqual(len(cb.writes), 3)
        self.assertEqual([h.cancelled for h in timers.handles],
                         [True, True, False])
        timers.fire_live()
        self.assertEqual(len(cb.writes), 4)       # exactly one restore
        self.assertEqual(self.data(cb.items), self.BEFORE)
        timers.fire_live()                        # nothing pending: no more
        keys.restore_pending_clipboard()
        self.assertEqual(len(cb.writes), 4)

    def test_a_paste_during_a_pending_restore_never_snapshots_our_text(self):
        cb, timers = FakeClipboard(self.BEFORE), FakeTimers()
        self.go(cb, timers, text="first temporary text")
        reads = []
        real_read = cb.read_all
        cb.read_all = lambda *a: (reads.append(1), real_read(*a))[1]
        self.go(cb, timers, text="second temporary text")
        self.assertEqual(reads, [])               # the original was carried
        timers.fire_live()
        self.assertEqual(self.data(cb.items), self.BEFORE)
        flat = b"".join(d for _, _, d in cb.items if d)
        self.assertNotIn("temporary".encode("utf-16-le"), flat)

    def test_something_the_user_copied_meanwhile_is_not_overwritten(self):
        newer = [(keys.CF_UNICODETEXT, "", "newer\0".encode("utf-16-le"))]
        cb, timers = FakeClipboard(self.BEFORE), FakeTimers()
        self.go(cb, timers)
        cb.user_copies(newer)
        timers.fire_live()
        self.assertEqual(cb.items, newer)

    def test_a_copy_between_two_dictations_becomes_the_new_original(self):
        newer = [(keys.CF_UNICODETEXT, "", "newer\0".encode("utf-16-le"))]
        cb, timers = FakeClipboard(self.BEFORE), FakeTimers()
        self.go(cb, timers, text="first temporary text")
        cb.user_copies(newer)                     # the user copied something
        self.go(cb, timers, text="second temporary text")
        timers.fire_live()
        self.assertEqual(self.data(cb.items), newer)   # not BEFORE

    def test_shutdown_with_a_restore_pending_restores_first(self):
        cb, timers = FakeClipboard(self.BEFORE), FakeTimers()
        self.go(cb, timers)
        self.assertTrue(keys.restore_pending_clipboard())
        self.assertEqual(self.data(cb.items), self.BEFORE)
        self.assertTrue(timers.handles[0].cancelled)
        self.assertFalse(keys.restore_pending_clipboard())   # only once

    def test_restore_off_after_a_pending_restore_puts_the_original_back_first(self):
        cb, timers = FakeClipboard(self.BEFORE), FakeTimers()
        self.go(cb, timers)
        keys.paste_text("kept text", False, clipboard=cb, sender=FakeSender(),
                        ready=lambda: True, sleep=lambda s: None)
        self.assertEqual(self.data(cb.items), keys.text_items("kept text"))
        timers.fire_live()                        # the cancelled timer: no-op
        self.assertEqual(self.data(cb.items), keys.text_items("kept text"))

    def test_a_real_timer_restores_by_itself(self):
        import time
        cb = FakeClipboard(self.BEFORE)
        keys.paste_text(TEXT, True, clipboard=cb, sender=FakeSender(),
                        ready=lambda: True, sleep=lambda s: None, linger=0.05)
        end = time.time() + 3
        while time.time() < end and self.data(cb.items) != self.BEFORE:
            time.sleep(0.01)
        self.assertEqual(self.data(cb.items), self.BEFORE)

class TypeInstead(Base):
    def test_uncopyable_content_is_never_touched_text_is_typed(self):
        before = [(0x0080, "", None),            # CF_OWNERDISPLAY
                  (keys.CF_UNICODETEXT, "", UTEXT)]
        cb = FakeClipboard(before)
        self.assertEqual(self.paste(cb), "typed")
        self.assertEqual(cb.writes, [])
        self.assertEqual(self.sender.calls, [("type", TEXT)])

    def test_unreadable_clipboard_is_never_touched(self):
        cb = FakeClipboard([(keys.CF_DIB, "", DIB)], readable=False)
        self.assertEqual(self.paste(cb), "typed")
        self.assertEqual(cb.writes, [])

    def test_too_big_is_typed(self):
        cb = FakeClipboard([(keys.CF_DIB, "", DIB)])
        snap = keys.take_snapshot(cb, max_bytes=100)
        self.assertEqual(snap.kind, "unrestorable")
        self.assertNotIn("BM-ish", repr(snap))    # never the content

    def test_text_events(self):
        ev = keys.text_events("a\nb\U0001F600")
        uni = [e for e in ev if e[2] & keys.KEYEVENTF_UNICODE]
        self.assertEqual([e[1] for e in uni[::2]],
                         [ord("a"), ord("b"), 0xD83D, 0xDE00])
        # The line break is Shift+Enter (a new line, not "send").
        self.assertIn((keys.VK_SHIFT, 0, 0), ev)
        self.assertIn((keys.VK_RETURN, 0, 0), ev)
        self.assertEqual(len(keys.text_events("x\r\ny")),
                         len(keys.text_events("x\ny")))


class Blocked(Base):
    def test_held_key_sends_nothing_and_keeps_the_clipboard(self):
        now = [0.0]

        def sleep(s):
            now[0] += s
        before = [(keys.CF_UNICODETEXT, "", UTEXT)]
        cb = FakeClipboard(before)
        sender = FakeSender()
        with self.assertRaises(keys.PasteBlocked):
            keys.paste_text(TEXT, True, clipboard=cb, sender=sender,
                            ready=lambda: False, wait=1.0, sleep=sleep,
                            clock=lambda: now[0])
        self.assertEqual(sender.calls, [])
        self.assertEqual(cb.items, before)

    def test_guard_refusal_means_no_keys_and_clipboard_restored(self):
        now = [0.0]

        def sleep(s):
            now[0] += s
        import contextlib

        @contextlib.contextmanager
        def guard():
            yield False                # e.g. a recording just started
        before = [(keys.CF_UNICODETEXT, "", UTEXT)]
        cb = FakeClipboard(before)
        sender = FakeSender()
        with self.assertRaises(keys.PasteBlocked):
            keys.paste_text(TEXT, True, clipboard=cb, sender=sender,
                            ready=lambda: True, guard=guard, wait=0.5,
                            sleep=sleep, clock=lambda: now[0])
        self.assertEqual(sender.calls, [])
        self.assertEqual(self.data(cb.items), before)


if __name__ == "__main__":
    unittest.main()
