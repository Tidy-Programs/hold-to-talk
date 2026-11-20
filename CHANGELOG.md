# Changelog

All notable changes to Hold to talk. The version is `APP_VERSION` in
`settings.py`.

## 0.9.1

Release date: not yet released

**Fixed**
- The log scrubber now removes a Windows account name that contains a space
  (`stt.scrub` used to leave the second word), and the profile folder in
  either slash style.
- A speech model that a failed or interrupted download had left incomplete
  (found on a freshly installed PC: "Unable to open file 'model.bin'", then
  "No speech model could be loaded", with no way out) is now noticed before
  the speech library sees it. The app checks that the model folder is
  complete (`model.bin` readable and at least 1 MB, plus the config,
  tokenizer, vocabulary and, for `large-v3-turbo`, preprocessor files), counts
  an incomplete folder as "not on this computer", and fetches the missing
  files at the pinned revision (`stt.resolve_model`). The installer's check
  ("is the model already on this PC?") and its download use the same rule, so
  Setup no longer says an unfinished model is present and skips it.
- When the model cannot be downloaded, the window and the tray tooltip now say
  why in one plain sentence and what to do (no connection, timeout, proxy,
  refused or busy server, disk full, file in use, interrupted), instead of a
  library message with a folder path. The log keeps the detail, without your
  account name or the full cache path.
- A failed download is no longer left to chance: the app tries again by itself
  after 5 seconds, then 15, then 60, then every 5 minutes (the message box
  about the failure is shown once, not at every attempt); "Try again" in the
  window retries at once and starts the waits over.

