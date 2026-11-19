# How Hold to talk works

For a developer making a first change. Every claim names the file and
function it is about, so you can check it against the code. If the code and
this document disagree, the code is right and this file needs fixing. It was
last checked against version 0.9.0 (`settings.APP_VERSION`).

Contents:
[Big picture](#big-picture) · [Stack and files](#stack-and-files) ·
[Glossary](#glossary) · [Processes and threads](#processes-and-threads) ·
[One dictation, step by step](#one-dictation-step-by-step) ·
[Rules you must not break](#rules-you-must-not-break) ·
[Why it is built this way](#why-it-is-built-this-way) ·
[Models](#models) · [The local API and its security](#the-local-api-and-its-security) ·
[Where data lives](#where-data-lives) · [How settings apply live](#how-settings-apply-live) ·
[Install, update, uninstall](#install-update-uninstall) ·
[Tests](#tests-and-keeping-them-away-from-real-data) · [How to add...](#how-to-add)

## Big picture

Hold to talk is one Python program (the **engine**) that runs in the
background with a tray icon. It watches the keyboard 66 times a second; while
the hold-to-talk key is down it records the microphone into memory. When the
key is released a worker thread turns the audio into text with a local speech
model, passes the text through a chain of text steps (tidy fillers, optional
AI rewrite through a local Ollama server, the user's dictionary and snippets),
pastes the result through the clipboard, and stores it in a local history.
Two other processes are optional helpers: a tiny **mouse helper** that
exists only when a mouse button is the hold-to-talk key, and the **window**,
a web page the engine serves on the loopback interface and that Microsoft
Edge (or Chrome) shows in app mode. Nothing in the dictation path needs the
internet; the only downloads are libraries and speech models at install or
first start.

To try a change: install the prerequisites (see the README's Step 0), run
`start.bat` (it builds or repairs the shared Python environment and starts
the app from your source folder; `start.bat console` runs it in the console
with a detailed log), make your change, restart, and run the tests.

## Stack and files

| Piece | What it is used for |
| --- | --- |
| Python 3.10 to 3.13 (3.12 tested), Windows only | Everything; Win32 through `ctypes` |
| Tk (`tkinter`, ships with Python) | The hidden root window, the on-screen indicator, the main-thread loop |
| pystray and Pillow | The tray icon and its menu; drawing the icons |
| sounddevice (PortAudio) and numpy | Microphone capture and audio arrays |
| faster-whisper on CTranslate2 | Speech recognition (`stt.py`) |
| SQLite (`sqlite3`) | The dictation history (`textrules.History`) |
| `http.server` | The local server for the window (`api.py`) |
| Edge or Chrome in `--app` mode | Shows the window (`uiwin.py`) |
| Ollama (a separate program) | Optional AI cleanup (`cleanup.py`) |
| PowerShell 5.1 and batch files | Install, update, uninstall |

The window's pages are plain HTML, CSS and JavaScript with no build step.

```
dictate.py            entry point, flags, single-instance lock, wiring
settings.py           defaults, config.json load/save, migration, apply_changes,
                      APP_VERSION and TERMS_VERSION
terms.py              the Terms of Use: the accepted-version record and TERMS.md
app.py                App: Tk loop, key state machine, worker, paste, reload
audio.py              Recorder (microphone), resampling, device list
stt.py                Engine: faster-whisper load (GPU then processor), transcribe
pipeline.py           STAGES, run, normalize, text_to_paste
cleanup.py            AI cleanup stage, Ollama client, guard rails, listener check
textrules.py          dictionary, snippets, spoken formatting, History (SQLite)
learn.py              suggestions from corrections; manage entries by hand
keys.py               key names, hotkey parsing, key state, input, clipboard, paste_text
sounds.py             the two chimes, generated in code
overlay.py            the on-screen indicator (a Tk window)
mousehook.py          engine side of the mouse-button hook
mousehelper.py        the helper process that holds the low-level mouse hook
api.py                the local HTTP server for the window
uiwin.py              finds, starts, focuses and closes the Edge/Chrome window
tray.py               tray icon and menu; Start with Windows registry helpers
app.ico               the icon (made by tools/make_icon.py)
ui/index.html         the window's page shell
ui/app.css            the window's styles
ui/app.js             the window's pages (Home, History, Dictionary, ...)
install.bat           no arguments: opens the setup window; any argument: text installer
install-gui.ps1       the setup window (WPF); runs install.ps1 and shows its progress
install.ps1           installer and updater (the engine, and the text installer)
htt-common.ps1        paths and helpers shared by install.ps1 and uninstall.ps1
install-files.txt     the list of files the installer copies (an allow-list)
uninstall.bat         starts a copy of uninstall.ps1 from a private temp folder
uninstall.ps1         uninstaller
start.bat             run from a source folder
TERMS.md              the Terms of Use (text, with the version on its first line)
requirements.txt      version ranges (what a developer edits)
requirements-cuda.txt version ranges of the NVIDIA runtime
requirements.lock.txt exact, hash-checked versions the installer uses
requirements-cuda.lock.txt  the same for the NVIDIA runtime
tools/make_locks.py   regenerates the lock files from a working environment
tools/make_icon.py    regenerates app.ico
tests/                the unit tests (fakes.py holds the shared fakes)
.github/workflows/tests.yml   runs the tests on GitHub (core requirements only)
README.md, CHANGELOG.md, THIRD-PARTY-NOTICES.md, LICENSE, ARCHITECTURE.md
```

`dictate.py` flags: `--quit`, `--open`, `--uninstall`, `--list-devices`,
`--mic-test`, `--selftest WAV`, `--smoke [SECONDS] [--wav WAV] [--tray]`,
`--dry-run`, `--ui-port N`, `--config FILE`, `--history FILE`, `--log FILE`,
`--log-text`, `-v`. `ARCHITECTURE.md` is not in `install-files.txt` (it is
for developers); `tests/test_install.py` checks that.

## Glossary

| Word | Meaning |
| --- | --- |
| **hotkey** | The key or key combination as the code names it: the hold-to-talk key (`hotkey` setting) and the pause shortcut (`pause_hotkey`). A "spec" is its text, such as `ctrl+windows`. |
| **tick** | One run of `App._tick`, every `TICK_MS` (15 ms), on the Tk thread: read keys, run the state machine, draw the indicator. |
| **take** | One recording as an object (`audio.Take`): the audio plus a way to wait until the last audio still in the driver's buffers has arrived. |
| **tail** | That last bit of audio, still in the driver's buffers when the key is released. `Recorder.stop_async` keeps the take open until the tail has arrived. |
| **held** (in `("take", take, held)`) | How many seconds the key was held. Not to be confused with a held recording. |
| **held recording** | The one recording whose transcription failed, kept in memory for `HELD_SECONDS` (5 minutes) so "Try that recording again" can retry it (`App._hold_recording`, `retry_held_recording`). |
| **stage** | One function in `pipeline.STAGES`: text in, text out. |
| **trace** | The list of `pipeline.TraceEntry` (stage name, text after it, seconds, failed, error) that `pipeline.run` fills in. |
| **record** | The dict `App._process` builds for one finished dictation (raw and final text, timings, stage list, cleanup status, paste method, `console_safety`...). Hooks `on_result` and `on_dictation` get it. |
| **pill** | The on-screen indicator (`overlay.Overlay`). The documents for users call it the on-screen indicator. |
| **mask key** | The unassigned key `VK_MASK` that `keys.tap_mask_key` taps so releasing Win or Alt does not open the Start menu or a menu bar. |
| **ticket** | A single-use random token, valid `TICKET_SECONDS` (120 s), that the window page swaps for the access key (`Server.launch_url`, `Server._redeem`). |
| **access key** | The per-run random secret the window sends in the `X-HTT-Key` header (`Server.secret`, `api.KEY_HEADER`). The code calls it the secret. |
| **test instance** | An engine started with any of `--config`, `--history`, `--log`: it uses only files next to the one given, has its own lock and events, never touches real data (`dictate.data_paths`). |
| **snippet trigger** | The phrase the user says to get a snippet's text (`textrules._snippet_pairs`). |
| **learned record** | An entry in `config.json`'s `learned` list that remembers what a learned dictionary or vocabulary entry came from, so it can be undone (`learn.undo`). |
| **console safety** | If the AI rewrite added line breaks and the window in front is a terminal, the text from before the rewrite is pasted (`App._console_safe_final`, `_in_console`). |
| **snapshot** | What `keys.take_snapshot` read from the clipboard so it can be put back after a paste. `unrestorable` means it cannot be put back exactly. |
| **quarantine** | Moving a damaged `history.sqlite3` aside as `history.sqlite3.corrupt-<time>` (`History._quarantine`). |
| **hub** | `api.Hub`: fans live events out to every connected window. |
| **data folder** | `settings.DATA_DIR`: `%LOCALAPPDATA%\HoldToTalk\data`, or `HOLDTOTALK_HOME`. |

## Processes and threads

**1. The engine** is one Python process: `pythonw dictate.py` (no console;
`python dictate.py` runs it in a console). `dictate.main` parses the flags and
`dictate.run_app` builds everything. It takes a named mutex first
(`dictate._single_instance`); a second start signals the first one's "open"
event instead (`dictate.open_running`). `dictate._listen` also waits for a
"quit" event, which is what `dictate.py --quit` sets (`dictate.cmd_quit`).

| Thread | Started by | Job |
| --- | --- | --- |
| Tk main thread | `app.App.run` (`root.mainloop`) | Owns the hidden Tk root, the on-screen indicator (`overlay.Overlay`), the recorder and the key state machine. `App._tick` runs every `TICK_MS`: `_step` then `_draw`. |
| `transcribe` | `App.__init__` | `App._worker`: takes finished recordings from the `_jobs` queue and runs speech recognition, the pipeline and the paste, strictly in order. |
| `load-model` | `App.load_engine` | Loads the Whisper model so the tray can show "Getting ready" meanwhile. |
| `preload-speech` | `dictate._preload_engine` | Imports faster-whisper and ctranslate2 while the tray and window are built. |
| `tray`, `tray-poll` | `tray.TrayHandle` | pystray's own message loop; the poller asks it to redraw the icon and menu. |
| `tray-apply` | `TrayHandle._apply_soon` | Short-lived: applies a tray menu change (`settings.apply_changes` waits for the Tk thread, so the tray thread must not). |
| `ui-server`, `ui-live`, `ui-health` (plus one thread per request) | `api.Server.start` | The local web server for the window; a sampler that feeds live events; the health checks (Ollama, microphone, Start with Windows). |
| `mic-test` | `Server.p_mic_test` | The 3-second microphone level test. |
| (unnamed `threading.Timer`) | `Server.p_overlay_preview` | Shows the indicator's "working" look after "listening" in the colour preview. |
| `signal-listener` | `dictate._listen` | Waits for the quit and open events. |
| `chime` | `sounds.play` | Plays a chime so the caller never blocks. |
| `cleanup-start` | `cleanup.prepare_in_background` | At start-up and whenever the cleanup settings change while enabled (`dictate.cleanup_reload_hook`): make sure Ollama runs and load the model. |
| `cleanup-warm` | `cleanup._rewarm_in_background` | Reload the model after a timeout if Ollama had unloaded it. |
| `cleanup-probe` | `cleanup._probe_in_background` | Re-checks the Ollama server when a dictation found it not "up"; one at a time. |
| `clipboard-restore` | `keys._timer_schedule` (a `threading.Timer`) | Puts the user's clipboard back `linger` (0.5 s) after a paste. |
| `history-writer` | `textrules.History` (`_write_loop`) | Writes history rows, so `History.record` returns at once. |
| `mousehook-reader` | `mousehook.MouseHook` | Reads the mouse helper's pipe. |
| `open-window` | `uiwin.AppWindow.open_async` | Finds or starts the window without blocking. |
| (unnamed) | `dictate.run_app` | Message boxes when there is no console. |
| (PortAudio's own) | `audio.Recorder` | The microphone callback; it only copies audio into a buffer. |

Rule: anything from another thread that touches Tk or `App` state goes
through `App.call_soon(fn)`, which queues `fn` for the next tick.
`App.reload_config` can be called from any thread (it hops to the Tk thread
and waits at most 10 s).

**2. The mouse helper** (`mousehelper.py`) runs only while a mouse button is
part of the hold-to-talk key (or a key is being captured). `mousehook.MouseHook`
starts it, talks to it over a pipe, puts it in a Job object that kills it when
the engine dies; the helper also exits if it hears no ping for 30 s. It
swallows only the chosen button's down and up events. If it cannot run,
`App._note_hook_failure` falls back to polling the button, which then also
does its normal job. See "Why it is built this way" for why it is separate.

**3. The window** (`uiwin.AppWindow`): Microsoft Edge (else Chrome) started
with `--app=URL` and its own profile folder (`uiwin.profile_dir`), with the
flags in `uiwin.BROWSER_FLAGS` (no sync, no extensions, no background
networking, `--disable-gpu`, `--renderer-process-limit=1`, `--disable-breakpad`,
`--no-pings`, `--disable-component-update`). It is a separate process, so a
stuck window cannot affect dictation. Without Edge or Chrome it falls back to
the default browser (`webbrowser.open`), where the app cannot focus or close
it. That fallback is disclosed where it happens: `AppWindow._launch` logs one
plain line, `AppWindow.mode` becomes `"browser"` (reported as `window` in
`/api/state`) and the window shows a small notice (`#browser-note`,
`browserNote()` in `ui/app.js`) that it runs in the user's ordinary browser,
where that browser's extensions and settings apply.

Which window is the app's: only windows of the browser process this object
started, identified by process id, full program path and start time
(`uiwin._same`). The same identity is written to `htt-window.json`
(`uiwin.OWNER_FILE`) in the profile folder, so the next run recognises a window
an earlier run left open and replaces it. A window whose page is not connected
to this run's event stream (`api.Server.page_connected`, `api.CONNECTED_GRACE`
15 s) is replaced too. On close the browser and its helper processes are ended
(`CLOSE_SECONDS` 3 s).

**4. Ollama** is a separate program. `cleanup.ensure_server` starts
`ollama serve` hidden and detached only when AI cleanup is on, `ollama.exe` is
at `%LOCALAPPDATA%\Programs\Ollama\ollama.exe` (`cleanup.ollama_exe`) and the
address is the default local one (`cleanup._is_default_local_url`). It is
started with `DETACHED_PROCESS`, so it outlives the app; the app never stops
it.

## One dictation, step by step

1. **Key down.** On every tick `App._poll` asks `App._combo_down` whether the
   hold-to-talk key is held. Keys are polled with `GetAsyncKeyState`
   (`keys.key_down`). A mouse button comes from the helper (`App._key_down`).
2. **Start.** `App._start` calls `audio.Recorder.start`, plays the start chime
   (`App._chime` -> `sounds.play`), and, only if `sounds.mute_start` is true,
   `Recorder.mute_start` silences the first chime-length plus `CHIME_MARGIN`
   (`App._mute_start_wanted`). For keys that include Win or Alt it taps the
   mask key (`keys.tap_mask_key`).
3. **Listening.** The Tk thread draws the indicator from the recorder's level
   (`App._draw`, `overlay.Overlay.update`). Another key pressed in the first
   `CANCEL_WINDOW` (0.5 s) means the key was part of a shortcut: the
   recording is discarded (`App._discard`, which also plays the stop chime).
4. **Key up.** `App._finish` calls `Recorder.stop_async`, which keeps the
   take open until the tail has arrived, and queues `("take", take, held)` on
   `App._jobs`. The stop chime plays once the tail is in
   (`App._service_recorder`).
5. **Worker.** `App._worker` calls `App._take_samples`: it waits for the take
   and drops it if it is shorter than `min_seconds` (`App._min_seconds`, never
   below 0.1) or only silence (`SILENCE_PEAK`). If there is no speech engine,
   the audio is kept as the held recording (`App._hold_recording`). Then
   `App._process`.
6. **Speech to text.** `App._transcribe` calls `stt.Engine.transcribe`
   (faster-whisper, beam size 5, voice-activity filter with
   `min_silence_duration_ms` 500, `condition_on_previous_text` off, a
   "Glossary:" prompt from `textrules.vocabulary_terms` through
   `stt.glossary_prompt`, limited to `stt.PROMPT_MAX_TOKENS`). Audio stays in
   memory. If transcription fails it retries once, then reloads the model,
   then (if it was on the GPU) loads it on the processor
   (`App._reload_engine`), then gives up (status "failed"; the audio becomes
   the held recording).
7. **Text pipeline.** `pipeline.run` applies `pipeline.STAGES` in order:
   `pipeline.normalize` (fillers if the language is English or unknown, spaces,
   first capital), `cleanup.clean` (AI cleanup, optional), `textrules.apply`
   (snippets, spoken formatting, dictionary). A stage that raises or returns a
   non-string is logged by class only and skipped; the text from before it
   carries on. The detected language is passed to stages whose signature
   accepts `language` (`pipeline._accepts_language`). Each stage's output and
   time go into the trace.
8. **Console safety.** `App._console_safe_final` computes the text as it would
   be without the AI rewrite if that stage added line breaks. `App._deliver`
   pastes it instead when the window in front is a terminal
   (`app.CONSOLE_CLASSES`).
9. **Paste.** `pipeline.text_to_paste` adds the trailing space. `App._deliver`
   waits (in order, without limit) while a recording is in progress or the
   key is held, then calls `keys.paste_text`. That waits up to 10 s for no
   modifier key to be held (else `keys.PasteBlocked`), takes a snapshot of the
   clipboard, writes our text marked with `keys.PRIVATE_MARKERS`, sends Ctrl+V
   (`keys.Win32Input`), and hands the snapshot to `keys._Restorer`. The
   restore runs 0.5 s later on the `clipboard-restore` timer thread. A second
   paste inside that time takes the original over, so the restore happens once;
   if something else wrote to the clipboard meanwhile
   (`GetClipboardSequenceNumber` differs) nothing is restored. `App._shutdown`
   and `atexit` call `keys.restore_pending_clipboard`. The restored copy is
   marked too. If the snapshot is unrestorable (over `MAX_RESTORE_BYTES`, or a
   format that cannot be copied), or writing our text fails, the text is typed
   instead as Unicode key events (`keys.text_events`; a line break becomes
   Shift+Enter). With `restore_clipboard` off the text stays on the clipboard,
   marked.
10. **Record.** `App._process` builds the record and `App._emit` calls the
    `on_result` and `on_dictation` hooks on the Tk thread:
    `dictate.attach_history` stores it (`textrules.History.record`, written on
    `history-writer`) and `api.Server` shows it in the window's feed (the
    last `FEED_SIZE` 50 stay in memory even with "Save history" off).

## Rules you must not break

| Rule | Why it matters | Enforced by |
| --- | --- | --- |
| **Dictated text is never logged.** Log durations, counts and classes, never the text, the vocabulary, the dictionary or snippet contents, and never an exception message that may quote them (`App._log_error`, `pipeline._safe_error`, `api.log_failure`). Only `--log-text` (`App.log_text`) adds text. | The log is a plain file the user may share. | `tests/test_logging.py` (a static check of every log call in `cleanup`, `pipeline`, `textrules`, `learn`, `settings`, `stt`, `sounds`); `tests/test_engine.py` and `tests/test_perf_engine.py` (the engine and app paths at run time); `tests/test_api_hardening.py` (errors logged by class). `app.py`, `api.py` and `dictate.py` are covered by the run-time tests only. |
| **Loopback only, and the trust check.** The window's server binds `127.0.0.1` and checks Host, Origin and the access key. Ollama is contacted only at a loopback address, and only after the owner of the local port is checked (`cleanup._listener_verdict`). | Anything else on the machine or in the user's browser could otherwise read the history or receive dictations. | `tests/test_api.py` (loopback, secret, ticket, Origin), `tests/test_api_hardening.py`, `tests/test_cleanup.py` (endpoint, the removed `allow_remote` key, listener verdicts), `tests/test_settings.py`. |
| **Tk objects only on the Tk thread.** Everything else goes through `App.call_soon` or `App.reload_config`. | Tk is not thread-safe. | Not enforced by a test. `tests/test_perf_engine.py::test_call_soon_callbacks` covers `call_soon` itself. |
| **Pipeline stages return text and may not raise away the user's words.** `pipeline.run` skips a stage that fails; `cleanup.clean` returns its input on any problem. | A broken stage must never lose a dictation. | `tests/test_pipeline.py` (a failing stage and a non-string result are skipped), `tests/test_cleanup.py`. |
| **Config is read at use, not cached.** Read options from `cfg` on each call (`sounds.settings_of` is the pattern). | Settings apply to the next dictation without a restart. | Not enforced for new code. `tests/test_flow.py`, `tests/test_api.py::test_settings_apply_live` and `tests/test_wiring.py` show the existing code applying changes live. |
| **Test instances never touch real data.** | A test or experiment must not damage the user's history or settings. | `tests/test_00_home.py`, `tests/fakes.py`, `tests/test_paths.py`, `tests/test_startup.py`; the installer tests use temporary folders and a throwaway registry key (`tests/test_install.py`). |
| **Nothing personal in tests, examples or documents.** Use invented examples such as the made-up word "Zorvex"; write `%LOCALAPPDATA%`, never a real path. | The project is meant to be published. | By review. Only partly automatic: `tests/test_install.py` checks that no personal-data file name is in the install list and that the lock files hold no user name. |
| **Nothing dictates before the Terms of Use are accepted, and acceptance is an active, recorded step.** `settings.TERMS_VERSION` is the one source of the version (the first line of `TERMS.md` must match it). Until `terms-accepted.json` in the data folder holds that version, `App._poll` ignores the hold-to-talk key and the mouse hook is not started; the only ways to the record are the installer's typed `I agree` and `POST /api/terms/accept` from the terms screen. `-Yes` (unattended) writes no record, and a record whose `how` is `unattended` does not count (`terms.is_current` accepts only `installer` and `app`), so the app asks the person who uses it. There is no flag, setting or environment variable that skips it; tests make the record in their own temporary data folder (`tests/fakes.py: accept_terms_in`). Raising `TERMS_VERSION` asks everyone again. | The terms are only worth anything if acceptance is real and can be shown. | `tests/test_terms.py` (text, record, hotkey, API, tray), `tests/test_install.py` (`test_7*`, `test_8`, `test_9`). |
| **Data deletion by uninstall is by name.** `htt-common.ps1`'s `Remove-AppDataFiles` removes only the names the app creates and never the folder recursively. | The data folder may be `HOLDTOTALK_HOME`, which can point anywhere. | `tests/test_install.py` (`test_6_*`). When the app learns to create a new file in the data folder, add its name there and to that test. |

## Why it is built this way

The code does not record reasons; this table holds only the reasons the
project's own records give. Where none is given it says "not recorded".

| Decision | Reason |
| --- | --- |
| The keyboard is polled every 15 ms (`GetAsyncKeyState`) instead of hooked | A global keyboard hook would see every keystroke the user types, which is unwanted in a privacy-focused app and is the kind of thing antivirus tools flag. Polling adds no input lag and cannot leave a key stuck. The price: keyboard keys still reach the focused program. Why 15 ms specifically: not recorded. |
| Mouse buttons use a low-level mouse hook, in a separate helper process | A held Back button would navigate the browser back on release, so it must be swallowed, which needs a low-level mouse hook. A hook callback runs for every pointer movement; inside the main process it would wait on Python's GIL (global interpreter lock) while audio is transcribed (measured there: a 1 ms sleep overshooting by a median of 14.6 ms and up to about 100 ms), making the whole system's pointer stutter. |
| The on-screen indicator is a Tk window | Tk ships with Python and can make a click-through, never-focused, always-on-top window. |
| The window is a web page in Edge "app mode", not an embedded web view | Measured on the development machine, Edge app mode opened in about 0.5 s against 1.7 to 4.5 s for the embedded web-view library, needed no extra packages and used less memory. |
| The window talks to the engine over a loopback HTTP server guarded by a one-time ticket, a per-run access key and Host/Origin checks | The history holds everything the user has dictated, and any other website open in the user's browser could otherwise try to reach a local server. |
| User data lives in the user's profile, apart from the program files, and is local rather than roaming | So that updating or reinstalling never touches it and it can never be published by accident with the source; local because it is private and machine-specific. |
| The Python environment lives in `%LOCALAPPDATA%\HoldToTalk\venv` | A short path, so that Windows' 260-character path limit is never hit wherever the source or program folder is. |
| `install.bat` starts PowerShell with `-ExecutionPolicy Bypass` | Windows' default policy refuses to run unsigned local scripts; the option applies to that one command and changes no system setting. |
| Text is pasted through the clipboard | It is fast and reliable for long text in any program. It is typed instead only when the clipboard's previous contents could not be restored faithfully. |
| Speech models: `large-v3-turbo` on an NVIDIA card, `small` on the processor | Measured on the development machine (a high-end NVIDIA graphics card; a desktop 8-core processor): `large-v3-turbo` transcribed a dictation in about 0.26 to 0.6 s; `small` on the processor took about 5 to 6 s for 9 s of speech; `large-v3-turbo` on the processor took about 25 s per clip, which is why it is not used there. This project has not measured recognition accuracy. |
| AI cleanup model `gemma4:e2b-it-qat` | Picked from a three-model comparison on 25 sample transcripts, for speed and for making the fewest unwanted edits; of the two alternatives, both were several times slower and one obeyed an instruction that had been dictated as text. Because any model can misbehave, its output is treated as untrusted and checked before use. |
| Dependencies are installed from lock files with checksums | So that every install gets the same tested versions and nothing is built from source. |
| History in SQLite; a named mutex for one instance; `pythonw` for no console; the install list as an allow-list; the limits on the local server | Not recorded here. (The module docstrings of `textrules.History` and `install-files.txt` say something about the first and the allow-list; read them, not this table.) |

## Models

**Speech recognition**: faster-whisper (CTranslate2) with settings in
`settings.DEFAULTS`: `model` = `large-v3-turbo` for a graphics card,
`cpu_model` = `small` for the processor, `device` = `auto` (or `cuda` /
`cpu`), and `compute_type` = `auto` (or one of `settings.COMPUTE_TYPES`).
faster-whisper maps the names to Hugging Face repositories
(`mobiuslabsgmbh/faster-whisper-large-v3-turbo` and
`Systran/faster-whisper-small`, see `faster_whisper.utils._MODELS`).

**Pinned revisions.** Both models are loaded and downloaded at an exact commit,
not at "whatever the repository holds now": `stt.MODEL_REVISIONS` (name to
40-character commit hash) is the one place, `stt.model_kwargs` passes it as
`revision=` to `WhisperModel` (cache lookup and download alike, so a cached
snapshot still loads offline) and the installer's optional pre-download reads
the same table. A model name that is not in the table (a custom `model`) is
loaded unpinned. To update a pin: take the new commit from the model's page
on huggingface.co (`/api/models/<repository>`, field `sha`), put it in
`MODEL_REVISIONS`, run the tests, say so in CHANGELOG.md, and release.

**How GPU or processor is chosen.** `stt.Engine.__init__` builds a list of
attempts. For `auto` or `cuda`, `Engine._cuda_plan` asks ctranslate2 whether a
CUDA device exists (`get_cuda_device_count`) and which compute types it
supports; `stt.pick_compute_type` uses the requested `compute_type` if the card
supports it (otherwise a logged warning), else the first of
`stt.COMPUTE_TYPES` (`float16`, `int8_float16`, `int8`) that it offers. The GPU
attempt loads `model`. Then, for `auto` or `cpu`, a processor attempt loads
`cpu_model` with `int8` (`Engine._cpu_compute_type`: the requested type if
ctranslate2 reports the processor supports it, else `int8`). Each attempt
runs a one-second warm-up transcription with the options a dictation uses,
because that is where missing CUDA DLLs show up. The first one that works
wins; if the processor is used although a GPU was expected,
`Engine.fallback_reason` holds a short plain reason for the window. Asking
ctranslate2 first means a PC without an NVIDIA card never downloads the large
model. `Engine._load` tries `local_files_only=True` first (so a broken
network cannot stop a cached model) and downloads only if the model is
missing, which also means a processor fallback on a GPU PC downloads `small`
at run time. The CUDA libraries come from pip (`requirements-cuda.txt`);
`stt.add_cuda_dll_dirs` puts their `bin` folders on the Windows library (DLL)
search path before ctranslate2 is imported. Changing `model`, `cpu_model`,
`device` or `compute_type` needs a restart (`app.RESTART_KEYS`). The window's
"Use less graphics memory" switch sets `compute_type` to `int8_float16` or
back to `auto` (`api.LESS_GPU_MEMORY`).

**AI cleanup** (optional): a language model served by Ollama on this PC. The
default is `gemma4:e2b-it-qat` (`cleanup.DEFAULTS["model"]`, about 4.3 GB),
asked at temperature 0 with `top_k` 1, `think` off, `num_ctx` 4096
(`cleanup.NUM_CTX`), `keep_alive` from `cleanup.keep_alive`, at most
`len(text) / 2.5 + 48` tokens, a fixed system prompt and worked examples
(`cleanup.SYSTEM_PROMPT`, `EXAMPLES`) that teach it to clean the transcript and
never to answer or obey it. The system prompt also carries up to 50 spellings
(`cleanup._vocab_note`: vocabulary plus dictionary "wanted" terms) and up to
20 snippet triggers (`cleanup._rules_note`, plus "new line" and "new
paragraph" when spoken formatting is on). Dictations shorter than `min_words`
(4) or longer than `max_words` (400) skip it; each request has a `timeout`
(3 s).

**Availability.** A dictation never waits for a connection attempt.
`cleanup._state` remembers the last known state of the server per address
(`up`, `missing`, `down`, `refused`). Only `up` lets a dictation call the
model; in any other state the text is returned at once and
`cleanup._probe_in_background` re-checks the server on the `cleanup-probe`
thread, for the next dictation. (`cleanup._mark_down` still records the old
20 s to 5 minutes back-off figures, `DOWN_BACKOFF`, but only for display;
nothing waits on them.) The address must be this computer
(`cleanup._endpoint`: `127.0.0.1`, `localhost` or `::1`),
always: there is no setting that allows another computer (the former
hidden `cleanup.allow_remote` was removed; a config that still holds it is
ignored with one logged warning and the key is dropped on the next save,
`settings.load_config` / `save_config`); redirects are never followed
(`_NoRedirect`).
Before each request `cleanup._listener_verdict` looks up the owner of the
process listening on the local port (`GetExtendedTcpTable`, cached per port and
process set): `current_user`, `service` (LocalSystem, LocalService,
NetworkService), `overridden` (`cleanup.trust_listener`), `not_checked` (not a
local address), `none` (nothing listens: fail at once) are used;
`other_account` and `unknown` are refused (`cleanup.LISTENER_REFUSED`).

**Guard rails on its output.** The original text is kept, never the model's,
when `cleanup.clean_verbose` finds that the output:
- is empty or contains a character of Unicode category Cc, Cf, Cs, Zl or Zp
  other than a newline (`_BAD_CATEGORIES`, checked on the raw reply before
  `_tidy` and again in `_check`);
- contains a shell metacharacter not already in the transcript
  (`_SHELL_META`), or a token more than `TOKEN_SLACK` (8) characters longer
  than the longest transcript token;
- is more than `MAX_GROWTH` (1.25) times plus `GROWTH_SLACK` words long, or
  `MAX_CHAR_GROWTH` (1.25) times plus `CHAR_SLACK` (40) characters long, or
  has fewer than `MIN_KEEP` (0.4) of the words (inputs of 10 words or more);
- has more than `MAX_NOVEL` (0.25) of words that were not in the input, or
  loses content words beyond `MAX_DROPPED` (0.1, at least
  `MIN_DROPPED_ALLOWED` 2; `MAX_DROPPED_CORRECTION` 0.5 when the speaker
  corrected themselves);
- has line breaks, unless the transcript was a spoken enumeration
  (`_enumerated`) and every line is a first line followed by list items made
  of words from the transcript (`_check_lines`, `_check_item`);
- starts like an assistant reply, or adds, drops, swaps or moves a negation
  (matched per negation with `NEG_CONTEXT` 2 neighbouring words; a dropped one
  is excused only by an English correction cue within `CUE_WINDOW` 5 words that
  is itself gone from the result: `_check_negations`);
- removed or created a snippet trigger or a spoken "new line"
  (`textrules.rule_hits`).
For non-English dictations the English-only allowances (fillers, correction
cues, small free words) are not applied (`cleanup._is_english`). These checks
catch many unwanted changes, not all.

## The local API and its security

The window is a web page served by the engine itself, so other programs and
web pages on the same machine can reach the server unless it defends itself.
The history holds everything the user dictated, so `api.py` is strict. Its
module docstring has the full list; the main points:

- **Loopback only.** `api.Server` refuses any host that is not loopback
  (`_is_loopback`) and binds `127.0.0.1` on a free port.
- **Access key.** `Server.secret` is random per run (`secrets.token_urlsafe`)
  and must arrive in the `X-HTT-Key` header on every API call
  (`Server._check_key`, constant-time compare, checked before a body is read).
- **One-time ticket.** The page cannot be given the key in a header on first
  load, so `Server.launch_url` makes a single-use ticket valid for
  `TICKET_SECONDS` (120 s) and puts it in the URL fragment (`#t=...`), which
  browsers never send to a server or in a Referer. The page POSTs it to
  `/api/hello` (`Server._redeem`) and gets the key, which it keeps in
  `sessionStorage`. There are no cookies: a browser would send a `127.0.0.1`
  cookie to every local port.
- **Host, Origin, Sec-Fetch-Site.** `Server._route` requires the `Host` header
  to be exactly `127.0.0.1:<port>` (stops DNS rebinding); an `Origin`, when
  sent, must be this server's; `Sec-Fetch-Site`, when sent, must be
  `same-origin` or `none`; POST requires an `Origin` and `Content-Type:
  application/json`. No CORS (cross-origin sharing) headers are ever sent.
- **Bounded.** Bodies are capped at `MAX_BODY` (256 KB, answered 413).
  `MAX_CONNECTIONS` (48) requests are read at once; further connections are
  closed. Deadlines are real totals, not per read (`_DeadlineReader`):
  `HEADER_TIMEOUT` 5 s for the request line and headers (also the idle
  keep-alive wait), `BODY_TIMEOUT` 10 s for the body, `WRITE_TIMEOUT` 10 s
  for the client to accept an answer, `PRESSURE_SECONDS` 1 s for headers
  while three quarters of the slots are in use, `DRAIN_LIMIT` 4 MB read away
  from a refused body in at most `DRAIN_SECONDS` 2 s. A live stream gives its
  slot back once authenticated (`release_slot`) and counts against
  `MAX_STREAMS` (16, answered 503). This frees slots quickly; it does not stop
  a program of the same account from reconnecting over and over.
- **Never raises out of a handler.** A lone surrogate in JSON is a 400
  (`has_surrogate`); other methods get 501 (HEAD 405); an unexpected error is
  logged by class and code lines only (`log_failure`, `_HTTPServer.handle_error`).
- **Static files** come from a fixed list (`api.STATIC`), never from a path in
  the request. No route reads or writes a file the request names or runs a
  command.
- **The Terms of Use** are not a static file. `GET /api/terms` (access key
  required) returns the text of the one fixed file `TERMS.md`
  (`terms.terms_path()`, never built from the request) with the version and
  the local acceptance record; it refuses (503) if the file is missing or its
  first line is not `settings.TERMS_VERSION`. `POST /api/terms/accept` takes
  `{"version"}`, must equal `TERMS_VERSION`, and writes the record with how
  "app" (`App.accept_terms`, which also lifts the hold on the hotkey at once).
  `POST /api/quit` (the terms screen's Quit) works only while the terms are
  pending, so it adds no way to stop a running, accepted app beyond the tray.
  `live.terms.pending` and `info.terms` report the state to the window.
- **Browser hardening.** Responses carry a strict Content-Security-Policy
  (`api.CSP`), `nosniff`, `no-referrer` and cross-origin isolation headers
  (`Server._security_headers`).
- **Logs.** The log viewer's lines go through `Server._redact`, which removes
  the key and any ticket. `GET /api/log` returns only the in-memory buffer
  (`LOG_LINES` 400); "Clear history" clears it too.

Routes are the dictionaries `api.GET_ROUTES` and `api.POST_ROUTES`. Live
updates (state, microphone level, new dictations) go over a server-sent-events
stream, `GET /api/events` (`Server._stream`, `api.Hub`).

## Where data lives

| What | Where | Defined in |
| --- | --- | --- |
| Settings, vocabulary, dictionary, snippets, learned records | `%LOCALAPPDATA%\HoldToTalk\data\config.json` | `settings.CONFIG_PATH` |
| Dictation history | `...\data\history.sqlite3` (SQLite, plus `-wal`/`-shm` side files) | `settings.HISTORY_PATH`, `textrules.History` |
| Log | `...\data\logs\dictate.log` (rotated to `.1` at 1 MB) | `settings.LOG_PATH`, `dictate.CappedLog` |
| Files the app may leave beside them | `config.json.bad`, `config.json.bad-<time>`, `config.json.copying`, `.cfg-*.tmp`, `history.sqlite3.corrupt-<time>`, `history.sqlite3.copying` | `settings.save_config`, `settings.load_config`, `textrules.History._quarantine` |
| Record of accepting the Terms of Use: `{"version", "accepted_at" (UTC), "how"}`, how is `installer` or `app` (an `unattended` one is read but never counts as acceptance); local only, nothing personal; a `.terms-*.tmp` while being written | `...\data\terms-accepted.json` | `terms.py` (app), `Write-TermsRecord` in `htt-common.ps1` (installer); removed by `Remove-AppDataFiles` |
| Window's browser profile | `%LOCALAPPDATA%\HoldToTalk\window` (a test instance: `window-<tag>`) | `uiwin.profile_dir` |
| Python environment | `%LOCALAPPDATA%\HoldToTalk\venv` | `install.ps1` |
| Program files | `%LOCALAPPDATA%\Programs\HoldToTalk` | `install.ps1` |
| Speech models | The Hugging Face cache: `HF_HUB_CACHE`, else `HF_HOME\hub`, else `%USERPROFILE%\.cache\huggingface\hub` | `dictate._model_cache_dir`, `uninstall.ps1` |
| Start with Windows | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, value `HoldToTalk`; Task Manager's on/off flag under `...\Explorer\StartupApproved\Run` | `tray.set_autostart` |

`settings.DATA_DIR` is `%LOCALAPPDATA%\HoldToTalk\data`, or the folder in the
environment variable `HOLDTOTALK_HOME` (`settings._default_data_dir`). Setting
`HOLDTOTALK_HOME` alone does not make an isolated instance: `dictate.instance_suffix`
returns an empty tag unless `--config`, `--history` or `--log` was used, so such
an engine shares the real mutex, events, window profile and Start with Windows
name.

**Migration.** Data that older versions kept next to the code is moved to the
data folder once, by `dictate.migrate_once`, only for the real instance (not a
test instance, and not when `HOLDTOTALK_HOME` is set, which only logs that old
data was not moved), after the single-instance lock. `settings.migrate_legacy_data`
copies `config.json` (via `config.json.copying`) and copies the history with
sqlite3's backup API (via `history.sqlite3.copying`), compares the row count,
and only then renames the old files `*.migrated` (never deleting or
overwriting; if the new file already exists the old one is left, "kept_new").
`dictate._retire_old_log` renames the old `dictate.log` and `dictate.log.1`
next to the code the same way, because older logs held dictated text. On
failure the app shows a message box and a "data" notice in the window and runs
with defaults and an empty history (`dictate.migration_problem`). `load_config`
blanks excerpts of dictations that older versions stored in `learned` records
and rewrites the file (`learn.scrub_excerpts`).

**Damaged files.** `load_config` on a file it cannot parse copies it to
`config.json.bad` and uses defaults. `save_config` on a file it cannot parse
writes nothing, copies it to `config.json.bad-<timestamp>` and raises
`settings.ConfigError`. A damaged history database is renamed
`history.sqlite3.corrupt-<time>` and a fresh one started (or an in-memory one
for the session if it cannot be moved); `History.clear` deletes those copies
too.

**Start with Windows.** `dictate.fix_autostart` runs at every start of the
real instance: if the entry points at another `dictate.py` it is rewritten to
this copy only when this copy is the installed one (`dictate.install_dirs`) or
inside the folder the entry points at; a checkout run by `start.bat` leaves it
alone, and so does an entry disabled in Task Manager. `tray.autostart_enabled`
compares the command text exactly.

## How settings apply live

The window (or the tray menu) never edits the running state directly. A change
goes through `settings.apply_changes`: it takes `settings.CONFIG_LOCK`, writes
`config.json` atomically (`settings.save_config`: temp file, `fsync`,
`os.replace`; a section such as `cleanup` only changes the keys named), then
calls `App.reload_config`. That runs `App._reload` on the Tk thread, which
re-reads the file (`settings.load_config`), compiles a changed hold-to-talk
key (an invalid one is rejected and the old one kept), switches microphone,
updates the indicator colours, swaps `self.cfg` and `engine.cfg`, and calls the
`on_reload` hooks (`dictate.cleanup_reload_hook` starts Ollama and loads the
model when the cleanup settings changed and cleanup is enabled). The pipeline
and engine read `app.cfg` on every dictation, so vocabulary, dictionary,
snippets, language and the text options apply to the next one.
`model`, `cpu_model`, `device` and `compute_type` are in `app.RESTART_KEYS`:
the file is updated, but they apply after a restart. A value of the wrong type
in `config.json` is replaced by its default with a warning that names the key
but never quotes the value (`settings.validate`). Editing `config.json` by
hand is picked up the next time anything triggers a reload (any change in the
window) or at the next start.

## Install, update, uninstall

There is one installer, `install.ps1`, with two faces. `install.bat` with no
arguments opens the setup window (`install-gui.ps1`, described under "The
setup window" below); with any argument, `-Console` included, it starts
`install.ps1` as the text installer in the Command Prompt. Both are started
with `-ExecutionPolicy Bypass -File` and work under Windows PowerShell 5.1,
which every supported Windows has, so the window works before Python is
installed. The window never installs anything itself: it runs `install.ps1`
as a hidden child process and shows what it prints.

The text installer first. Before anything is changed it shows the
summary of the Terms of Use (the "## Summary" section of `TERMS.md`, read by
`Get-TermsSummary`; the installer stops if `TERMS.md` is missing or its
version differs from `settings.TERMS_VERSION`), says where the full text is,
and asks for the words `I agree` in any letter case (`Confirm-Terms`). `READ`
prints the whole text in the window a screen at a time and `NOTEPAD` opens it
in Notepad (in test mode only names the file); both ask again. (Whole words,
so that a plain `n` for "no" can never open anything.) Any other answer,
closed input included, exits 3 having changed nothing. After the summary the
installer prints "This includes the sections on no warranty, limits of
liability, assumption of risk, indemnity and disputes (sections 9 to 14)." and
the instruction to type `I agree`. `-Yes` installs without questions but does
not accept for anyone: it prints the summary and a line saying so, writes no
record (`Confirm-Terms` returns "unattended", and the main block skips
`Write-TermsRecord`), and the app asks the person who uses it. The record (`Write-TermsRecord`, the data
folder's `terms-accepted.json`) is written once Python and the files have been
checked, just before the first real change (step 3 below); the installer asks
on every run. Steps, in order:

1. **Python** (`Find-Python`): `HTT_PYTHON` if set, then `py -3.12`, `-3.13`,
   `-3.11`, `-3.10`, then `python`; each is probed by a script run as
   `python -I` that must report 64-bit, machine `AMD64`, 3.10 to 3.13, with
   `tkinter` and `venv`. The Microsoft Store shortcut is refused.
2. **Files.** It copies exactly the files named in `install-files.txt` (an
   allow-list: a source folder may hold personal files; `Test-ManifestEntry`
   refuses an entry that is rooted, has a drive letter or a colon, contains `..`,
   a quote character or a brace, and `Install-Files` checks again that each target
   is inside the install folder), unblocks them, and
   removes files with the extensions `.py .bat .ps1 .ico .md .txt .html .css
   .js` that an older version installed and the list no longer names.
3. **The running app** is asked to quit (`dictate.py --quit`), never killed.
4. **The environment** (`Ensure-Environment`) is judged by running its Python;
   if it cannot run it is deleted and recreated with `python -I -m venv`.
5. **Requirements.** pip installs from `requirements.lock.txt` (exact versions
   with sha256 hashes) with `-I -m pip install --disable-pip-version-check
   --only-binary :all:`, so nothing is built from source and pip refuses any
   file whose hash is not listed. If the lock install fails, the installer
   warns and retries from the range file (`requirements.txt`).
   `-UseRanges` (or `HTT_USE_RANGES=1`) installs from the ranges directly. The
   NVIDIA runtime (`requirements-cuda*.txt`) is added only if `nvidia-smi.exe`
   is in System32 or a video controller's name contains "NVIDIA"
   (`Test-NvidiaGpu`; `-Gpu yes|no` overrides) and the person agrees:
   `Resolve-GpuChoice` asks "Install the NVIDIA runtime?" (default yes;
   installing means accepting NVIDIA's licence) in an interactive install,
   skips the question when the runtime is already installed (the stored hash
   equals the one with the runtime), and with `-Yes` installs it and prints the
   notice. A "no" installs the plain requirements, stores the `--no-cuda--`
   hash and prints that the processor will be used; running the installer
   again asks again (and adds the runtime on a yes). `start.bat`
   (`-EnvOnly`) keeps an earlier "no". The app copes: `stt.cuda_runtime_installed`
   (the pip `nvidia\cublas` DLLs, or a cuBLAS on `PATH`) is checked in
   `Engine._cuda_plan`, so without the runtime the graphics card is not tried,
   the large GPU model is never downloaded for nothing and the window says the
   NVIDIA runtime is not installed. A hash of the file pip
   actually installed from (plus `--cuda--` or `--no-cuda--`) is stored in
   `venv\htt-requirements.sha256`; pip runs again whenever it differs. If only
   the NVIDIA part fails, the hash file is not written, so the next run
   retries.
6. **Speech model.** The model name is read by regular expression from
   `settings.py` (`Read-SourceFact`: the `"model":` and `"cpu_model":` lines of
   `DEFAULTS` must keep that literal `"key": "value"` form, as do `APP_NAME` and
   `APP_VERSION`). It asks before downloading, and uses the same pinned revision
   as the app (`MODEL_REVISIONS` in `stt.py`, read by `Read-SourceFact` from the
   `"name": "40-hex-commit"` lines, passed as `revision=` to
   `faster_whisper.download_model`).
7. **Shortcuts and registry.** Start menu and Desktop shortcuts run
   `pythonw dictate.py --open`; the Installed apps entry is written under
   `HKCU\...\Uninstall\HoldToTalk`.

The helper scripts (`probe.py`, `model.py`) are written to a folder made fresh
for the run, `htt-<guid>` in the temp folder, readable only by this account
(`Get-WorkDir`), and run with `python -I`, so a file planted in a shared
folder such as `%TEMP%` is never imported. Updating is running `install.bat`
again; it keeps `data`.

### The setup window

`install-gui.ps1` is a WPF window built from XAML held in the script, in the
app's own colours (the palette of `ui/app.css`, light or dark after Windows'
setting). `install.bat` runs it with `-Launch` in its console: the launcher
starts the window in a second PowerShell that has no console (`-STA`), waits
for a named event that the window sets once it has loaded, and returns 0, so
the console closes. If the window process ends first, the launcher prints one
line with the reason (the window writes it to a file named by `-ReasonFile`)
and returns 1, and `install.bat` goes on with the text installer.

The window and the engine talk through the engine's command line and its
output, nothing else:

- `install.ps1 -Check` changes nothing and prints one **facts** line: the
  folders, the installed version if there is one, the Python it found (or the
  list of what it tried), whether there is an NVIDIA card and whether the
  runtime is already in the environment, whether the model is on disk, the
  sizes, the free disk space and the titles of the steps. The window's third
  page is drawn from it. "Check again" runs it again.
- `install.ps1 -Progress -AcceptedTerms <version> -Nvidia yes|no -Model
  yes|no -DesktopShortcut yes|no` does the install. In this mode the engine
  never reads input and never starts the app. It prints its ordinary text,
  and in between it prints **marker lines**: `@@htt ` followed by one line of
  JSON with a `kind` (`step`, `activity`, `warning`, `error`, `done`). The
  window reads standard output and standard error as UTF-8, treats marker
  lines as data and everything else as text for "Show details".

What the window does with that output is deliberately little, because pip
and the model download write to the same pipes as the engine:

- A marker is believed only on standard output; a line on standard error is
  text, whatever it starts with. Only a line feed ends a line (the window reads
  the pipes with its own `HttLfReader`, not `StreamReader.ReadLine`, which also
  ends a line at a lone carriage return): a line that holds a lone carriage
  return, such as `progress<CR>@@htt {...}`, is split there by `Receive-RawLine`
  and its pieces are log text only, so a program cannot forge a marker with it.
  A line longer than the reader's buffer limit (1 000 000 characters) is cut
  there; the cut piece and the next line the reader returns (which may be the tail
  of the same long line) are flagged `Untrusted` by the reader and never taken as
  markers (`Invoke-EngineTick` passes `Trusted` only for standard output and a
  line that is not flagged).
- A marker is never run or evaluated. It is parsed as JSON, and its fields
  are shown as plain text. A step marker outside the list of steps from the
  facts is ignored.
- No marker says what to start. "Start Hold to talk now" runs
  `<environment>\Scripts\pythonw.exe "<install folder>\dictate.py" --open`,
  worked out by the window from the folders in the facts, and only if that
  file exists. "Open folder" opens a path only if it is a folder.
- `-Check` runs before the Terms of Use page is shown. It looks for Python
  (running `py` and `python` to ask their version) and, if the environment
  exists, asks it whether the speech model is on disk. It changes nothing.

The rules about the Terms of Use are kept by the engine, not the window:

- `-AcceptedTerms` is allowed only together with `-Progress`, and its value
  must equal the version in `TERMS.md` and `settings.TERMS_VERSION`; otherwise
  the engine exits 1 before anything is changed. The record is written with
  the way "installer", at the same point as in the text installer.
- The window passes it only after a person has ticked the box and chosen
  **I agree** (the box starts unticked; the button does nothing without it).
- `-Progress` without `-AcceptedTerms` exits 3 with nothing changed. `-Yes`
  still never accepts, and is refused together with `-Progress` or
  `-AcceptedTerms`.
- What code cannot do: tell a person from a program. Someone who runs
  `install.ps1 -Progress -AcceptedTerms 1.0` by hand is making the statement
  themselves, exactly as if they had typed `I agree` at the text prompt, and
  it is recorded the same way. The same is true of a program that types into
  the prompt or clicks the button. The installer makes acceptance an explicit
  step that does not happen by default or by accident; it does not, and
  cannot, prove who took it.

Exit codes of the engine in this mode: 0 done, 1 failed, 3 the terms were not
accepted, 4 no suitable Python (nothing changed). Exit codes of the window:
0 installed, 1 failed, 2 stopped during the install, 3 closed before
installing (nothing changed).

For the tests the window takes `-SelfTest` (checks its logic without showing
anything), `-Engine <path>` (another engine: `tests/fixtures/
fake_install_engine.ps1` plays back canned output), `-Shots <folder>` (saves
a picture of every page from canned facts with made-up paths) and `-AutoRun`
(drives the window without a person). `-AutoRun` ticks the terms box by
itself, so it refuses to run unless all five test overrides (`-InstallRoot
-EnvDir -StartMenuDir -DesktopDir -RegKey`) are given, the four folders are
inside the temp folder and the key is under `Software\HoldToTalkTest\`, and the
environment does not lead out of the temp folder either: `HOLDTOTALK_HOME` (the
data folder, where the uninstaller deletes files and the install writes the
record of the Terms) and the other variables `Get-HttLayout` reads for a path
(`HTT_INSTALL_ROOT`, `HTT_ENV_DIR`, `HTT_START_MENU_DIR`, `HTT_DESKTOP_DIR`) must
be unset or inside the temp folder (`Get-SandboxProblem`, in both windows): a
test can never install for real or record a real acceptance. `-Engine` is
honoured only under the same conditions (or in `-SelfTest` and `-Shots`).
When a test checks that a script parses, the file names go to PowerShell in
an environment variable (`ps_parse_errors` in `tests/test_install.py`):
anything placed after `-Command <text>` on the command line becomes part of
the command, so a script named there would be run. Pictures of the
real window show folder paths that contain the Windows account name; do not
publish one, use `-Shots`.

**Install Python for me.** When the facts say there is no suitable Python, the
third page offers, besides "Open python.org" and "Check again", a primary button
"Install Python for me". The panel is short on purpose: a plain lead ("Python is
a free program that Hold to talk needs. Choose the button and Setup downloads it
from python.org (about 27 MB) and installs it for your Windows account only.
Nothing is downloaded until you choose the button."), the three buttons directly
under it (visible without scrolling at 780x580 and at 880x470), and a collapsed
"Details" expander. The details say what is downloaded (the official Python
3.12.10 installer, 64-bit), that it is checked against a fixed fingerprint and the
Python Software Foundation's signature, what is installed (Python 3.12.10 for this
Windows account only, with the "py" launcher) and what is not (no administrator
rights, python.exe is not added to PATH, no file associations, no Start menu
shortcuts; the launcher may add its own folder to PATH), that a PC with an ARM
processor still takes the 64-bit installer, and that Python has its own licence
(the PSF License). They also hold the "install it yourself" steps and what the
check looked at. Nothing is downloaded before the click. One file is
pinned in `$script:PyPin`: the exact address
`https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe`,
its size (26 964 224 bytes) and its SHA-256. 3.12.10 is the newest 3.12 release
with a Windows installer (later 3.12 releases are source-only). The window
downloads it with `HttpClient` over TLS 1.2 or newer (redirects are refused, the
length must equal the pinned size) in a state machine advanced by its timer (so
it never waits on the network; Cancel stops it and deletes the partial file) into a
folder made fresh for the run (`htt-py-<guid>` in the temp folder, this account
only, created new by `New-PrivateTempDir`; if its access rights cannot be set the
run stops with a plain error). When the download ends the file is opened at once
with `FileAccess.Read` and `FileShare.Read` and stays open until the installer
process has been started, so it cannot be replaced or renamed in between;
`Test-PythonInstaller` takes the size and the SHA-256 from that open handle and
also requires `Get-AuthenticodeSignature` status `Valid` with a signer subject
containing "Python Software Foundation"; if either fails the file is deleted, a
plain error is shown and nothing runs. It then runs
`/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1
InstallLauncherAllUsers=0 Include_tcltk=1 Include_test=0 SimpleInstall=1
AssociateFiles=0 Shortcuts=0` (option names from
docs.python.org/3.12/using/windows.html, "Installing Without UI"), waits for
it, treats exit code 0 and 3010 (Windows Installer: done, a restart may be needed
later; the panel says so) as success and any other code as a failure, deletes the
folder in every case, refreshes its own `PATH` and `HTT_PYTHON` for the child
process (the launcher's installation changes the user's PATH, which a running
window does not see) and runs "Check again". After 15 minutes
(`$script:PyTimeoutSeconds`) the panel says the installer is taking a long time and
shows "Stop waiting"; it, and closing the window while the installer runs, ask
"Stop waiting for Python?" (Stop waiting / Keep waiting), and on yes
`Stop-PythonInstallWait` ends only the process object this window started, cleans
up and shows the manual route. In test mode (the five overrides, or any harness)
the button calls `Invoke-PythonInstallStub`, which only records the call and sets
`HTT_FAKE_PYTHON_INSTALLED`, so the fake engine's "nopython" scenario then finds
Python; the real download and run refuse in test mode unless the self-test injects
a local source and a fake installer (`$script:PyOpen`, `$script:PyRun`).
`install.ps1` itself is unchanged by this.

### The Setup.exe

`HoldToTalk-Setup-<version>.exe` is one file for people who do not want a ZIP.
It is only a carrier (Inno Setup 6, `installer\holdtotalk.iss`); it installs,
registers and uninstalls nothing itself. `CreateAppDir=no`, `Uninstallable=no`,
`CreateUninstallRegKey=no` and `PrivilegesRequired=lowest` mean it has no
install folder, no Start menu group, no uninstaller and no entry in Installed
apps, and never asks for administrator rights; every Inno page is skipped.
`install.ps1` does all of that, exactly as for the ZIP route, which is unchanged.

What it does: `InitializeSetup` unpacks the files named in `install-files.txt`
plus `files.sha256` into `{tmp}\app` (Inno's private temporary folder; the
files are `dontcopy` entries extracted one by one and moved into their folders
by a generated `PlaceFiles`), then runs `{sys}\WindowsPowerShell\v1.0\
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden
-File {tmp}\app\install-gui.ps1` started with `SW_HIDE` (no console window; the
WPF window still shows) and waits. So the setup window, and through it
`install.ps1`, run from the unpacked folder, which exists until the exe ends.
Setup then exits with the window's exit code (0, 1, 2, 3): under `/SILENT` and
`/VERYSILENT` through `GetCustomSetupExitCode` and Inno's normal clean-up; when
run by a person, Inno would show a wizard page, so it calls `ExitProcess`
instead and a hidden PowerShell removes the temporary folder a moment later. That
child runs a small script (`CLEANUP_SCRIPT` in `tools\build_setup.py`, written by
the exe to a file of its own in the temp folder and started with `-File`, which
deletes itself): the folder's name reaches it in the environment variable
`HTT_CLEAN_TARGET`, never through `cmd` (where a `%` would be expanded) or in a
command text; it removes only a folder named `is-<letters and digits>.tmp` directly
in the temp folder, retrying for up to 30 seconds. Inno may hand over `{tmp}` in its
8.3 short form, so the folder's parent and the temp folder are both turned into long
names (`GetLongPathNameW`) before they are compared. A target that is itself a
reparse point (attribute 1024) is refused, and the contents are deleted by the same
no-follow walk as `Remove-TreeNoFollow` (inlined: the script cannot dot-source
`htt-common.ps1`), so a junction inside is removed as the link and what it points to
survives; ReadOnly is cleared on the way. If
PowerShell or the script is missing, the process cannot start, or the window
process ends with a code outside 0 to 3 (it could not even show itself), one plain
message box says the setup window could not be opened on this PC and that the app
can also be installed from the ZIP on the project's page with `install.bat`, and
the exit code is 1 (no box when `WizardSilent`, that is under `/SILENT` and
`/VERYSILENT`).

The window is started with `-FromSetupExe`. In that route its folder is Setup's
private temporary folder, which is deleted when the exe ends, so the window never
offers or starts the text installer (the "Use the text installer instead" button
is hidden, `Open-TextInstaller` and `Start-TextInstallerFallback` do nothing) and
its messages about a missing or wrong file say "Download it again from the
project's page, or download the ZIP there and run install.bat" instead of "Extract
the whole ZIP" (`Get-DownloadAdvice`). Where the window itself tells a person to
try again it says "run Setup again" instead of "run install.bat again"
(`Get-RunAgainName`); a warning or error that comes from the engine and names
`install.bat` gets one more line, "(If you used the Setup file: run Setup again.)"
(`Add-SetupExeHint`). A document opened in Notepad (the Terms,
LICENSE, ...) is opened from a copy in a private folder of its own
(`htt-doc-<guid>` in the temp folder, `Get-DocumentPath`), so Notepad never holds
a file of the folder Setup deletes; the window deletes that copy on exit
(`Remove-DocumentCopies`) when it can.

Tamper check. `tools\build_setup.py` writes `files.sha256` (first line `#
htt-sha256 1`, then `<lowercase sha256>  <path/with/forward/slashes>` for each
file of `install-files.txt`). When that file is next to `install.ps1`,
`Test-DownloadIntact` runs at the start of an install and of `-Check`, before
anything is read or changed: every listed file must exist and match, every file
of `install-files.txt` must be listed, and no other entry may exist; otherwise
it stops with "This download is damaged or was changed. Download it again from
the project's page." and exit code 1 (plus an error marker in machine mode).
With no `files.sha256` (the ZIP route, a checkout) nothing is checked or
printed. The file is not in `install-files.txt` and is never copied into the
install folder. It guards against a damaged or partly replaced download; it is
not a signature (whoever can rewrite the exe can rewrite both).

Build: `python -B tools\build_setup.py --iscc <path to ISCC.exe>` (or set
`HTT_ISCC`; `--out` changes `dist\`). It copies exactly the files of
`install-files.txt` into a staging folder with a neutral name
(`C:\htt-build-<random>`, never under the user profile), writes `files.sha256`
and the generated include files there (`files.inc`, `place.inc`, `cleanup.inc`)
and gives every staged file the same modification time (2026-01-01 00:00:00 UTC;
the script has `TimeStampsInUTC=yes`), so the build machine's clock and time zone
are not recorded. Entries of `install-files.txt` that are rooted, have a drive
letter or a colon, contain `..`, a quote or a brace are refused (and by
`install.ps1`, `Test-ManifestEntry`). git is always called with
`--literal-pathspecs`, so `[`, `*` or `?` in a name is never a pattern. Then, BEFORE compiling, `staging_scan`
searches every staged file, `files.sha256`, the generated `.inc` files and the
`.iss` for the Windows account name, the profile folder's name, the computer name
and the domain name (a value shorter than 3 characters, equal to a generic word
such as "user", "admin" or "pc", or empty is skipped, and the build says which were
skipped; it never prints a value) and for `C:\Users\` followed by anything other
than exactly `you` (the name must be followed by a path separator, a quote, white
space or the end: `you.x`, `youth` and `you_1` are hits), as UTF-8, as the ANSI code page and as UTF-16LE, ignoring case; any hit
stops the build before anything is compiled. The compiled exe is LZMA-compressed,
so it is searched afterwards only as a second, clearly labelled "loader only"
check (the staging path and the same values). The build then removes the staging
folder and writes the exe and `SHA256SUMS.txt` to `dist\` (ignored by git). It
refuses to build when files of `install-files.txt` have uncommitted changes,
are untracked, or are not tracked by git unless `--allow-dirty` is given (and then
says so loudly); a listed file that git ignores is refused even then. Version,
name and publisher ("Hold to talk contributors") in the exe's version resource
come from `settings.py` and the script; there are no URLs or personal names in it.

Test pass-through: `/HTTARGS="<arguments>"` is appended after `-File
install-gui.ps1`, so a test can add `-AutoRun <scenario>` and the five test
overrides (`tests/test_setup_exe.py`; paths cannot contain quotes or spaces).
It is safe because the arguments are the script's own, never PowerShell
switches, and `install-gui.ps1` refuses `-AutoRun` (which ticks the terms box
itself) unless all five overrides point into the temp folder and a key under
`Software\HoldToTalkTest\`: it cannot be used for a silent real install.

`tests/test_installer_hardening.py` proves the link and file-handling rules by running
them in a temporary folder: a real junction (`mklink /J`) that the no-follow removal and
both delayed-cleanup scripts must not follow, a read-only folder in the tree, an 8.3
short-form target, `HttLfReader` compiled under Windows PowerShell 5.1, and a program
held open with `FileShare.Read` (as the Python path holds the installer) that still
starts while a write to it fails.

### The uninstaller window

`uninstall.bat` with no arguments opens `uninstall-gui.ps1`, in the same style
as the setup window (the palette, fonts and styles are copied into it, not shared).
It never removes anything itself: like the setup window it runs `uninstall.ps1`
as a hidden child process. `uninstall.ps1 -Check` changes nothing and prints one
`facts` marker (folders, version, whether there is data, the model cache, the step
titles); `uninstall.ps1 -Progress -DeleteUserData yes|no` removes without ever
reading input and prints `step`, `warning`, `error` and `done` markers
(`@@htt ` and one line of JSON, believed from standard output only). The answer
is required, never assumed; `-DeleteData` stays the switch of the text mode and is
refused with `-Progress`. In this mode the engine also removes `uninstall.bat` and
the program folder itself when `-BatDir` is that folder (the batch file has
finished: it only opened the window) and exits 0. The pages are: "Remove Hold to
talk?" (what will be removed and kept), the progress, and the result (removed, with
what is left on purpose; or a failure with "Copy details"). The box "Also delete my
settings, dictation history and logs (this cannot be undone)" (unticked) sits in a
row of its own under the scrolling part of the first page, never over it, with the
line "Your settings, history and logs are kept unless you tick this box." above it,
so the choice and what is kept are seen without scrolling. When data was deleted
but files the app did not make were left, the result says "These were not made by
the app, so they were not deleted: <names>." and "They are in:" with the folder. An
error shown before anything was removed (the check failed) marks step 1, Confirm,
as the failed step in the rail. Ticking
the box shows "Delete your settings and history for good?" (Delete / Keep; Esc is
Keep); the engine gets `-DeleteUserData yes` only for a ticked and confirmed box.
There is no Cancel or Close while files are being removed. Exit codes of the window:
0 removed, 1 failed, 3 closed before removing. For tests it takes `-SelfTest`,
`-Shots <folder>`, `-Engine <path>` (`tests/fixtures/fake_uninstall_engine.ps1`)
and `-AutoRun remove|remove-data|remove-keep|cancel`, with the same sandbox rule as
the setup window (five overrides, folders under the temp folder, key under
`Software\HoldToTalkTest\`); only `Set-DataBoxForTest` may tick the box by program, and
it refuses outside that sandbox.

`uninstall.bat` makes `htt-uninstall-<random>` in the temp folder (failing if
it exists), restricts it to this account with `icacls` (the same protection as
`Get-WorkDir`), copies `uninstall.ps1` and `htt-common.ps1` (and, when they are
there, `uninstall-gui.ps1` and `app.ico`) into it and runs them from there, so the
install folder can be deleted. With no arguments it runs
`uninstall-gui.ps1 -Launch` from that copy (the launcher starts the window without a
console, as for the setup window; the window deletes the copy when it ends, only a
folder named `htt-uninstall-<digits>` directly in the temp folder), and when the
window cannot be shown it prints one line and goes on with the text uninstaller; with
any argument it runs the text uninstaller. If the folder cannot be
made private, nothing runs. `uninstall.ps1` asks the app to quit, runs
`dictate.py --uninstall` (the Start with Windows entry) and then removes the
`Run` and `StartupApproved` values directly, removes the shortcuts and the
Installed apps key, the environment and the `window`, `window-*` and
`quit-probe` folders under `%LOCALAPPDATA%\HoldToTalk`, asks before touching
`data` (`Remove-AppDataFiles`, by name only), empties the program folder, and
exits 10 so `uninstall.bat` deletes itself (exit codes: 0 done, 10 done and
delete the batch file, 2 cancelled, 1 failed). It never touches the model cache,
pip's cache or Ollama; it prints where they are. It never follows a link out of
the tree it removes: a folder to remove that is itself a reparse point (a junction
or symbolic link: the environment, a `window*` folder, the program folder) is
refused with a warning and left alone (`Test-IsLink`), and a link inside it is
removed as the link, not followed (`Remove-TreeNoFollow` in `htt-common.ps1`, used
by `Remove-PathWithRetry`, because Windows PowerShell 5.1's `Remove-Item -Recurse`
can walk into a junction). The window's own temporary copy (`Remove-OwnTempCopy`) is removed by the same no-follow
walk (`Remove-OwnTree`, inlined because the window does not load `htt-common.ps1`)
and never when the copy is itself a link; when something still holds it, a hidden
PowerShell (`Get-DelayedCleanupScript`) gets the folder in an environment variable
(not through `cmd`) and removes only `htt-uninstall-<digits>` directly in the temp
folder, with the same checks as the Setup.exe's script (long names, a link target
refused, no recursive delete that could follow a link). `Remove-TreeNoFollow` clears
the ReadOnly attribute of ordinary folders and files before deleting them, so a
read-only folder cannot make the removal fail.

**Test overrides** (`htt-common.ps1`): `-InstallRoot`, `-EnvDir`,
`-StartMenuDir`, `-DesktopDir`, `-RegKey` (or `HTT_INSTALL_ROOT`,
`HTT_ENV_DIR`, `HTT_START_MENU_DIR`, `HTT_DESKTOP_DIR`, `HTT_REG_KEY`) must be
given all together: with only some, the installer stops with "Test overrides
must be given all together" before creating anything (`Get-HttLayout -Strict`).
They switch on test mode, in which the "quit" goes to a throw-away test
instance, so a test run can never stop the real app. `HTT_RUN_KEY` (a key under
`Software\HoldToTalkTest\` only) stands in for the Run key. `-NoDeps` (or
`HTT_SKIP_DEPS=1`) skips pip, `-SkipModel`, `-NoLaunch` and `-Yes` skip the
questions, `-PipArgs` (or `HTT_PIP_ARGS`) adds pip arguments such as
`--dry-run`, `HTT_NO_PAUSE` removes the pauses. `start.bat` runs
`install.ps1 -EnvOnly` (the same Python search and environment repair,
nothing else; honours `HTT_ENV_DIR`) and then starts `dictate.py --open` from
the current folder.

**Refreshing the lock files.** In the app's environment, update the packages
(`python -m pip install -U -r requirements.txt`, and `-r requirements-cuda.txt`),
try the app, then run `python tools\make_locks.py`. It reads the installed
versions and asks pypi.org for the hashes (a small listing per package; only
package names and versions are sent). `python tools\make_locks.py --check`
says whether a lock still agrees with its range file, and the test suite fails
if it does not.

## Tests and keeping them away from real data

The tests are for Windows only: they use Win32, and `tests/test_install.py`
runs the real `install.bat` and `uninstall.bat`. Run from the project folder,
in the environment made by `start.bat` or from `requirements.txt`:

```
python -B -m unittest discover tests
```

On the development PC that ran about 850 tests in about seven minutes; the
installer tests are most of that. `-B` keeps `__pycache__` out of the folder.
To run one file or one test, use discovery with a pattern and `-k`:

```
python -B -m unittest discover tests -p "test_paste.py"
python -B -m unittest discover tests -p "test_install.py" -k UninstallTempFolder
```

(`python -m unittest tests.test_paste` does not work: the test files import
`fakes` from the tests folder.) The tests need no GPU, microphone, Ollama,
internet or desktop interaction: the speech model, ctranslate2, the
clipboard, the keyboard, the chimes, the microphone and Win32 windows are
replaced by fakes (`tests/fakes.py`), Ollama by a small fake HTTP server on a
loopback port (`tests/test_cleanup.py`). The installer tests run `install.bat`
into temporary folders with a throwaway registry key under `HKCU\Software`,
`-NoDeps`, `-SkipModel` and `-NoLaunch`; they need Windows PowerShell and a
64-bit Python 3.10 to 3.13 with tkinter. `tests/test_install_progress.py`
covers what the installer reports to the setup window (`-Check`, `-Progress`,
the refusals around `-AcceptedTerms`). `tests/test_install_gui.py` drives the
window itself with `-AutoRun`, off-screen and without taking the focus,
against the fake engine and once against the real one; it is skipped where
WPF is missing. `.github/workflows/tests.yml` runs the
suite on `windows-latest` with Python 3.12 and only `requirements.txt`.

Tests stay away from real data in four ways. (1) `tests/fakes.py` and
`tests/test_00_home.py` point `HOLDTOTALK_HOME` at a fresh temporary folder
(`htt-test-home-...`) before any project module is imported, so
`settings.CONFIG_PATH`, `HISTORY_PATH` and `LOG_PATH` are inside it;
`test_00_home` fails if they are not. (2) `dictate.py --config/--history/--log`
makes a test instance (`dictate.data_paths`): files you did not name go next
to the one you did, nothing is migrated, it has its own mutex and events
(`dictate.instance_suffix`), its own window profile and no Start with Windows
entry. (3) `App(dry_run=True)` never pastes, touches the clipboard or plays a
chime. (4) The installer tests strip every `HTT_*` variable and
`HOLDTOTALK_HOME` from the environment, use temporary folders and a throwaway
key, and refuse half-given overrides.

## The Terms of Use

`TERMS.md` is the text; its first line is `Version X - date` and `X` must equal
`settings.TERMS_VERSION` (`tests/test_terms.py`). The `## Summary` section up
to the first `---` line is shown by the installer and the window as the
summary. To change the terms: edit `TERMS.md`, raise `TERMS_VERSION` and the
version line together, and add a CHANGELOG line; every user is then asked
again (installer on its next run; the app at its next start, until the record
for the new version exists). The window's terms screen (`termsScreen` in
`ui/app.js`) fetches the text with `GET /api/terms` and draws it with
`textContent` only; `dictate.run_app` opens the window on it when
`app.terms_pending`, and shows a message box when there is no console
(`dictate.terms_pending_message`). The tray's grey icon and status text come
from `TrayHandle._terms_pending`. A new test or helper that builds an `App`
must have a temporary data folder with a current record: `tests/fakes.py`
makes one in the shared test home, and `accept_terms_in(folder)` makes one
anywhere else.

## Release checklist

The only evidence of what a user accepted is the released version itself, so
for every release:

1. Raise `APP_VERSION` (and `TERMS_VERSION` if `TERMS.md` changed, with the
   version line and date) and update CHANGELOG.md.
2. Run the whole test suite and the release scan
   (`release-safety` skill, `scan_release.py`).
3. Tag the release in version control. The tag preserves that release's
   `TERMS.md`, installer (`install.ps1`) and terms screen (`ui/`), which is
   what a later question of "what did this person agree to" is answered from.
4. Keep, with the release, a screenshot of the installer prompt and of the
   terms screen as they were in that release.
5. Check that the model pins in `stt.MODEL_REVISIONS` are the ones tested.

## How to add...

**A pipeline stage.** Example: a stage that fixes a lone lower-case "i".

1. Write the function. Use `(text, cfg)`, or `(text, cfg, language=None)` if it
   depends on the language (`pipeline._accepts_language` reads the signature;
   the pipeline passes `language=` only when it is known). It must return a
   `str`:

   ```python
   def fix_lone_i(text, cfg):
       return re.sub(r"\bi\b", "I", text)
   ```

2. Insert it into `pipeline.STAGES` where it belongs (before `textrules.apply`
   if the user's snippets and dictionary must still have the last word):
   `STAGES = [normalize, fix_lone_i, cleanup.clean, textrules.apply]`. A stage
   placed after `cleanup.clean` is also re-run by
   `App._console_safe_final`, so it must be safe to run twice.
3. Read options from `cfg` on each call, never at import time. Log durations
   and counts, never the text.
4. Give it a display name in `STAGE_NAMES` in `ui/app.js`; otherwise the
   History trace shows the raw name `module.function`.
5. If it lives in a new module, add the file to `install-files.txt`
   (`tests/test_install.py` fails otherwise).
6. Add a test in `tests/test_pipeline.py`.

**A setting with a window control.** Example: `capitalize_first`, a yes/no
setting that default-on keeps today's behaviour.

1. Put the default in `settings.DEFAULTS` (`"capitalize_first": True`), or in
   your module's `DEFAULTS` inside a section already named in
   `settings.SECTIONS` (as `sounds.DEFAULTS` is). `settings.validate`
   type-checks it from the default's type; add a rule in `settings._special`
   if it needs more.
2. Read it where it is used, on each call, through a small function
   (`sounds.settings_of` is the pattern):
   `if cfg.get("capitalize_first", True) and text[:1].islower(): ...` in
   `pipeline.normalize`.
3. If it cannot apply live (a model), add the key to `app.RESTART_KEYS`.
4. For the window: add the key to the tuple of yes/no keys in
   `api.Server._settings_view` (that is where the value is read for the
   Settings page; `r_config` just returns it) and to the matching tuple in
   `api.Server.p_settings` (a hard-coded `if`/`elif` chain that ends with
   "Unknown setting", so a new key needs its own branch; yes/no keys share
   one). For other types write a branch that checks the type and sets
   `changes[...]`, or `changes.setdefault("<section>", {})[...]`.
   `settings.validate_change` only covers hold-to-talk key, pause shortcut,
   language and sounds. Then add the control in `ui/app.js`, in
   `views.settings.config`: `settingRow(title, help, toggle('capitalize_first',
   'Capitalise the first letter'))` in the right group.
5. Tests: `tests/test_settings.py` (default, validation), the module's own
   test, and `tests/test_api.py` for the route.
6. Documents: add a row to the README settings table. That table is kept by
   hand; nothing tests it.

**A hotkey (key) name.** Key names live in `keys.py`. Add a tuple to
`keys._NAMED`: `(canonical name, virtual-key code, (aliases...))`, lower case,
single spaces, no `+`. Example: `("sleep", 0x5F, ("sleep key",))`.
`KEY_NAMES` is built from it, `keys.parse_hotkey` accepts every alias and a
captured key is saved under the canonical name (`keys.canonical_hotkey`);
`PLAIN_KEYS` and `CAPTURE_VKS` follow automatically, so capture in the window
works. Modifiers and their left and right variants are in `KEY_NAMES` and
`MODIFIER_FAMILIES`; mouse buttons are `MOUSE_NAMES` (with `MOUSE_CANONICAL`
and `MOUSE_VKS`); the primary buttons are refused through `FORBIDDEN_NAMES`.
If releasing the key alone has a side effect in Windows (as Win and Alt do),
add its code to `keys.RELEASE_SENSITIVE`. Add cases to `tests/test_keys.py`.

**A new file the app writes into the data folder.** Add its name to
`Remove-AppDataFiles` in `htt-common.ps1` and to the lists in
`tests/test_install.py::test_6_deleting_data_removes_only_what_the_app_made`,
or the uninstaller will leave it behind. Add it to the README's uninstall list.
