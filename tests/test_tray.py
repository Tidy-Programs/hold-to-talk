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
import unittest
import uuid
from pathlib import Path

import keys
import settings
import tray


def dev(index, name, hostapi, default=False):
    return {"index": index, "name": name, "hostapi": hostapi, "channels": 2,
            "default_rate": 44100.0, "is_default": default}


# A typical sounddevice list (invented names): one mic per host API.
DEVICES = [
    dev(0, "Microsoft Sound Mapper - Input", "MME"),
    dev(1, "Microphone (Zorvex Studio 4x4 G", "MME", default=True),
    dev(2, "Headset Microphone (Quillon Li", "MME"),
    dev(6, "Primary Sound Capture Driver", "Windows DirectSound"),
    dev(7, "Microphone (Zorvex Studio 4x4 Gen 2)", "Windows DirectSound"),
    dev(8, "Headset Microphone (Quillon Link 300)", "Windows DirectSound"),
    dev(15, "Microphone (Zorvex Studio 4x4 Gen 2)", "Windows WASAPI"),
    dev(16, "Headset Microphone (Quillon Link 300)", "Windows WASAPI"),
    dev(18, "Microphone (Fakechip HD Audio Mic input)", "Windows WDM-KS"),
    dev(20, "Stereo Mix (Fakechip HD Audio Stereo input)", "Windows WDM-KS"),
    dev(23, "Microphone (Zorvex Studio 4x4 Gen 2)", "Windows WDM-KS"),
]


def fake_resolve(spec):
    """audio.resolve_device's rules over DEVICES (MME preferred)."""
    if isinstance(spec, int) or (isinstance(spec, str) and spec.isdigit()):
        return int(spec)
    want = spec.lower()
    matches = [d for d in DEVICES if want in d["name"].lower()]
    if not matches:
        raise ValueError(spec)
    exact = [d for d in matches if d["name"].lower() == want]
    for pool in (exact, matches):
        for d in pool:
            if d["hostapi"] == "MME":
                return d["index"]
    return (exact or matches)[0]["index"]


class Devices(unittest.TestCase):
    def test_one_entry_per_physical_mic(self):
        groups = tray.dedupe_devices(DEVICES)
        self.assertEqual([g["name"] for g in groups],
                         ["Microphone (Zorvex Studio 4x4 Gen 2)",
                          "Headset Microphone (Quillon Link 300)"])
        self.assertEqual(sorted(groups[0]["indices"]), [1, 7, 15, 23])
        self.assertEqual(sorted(groups[1]["indices"]), [2, 8, 16])
        self.assertTrue(groups[0]["is_default"])
        self.assertFalse(groups[1]["is_default"])

    def test_wdm_ks_only_list_is_still_shown(self):
        only = [dev(3, "Mic A", "Windows WDM-KS"), dev(4, "Mic A",
                                                      "Windows WDM-KS")]
        self.assertEqual([g["name"] for g in tray.dedupe_devices(only)],
                         ["Mic A"])

    def test_short_names_are_not_prefix_merged(self):
        groups = tray.dedupe_devices([dev(1, "Mic", "MME"),
                                      dev(2, "Mic 2", "MME")])
        self.assertEqual(len(groups), 2)

    def test_config_value_is_a_name_that_resolves_back(self):
        groups = tray.dedupe_devices(DEVICES)
        value = tray.device_config_value(groups[0], fake_resolve)
        self.assertEqual(value, "Microphone (Zorvex Studio 4x4 G")
        self.assertIs(tray.current_device_group(groups, value, fake_resolve),
                      groups[0])
        self.assertIs(tray.current_device_group(groups, 16, fake_resolve),
                      groups[1])
        self.assertIsNone(tray.current_device_group(groups, None,
                                                    fake_resolve))
        self.assertIsNone(tray.current_device_group(groups, "nope",
                                                    fake_resolve))

    def test_real_device_list_does_not_crash(self):
        groups = tray.dedupe_devices(tray.audio.list_devices())
        names = [g["name"].lower() for g in groups]
        self.assertEqual(len(names), len(set(names)))