**New**
- Settings > Sounds > **Mute other sound while dictating** (off by default,
  `sounds.mute_others`): while you hold the key every other program's sound is
  muted on all active output devices, and exactly those sessions are unmuted
  when you let go, cancel, pause or quit. Sessions already muted are left as
  they are, the app's own chimes stay audible, and a program that starts
  playing while the key is held is not muted in this first version. It never
  leaves the PC silent: a safety timer (5 minutes), and a small recovery file
  (`audio-muted.json`, removed by the uninstaller's "delete my data") that the
  next start uses to unmute after a crash. Uses the Windows audio API through
  `ctypes`; no new package.
- While the app downloads the speech model, the window says "Downloading the
  speech model (about 1.5 GB)... this can take several minutes", with a
  percentage measured from the size of the download folder, and the tray
  tooltip says "Downloading the speech model" with the percentage.

## 0.9.0

Release date: 18 November 2026

The first release. What it contains, by area:

**Dictation**
- Hold a key (default Ctrl + Windows), speak, release: the speech is
  transcribed on this PC with faster-whisper (an NVIDIA graphics card if
  there is one, otherwise the processor) and pasted into the window you are
  using. The key can also be a mouse middle or side button, or a combination.
- A pause shortcut, start and stop chimes, and an on-screen indicator while
  you hold the key. The first quarter of a second of each recording can be
  silenced so the start chime is not recorded (`sounds.mute_start`, off by
  default).
- Pasting goes through the clipboard, which is put back afterwards (a timer
  restores it shortly after the paste). If the clipboard cannot be restored
  exactly, the text is typed instead. The pasted text is marked so Windows
  keeps it out of clipboard history and cloud clipboard sync; this also
  applies when `restore_clipboard` is off.
- If AI cleanup adds line breaks and the window in front is a terminal, the
  text from before the rewrite is pasted instead.
- A recording that could not be transcribed is kept in memory for up to 5
  minutes, and Home offers "Try that recording again".
- Language choice, with a suggestion on Home when your last 10 dictations were
  all one language; "Use less graphics memory" (`compute_type`, needs a
  restart).

**Speech models**
- The two models the app uses are loaded and downloaded at exact, pinned
  commits (`MODEL_REVISIONS` in `stt.py`, also used by the installer's
  pre-download), so a changed repository is not downloaded unverified. How to
  update a pin is in ARCHITECTURE.md (Models). Without the NVIDIA runtime the
  app does not try the graphics card.

**AI cleanup (optional)**
- Tidies fillers and false starts through a local Ollama model. The model's
  answer is checked, and the plain transcript is pasted when a check fails;
  the checks catch many unwanted changes, not all.
- AI cleanup only ever talks to Ollama on this computer: there is no setting
  that allows another address, and an old `cleanup.allow_remote` key in a
  config file is ignored with one logged warning and dropped on the next save.
- A dictation never waits for a connection attempt to Ollama. The app checks
  which Windows account owns the program on Ollama's port before sending
  anything, and offers a switch to trust a program you know is yours.
- The window explains every cleanup result in a dictation's steps, including
  "the AI's rewrite was not used".

**Words and history**
- Dictionary, vocabulary and snippets; spoken "new line" and "new paragraph"
  as an option; suggestions for dictionary entries from your corrections in
  the history, each of which can be undone.
- History kept in a local database, with search, correction and "Clear
  history".

**The window and the tray**
- A tray icon and menu, and a window (Edge or Chrome in app mode) with Home,
  History, Dictionary, Vocabulary, Snippets and Settings.
- The window is served on the loopback interface only and protected by an
  access key, Host and Origin checks, and connection deadlines. Errors are
  logged by kind and location only, never with the dictated text.
- Only a browser window the app itself started is ever focused or closed.
- If neither Edge nor Chrome is found, the window opens as a tab in the
  default browser; the app logs one line and the window shows a notice that
  that browser's extensions and settings apply.

**Your data**
- Settings, history and logs live in `%LOCALAPPDATA%\HoldToTalk\data`, apart
  from the program files. Data kept next to the code by earlier development
  builds is moved there once. The log never contains dictated text.

**Install, update, uninstall**
- A per-user installer (`install.bat`) and uninstaller (`uninstall.bat`): no
  administrator rights, Start menu and Desktop shortcuts, an entry in
  Settings > Apps. On a PC with an NVIDIA card the installer asks before it
  installs the NVIDIA runtime (accepting NVIDIA's licence for those files);
  answering no installs a working processor-only app, and running the
  installer again can add it. `-Yes` installs it without asking, with a notice.
- Double-clicking `install.bat` opens a setup window (`install-gui.ps1`) in
  the app's colours, light or dark after Windows' setting, with five pages:
  Welcome; Terms of Use; what will be installed, where, and how big it is
  (with the NVIDIA runtime, the speech model download and the Desktop
  shortcut as boxes you can untick, and the free disk space); Installing,
  with the seven steps ticked off, a progress bar and the full text behind
  "Show details"; and Finish, with how to open the app and "Start Hold to
  talk now". Nothing is changed before you choose Install. If Python is
  missing the window says so, offers to install it (see below) or opens
  python.org for you, and checks again without starting over; if the install fails it says why and offers Try
  again. The window needs only what Windows already has, so it works before
  Python is installed.
- A single Setup file, `HoldToTalk-Setup-0.9.0.exe`, on the Releases page:
  it unpacks the app's files to a temporary folder and opens the setup
  window, with no console window and no administrator rights. It checks
  every file against a list of fingerprints and refuses a damaged or changed
  download. It is not digitally signed. The ZIP with `install.bat` remains.
- If Python is missing, the setup window can install it: it downloads the
  official Python 3.12.10 installer from python.org and runs it only when
  its fingerprint and the Python Software Foundation's signature both check
  out, for the Windows account only.
- Uninstalling opens a window in the same style. Deleting your settings and
  history needs a box that starts unticked and a confirmation.
- When the app is already installed, the same window is the updater and
  says so: it is named "Update", says that it updates the installed version
  and does not install a second copy, and its button, headings and result
  say "Update", "Updating" and "updated". With the same version already
  installed it says "Repair".
- The text installer is still there: `install.bat -Console` (or any other
  argument) runs it in the Command Prompt as before, and `install.bat` falls
  back to it by itself if the window cannot be shown. The window is only a
  front: it runs the same `install.ps1` and shows what it reports
  (`-Check`, `-Progress`; see ARCHITECTURE).
- Libraries install from exact, checksummed lock files (`requirements.lock.txt`,
  `requirements-cuda.lock.txt`) with `--only-binary :all:`, so nothing is
  built from source. The installer's helper scripts run in a private temporary
  folder with Python's isolated mode.
- The uninstaller runs from a private temporary folder, removes the Start with
  Windows entry, and deletes your data only if you say so, and then only the
  files the app itself creates, by name; anything else is left in place.

**Terms of Use**
- `TERMS.md`, version 1.0: Terms of Use for the app, with a plain-words
  summary, a warranty disclaimer and a limitation of liability in capitals,
  and sections on responsibilities, other people's software, privacy,
  assumption of risk, indemnity, disputes and changes.
- `LICENSE` is the Hold to talk Licence, version 1.0: free to install and
  use on your own computers (or one you have been given to use) for any
  lawful purpose including work, and to change for your
  own use; not to be passed on, published or sold, changed or unchanged. The
  app is free to use but is not open-source software. `LICENSE` governs what
  may be done with copies and the source code; the Terms of Use govern the
  rest.
- Accepting them is an active step that is recorded. The setup window shows
  the whole text and goes on only when you tick "I have read and agree to the
  Terms of Use" (unticked to begin with) and then choose "I agree"; Cancel
  closes it with nothing changed. The text installer shows the
  summary and where the full text is, offers to show all of it in the window
  or in Notepad, and continues only when you type `I agree` (any capitals);
  anything else stops it with nothing changed (exit code 3). The setup
  window, the text prompt and the app's "I agree to the Terms of Use" button
  say what is being agreed to (the sections on no warranty, limits of liability,
  assumption of risk, indemnity and disputes, sections 9 to 14). `-Yes`
  installs without questions but does not accept the terms for the person who
  will use the app: it records nothing, and the app shows the terms and asks.
  A record that says the install was unattended does not count as
  acceptance.
- The app asks too, for a copy that was never installed (`start.bat`) and
  whenever the version of the terms changes: until they are accepted the
  hold-to-talk key does nothing, the tray menu and Home say why, and the
  window opens on a terms screen with "I agree to the Terms of Use" and Quit (a start with no
  window shows a message box). Accepting takes effect at once, without a
  restart. Settings > About shows the version and date accepted and has
  View terms.
- The record is `terms-accepted.json` in the data folder (version, UTC time,
  and "installer" or "app"); it is local, holds nothing
  personal, and "delete my data" in the uninstaller removes it.

**Documents**
- README (for users), ARCHITECTURE (for developers), THIRD-PARTY-NOTICES
  (licences), LICENSE (the licence), TERMS.md (the Terms of Use), and
  plain-language notices: a
  "No warranty" section in the README that points to TERMS.md and LICENSE, a
  line in Settings > About, and SECURITY.md (how to report a vulnerability
  privately).
