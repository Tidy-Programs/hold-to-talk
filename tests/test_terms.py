"""The Terms of Use: TERMS.md itself, the acceptance record (terms.py), the
hotkey doing nothing until they are accepted (app.py), the window's server
(GET /api/terms, POST /api/terms/accept, /api/quit) and the tray's words.

Nothing here touches the real data folder: every test works in a temporary
one (settings.DATA_DIR is replaced), no key, clipboard, audio or window is
real, and the engine is a stand-in."""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import json
import re
import shutil
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import app as app_mod
import keys
import settings
import terms
import test_api
import tray
from app import App
from fakes import FakeRecorder, FakeSounds, accept_terms_in
from test_flow import CTRL, WIN, cfg_with, wait_for

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "TERMS.md").read_text(encoding="utf-8")

# Names of places that must not appear anywhere in the terms (the project
# names no country, state or city), and the sections the terms must have.
PLACES = (
    "United States", "America", "Canada", "Mexico", "Brazil", "Argentina",
    "United Kingdom", "England", "Scotland", "Wales", "Ireland", "France",
    "Germany", "Italy", "Spain", "Netherlands", "Sweden", "Norway", "Denmark",
    "Finland", "Poland", "Switzerland", "Austria", "Belgium", "Portugal",
    "Russia", "China", "Japan", "Korea", "India", "Australia", "New Zealand",
    "Africa", "Israel", "Turkey", "Europe", "European Union", "California",
    "Delaware", "New York", "Texas", "Washington", "Florida", "Oregon",
    "Virginia", "London", "Paris", "Berlin", "Amsterdam", "Dublin", "Madrid",
    "Rome", "Tokyo", "Sydney", "Toronto", "Seattle", "Brussels", "Vienna")
SECTIONS = (
    (1, "Agreeing to these terms"), (2, "Who \"we\" are"),
    (3, "How these terms relate to the licence"),
    (4, "What the app is and is not"), (5, "Your responsibilities"),
    (6, "Speech recognition and AI output"),
    (7, "Other people's software, models and services"), (8, "Privacy"),
    (9, "NO WARRANTY"), (10, "LIMITATION OF LIABILITY"),
    (11, "Assumption of risk"), (12, "Indemnity"),
    (13, "Where the law does not allow these limits"), (14, "Disputes"),
    (15, "Changes"), (16, "Ending"), (17, "General"), (18, "Contact"))


def section(number):
    """The text of one numbered section of TERMS.md (heading excluded)."""
    m = re.search(rf"^## {number}\. .*?\n(.*?)(?=^## |\Z)", TEXT, re.M | re.S)
    return m.group(1).strip()


