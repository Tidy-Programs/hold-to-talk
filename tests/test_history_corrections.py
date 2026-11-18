import atexit
import os
import shutil
import tempfile

# Never touch real user data: the data folder is a temporary one (also shared
# with test_00_home.py) before any project module is imported.
if "htt-test-home-" not in os.environ.get("HOLDTOTALK_HOME", ""):
    _home = tempfile.mkdtemp(prefix="htt-test-home-")
    os.environ["HOLDTOTALK_HOME"] = _home
    atexit.register(shutil.rmtree, _home, True)


import datetime
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

import textrules

OLD_SCHEMA = ("CREATE TABLE history ( id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " ts REAL NOT NULL, raw TEXT NOT NULL, final TEXT NOT NULL,"
              " seconds REAL)")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="htt-corr-")
        self.path = Path(self.dir) / "h.sqlite3"
        self.opened = []
        self.saved_wait = textrules.READ_FLUSH_WAIT
        textrules.READ_FLUSH_WAIT = 5.0

    def tearDown(self):
        textrules.READ_FLUSH_WAIT = self.saved_wait
        for h in self.opened:
            h.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def H(self, cfg=None, **kw):
        h = textrules.History(self.path, cfg=cfg, **kw)
        self.opened.append(h)
        return h

    def make_old_db(self, rows):
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(OLD_SCHEMA)
        conn.execute("CREATE INDEX history_ts ON history(ts)")
        for r in rows:
            conn.execute("INSERT INTO history (ts, raw, final, seconds)"
                         " VALUES (?, ?, ?, ?)", r)
        conn.close()

    def set_ts(self, entry_id, ts):
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.execute("UPDATE history SET ts = ? WHERE id = ?", (ts, entry_id))
        conn.close()


