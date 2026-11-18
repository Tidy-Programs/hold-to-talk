import fakes  # noqa: F401  (HOLDTOTALK_HOME -> a temporary folder first)

import unittest

import keys
from app import App


class ParseHotkey(unittest.TestCase):
    def test_default(self):
        self.assertEqual(keys.parse_hotkey("ctrl+windows"),
                         [(0x11,), (0x5B, 0x5C)])

    def test_case_and_spaces(self):
        self.assertEqual(keys.parse_hotkey(" Right  Alt + F9 "),
                         [(0xA5,), (0x78,)])

    def test_letters_digits_and_function_keys(self):
        self.assertEqual(keys.parse_hotkey("ctrl+shift+d"),
                         [(0x11,), (0x10,), (ord("D"),)])
        self.assertEqual(keys.parse_hotkey("alt+1"), [(0x12,), (ord("1"),)])
        self.assertEqual(keys.parse_hotkey("f1"), [(0x70,)])
        self.assertEqual(keys.parse_hotkey("f24"), [(0x87,)])

    def test_single_key(self):
        self.assertEqual(keys.parse_hotkey("caps lock"), [(0x14,)])

    def test_errors(self):
        for bad in ("", "ctrl+", "ctrl++v", "hyper", "ctrl+ü", "ctrl+ctrl",
                    "win+windows", None, 5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    keys.parse_hotkey(bad)


class MoreKeys(unittest.TestCase):
    """F1: the keys people ask for, with obvious aliases, all capturable."""

    def test_names_and_aliases(self):
        for names, vk in ((("home",), 0x24), (("end",), 0x23),
                          (("page up", "pgup", "PageUp"), 0x21),
                          (("page down", "pgdn"), 0x22),
                          (("insert", "ins"), 0x2D), (("delete", "del"), 0x2E),
                          (("up", "up arrow", "arrow up"), 0x26),
                          (("down",), 0x28), (("left",), 0x25),
                          (("right", "right arrow"), 0x27),
                          (("num 0", "numpad 0", "num0"), 0x60),
                          (("num 9",), 0x69), (("num lock",), 0x90),
                          (("num multiply", "num *"), 0x6A),
                          (("num plus", "num add"), 0x6B),
                          (("num minus", "num -"), 0x6D),
                          (("num decimal", "num ."), 0x6E),
                          (("num divide", "num /"), 0x6F),
                          (("print screen", "prtsc", "printscreen"), 0x2C),
                          (("semicolon", ";"), 0xBA), (("equals", "="), 0xBB),
                          (("comma", ","), 0xBC), (("minus", "-"), 0xBD),
                          (("period", "."), 0xBE), (("slash", "/"), 0xBF),
                          (("backtick", "`"), 0xC0),
                          (("left bracket", "["), 0xDB),
                          (("backslash", "\\"), 0xDC),
                          (("right bracket", "]"), 0xDD),
                          (("quote", "'"), 0xDE),
                          (("volume mute", "mute"), 0xAD),
                          (("volume up",), 0xAF), (("volume down",), 0xAE),
                          (("play pause", "media play pause"), 0xB3),
                          (("next track",), 0xB0),
                          (("previous track", "prev track"), 0xB1)):
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(keys.parse_hotkey(name), [(vk,)])
        self.assertEqual(keys.parse_hotkey("ctrl+;"), [(0x11,), (0xBA,)])
        self.assertEqual(keys.parse_hotkey("ctrl+-"), [(0x11,), (0xBD,)])

    def test_every_key_can_be_captured_and_parsed_back(self):
        for vk, name in keys.PLAIN_KEYS.items():
            with self.subTest(name=name):
                self.assertIn(vk, keys.CAPTURE_VKS)
                self.assertNotIn("+", name)
                spec = keys.combo_from_vks({0xA2, vk})
                self.assertEqual(keys.parse_hotkey(spec)[-1], (vk,))
        self.assertEqual(keys.combo_from_vks({0xA0, 0x21}), "shift+page up")
        self.assertEqual(keys.combo_from_vks({0x65}), "num 5")

    def test_plain_words(self):
        self.assertEqual(keys.plain_hotkey("pgup"), "Page Up")
        self.assertEqual(keys.plain_hotkey("ctrl+num 5"), "Ctrl + Num 5")
        self.assertEqual(keys.plain_hotkey("num plus"), "Num +")
        self.assertEqual(keys.plain_hotkey("volume up"), "Volume Up")
        self.assertEqual(keys.plain_hotkey("up"), "Up arrow")
        semi = keys.plain_hotkey("ctrl+semicolon")
        self.assertTrue(semi.startswith("Ctrl + ") and len(semi) == 8, semi)


class CompileHotkey(unittest.TestCase):
    def test_modifier_families_are_ignored_as_other_keys(self):
        groups, other, mask = App._compile_hotkey("ctrl+windows")
        for vk in (0x11, 0xA2, 0xA3, 0x5B, 0x5C, keys.VK_MASK):
            self.assertNotIn(vk, other)
        for vk in (0x10, 0x12, ord("A"), 0x20):
            self.assertIn(vk, other)
        self.assertTrue(mask)          # Win in the hotkey: mask the Start menu

    def test_no_mask_without_win_or_alt(self):
        self.assertFalse(App._compile_hotkey("ctrl+shift+space")[2])
        self.assertTrue(App._compile_hotkey("right alt")[2])


if __name__ == "__main__":
    unittest.main()