class TermsText(unittest.TestCase):
    def test_starts_with_the_version_line_that_settings_names(self):
        first = TEXT.split("\n", 1)[0]
        m = re.fullmatch(r"Version (\S+) \u2014 (\d{1,2} [A-Z][a-z]+ \d{4})",
                         first)
        self.assertIsNotNone(m, first)
        self.assertEqual(m.group(1), settings.TERMS_VERSION)
        self.assertEqual(terms.version_line(TEXT), m.groups())

    def test_the_desktop_app_asks_for_the_same_version(self):
        # desktop/ (the Windows and Mac app) builds this TERMS.md in and keeps
        # the same terms-accepted.json record, so both apps must agree.
        source = (ROOT / "desktop" / "src-tauri" / "src" / "terms.rs").read_text(
            encoding="utf-8")
        m = re.search(r'pub const TERMS_VERSION: &str = "([^"]+)";', source)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), settings.TERMS_VERSION)
        self.assertIn('include_str!("../../../TERMS.md")', source)

    def test_every_numbered_section_is_there_in_order(self):
        found = re.findall(r"^## (\d+)\. (.+)$", TEXT, re.M)
        self.assertEqual([(int(n), t) for n, t in found],
                         [(n, t) for n, t in SECTIONS])

    def test_has_a_marked_summary_that_the_full_terms_override(self):
        self.assertIn("## Summary", TEXT)
        summary = terms.summary(TEXT)
        self.assertIn("only a summary", summary)
        self.assertIn("full terms win", summary)
        self.assertLess(TEXT.index("## Summary"), TEXT.index("## 1."))
        self.assertNotIn("## 1.", summary)

    def test_the_two_clauses_the_law_expects_to_stand_out_are_in_capitals(self):
        for number in (9, 10):
            body = re.search(rf"^## {number}\. .*?\n(.*?)(?=^## )", TEXT,
                             re.M | re.S).group(1)
            caps = [p for p in body.split("\n\n") if p.strip().startswith("**")]
            self.assertTrue(caps, number)
            letters = [c for c in caps[0] if c.isalpha()]
            self.assertGreater(len(letters), 400)
            self.assertTrue(all(c.isupper() for c in letters), number)
        self.assertIn("ONE HUNDRED US DOLLARS (US$100)", TEXT)
        self.assertIn("GREATER OF", TEXT)
        self.assertIn('"AS IS" AND "AS AVAILABLE"', TEXT)

    def test_names_no_contact_detail_person_or_place(self):
        self.assertNotRegex(TEXT, r"[\w.+-]+@[\w-]+\.[\w.-]+")
        self.assertNotRegex(TEXT, r"(?i)https?://|www\.|\.(com|org|net|io)\b")
        self.assertNotRegex(TEXT, r"\+?\d[\d ()\-]{7,}\d")           # a phone number
        low = TEXT.lower()
        for place in PLACES:
            self.assertIsNone(re.search(rf"\b{re.escape(place.lower())}\b", low),
                              place)
        for word in ("contributor", "unpaid"):
            self.assertIn(word, low)
        # No personal name: the capitalised words that are not the start of a
        # sentence must all be the names of things (products, companies,
        # documents, the app), never of people.
        things = {"Hold", "Windows", "Python", "NVIDIA", "Ollama", "GitHub",
                  "Microsoft", "Edge", "Google", "Chrome", "MIT", "US", "AI",
                  "LICENSE", "SECURITY.md", "THIRD", "PARTY", "NOTICES.md",
                  "README", "TERMS", "Use", "Terms", "English", "November",
                  "Version", "Summary", "Agree", "PC", "WARRANTY", "LIABILITY",
                  "OF", "I", "SECURITY", "NOTICES", "LICENSE.md",
                  "THIRD-PARTY-NOTICES.md"}
        body = re.sub(r"\*\*[^*]+\*\*", " ", TEXT)        # the capitals clauses
        unknown = set()
        for sentence in re.split(r"(?<=[.:;?!])\s+|\n+", body):
            sentence = re.sub(r"^(#+\s+|-\s+)?(\d+\.\s+)?([a-g]\.\s+)?", "",
                              sentence)
            rest = sentence.split(None, 1)[1:] or [""]    # not the first word
            for word in re.findall(r"\b[A-Z][A-Za-z.\-]+\b", rest[0]):
                if word.rstrip(".") not in things:
                    unknown.add(word)
        self.assertEqual(unknown, set())

    def test_acceptance_is_only_by_an_explicit_step(self):
        low = " ".join(TEXT.lower().split())
        for gone in ("by running it", "by installing the app", "continuing to use",
                     "or continuing", "installing or using it means",
                     "installing a release"):
            self.assertNotIn(gone, low)
        self.assertIn("you accept these terms by choosing or typing i agree in "
                      "the installer, or by choosing i agree in the app", low)
        self.assertIn("the app will not dictate until the current version of "
                      "these terms has been accepted", low)
        self.assertIn("you are asked to accept these terms before the app "
                      "will dictate", low)
        self.assertIn("you accept a new version by the same explicit step", low)

    def test_names_no_number_of_people_and_no_volunteers(self):
        low = TEXT.lower()
        self.assertNotIn("volunteer", low)
        self.assertIn("they are unpaid", low)
        self.assertIn("the people who make it", low)
        for word in ("two people", "three people", "team", "both of us",
                     "several people", "a group of", "both contributors"):
            self.assertIsNone(re.search(rf"\b{word}\b", low), word)

    def test_the_licence_rights_sentence_is_in_section_3(self):
        sec = section(3)
        self.assertIn("Hold to talk is licensed to you, not sold.", sec)
        self.assertIn("you may not pass it on, publish it or sell it", sec)
        self.assertIn("It is not open-source software.", sec)
        self.assertIn("LICENSE governs what you may do with copies of the "
                      "software and with its source code; these terms govern "
                      "everything else about using the app.", sec)
        self.assertIn("each is in addition to the other", sec)
        # What people send to the project: permission, not a transfer.
        self.assertIn("You keep whatever rights you have in it", sec)
        self.assertNotIn("MIT", sec)

    def test_section_10_has_the_carve_out_and_13_does_not_just_repeat_it(self):
        sec = section(10)
        carve = ("Nothing in sections 9 to 12 excludes or limits any "
                 "liability for death or personal injury caused by "
                 "negligence, for fraud or fraudulent misrepresentation, for "
                 "intentional misconduct or gross negligence, or any other "
                 "liability that cannot be excluded or limited under the law "
                 "that applies.")
        self.assertIn(carve, sec)
        self.assertGreater(sec.index(carve), sec.index("(US$100)"))
        thirteen = section(13)
        self.assertNotIn("death or personal injury", thirteen)
        self.assertIn("applies only as far as the law permits", thirteen)
        self.assertIn("rights that cannot be taken away", thirteen)

    def test_section_11_is_only_an_assumption_of_risk(self):
        low = TEXT.lower()
        for gone in ("release each contributor", "known and unknown",
                     "whether you know about them", "unknown claims",
                     "claims you do not know", "the release"):
            self.assertNotIn(gone, low)
        sec = section(11)
        self.assertIn("you accept responsibility for the consequences of the "
                      "risks described in sections 5, 6 and 7", sec)
        self.assertIn("see sections 10 and 13", sec)

    def test_the_indemnity_excludes_the_contributors_own_fault(self):
        self.assertIn("This does not require you to cover any loss to the "
                      "extent it was caused by a contributor's own "
                      "negligence, wrongdoing or breach of these terms.",
                      section(12))

    def test_disputes_use_residence_and_a_reasonable_discovery_period(self):
        sec = section(14)
        self.assertIn("habitual residence", sec)
        self.assertIn("after you knew, or should reasonably have known, of "
                      "the facts giving rise to it. Where the law requires a "
                      "longer minimum period, that period applies.", sec)
        self.assertIn("There is no arbitration clause", sec)
        self.assertIn("class, group or representative action", sec)
        self.assertNotIn("when it arose", sec)

    def test_ending_has_no_termination_of_the_permission_to_use(self):
        sec = section(16)
        self.assertNotIn("ends if you break", sec)
        self.assertNotIn("permission to use", sec)
        self.assertNotIn("MIT", sec)
        self.assertTrue(sec.rstrip().endswith("after you stop using the app."),
                        sec)

    def test_the_whole_agreement_is_terms_and_licence_only(self):
        sec = section(17)
        whole = re.search(r"These terms and LICENSE are the whole agreement"
                          r"[^.]*\.", sec)
        self.assertIsNotNone(whole)
        self.assertNotIn("THIRD-PARTY", whole.group(0))
        self.assertIn("THIRD-PARTY-NOTICES.md is given for information only "
                      "and is not part of this agreement.", sec)
        self.assertIn("Hold to talk is an independent project and is not "
                      "affiliated with, endorsed by or sponsored by the "
                      "makers of any product or service named in its "
                      "documents; those names belong to their owners.", sec)

    def test_privacy_names_every_exception_the_code_has(self):
        sec = " ".join(section(8).split())
        for needle in (
                "only on your own computer, in the app's data folder, and "
                "does not send them anywhere, with the exceptions described "
                "below",
                "spelling hints from your vocabulary, dictionary and snippet "
                "triggers",
                "which by design must be on your own computer (the app "
                "refuses any other address)",
                "belongs to your Windows account or to Windows itself",
                "trusts whatever program answers on that port, your "
                "dictations go to that program, whatever it is",
                "Microsoft Edge or Google Chrome, which make their own "
                "connections to their makers that the app does not control "
                "or inspect",
                "opens as a tab in your usual browser, where that browser's "
                "own extensions and settings apply",
                "download components from their publishers",
                "you decide what it contains"):
            self.assertIn(needle, sec)

    def test_the_summary_matches_the_sections(self):
        summary = " ".join(terms.summary(TEXT).split())
        self.assertIn("You are asked to accept these terms before the app "
                      "will dictate.", summary)
        self.assertNotIn("Installing or using it means", summary)
        self.assertNotIn("volunteers", summary)
        self.assertIn("This does not cover death or personal injury caused by "
                      "negligence", summary)
        self.assertIn("accept responsibility, as far as the law allows, for "
                      "the risks described in sections 5, 6 and 7", summary)
        self.assertIn("habitual residence", summary)
        self.assertNotIn("release", summary)

    def test_does_not_claim_a_law_or_say_it_is_legal_advice(self):
        low = TEXT.lower()
        self.assertNotIn("legal advice", low)
        for claim in ("gdpr", "complies with", "in compliance with",
                      "legally binding", "enforceable"):
            self.assertNotIn(claim, low.replace("cannot be enforced", ""))

    def test_the_licence_forbids_passing_the_app_on_and_nothing_says_mit(self):
        licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
        flat = " ".join(licence.split())
        self.assertTrue(licence.startswith("Hold to talk Licence\n"), licence[:80])
        # The licence and the terms are one agreement: the same version and date.
        self.assertEqual(licence.splitlines()[1].replace(",", " —"),
                         TEXT.splitlines()[0])
        self.assertIn("All rights reserved.", licence[:300])
        self.assertIn("it is not open-source software", flat)
        # May: use and change for own use. May not: pass on, sell.
        self.assertIn("install and use the Software on computers that you "
                      "own or control, or that you have been given to use "
                      "(for example by your employer), for any lawful "
                      "purpose, including at work", flat)
        self.assertIn("give, send, publish, upload, mirror or otherwise make "
                      "available the Software or any part of it, changed or "
                      "unchanged, to anyone else", flat)
        self.assertIn("sell, rent, lease or sublicense the Software", flat)
        # The carve-out for what the law does not allow to be limited.
        self.assertIn("death or personal injury caused by negligence", flat)
        self.assertIn("LICENSE governs", TEXT)
        # No document of the app still calls it MIT or open-source software.
        for name in ("LICENSE", "TERMS.md", "README.md", "CHANGELOG.md",
                     "SECURITY.md", "ui/app.js"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\bMIT\b", name)
            self.assertNotIn("free, open-source", text, name)
            self.assertNotIn("Free software", text, name)
        notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
        self.assertNotIn("itself is MIT", notices)

    def test_readme_introduces_the_terms_and_keeps_the_no_warranty_summary(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        before = readme.index("## Before you install")
        self.assertGreater(readme.index("TERMS.md"), before)
        section = re.search(r"^### No warranty\s*$(.*?)^#{1,3} ", readme,
                            re.M | re.S).group(1)
        flat = " ".join(section.split())
        for needle in ("TERMS.md", "LICENSE", "not legal advice"):
            self.assertIn(needle, flat)
        self.assertIn("I agree", readme)

    def test_the_documents_name_the_record(self):
        for name in ("ARCHITECTURE.md", "CHANGELOG.md"):
            self.assertIn("terms-accepted.json" if name == "ARCHITECTURE.md"
                          else "Terms of Use",
                          (ROOT / name).read_text(encoding="utf-8"), name)
        self.assertIn("TERMS.md", (ROOT / "SECURITY.md").read_text(
            encoding="utf-8"))


class DataFolderCase(unittest.TestCase):
    """A temporary data folder in place of settings.DATA_DIR."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="htt-terms-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(settings, "DATA_DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def record(self):
        return json.loads((self.tmp / "terms-accepted.json").read_text(
            encoding="utf-8"))


class Record(DataFolderCase):
    def test_no_record_means_not_accepted(self):
        self.assertIsNone(terms.read_record())
        self.assertFalse(terms.is_current())

    def test_accepting_writes_version_time_and_way_and_nothing_else(self):
        rec = terms.accept("app")
        data = self.record()
        self.assertEqual(data, rec)
        self.assertEqual(set(data), {"version", "accepted_at", "how"})
        self.assertEqual(data["version"], settings.TERMS_VERSION)
        self.assertEqual(data["how"], "app")
        self.assertRegex(data["accepted_at"],
                         r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertTrue(terms.is_current())
        self.assertEqual([p.name for p in self.tmp.iterdir()],
                         ["terms-accepted.json"])           # no temp file left

    def test_a_person_accepts_in_the_installer_or_the_app_and_no_other_way(self):
        for how in ("installer", "app"):
            terms.accept(how)
            self.assertEqual(terms.read_record()["how"], how)
            self.assertTrue(terms.is_current())
        for how in ("unattended", "hidden flag"):
            with self.assertRaises(ValueError):
                terms.accept(how)

    def test_an_unattended_record_is_read_but_is_not_an_acceptance(self):
        accept_terms_in(self.tmp, how="unattended")
        self.assertEqual(terms.read_record()["how"], "unattended")
        self.assertFalse(terms.is_current())
        terms.accept("app")              # an explicit I agree replaces it
        self.assertEqual(self.record()["how"], "app")
        self.assertTrue(terms.is_current())

    def test_an_older_version_asks_again(self):
        accept_terms_in(self.tmp, version="0.5")
        self.assertEqual(terms.read_record()["version"], "0.5")
        self.assertFalse(terms.is_current())

    def test_damaged_or_odd_records_count_as_none(self):
        path = self.tmp / "terms-accepted.json"
        for text in ("", "not json", "[]", "{}", '{"version": 1}',
                     json.dumps({"version": "1.0", "accepted_at": "x",
                                 "how": "magic"}),
                     json.dumps({"version": "", "accepted_at": "x",
                                 "how": "app"})):
            path.write_text(text, encoding="utf-8")
            self.assertIsNone(terms.read_record(), text)
            self.assertFalse(terms.is_current())

    def test_a_record_with_a_byte_order_mark_is_read(self):
        (self.tmp / "terms-accepted.json").write_bytes(
            b"\xef\xbb\xbf" + json.dumps({
                "version": settings.TERMS_VERSION,
                "accepted_at": "2026-11-16T10:00:00Z",
                "how": "installer"}).encode())
        self.assertTrue(terms.is_current())

    def test_the_text_is_read_from_the_fixed_file_only(self):
        self.assertEqual(terms.terms_path(), settings.APP_DIR / "TERMS.md")
        self.assertEqual(terms.read_text(), TEXT)


def make_app(engine=True):
    """A bare App (no Tk) like test_flow's, with fake keys and recorder."""
    eng = types.SimpleNamespace(transcribe=lambda s: "some words here",
                                device="cpu") if engine else None
    a = App.__new__(App)
    a._init_state(cfg_with(hotkey="ctrl+windows"), engine=eng, dry_run=False)
    a.recorder = FakeRecorder()
    a.hook_factory = mock.Mock(side_effect=OSError("no hook in tests"))
    return a


class TheHotkeyWaitsForTheTerms(DataFolderCase):
    def setUp(self):
        super().setUp()
        self.down = set()
        self.pastes = []
        self.sounds = FakeSounds()
        for target, name, value in (
                (keys, "key_down", lambda vk: vk in self.down),
                (keys, "tap_mask_key", lambda: None),
                (keys, "paste_text",
                 lambda text, restore=True, **kw: self.pastes.append(text)
                 or "pasted"),
                (app_mod, "sounds", self.sounds)):
            p = mock.patch.object(target, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        self.notices = []

    def start(self):
        a = self.app = make_app()
        a.on_notice.append(lambda k, m: self.notices.append((k, m)))
        worker = threading.Thread(target=a._worker, daemon=True)
        worker.start()
        self.addCleanup(lambda: (a._jobs.put(None), worker.join(5)))
        return a

    def hold(self, steps=3):
        self.down |= {CTRL, WIN}
        for _ in range(steps):
            self.app._step()

    def let_go(self):
        self.down.clear()
        self.app._step()

    def test_without_a_record_the_key_does_nothing(self):
        a = self.start()
        self.assertTrue(a.terms_pending)
        self.assertIsNone(a.terms_record)
        self.hold()
        self.assertEqual(a._state, "idle")
        self.assertEqual(a.recorder.starts, 0)
        self.let_go()
        self.assertEqual(self.pastes, [])
        self.assertEqual(a.recorder.starts, 0)

    def test_it_says_why_once_per_press(self):
        a = self.start()
        self.hold(5)
        self.let_go()
        self.hold(5)
        self.let_go()
        said = [m for k, m in self.notices if k == "terms"]
        self.assertEqual(len(said), 2)
        self.assertIn("Terms of Use", said[0])
        self.assertIn("I agree", said[0])
        self.assertEqual(a.recorder.starts, 0)

    def test_a_mouse_button_hook_is_not_started_either(self):
        a = self.start()
        a._set_hotkey(a._compile_hotkey("mouse4"))
        self.assertIsNone(a._hook_wanted())
        a.accept_terms("app")
        self.assertIsNotNone(a._hook_wanted())

    def test_accepting_enables_dictation_at_once_without_a_restart(self):
        a = self.start()
        self.hold()
        self.assertEqual(a.recorder.starts, 0)
        self.let_go()
        rec = a.accept_terms("app")
        a._step()
        self.assertEqual(rec["how"], "app")
        self.assertFalse(a.terms_pending)
        self.assertEqual(a.terms_record, rec)
        self.assertEqual(self.record(), rec)
        self.hold(1)
        self.assertEqual(a._state, "recording")
        self.assertEqual(a.recorder.starts, 1)
        self.let_go()
        self.assertTrue(wait_for(lambda: len(self.pastes) == 1))

    def test_an_older_record_asks_again(self):
        accept_terms_in(self.tmp, version="0.9")
        a = self.start()
        self.assertTrue(a.terms_pending)
        self.hold()
        self.assertEqual(a.recorder.starts, 0)
        self.let_go()
        a.accept_terms("app")
        self.assertEqual(a.terms_record["version"], settings.TERMS_VERSION)

    def test_an_unattended_record_leaves_the_terms_pending_until_i_agree(self):
        accept_terms_in(self.tmp, how="unattended")
        a = self.start()
        self.assertTrue(a.terms_pending)
        self.hold()
        self.assertEqual(a.recorder.starts, 0)
        self.let_go()
        a.accept_terms("app")
        self.assertFalse(a.terms_pending)
        self.assertEqual(self.record()["how"], "app")
        self.hold(1)
        self.assertEqual(a._state, "recording")

    def test_with_a_current_record_nothing_is_asked(self):
        accept_terms_in(self.tmp)
        a = self.start()
        self.assertFalse(a.terms_pending)
        self.hold(1)
        self.assertEqual(a._state, "recording")
        self.let_go()
        self.assertEqual([k for k, _ in self.notices if k == "terms"], [])

    def test_a_record_that_cannot_be_written_changes_nothing(self):
        a = self.start()
        with mock.patch.object(terms, "accept", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                a.accept_terms("app")
        self.assertTrue(a.terms_pending)
        self.hold()
        self.assertEqual(a.recorder.starts, 0)

    def test_the_test_helpers_write_the_same_record_the_app_reads(self):
        accept_terms_in(self.tmp)
        self.assertTrue(terms.is_current())
        self.assertEqual(self.record()["version"], settings.TERMS_VERSION)


class TermsFakeApp(test_api.FakeApp):
    """What api.Server sees of the real App's terms members."""

    quit_asked = False

    @property
    def terms_pending(self):
        return not terms.is_current()

    @property
    def terms_record(self):
        return terms.read_record()

    def accept_terms(self, how="app"):
        return terms.accept(how)

    def quit(self):
        self.quit_asked = True


class OverHttp(test_api.ServerCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="htt-terms-api-"))
        self.addCleanup(shutil.rmtree, self.data, True)
        for p in (mock.patch.object(settings, "DATA_DIR", self.data),
                  mock.patch.object(test_api, "FakeApp", TermsFakeApp)):
            p.start()
            self.addCleanup(p.stop)
        super().setUp()

    def test_the_text_is_served_only_with_the_access_key(self):
        status, data, _ = self.get("/api/terms", key=False)
        self.assertEqual(status, 401)
        self.assertNotIn(b"Terms of Use", json.dumps(data).encode())
        status, data, _ = self.get("/api/terms",
                                   headers={api_key(): "x" * 43})
        self.assertEqual(status, 401)
        status, data, _ = self.get("/api/terms")
        self.assertEqual(status, 200)
        self.assertEqual(data["text"], TEXT)
        self.assertEqual(data["version"], settings.TERMS_VERSION)
        self.assertTrue(data["pending"])
        self.assertIn("only a summary", data["summary"])

    def test_it_is_not_a_static_file_and_no_path_is_taken_from_a_request(self):
        for path in ("/TERMS.md", "/terms", "/api/terms/TERMS.md",
                     "/api/terms?path=LICENSE", "/ui/../TERMS.md"):
            status, data, _ = self.get(path, key=False)
            self.assertIn(status, (401, 404), path)
            self.assertNotIn("Terms of Use", json.dumps(data), path)
        status, data, _ = self.get("/api/terms?path=README.md")
        self.assertEqual(data["text"], TEXT)           # the query is ignored

    def test_the_app_reports_that_the_terms_are_pending(self):
        state = self.ok("GET", "/api/state")
        self.assertTrue(state["live"]["terms"]["pending"])
        self.assertEqual(state["live"]["terms"]["version"],
                         settings.TERMS_VERSION)
        info = state["info"]["terms"]
        self.assertTrue(info["pending"])
        self.assertIsNone(info["accepted_at"])

    def test_accepting_writes_the_record_and_dictation_is_enabled_at_once(self):
        status, data, _ = self.post("/api/terms/accept",
                                    {"version": settings.TERMS_VERSION},
                                    key=False)
        self.assertEqual(status, 401)
        self.assertFalse((self.data / "terms-accepted.json").exists())
        data = self.ok("POST", "/api/terms/accept",
                       {"version": settings.TERMS_VERSION})
        self.assertTrue(data["ok"])
        self.assertFalse(data["terms"]["pending"])
        rec = json.loads((self.data / "terms-accepted.json").read_text(
            encoding="utf-8"))
        self.assertEqual(rec["how"], "app")
        self.assertEqual(rec["version"], settings.TERMS_VERSION)
        self.assertFalse(self.app.terms_pending)
        state = self.ok("GET", "/api/state")
        self.assertFalse(state["live"]["terms"]["pending"])
        self.assertEqual(state["info"]["terms"]["how"], "app")
        self.assertEqual(state["info"]["terms"]["accepted_at"],
                         rec["accepted_at"])

    def test_only_the_version_the_page_showed_can_be_accepted(self):
        for version in ("0.1", "", None, 1.0, "2.0"):
            status, data, _ = self.post("/api/terms/accept",
                                        {"version": version})
            self.assertEqual(status, 409, version)
        status, _, _ = self.post("/api/terms/accept", {})
        self.assertEqual(status, 409)
        self.assertFalse((self.data / "terms-accepted.json").exists())
        self.assertTrue(self.app.terms_pending)

    def test_an_older_record_asks_again(self):
        accept_terms_in(self.data, version="0.9")
        state = self.ok("GET", "/api/state")
        self.assertTrue(state["live"]["terms"]["pending"])
        self.assertEqual(state["info"]["terms"]["accepted_version"], "0.9")

    def test_an_unattended_record_is_pending_and_the_app_agree_replaces_it(self):
        accept_terms_in(self.data, how="unattended")
        state = self.ok("GET", "/api/state")
        self.assertTrue(state["live"]["terms"]["pending"])
        self.assertTrue(state["info"]["terms"]["pending"])
        self.assertEqual(state["info"]["terms"]["how"], "unattended")
        self.ok("POST", "/api/terms/accept", {"version": settings.TERMS_VERSION})
        rec = json.loads((self.data / "terms-accepted.json").read_text(
            encoding="utf-8"))
        self.assertEqual(rec["how"], "app")
        state = self.ok("GET", "/api/state")
        self.assertFalse(state["live"]["terms"]["pending"])
        self.assertEqual(state["info"]["terms"]["how"], "app")

    def test_with_a_current_record_nothing_is_asked(self):
        accept_terms_in(self.data)
        state = self.ok("GET", "/api/state")
        self.assertFalse(state["live"]["terms"]["pending"])
        self.assertFalse(state["info"]["terms"]["pending"])
        self.assertEqual(state["info"]["terms"]["accepted_version"],
                         settings.TERMS_VERSION)

    def test_terms_that_cannot_be_read_cannot_be_accepted(self):
        with mock.patch.object(terms, "terms_path",
                               lambda: self.data / "missing.md"):
            status, data, _ = self.get("/api/terms")
            self.assertEqual(status, 503)
            self.assertIn("TERMS.md", data["error"])
            status, data, _ = self.post("/api/terms/accept",
                                        {"version": settings.TERMS_VERSION})
            self.assertEqual(status, 503)
        self.assertFalse((self.data / "terms-accepted.json").exists())

    def test_terms_of_another_version_cannot_be_accepted(self):
        other = self.data / "TERMS.md"
        other.write_text("Version 9.9 \u2014 1 January 2030\n\ntext\n",
                         encoding="utf-8")
        with mock.patch.object(terms, "terms_path", lambda: other):
            status, data, _ = self.post("/api/terms/accept",
                                        {"version": settings.TERMS_VERSION})
        self.assertEqual(status, 503)
        self.assertFalse((self.data / "terms-accepted.json").exists())

    def test_quit_works_only_while_the_terms_are_pending(self):
        status, _, _ = self.post("/api/quit", {}, key=False)
        self.assertEqual(status, 401)
        self.assertFalse(self.app.quit_asked)
        self.ok("POST", "/api/quit", {})
        self.assertTrue(self.app.quit_asked)
        self.app.quit_asked = False
        accept_terms_in(self.data)
        status, data, _ = self.post("/api/quit", {})
        self.assertEqual(status, 409)
        self.assertFalse(self.app.quit_asked)

    def test_the_window_shows_a_terms_screen_with_the_two_buttons(self):
        js = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="terms"', html)
        for needle in ("'I agree to the Terms of Use'", "'Quit'",
                       "'View terms'", "/api/terms",
                       "/api/terms/accept", "/api/quit", "terms?.pending"):
            self.assertIn(needle, js)
        self.assertIn("This includes the sections on no warranty, limits of "
                      "liability, assumption of risk, indemnity and disputes "
                      "(sections 9 to 14).", js)
        self.assertNotRegex(js, r"\.innerHTML|insertAdjacentHTML")


def api_key():
    import api
    return api.KEY_HEADER


class TheTrayAndTheStartMessage(unittest.TestCase):
    def handle(self, **app):
        h = tray.TrayHandle.__new__(tray.TrayHandle)
        h.app = types.SimpleNamespace(
            status="ready", paused=False, recorder=None,
            cfg={"hotkey": "ctrl+windows"}, **app)
        return h

    def test_the_tray_says_why_it_is_grey(self):
        h = self.handle(terms_pending=True)
        self.assertEqual(h.state(), "paused")
        self.assertIn("Terms of Use", h._status_text())
        self.assertIn("accept", h._status_text())

    def test_the_tray_is_unchanged_when_nothing_is_pending(self):
        for kw in ({}, {"terms_pending": False}):
            h = self.handle(**kw)
            self.assertEqual(h.state(), "ready")
            self.assertTrue(h._status_text().startswith("Ready"))

    def test_the_tray_redraws_when_the_terms_are_accepted(self):
        pending = self.handle(terms_pending=True)
        done = self.handle(terms_pending=False)
        self.assertNotEqual(pending._signature(), done._signature())

    def test_a_start_without_a_window_still_says_what_to_do(self):
        import dictate
        text = dictate.terms_pending_message()
        for needle in ("Terms of Use", "icon in the notification area",
                       "I agree", "TERMS.md", settings.TERMS_VERSION):
            self.assertIn(needle, text)
        import inspect
        source = inspect.getsource(dictate.run_app)
        self.assertIn("terms_pending", source)
        self.assertIn("message_box", source[source.index("terms_pending"):])


if __name__ == "__main__":
    unittest.main()