class Migration(Base):
    def test_old_database_is_upgraded_in_place(self):
        now = time.time()
        self.make_old_db([(now - 10, "um hi", "Hi there.", 1.5),
                          (now - 5, "send jason", "Send JSON.", 2.0)])
        h = self.H()
        with self.assertLogs("holdtotalk.textrules", "INFO") as logs:
            rows = h.recent()
        self.assertIn("upgraded", "\n".join(logs.output))
        self.assertEqual([r["final"] for r in rows], ["Send JSON.", "Hi there."])
        self.assertEqual([r["corrected"] for r in rows], [None, None])
        self.assertEqual(rows[0]["text"], "Send JSON.")
        self.assertEqual(rows[1]["raw"], "um hi")
        self.assertEqual(rows[1]["seconds"], 1.5)
        conn = sqlite3.connect(str(self.path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(history)")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        self.assertEqual(cols, set(textrules.HISTORY_COLUMNS))
        self.assertEqual(version, textrules.HISTORY_SCHEMA)
        # Old rows (words NULL) are counted on the fly.
        s = h.stats()
        self.assertEqual(s["all"], {"dictations": 2, "words": 4,
                                    "seconds": 3.5})
        # Corrections work on migrated rows, and new rows can be added.
        self.assertEqual(h.correct(rows[0]["id"], "Send Jason.")["corrected"],
                         "Send Jason.")
        h.record("x", "New one", 1)
        self.assertEqual(h.count(), 3)
        h.close()
        # Re-opening an upgraded file changes nothing.
        h2 = self.H()
        self.assertEqual(h2.get(rows[0]["id"])["corrected"], "Send Jason.")

    def test_old_app_can_still_write_after_upgrade(self):
        self.make_old_db([(time.time(), "a", "A", 1.0)])
        h = self.H()
        h.count()                                       # upgrades
        old = sqlite3.connect(str(self.path), isolation_level=None)
        old.execute("INSERT INTO history (ts, raw, final, seconds)"
                    " VALUES (?, ?, ?, ?)", (time.time(), "b", "B two", 2.0))
        old.execute("SELECT id, ts, raw, final, seconds FROM history"
                    " ORDER BY id DESC LIMIT 5").fetchall()
        old.close()
        self.assertEqual([r["final"] for r in h.recent()], ["B two", "A"])
        self.assertEqual(h.stats()["all"]["words"], 3)

    def test_concurrent_upgrade_by_two_instances(self):
        self.make_old_db([(time.time(), "a", "A", 1.0)])
        errors, results = [], []
        barrier = threading.Barrier(4)

        def open_and_read():
            try:
                h = textrules.History(self.path, busy_timeout=5.0)
                barrier.wait()
                results.append(len(h.recent()))
                h.close()
            except Exception as exc:          # pragma: no cover
                errors.append(exc)
        threads = [threading.Thread(target=open_and_read) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(results, [1, 1, 1, 1])

    def test_new_database_has_new_schema(self):
        h = self.H()
        h.record("r", "F", 1)
        self.assertEqual(h.recent()[0]["corrected"], None)
        conn = sqlite3.connect(str(self.path))
        words = conn.execute("SELECT words FROM history").fetchone()[0]
        conn.close()
        self.assertEqual(words, 1)


class Corrections(Base):
    def test_correct_get_uncorrect(self):
        h = self.H()
        h.record("raw a", "Open the server folder.", 1)
        h.record("raw b", "Second.", 1)
        a, b = sorted((r["id"] for r in h.recent()))
        before = time.time()
        row = h.correct(a, "Open the Zorvex folder.")
        self.assertEqual(row["final"], "Open the server folder.")   # kept
        self.assertEqual(row["corrected"], "Open the Zorvex folder.")
        self.assertEqual(row["text"], "Open the Zorvex folder.")
        self.assertGreaterEqual(row["corrected_ts"], before - 1)
        self.assertEqual(h.get(a), row)
        self.assertIsNone(h.get(b)["corrected"])
        self.assertEqual([r["id"] for r in h.recent(corrected=True)], [a])
        self.assertEqual([r["id"] for r in h.recent(corrected=False)], [b])
        self.assertEqual(len(h.recent(corrected=None)), 2)
        self.assertEqual([r["id"] for r in h.corrections()], [a])
        self.assertEqual([r["id"] for r in h.search("Zorvex")], [a])
        # correcting again replaces the correction
        self.assertEqual(h.correct(a, "Open the Zorvex files.")["corrected"],
                         "Open the Zorvex files.")
        self.assertTrue(h.uncorrect(a))
        self.assertFalse(h.uncorrect(a))
        self.assertIsNone(h.get(a)["corrected"])
        self.assertEqual(h.get(a)["text"], "Open the server folder.")
        self.assertEqual(h.corrections(), [])

    def test_correct_back_to_final_clears(self):
        h = self.H()
        h.record("r", "Text.", 1)
        i = h.recent()[0]["id"]
        h.correct(i, "Other.")
        row = h.correct(i, "Text.")
        self.assertIsNone(row["corrected"])
        self.assertIsNone(row["corrected_ts"])

    def test_bad_input(self):
        h = self.H()
        h.record("r", "Text.", 1)
        i = h.recent()[0]["id"]
        self.assertIsNone(h.correct(i, ""))
        self.assertIsNone(h.correct(i, "   "))
        self.assertIsNone(h.correct(i, None))
        self.assertIsNone(h.correct(9999, "x"))
        self.assertIsNone(h.correct("nope", "x"))
        self.assertFalse(h.uncorrect("nope"))
        self.assertIsNone(h.get("nope"))
        self.assertIsNone(h.get(9999))
        self.assertIsNone(h.get(i)["corrected"])

    def test_correct_a_just_recorded_row(self):
        h = self.H()
        h.record("r", "Queued.", 1)                 # still in the queue
        rows = h.recent()
        self.assertEqual(h.correct(rows[0]["id"], "Fixed.")["corrected"],
                         "Fixed.")

    def test_after_close(self):
        h = self.H()
        h.record("r", "T", 1)
        i = h.recent()[0]["id"]
        h.close()
        self.assertIsNone(h.correct(i, "x"))
        self.assertFalse(h.uncorrect(i))
        self.assertEqual(h.corrections(), [])
        self.assertEqual(h.stats()["all"]["dictations"], 0)

    def test_concurrent_corrections_and_records(self):
        h = self.H()
        for i in range(20):
            h.record(f"r{i}", f"final {i}", 1)
        ids = [r["id"] for r in h.recent(100)]
        errors = []

        def corrector(k):
            try:
                for n in range(30):
                    i = ids[(k * 7 + n) % len(ids)]
                    if n % 3 == 2:
                        h.uncorrect(i)
                    else:
                        row = h.correct(i, f"fix {k} {n}")
                        assert row is not None and row["id"] == i
                    h.stats()
            except Exception as exc:              # pragma: no cover
                errors.append(exc)

        def recorder():
            try:
                for n in range(40):
                    h.record("x", f"extra {n}", 0.5)
                    h.corrections(5)
            except Exception as exc:              # pragma: no cover
                errors.append(exc)
        threads = [threading.Thread(target=corrector, args=(k,))
                   for k in range(4)]
        threads.append(threading.Thread(target=recorder))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(h.flush(10))
        self.assertEqual(errors, [])
        self.assertEqual(h.count(), 60)
        for row in h.recent(100):
            if row["corrected"] is not None:
                self.assertTrue(row["corrected"].startswith("fix "))
                self.assertIsNotNone(row["corrected_ts"])
        self.assertEqual(h.stats()["corrections"],
                         len(h.recent(100, corrected=True)))


class Stats(Base):
    def test_periods(self):
        h = self.H(cfg={"learned": [{"id": "a"}, {"id": "b"}, "junk"]})
        # Wednesday 2026-11-11 15:00 local time
        wednesday = datetime.datetime(2026, 11, 11, 15, 0)
        self.assertEqual(wednesday.weekday(), 2)
        now = wednesday.timestamp()
        day = 86400
        for final, secs in (("one two three", 1.0), ("four five", 2.0),
                            ("six", 3.0), ("seven eight nine ten", 4.0)):
            h.record("r", final, secs)
        ids = sorted(r["id"] for r in h.recent())
        self.set_ts(ids[0], now - 1)                  # today
        self.set_ts(ids[1], now - 3600 * 14)          # today, 01:00
        self.set_ts(ids[2], now - 2 * day)            # Monday: this week
        self.set_ts(ids[3], now - 4 * day)            # last week
        h.correct(ids[3], "seven eight nine ten eleven")
        s = h.stats(now=now)
        self.assertEqual(s["today"], {"dictations": 2, "words": 5,
                                      "seconds": 3.0})
        self.assertEqual(s["week"], {"dictations": 3, "words": 6,
                                     "seconds": 6.0})
        self.assertEqual(s["all"], {"dictations": 4, "words": 10,
                                    "seconds": 10.0})
        self.assertEqual(s["audio_seconds"], 10.0)
        self.assertEqual(s["corrections"], 1)
        self.assertEqual(s["learned"], 2)
        self.assertEqual(s["since"], now - 4 * day)

    def test_empty(self):
        s = self.H().stats()
        self.assertEqual(s["all"], {"dictations": 0, "words": 0,
                                    "seconds": 0.0})
        self.assertEqual(s["today"]["dictations"], 0)
        self.assertEqual((s["corrections"], s["learned"], s["since"]),
                         (0, 0, None))

    def test_retention_keeps_all_time_counts(self):
        h = self.H(cfg={"textrules": {"history_limit": 3}})
        for i in range(5):
            h.record("r", f"w{i} x", 1.5)
        first = min(r["id"] for r in h.recent())
        h.correct(first, "corrected")
        for i in range(2):
            h.record("r", "y", None)                  # prunes the corrected
        self.assertEqual(h.count(), 3)
        s = h.stats()
        self.assertEqual(s["all"], {"dictations": 7, "words": 12,
                                    "seconds": 7.5})
        self.assertEqual(s["corrections"], 1)
        self.assertEqual(s["today"]["dictations"], 3)  # stored rows only
        h.clear()
        self.assertEqual(h.stats()["all"]["dictations"], 0)

    def test_delete_removes_from_counts(self):
        h = self.H()
        h.record("r", "a b", 1)
        h.record("r", "c", 1)
        h.delete(h.recent()[0]["id"])
        self.assertEqual(h.stats()["all"]["words"], 2)

    def test_cheap(self):
        h = self.H()
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        h.count()
        now = time.time()
        conn.executemany(
            "INSERT INTO history (ts, raw, final, seconds) VALUES (?,?,?,?)",
            [(now - i * 60, "r", "some words in a dictation " * 3, 4.0)
             for i in range(2000)])
        conn.close()
        h.stats()
        started = time.perf_counter()
        for _ in range(10):
            s = h.stats()
        took = (time.perf_counter() - started) / 10
        self.assertEqual(s["all"]["dictations"], 2000)
        self.assertEqual(s["all"]["words"], 2000 * 15)
        self.assertLess(took, 0.1)

    def test_word_count(self):
        self.assertEqual(textrules.word_count("Don't stop, it's 3 o'clock!"), 5)
        self.assertEqual(textrules.word_count(""), 0)
        self.assertEqual(textrules.word_count(None), 0)


if __name__ == "__main__":
    unittest.main()