class Hotkeys(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(settings.normalize_hotkey(" Ctrl + Windows "),
                         "ctrl+windows")
        self.assertEqual(settings.normalize_hotkey("Right  Alt"), "right alt")
        for bad in ("", "  ", "ctrl+", "hyper", "win+windows", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                settings.normalize_hotkey(bad)

    def test_combo_from_captured_keys(self):
        self.assertEqual(keys.combo_from_vks({0xA2, 0x5B}), "ctrl+windows")
        self.assertEqual(keys.combo_from_vks({0x11, 0xA2, 0x10, 0x44}),
                         "ctrl+shift+d")
        self.assertEqual(keys.combo_from_vks({0xA5}), "alt")
        self.assertEqual(keys.combo_from_vks({0x78}), "f9")
        self.assertEqual(keys.combo_from_vks({0x01}), "")      # mouse button
        for vks in ({0xA2, 0x5B}, {0x14}, {0x20, 0x11}, {0x31, 0x12}):
            keys.parse_hotkey(keys.combo_from_vks(vks))          # all valid

    def test_plain_words(self):
        self.assertEqual(keys.plain_hotkey("ctrl+windows"), "Ctrl + Windows")
        self.assertEqual(keys.plain_hotkey("mouse4"), "Mouse Back button")
        self.assertEqual(keys.plain_hotkey("XButton2"), "Mouse Forward button")
        self.assertEqual(keys.plain_hotkey("ctrl+mouse3"),
                         "Ctrl + Middle mouse button")
        self.assertEqual(keys.plain_hotkey("right alt"), "Right Alt")
        self.assertEqual(keys.plain_hotkey("f9"), "F9")
        self.assertEqual(keys.plain_hotkey("hyper"), "hyper")      # invalid


class FormValues(unittest.TestCase):
    def test_language(self):
        self.assertIsNone(settings.normalize_language("Auto-detect"))
        self.assertIsNone(settings.normalize_language(""))
        self.assertIsNone(settings.normalize_language(None))
        self.assertEqual(settings.normalize_language("en - English"), "en")
        self.assertEqual(settings.normalize_language(" DE "), "de")
        for bad in ("klingon", 5):
            with self.assertRaises(ValueError):
                settings.normalize_language(bad)
        self.assertIn(("en", "English"), settings.languages())

    def test_cleanup_enabled_matches_cleanup_stage(self):
        # The menu tick must match what cleanup.clean() does: a missing or
        # malformed section means cleanup.DEFAULTS (enabled).
        import cleanup
        self.assertEqual(settings.cleanup_enabled({}),
                         cleanup.DEFAULTS["enabled"])
        self.assertEqual(settings.cleanup_enabled({"cleanup": True}),
                         cleanup.DEFAULTS["enabled"])
        self.assertTrue(settings.cleanup_enabled({"cleanup": {"enabled": 1}}))
        self.assertFalse(settings.cleanup_enabled(
            {"cleanup": {"enabled": False}}))


class FakeApp:
    """reload_config() like app.App._reload, against a temp config path."""

    def __init__(self, path):
        self.path = path
        self.cfg = settings.load_config(path)
        self.paused = False

    def reload_config(self):
        old, new = self.cfg, settings.load_config(self.path)
        result = {"applied": [], "restart_required": [], "errors": []}
        if new.get("hotkey") != old.get("hotkey"):
            try:
                keys.parse_hotkey(new.get("hotkey"))
            except ValueError as exc:
                result["errors"].append(f"hotkey: {exc}")
                new["hotkey"] = old["hotkey"]
        for k in sorted(set(old) | set(new)):
            if old.get(k) != new.get(k):
                result["applied"].append(k)
        self.cfg = new
        return result


class ConfigEdits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.path.write_text(json.dumps({
            "hotkey": "ctrl+windows", "future_key": {"x": [1, 2]},
            "cleanup": {"enabled": False, "model": "m", "extra": [1]},
        }), encoding="utf-8")
        self.app = FakeApp(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def data(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_round_trip_keeps_unknown_and_nested_keys(self):
        changes = {
            "language": "en", "vocabulary": ["Zürich"],
            "dictionary": [{"heard": "jason", "wanted": "JSON"}],
            "snippets": [{"trigger": "my sig", "text": "Best,\nQ"}],
            "cleanup": {"enabled": True}, "trailing_space": False,
        }
        result = settings.apply_changes(self.app, changes, self.path)
        self.assertEqual(result["errors"], [])
        data = self.data()
        self.assertEqual(data["future_key"], {"x": [1, 2]})
        self.assertEqual(data["cleanup"],
                         {"enabled": True, "model": "m", "extra": [1]})
        self.assertEqual(data["snippets"][0]["text"], "Best,\nQ")
        self.assertEqual(data["dictionary"],
                         [{"heard": "jason", "wanted": "JSON"}])
        self.assertEqual(data["hotkey"], "ctrl+windows")
        self.assertIs(data["trailing_space"], False)
        self.assertTrue(settings.cleanup_enabled(self.app.cfg))
        self.assertEqual(os.listdir(self.tmp.name), ["config.json"])

    def test_cleanup_toggle_when_missing(self):
        self.path.write_text("{}", encoding="utf-8")
        self.app = FakeApp(self.path)
        settings.apply_changes(self.app, {"cleanup": {"enabled": True}},
                           self.path)
        self.assertEqual(self.data()["cleanup"], {"enabled": True})

    def test_rejected_value_is_rolled_back_in_file(self):
        result = settings.apply_changes(self.app, {"hotkey": "hyper",
                                               "language": "de"}, self.path)
        self.assertEqual(result["rejected"], ["hotkey"])
        self.assertTrue(result["errors"][0].startswith("hotkey:"))
        data = self.data()
        self.assertEqual(data["hotkey"], "ctrl+windows")
        self.assertEqual(data["language"], "de")         # the rest applied
        self.assertEqual(self.app.cfg["hotkey"], "ctrl+windows")

    def test_reload_failure_is_reported(self):
        def boom():
            raise TimeoutError("The Tk thread did not respond")
        self.app.reload_config = boom
        result = settings.apply_changes(self.app, {"language": "fr"}, self.path)
        self.assertIn("did not respond", result["errors"][0])


class Autostart(unittest.TestCase):
    def setUp(self):
        self.name = f"HoldToTalkTest-{uuid.uuid4().hex[:8]}"
        self.real_before = tray.get_autostart(tray.AUTOSTART_NAME)

    def tearDown(self):
        tray.set_autostart(False, self.name)
        self.assertIsNone(tray.get_autostart(self.name))
        # The real entry is never touched.
        self.assertEqual(tray.get_autostart(tray.AUTOSTART_NAME),
                         self.real_before)

    def test_command(self):
        self.assertEqual(
            tray.autostart_command(r"C:\venv\Scripts\pythonw.exe",
                                   r"C:\app\dictate.py"),
            r'"C:\venv\Scripts\pythonw.exe" "C:\app\dictate.py"')
        cmd = tray.autostart_command()
        exe, script = cmd.split('" "')
        self.assertTrue(exe.lower().endswith('pythonw.exe'))
        self.assertTrue(Path(exe.strip('"')).exists())
        self.assertEqual(Path(script.strip('"')),
                         settings.APP_DIR / "dictate.py")
        self.assertTrue(Path(script.strip('"')).exists())

    def test_add_and_remove_throwaway_entry(self):
        self.assertFalse(tray.autostart_enabled(self.name))
        cmd = tray.autostart_command()
        tray.set_autostart(True, self.name, cmd)
        self.assertEqual(tray.get_autostart(self.name), cmd)
        self.assertTrue(tray.autostart_enabled(self.name, cmd))
        self.assertFalse(tray.autostart_enabled(self.name, '"x" "y"'))
        tray.set_autostart(False, self.name)
        self.assertIsNone(tray.get_autostart(self.name))
        self.assertFalse(tray.autostart_enabled(self.name))
        tray.set_autostart(False, self.name)        # removing twice is fine


class Menu(unittest.TestCase):
    """The menu is built without starting the icon (no window, no thread)."""

    def handle(self, input_device):
        app = type("App", (), {})()
        app.cfg = {"hotkey": "f23", "input_device": input_device,
                   "cleanup": {"enabled": False}}
        app.paused, app.status, app.recorder = False, "ready", None
        app.mouse_hook_state = "off"
        h = tray.TrayHandle.__new__(tray.TrayHandle)
        h.app, h.on_open, h.config_path = app, lambda: None, None
        h.autostart_name, h._groups = None, []
        return h

    def walk(self, menu):
        """Every item's text and state, as pystray evaluates them."""
        out = []
        for item in menu.items:
            out.append((item.text, item.checked, item.enabled, item.visible))
            if item.submenu:
                out += self.walk(item.submenu)
        return out

    def setUp(self):
        self.saved = tray.audio.list_devices
        tray.audio.list_devices = lambda: [
            dev(0, "Microphone (Zorvex USB Fake)", "MME", default=True)]

    def tearDown(self):
        tray.audio.list_devices = self.saved

    def test_unplugged_microphone_menu_builds(self):
        # W4: the chosen microphone is unplugged -> its "(not found)" item
        # was built with checked=True and pystray raised ValueError: True.
        items = self.walk(self.handle("Zorvex Headset Fake")._build_menu())
        texts = [t for t, *_ in items]
        self.assertIn("Zorvex Headset Fake (not connected)", texts)
        self.assertIn(tray.DEFAULT_MIC_LABEL, texts)
        missing = items[texts.index("Zorvex Headset Fake (not connected)")]
        self.assertEqual(missing[1:3], (True, False))   # checked, disabled

    def test_every_item_evaluates(self):
        for value in (None, "Zorvex Headset Fake", 0):
            for _, checked, enabled, visible in self.walk(
                    self.handle(value)._build_menu()):
                self.assertIn(checked, (True, False, None))
                self.assertIsInstance(enabled, bool)
                self.assertIsInstance(visible, bool)

    def test_same_words_as_the_window(self):
        h = self.handle(None)
        h.app.status = "loading"
        self.assertEqual(h._status_text(), "Getting ready…")
        h.app.status = "failed"
        self.assertEqual(h._status_text(), "Can’t dictate right now")

    def test_the_download_and_its_failure_are_in_the_tooltip(self):
        h = self.handle(None)
        h.app.status = "loading"
        h.app.download_progress = lambda: {"size": "about 0.5 GB",
                                           "percent": 42}
        self.assertEqual(h._status_text(), "Downloading the speech model… 42%")
        h.app.download_progress = lambda: {"size": None, "percent": None}
        self.assertEqual(h._status_text(), "Downloading the speech model…")
        h.app.status = "failed"
        h.app.failure = {"kind": "connection", "advice": "x",
                         "reason": "no connection to huggingface.co"}
        h.app.auto_retry_in = lambda: 5.0
        self.assertEqual(h._status_text(), "Speech model not downloaded: no "
                         "connection to huggingface.co. Trying again soon.")
        self.assertLessEqual(len(f"{tray.settings.APP_NAME}: "
                                 + h._status_text()), 127)
        self.assertEqual(h._signature()[-1], h._status_text())


class Icons(unittest.TestCase):
    def test_states(self):
        imgs = {s: tray.make_icon(s) for s in ("ready", "paused", "listening")}
        for img in imgs.values():
            self.assertEqual(img.size, (64, 64))
        self.assertNotEqual(imgs["ready"].getpixel((10, 32)),
                            imgs["paused"].getpixel((10, 32)))
        self.assertNotEqual(imgs["ready"].getpixel((10, 32)),
                            imgs["listening"].getpixel((10, 32)))


if __name__ == "__main__":
    unittest.main()
