# Hold to talk

Hold to talk is a free dictation program for Windows. You **hold a key,
speak, and let go**, and your words are pasted where your cursor is, in most
Windows programs.

The speech is turned into text on your own PC by Whisper, a speech-recognition
model. Your voice and your text are never sent anywhere by this app; the
connections that do exist (downloads, and the browser that shows the app's
window) are listed under "Before you install".

An optional feature, **AI cleanup**, tidies up "um"s and false starts before
the text is pasted. It also runs on your PC.

## Quick start

1. You need Windows 11 (Windows 10 has not been tested).
2. On this project's GitHub page, open **Releases** and download
   **`HoldToTalk-Setup-0.9.0.exe`** (the number is the version).
3. Double-click it. A setup window shows the Terms of Use, what will be
   installed and where, and the progress. If Python is not on your PC yet,
   the window offers to install it for you.
4. Start **Hold to talk** from the Start menu, hold **Ctrl + Windows**, speak,
   and let go.

The Setup file is not digitally signed, so Windows may show a blue "Windows
protected your PC" screen first (see "Troubleshooting"). You can instead
download the project as a ZIP and run `install.bat`, which opens the same
setup window from files you can read first.

The full walk-through is in "Installing, step by step". Read "Before you
install" first: it has the Terms of Use and what the app connects to.

Hold to talk is free to use and to change for yourself. It is not
open-source software: please do not pass it on or republish it; send people
the link to this page instead. See "Licence". Problems? See
"Troubleshooting" and "Reporting a bug or asking a question".

## What it is, and what it is not

- It **is** a small program that sits in the tray (the row of little icons
  next to the clock; Windows calls it the notification area). Click its icon
  to open **the window**, where you set the hold-to-talk key, see what you
  dictated and teach the app your words.
- It is **not** a cloud service. There is no account, no sign-in, no
  analytics, and no check for updates.
- It is **not** an app-store app. You install it from files: double-clicking
  `install.bat` opens a setup window, and the scripts behind it are plain
  text that you can read first. Nothing is digitally signed.
- It is **not** tested everywhere. It was developed and measured on one
  Windows 11 PC with an NVIDIA graphics card. Windows 10 and Windows on ARM
  have not been tested by this project. Recognition accuracy has not been
  measured.

## Before you install

### Terms of Use

Hold to talk comes with Terms of Use, in the file `TERMS.md`. They say what
you can expect from the app, what you are responsible for, and the limits on
what the people who make it can be held to. **You are asked to accept them
explicitly, in the installer or in the app; the app does not dictate
until you have.** They sit next to, and do not replace, the licence in
`LICENSE`, which says what you may do with the program and its code: use it
free of charge and change it for your own use, but not pass it on, publish it
or sell it.

You are asked, not assumed. The setup window shows the whole text on its own
page, with links that open it (and the licence and notices) in Notepad. It
goes on only when you tick **I have read and agree to the Terms of Use** and
then choose **I agree**; the box starts unticked, and Cancel closes the window
with nothing changed. The text installer (`install.bat -Console`, see
"The text installer") shows a short summary and the place of the full text,
lets you read all of it in the window (type `READ`) or in Notepad (type
`NOTEPAD`), and only continues when you type the words `I agree`; anything
else stops it with nothing changed. The setup window, the text prompt and the
app's **I agree to the Terms of Use** button all say what you are agreeing to,
including the sections on no warranty, limits of liability, assumption of risk,
indemnity and disputes (sections 9 to 14). The unattended switch (`-Yes`)
installs without questions but does **not** accept the terms on anyone's
behalf: it records no acceptance, and the app asks the person who uses it. If
you run the app from source with `start.bat`, after an unattended install, or
when the terms change in a newer version, the app asks: its hold-to-talk key
does nothing, the tray icon says why, and the window opens on the terms with
**I agree to the Terms of Use** and **Quit** buttons. Settings > About shows
the version you accepted and has a **View terms** button.

Your acceptance is recorded in one small file in your data folder,
`terms-accepted.json`: the version of the terms, the date and time (UTC), and
whether it was given in the installer or in the app. It holds nothing about you, stays on your PC and is never sent
anywhere. Uninstalling and choosing to delete your data removes it.

### No warranty

Hold to talk is free to use and is provided as it is, with no
warranty of any kind. As far as the law allows, the people who made it are not
liable for damage or loss from using it. Some rights cannot be excluded.
`TERMS.md` (sections 9 to 14) and `LICENSE` have the exact terms, and those
are what apply; this section is only a plain-words summary. **This summary
and this README are not legal advice.**

- **It makes mistakes.** Speech recognition and AI cleanup get words wrong.
  Read what was pasted before you send it or rely on it. Do not use it where
  a wrong word could cause harm, such as medical, legal, financial or
  safety-related uses, or any other critical use.
- **It pastes into whatever window is in front.** You are responsible for
  where your cursor is when you let go of the hold-to-talk key.
- **It uses other people's software and models,** each under its own licence.
  See `THIRD-PARTY-NOTICES.md`.
- **The rules that apply to you are yours to follow** where you use it, for
  example workplace rules or rules about recording people.

### What you need

- **Windows 11, 64-bit.** (Windows 10 is untested.)
- **64-bit Python 3.12** from python.org. The installer does not include it;
  Step 0 below shows how to get it. Python 3.10, 3.11 and 3.13 are accepted
  by the installer but have not been tested.
- **Microsoft Edge or Google Chrome** to show the window. Edge comes with
  Windows. If neither is found, the window opens in your default browser as
  an ordinary tab.
- **A microphone**, and Windows must let desktop programs use it: Settings >
  Privacy & security > Microphone > "Let desktop apps access your
  microphone" must be on.
- **An internet connection while installing** (see the download list).
- **Optional: an NVIDIA graphics card** (a "GPU") for much faster speech
  recognition. Not sure if you have one? Press Ctrl+Shift+Esc to open Task
  Manager, click **Performance**, and look for a "GPU" entry whose name
  starts with NVIDIA. Without one, the app uses the processor instead.

### What it downloads, and how big

| What | Size | When |
| --- | --- | --- |
| Python libraries (the app's helper code, from PyPI, the Python Package Index) | "a few hundred MB" (the installer's own estimate) | install |
| Speech model for the processor, "small" | about 0.5 GB | install (you are asked) or first start |
| Speech model for an NVIDIA card, "large-v3-turbo" | about 1.5 GB | install (you are asked) or first start |
| NVIDIA runtime (NVIDIA's add-on libraries that let the card do the work), only if an NVIDIA card is found and you say yes when asked | about 1.3 GB to download, about 2 GB on disk | install |
| Ollama (the program that runs AI cleanup) and its model `gemma4:e2b-it-qat` | model about 4.3 GB; Ollama's own size is not recorded here | only if you choose AI cleanup, and you install both yourself |

Adding these up gives roughly 1 GB of disk space for a processor-only PC
without AI cleanup, and roughly 4 GB with an NVIDIA card. AI cleanup adds
about 4.3 GB and Ollama itself. These totals are estimates added up from the
figures above; the project has not measured a finished install.

### What it changes on your PC

Everything is for your own Windows account. It needs no administrator rights
and writes nothing to Program Files or to the machine-wide part of the
registry.

1. **Program files** in `%LOCALAPPDATA%\Programs\HoldToTalk`.
   (`%LOCALAPPDATA%` is a Windows shortcut for the folder `AppData\Local`
   inside your user folder. Paste `%LOCALAPPDATA%` into the address bar of
   File Explorer to open it.)
2. **A private Python environment**, a copy of the libraries the app needs, in
   `%LOCALAPPDATA%\HoldToTalk\venv`. It is separate from any other Python on
   your PC.
3. **A shortcut named "Hold to talk" in the Start menu** and **another on the
   Desktop**.
4. **An entry in Settings > Apps > Installed apps**, so you can uninstall it
   there. It is stored in your own part of the registry at
   `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\HoldToTalk`.
5. **Your data folder**, `%LOCALAPPDATA%\HoldToTalk\data`. The installer
   creates it and puts one small file in it, `terms-accepted.json`, the
   record of accepting the Terms of Use (see above). The first time the app
   runs it adds your settings, your dictation history and the log. Updating
   and reinstalling keep all of it.
6. **The window's browser profile** in `%LOCALAPPDATA%\HoldToTalk\window`,
   created the first time the window opens. It is a separate browser profile,
   so it never mixes with your own browsing.
7. **The speech model** in the Hugging Face cache,
   `%USERPROFILE%\.cache\huggingface\hub`. Hugging Face is a website that
   hosts AI models; the cache is a folder where its downloads are kept.
   (`%USERPROFILE%` is your user folder.)
8. **pip's download cache**, `%LOCALAPPDATA%\pip\Cache`. pip is Python's
   package installer; it keeps copies of what it downloaded.
9. **Start with Windows is off.** Nothing starts when you sign in unless you
   switch on "Start with Windows" yourself (in the window under Settings >
   System, or in the tray menu). Switching it on adds one value named
   `HoldToTalk` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
10. While installing, a temporary folder named `htt-` followed by random
    characters is made in your temp folder and deleted at the end.

The installer starts PowerShell (the Windows scripting program) with
`-ExecutionPolicy Bypass` for its own command only. Windows normally refuses
to run unsigned scripts; the option lets this one installer script run. It
changes no Windows setting.

### What leaves your computer, and when

1. **Installing:** the libraries come from PyPI (pypi.org and its file
   servers), and the speech model comes from Hugging Face. Those sites see
   your internet address, as any website you visit does. You may also have
   downloaded Python from python.org.
2. **First start:** if you skipped the model download during install, or the
   graphics card cannot be used and the processor model is needed, the app
   downloads the missing speech model then. The app switches off Hugging
   Face's automatic usage reporting.
3. **Only if you use AI cleanup:** the app talks to Ollama on your own PC.
   See "AI cleanup" for exactly what is sent. That is not a connection to the
   internet. The address is `127.0.0.1`, your PC's own internal address, and
   the app always refuses any other address (there is no setting that allows
   one). AI cleanup only ever talks to Ollama on this computer. The app checks
   that the program answering on that port belongs to your Windows account or
   to Windows itself; if you switch on the trust setting, your dictations go to
   whatever program answers there (see "AI cleanup"). Ollama's own model
   download (`ollama pull`) is Ollama's business, not this app's.
4. **The window is a web page served by the app on your own PC** (also
   `127.0.0.1`) and shown by **Edge or Chrome**. **The browser makes its own
   connections to Microsoft or Google**, for example for its own updates and
   safety checks. This app does not control or inspect them. It starts the
   browser in its own profile and switches off the features it can (sync,
   extensions, background downloads, crash reports), but it cannot promise
   the browser sends nothing. Your dictated text is not sent to any website
   by the app: the window talks only to the app on your PC. **If neither Edge
   nor Chrome is installed, the window opens as a tab in your usual browser**
   instead. The app logs one line saying so, and the window shows a small
   notice that it is running in your ordinary browser. In that case your
   browser's own extensions and settings apply to the page, and the app
   cannot switch off any of them.

Your voice is recorded into memory, and the app never writes your voice to a
file (with one short exception, "Try that recording again", described under
Known limitations). There is no account, no analytics and no update check.

## Step 0: get Python

You only need this step if you want to install Python yourself. The setup
window can do it for you (**Install Python for me**), whichever way you
started it.

Skip this step if you already have 64-bit Python 3.10 to 3.13. To check:

1. Press the Windows key, type `cmd` and press Enter. A black window, the
   Command Prompt, opens.
2. Type `py -3.12 --version` and press Enter.
   - You will see `Python 3.12.` followed by a number: Python 3.12 is
     installed. (Try `py -3.13`, `py -3.11` or `py -3.10` if you have those.)
   - You will see `No suitable Python runtime found` or a similar message:
     install Python as below.
3. To check that it is 64-bit, type
   `py -3.12 -c "import struct; print(struct.calcsize('P')*8)"` and press
   Enter. You should see `64`. If you see `32`, install the 64-bit version.

You do not have to do these checks: the installer checks again. If it finds
no suitable Python, the setup window says so on its "What will be installed"
page. It offers **Install Python for me** (it downloads the official Python
3.12.10 installer from python.org, about 27 MB, checks its checksum and its
Python Software Foundation signature, and installs Python for your Windows
account only, with no administrator rights; it does not add python.exe to
your PATH, though Python's "py" launcher may add its own folder;
nothing is downloaded until you click, and Python comes under its own licence,
the PSF License), **Open python.org** and **Check again**, so you can get
Python and carry on without starting over.

To install Python:

1. Go to https://www.python.org/downloads/windows/ and click
   **Windows installer (64-bit)** for Python 3.12. Do not use the Microsoft
   Store version; the installer refuses it.
2. Run the downloaded file. Leave the options as they are; in particular keep
   "tcl/tk and IDLE" and "py launcher" ticked (they are by default; the app
   needs Tcl/Tk). You do not need to tick "Add python.exe to PATH".
3. When it says the setup was successful, click Close.

## Installing, step by step

There are two ways to get the same setup window:

- **The Setup file (easiest).** On this project's GitHub page, open
  **Releases**, download **`HoldToTalk-Setup-0.9.0.exe`** and double-click
  it. No ZIP, no black window, and Step 0 is not needed: if Python is
  missing, the window offers **Install Python for me**. The Setup file only
  unpacks the same files to a temporary folder and opens the setup window;
  it checks every file against a list of fingerprints first and stops if one
  was damaged or changed. Then carry on at step 5 below. Next to it on the
  Releases page is `SHA256SUMS.txt`, with the fingerprint of the Setup file
  itself, for people who want to check their download.
- **The ZIP and `install.bat`**, for people who want to read the scripts
  before running anything. That is steps 1 to 4:

1. **Download the program.** Go to this project's GitHub page, click the
   green **Code** button, then **Download ZIP**. (If you use
   Git, a developer tool for copying projects, you can `git clone` it instead.)
2. **Unblock the ZIP.** Right-click the downloaded ZIP file > **Properties**,
   tick **Unblock** at the bottom if it is there, click OK. (Windows marks
   downloaded files; unblocking stops it warning about every file inside.)
3. **Extract it.** Right-click the ZIP > **Extract All** > Extract. A normal
   folder opens.
4. **Run the installer.** Double-click **`install.bat`** in that folder. A
   small black window flashes up with "Opening the setup window..." and
   closes again; then the **Hold to talk Setup** window opens. It follows
   Windows' light or dark setting and has five pages, listed down its left
   side. Nothing on your PC is changed until you choose **Install** on the
   third page.
   If Windows shows a blue "Windows protected your PC" screen, that is
   SmartScreen reacting to an unsigned download from an unknown publisher. It
   is a Windows prompt, not a message from this app. You can read
   `install.bat`, `install-gui.ps1` and `install.ps1` in Notepad first; they
   are plain text.
5. **Welcome.** What the app is and its version. If an older version is
   already installed, the window is an updater instead: it is named
   **Update**, this page says it updates that version and does not install a
   second copy, your settings and history are kept, and the pages below say
   "Update" where this list says "Install". Choose **Next**.
6. **Terms of Use.** The whole text, in a box you can scroll, with links
   under it that open the terms, the licence, the third-party notices and the
   security note in Notepad. Tick **I have read and agree to the Terms of Use
   (version 1.0)**, then choose **I agree**. The box starts unticked, and the
   window does not go on without it. **Cancel** closes the window with nothing
   changed.
7. **What will be installed** (called **Choices** in the list on the left). A
   list of everything, where it goes and how big it is, with the free space
   on your disk underneath:
   - **Program files:** the app itself, in
     `%LOCALAPPDATA%\Programs\HoldToTalk`.
   - **Python environment and libraries:** a private copy of Python for this
     app, in `%LOCALAPPDATA%\HoldToTalk\venv`.
   - **NVIDIA runtime** (shown only on a PC with an NVIDIA card, ticked to
     begin with): NVIDIA's own software, about 1.3 GB to download and about
     2 GB on disk. Installing it means accepting NVIDIA's licence for those
     files. Untick it and the app is installed without it and recognises
     speech on the processor, which is slower; run the installer again later
     to add it. If it is already installed, the page says so.
   - **Speech model:** **Download the speech model now** is ticked to begin
     with. Untick it and nothing is lost, but the first start then downloads
     the model while the tray icon stays amber ("Getting ready") for many
     minutes with nothing else to see.
   - **Your data:** where your settings, history and logs are kept
     (`%LOCALAPPDATA%\HoldToTalk\data`). An update leaves them as they are.
   - **Shortcuts:** a Start menu shortcut, always, and a **Desktop shortcut**
     that you can untick.

   A folder that already exists (for example when you update) has an **Open
   folder** link next to it. Scroll down for the totals: how much will be
   downloaded, how much disk space is needed, and how much is free. If there
   may not be enough free space, a warning at the top of the page says so.
   Choose **Install**.
8. **Installing.** A progress bar and the seven steps, each ticked as it
   finishes: looking for Python, checking the files, making sure the app is
   not running, copying the program files, the Python environment and
   libraries (the slow one: a few minutes, longer with the NVIDIA runtime),
   the speech model, and the shortcuts and the Installed apps entry. **Show
   details** opens the full text of what the installer is doing. You can keep
   using your PC. **Cancel** asks first ("Stop installing?"), then stops the
   installer and shows **"Installation stopped"**: what was already done
   stays, nothing is broken, and **Try again** carries on where it stopped.
9. **Finish.** **"Hold to talk is installed"**, the folder it is in, anything
   that did not go as planned (for example the NVIDIA runtime failing to
   download, in which case the app uses the processor), and how to open the
   app. Leave **Start Hold to talk now** (next to the Close button) ticked
   and choose **Close** to start it.
10. **If something goes wrong,** the last page says what happened instead.
    Nothing is half-broken: fix the problem (the message says what), then
    choose **Try again**, which is the same as running Setup (or
    `install.bat`) again;
    it carries on where it stopped. **Copy details**
    puts the installer's text on the clipboard, for a bug report; read it
    before you paste it anywhere, because it contains folder paths with your
    Windows account name. See Troubleshooting.

The installer does not install Ollama.

### The text installer

The same installer also runs as text in a Command Prompt, for people who
prefer that, for scripts, and as a fallback: if the setup window cannot be
shown, `install.bat` says why in one line and carries on in text by itself.

To choose it, open a Command Prompt in the extracted folder (see
Troubleshooting for how) and run `install.bat -Console`. Running `install.bat`
with any argument uses the text installer; only a plain double-click opens the
window. It prints a green title line, "Hold to talk 0.9.0 - installer", and
the places it will use, then the summary of the Terms of Use and where the
full text is, and asks you to type **`I agree`** (any capitals) to continue.
Type `READ` first to read the full terms in the window, or `NOTEPAD` to open
them in Notepad. Anything else stops it with nothing changed. It then prints
the same seven steps, and asks the same questions as it reaches them:
**"Install the NVIDIA runtime? [Y/n]"**, **"Download the speech model now?
[Y/n]"** and, after the green line **"Done. Hold to talk 0.9.0 is
installed."**, **"Start Hold to talk now? [Y/n]"**. Press Enter for yes, or
type `n`. If something goes wrong, a red line says "The installer stopped:"
with the reason.

`install.bat -Yes` installs without asking anything (the NVIDIA runtime
included, with the same notice printed). It does not accept the Terms of Use
for anyone: the app asks the person who uses it.

## First run

If you ran the installer and chose (or, in the text installer, typed)
**I agree**, the Terms of Use are already accepted. If you started the app any other way (for example `start.bat`), if
the installer ran with `-Yes`, or if the terms changed since you accepted, the
window opens on the terms first:
read them, then choose **I agree** (dictation starts at once, no restart) or
**Quit**. Until then the hold-to-talk key does nothing.

1. Start it from the Start menu (press the Windows key, type **Hold to
   talk**, press Enter), or from the Desktop shortcut. A tray icon appears,
   and the window opens in a moment.
2. The icon is **amber** while the speech model loads. About 4 seconds on
   the development PC once the model had been downloaded; longer on slower
   PCs, and much longer if the model still has to download. Windows may show
   a small "Ready. Hold ... and speak." message, naming your key, when it is
   done.
3. When the icon turns **teal**, it is ready.
4. Click into any text box, hold **Ctrl + Windows**, say a sentence, let go.
   You will see a small on-screen indicator (see below) while you hold, and
   then the text appears at the cursor.

| Tray icon | Meaning |
| --- | --- |
| Amber | Getting ready (loading the speech model). Dictation is off. |
| Teal | Ready. Hold the key and speak. |
| Coral | Listening. |
| Grey with a slash | Paused, the Terms of Use are not accepted yet, or it cannot dictate right now (the tray menu and the window say which). |

Cannot see the icon? Click the little arrow (^) next to the clock; drag the
icon out to keep it visible. Right-click the icon for the tray menu: Open,
the status, Pause or Resume, Microphone, AI cleanup, Start with Windows,
Quit.

If nothing is heard: Settings > Privacy & security > Microphone must allow
"Let desktop apps access your microphone".

## Using it

- **The hold-to-talk key.** The default is **Ctrl + Windows**. Hold it
  while you speak, let go to paste. The words are pasted (through the
  clipboard) into the program in front, and your clipboard is put back as it
  was. Change the key in the window, Settings > Hold-to-talk key. You can use
  a key combination, or the **middle mouse button**, or the **Back or Forward
  side button**. The left and right mouse buttons cannot be used.
  - A **keyboard** hold-to-talk key is not blocked: a key such as F9 still
    reaches the program in front. A combination that does nothing on its own,
    like the default, avoids that.
  - If you press another key within the first half second, the app assumes
    you meant a shortcut and throws the recording away.
  - A press shorter than about a third of a second (setting `min_seconds`,
    default 0.35) is ignored.
  - A **mouse button** used as the hold-to-talk key stops doing its normal job
    (a Back button stops going back) while the app is ready. Pause the app,
    or choose another key, to get it back.
- **The on-screen indicator.** While you hold the key, a small rounded
  indicator with moving bars appears at the bottom centre of the screen on
  the monitor where your mouse pointer is. Moving dots mean it is writing
  your words down. It is click-through and never takes the keyboard focus.
  Colours: Settings > Appearance.
- **Pause shortcut.** Settings > System > Pause shortcut lets you pick a key
  that turns dictation off and on (none by default), for games or when you
  type a lot. It must not be part of the hold-to-talk key. The indicator
  flashes "paused" or "resumed".
- **Chimes.** A short rising chime when it starts listening, a falling one
  when it stops. Switch them off or change the volume under Settings > Sounds.
  If your microphone picks up the start chime from your speakers, set
  `sounds.mute_start` (see "Settings that are only in the settings file"):
  it silences the first quarter of a second of each recording, which can clip
  the first syllable if you start speaking the moment you press. It is **off**
  by default.
- **Dictionary versus Vocabulary.** Both help with names and special words,
  in different ways:
  - **Dictionary** is a find-and-replace applied to the finished text.
    Entry: when it hears `zor vex`, write `Zorvex`. Before: "Ask zor vex
    about it." After: "Ask Zorvex about it." Matching ignores capital
    letters and applies to whole words only. It runs last (after AI cleanup),
    so it has the final word.
  - **Vocabulary** is a hint given to the speech model before it listens.
    Entry: `Zorvex`. It makes the model more likely to spell the word that
    way in the first place. Illustration: without the hint the model might
    write "Zor Vex"; with it, "Zorvex". It is a hint, not a guarantee. For a
    word that must always be written one way, add a Dictionary entry too.
    The model can only read a limited number of hints (about 200 tokens);
    the Vocabulary page tells you if the list is full and which words are
    ignored. Dictionary spellings count as hints too.
- **Snippets.** Say a short phrase and get a longer text: say "my
  signature" and your whole sign-off is pasted. The Snippets page has a
  "Fires when" choice: the phrase said as its own sentence (the default), the
  phrase being the whole dictation, or the phrase anywhere in a sentence.
- **Spoken formatting** (off by default, Settings > Text): saying "new line"
  or "new paragraph" as a phrase of its own starts a new line or paragraph.
- **History, correcting and learning.** The History page lists what you
  dictated, kept on this PC. Click a dictation to see the steps (what the
  speech model heard, what each step changed, what was pasted). Fix a
  mistake there and the app offers **"Learn from this fix?"**: it suggests
  Dictionary or Vocabulary entries, and you can undo each. "Learn clear-cut
  fixes automatically" (Settings > History and learning) accepts the
  clear-cut spelling fixes without asking. Copy puts a dictation on the
  clipboard.
- **"Try that recording again".** If a recording could not be turned into
  text (for example the speech model was not loaded), the app keeps that one
  recording in memory for up to 5 minutes. The Home page then offers **Try
  that recording again**. The app never writes it to a file, and it is
  dropped when the app quits.
- **Language.** By default the speech model detects the language. Choosing
  your language in Settings > Microphone and language is a little faster and
  more accurate (the window says). After 10 dictations in a row detected as
  one language, Home offers to set it for you. Removing "um" and "uh" only
  applies to English.
- **Terminals.** If AI cleanup adds line breaks to your text and the window in
  front is a terminal (Windows console, Windows Terminal, ConEmu, mintty,
  PuTTY, Alacritty, WezTerm), the app pastes the text from before the
  rewrite instead, because a line break pasted into a terminal can run a
  command. The History steps show "console_safety". Line breaks from your
  own snippets and spoken "new line" are not stopped.
- **If it types instead of pasting.** If your clipboard holds something the
  app cannot put back exactly (for example a very large copy), it types the
  text instead of using the clipboard, and does not touch your clipboard.

## Settings

Most settings are in the window (Settings page, and the Dictionary,
Vocabulary and Snippets pages). Changes apply to your next dictation. Exceptions
that need a restart of the app are marked below.

### In the window

| Where | Setting | Default |
| --- | --- | --- |
| Hold-to-talk key | The key (`hotkey`) | Ctrl + Windows (`ctrl+windows`) |
| Microphone and language | Microphone (`input_device`), a level test, language (`language`) | Windows default microphone; detect language |
| AI cleanup | Use AI cleanup (`cleanup.enabled`), model (`cleanup.model`), a trust switch (`cleanup.trust_listener`, shown only when needed) | on; `gemma4:e2b-it-qat`; off |
| Text | Remove filler words (`remove_fillers`), spoken formatting (`textrules.formatting_commands`), add a space after each dictation (`trailing_space`), restore the clipboard after pasting (`restore_clipboard`) | on; off; on; on |
| History and learning | Save history (`save_history`), learn clear-cut fixes automatically (`learning.auto_apply`) | on; off |
| Sounds | Chimes (`sounds.enabled`), volume (`sounds.volume`) | on; 0.5 (half) |
| Appearance | On-screen indicator colours (`overlay.color`, `overlay.background`) | `#f4f4f5` on `#17181c` |
| System | Use less graphics memory (`compute_type`, needs a restart), Start with Windows, Pause, Pause shortcut (`pause_hotkey`) | off; off; not paused; none |
| Snippets page | When snippets fire (`textrules.snippet_mode`: `sentence`, `whole`, `anywhere`) | `sentence` |
| Dictionary, Vocabulary, Snippets pages | Your lists (`dictionary`, `vocabulary`, `snippets`) | empty |

"Save history" off stops the History list being written to disk, but the
window still keeps your last 50 dictations in memory until you quit.
"Clear history" on the History page deletes every dictation and also any
damaged-database copies the app set aside earlier.

"Use less graphics memory" loads the speech model in a smaller form.
Measured on the development PC's card, it needs about 1 GB less graphics
memory at about the same speed. The window says it costs a very small loss
of accuracy; the project has not measured accuracy.

### Settings that are only in the settings file

| Setting | What it does | Default |
| --- | --- | --- |
| `model` | Speech model used on an NVIDIA card (needs a restart) | `large-v3-turbo` |
| `cpu_model` | Speech model used on the processor (needs a restart) | `small` |
| `device` | `auto`, `cuda` (the NVIDIA card) or `cpu` (the processor) (needs a restart) | `auto` |
| `compute_type` | How the model is loaded: `auto`, `float16`, `int8_float16`, `int8`, `bfloat16`, `int8_bfloat16`, `float32`, `int8_float32` or `int16`. A type the device cannot do is replaced by `auto` (needs a restart) | `auto` |
| `min_seconds` | Presses shorter than this many seconds are ignored (never below 0.1) | `0.35` |
| `sounds.mute_start` | Silence the first quarter of a second of each recording so the start chime is not recorded | `false` |
| `cleanup.timeout` | Seconds AI cleanup may take before the plain text is pasted | `3.0` |
| `cleanup.min_words` | Dictations with fewer words get no AI cleanup | `4` |
| `cleanup.max_words` | Dictations with more words get no AI cleanup | `400` |
| `cleanup.keep_alive` | How long Ollama keeps the model in memory when idle | `"2h"` |
| `cleanup.url` | Where Ollama answers. It must be on this computer (`127.0.0.1`, `localhost` or `::1`); any other address is refused | `http://127.0.0.1:11434` |
| `textrules.history_limit` | How many dictations the history keeps | `2000` |
| `replacements` | An older, simpler form of the Dictionary (`{"heard": "wanted"}`); prefer the Dictionary page | empty |

The file also stores your lists and a record of what the app learned
(`learned`). Do not edit `learned` by hand.

**How to open and edit the settings file safely**

1. Quit the app: right-click its tray icon > **Quit**.
2. Press Win+R, paste `%LOCALAPPDATA%\HoldToTalk\data` and press Enter. A
   folder opens.
3. Make a backup: click `config.json`, press Ctrl+C then Ctrl+V. A copy
   named "config - Copy.json" appears. If there is no `config.json` yet, the
   app has not saved any setting; start the app, change one setting in the
   window and change it back, then quit again.
4. Right-click `config.json` > **Open with** > **Notepad**.
5. Add or change only the lines you need. The file is JSON: every name and
   text value is in straight double quotes, items inside the same braces are
   separated by commas (no comma after the last one), and the whole file is
   inside one pair of curly braces. A complete small example that changes a
   few of the settings above:

   ```
   {
     "device": "auto",
     "min_seconds": 0.5,
     "sounds": { "mute_start": true },
     "cleanup": { "timeout": 5.0, "min_words": 6 }
   }
   ```

   Your real file will also contain your dictionary, vocabulary and snippets;
   keep those lines and put new ones inside the same outer braces.
6. Save (Ctrl+S), close Notepad, start the app from the Start menu.
7. If you made a mistake, the app tells you in the window ("the settings file
   has a mistake in it"), uses the default values, and keeps a copy of the
   unreadable file named `config.json.bad`. It does not overwrite your file.
   Fix the mistake, or copy your backup back over `config.json`.
   A wrong type for a single setting (text where a number is expected, say)
   replaces only that setting by its default, and the log names it.

## AI cleanup (optional)

With AI cleanup on, a small AI language model removes "um", false starts and
repeated words, and applies spoken self-corrections ("Tuesday, no wait,
Wednesday" becomes "Wednesday"). It runs through **Ollama**, a free program
that runs AI language models on your own PC (ollama.com). It is optional; the
app works without it. It is **on by default**, and if Ollama is not
there the app notices, skips the step and pastes the plain transcript.

To use it:

1. Install Ollama from ollama.com. You will see an Ollama icon in the tray.
2. Open the Command Prompt (Windows key, type `cmd`, Enter) and run
   `ollama pull gemma4:e2b-it-qat`. This downloads about 4.3 GB. You will see
   a progress bar and finally "success".
3. In the window, Settings > AI cleanup shows whether the app found Ollama
   and the model.

If Ollama is installed in its standard per-user folder
(`%LOCALAPPDATA%\Programs\Ollama`), the app starts Ollama in the background
when AI cleanup is on and it is not already running. That Ollama keeps
running after you quit the app, and after you uninstall it; the app never
stops it. An Ollama installed elsewhere (for example as a Windows service) is
never started by the app, only used if it is already running.

**What is sent to Ollama.** AI cleanup only ever talks to Ollama on this
computer (`127.0.0.1`, port 11434); the app refuses any other address, and
there is no setting that changes that. With AI cleanup on, each dictation of 4 to 400 words (settings
`cleanup.min_words` and `cleanup.max_words`) sends:
- the dictation text;
- up to 50 of your Vocabulary and Dictionary spellings, so it spells them
  exactly;
- up to 20 of your snippet trigger phrases (plus "new line" and "new
  paragraph" if spoken formatting is on), so it leaves them alone.
Your voice recording and your snippet texts are not sent.

**How the risk is reduced.**
- The model's answer is treated as untrusted. It is checked, and if a check
  fails the original text is pasted. The checks catch many unwanted changes,
  not all. Among other things they reject: an empty answer or one with hidden
  control characters; a "not", "never" or other negation added or dropped (unless
  you corrected yourself); an answer much longer or shorter than what you
  said, or with many words you did not say; line breaks that are not a list
  you spoke; and a changed snippet trigger. Look over cleaned text now and
  then. The History steps show whether the rewrite was used.
- A dictation never waits for a connection: if Ollama is not known to be
  running, the text is pasted at once and Ollama is checked in the background.
  A dictation can still wait up to `cleanup.timeout` (3 seconds) for the
  model to answer; then the plain text is pasted.
- The model was chosen from a comparison of three models on 25 sample
  transcripts, for speed and for making the fewest unwanted edits. Of the two
  others, both were several times slower and one obeyed an instruction that
  had been dictated as text. Accuracy was not measured beyond that.

**How it decides whether to trust the program on its port.** A "port" is a
numbered doorway on your PC that a program listens on; Ollama uses 11434. On
a PC shared by several Windows accounts, another account could run its own
program on that doorway first. So, before sending a word, the app looks up
which Windows account owns the program listening on that port:
- If it is your own account, it sends.
- If it can see that it is a Windows built-in service account (SYSTEM, Local
  Service or Network Service), it sends. Those are the accounts Ollama runs as
  if it was installed as a service.
- If another Windows account owns it, nothing is sent, and the window says
  "another Windows account is answering on the AI cleanup port".
- If it cannot find out who owns it, nothing is sent either, and the window
  says so. The code treats an unreadable owner this way. Whether Windows
  lets your account read the owner of a service-run Ollama on your PC was not
  tested by this project, so a service-run Ollama may land here.

If you know the program on that port is yours, switch on
"Trust whatever answers on the AI cleanup port" (Settings > AI cleanup; it is
only shown while the app is blocking, or already trusting, the port; in the
file `cleanup.trust_listener`). **The trust switch skips this check.** If you
switch it on, your dictations go to whatever program answers on that port,
whatever it is and whoever it belongs to. The window's "Allow this program" button on Home does the same.
On a shared PC, leave AI cleanup off unless you trust the other accounts.

## Speed and resource use

These are measurements on **one** development PC (an 8-core desktop
processor, a high-end NVIDIA graphics card), medians. They are not promises.

- **From letting go of the key to the text being ready, with AI cleanup on:**
  about 0.7 s for a short sentence and about 0.9 s for about 8 seconds of
  speech. When the previous dictation was moments earlier: about 0.45 s and
  0.6 s. (The graphics card drops to a low-power state after a few idle
  seconds, so the first dictation after a pause is slower.)
- **Without a graphics card** (the "small" model on the processor): about 5
  to 6 seconds to transcribe 9 seconds of speech. Laptops will be slower. The
  bigger model on the processor took about 25 seconds per clip on the same
  PC, which is why the app does not use it there.
- **Idle:** about 1.4 to 1.8 percent of one processor core.
- **Memory:** the app about 0.8 GB; its window about 135 MB; on the graphics
  card, about 2.4 GB for the speech model and, while it is loaded, about 2.9
  GB more for the AI cleanup model. "Use less graphics memory" saves about
  1 GB at about the same speed.
- **Start to ready:** about 4 seconds once the model is downloaded.

## Privacy, precisely

- **Your voice and your text are never sent anywhere by this app.** The
  connections that exist are the ones listed under "Before you install":
  downloads at install or first start, Ollama on your own PC, and the
  browser's own connections. If neither Edge nor Chrome is installed, the
  window opens as a tab in your usual browser, where that browser's own
  extensions and settings apply (the app logs one line and the window shows a
  notice when this happens).
- **Voice:** recorded into memory, transcribed, and thrown away. The app never
  writes your voice to a file. A recording that could not be transcribed is kept in memory for up
  to 5 minutes (see "Try that recording again").
- **Text:** the app stores your history, settings, dictionary and snippets
  only in the data folder, `%LOCALAPPDATA%\HoldToTalk\data` (`config.json`,
  `history.sqlite3`, `logs\dictate.log`, and `terms-accepted.json`, the
  record of accepting the Terms of Use). To clear the history: History >
  **Clear history**, or switch off "Save history", or delete
  `history.sqlite3`. To wipe everything, uninstall and answer yes to deleting
  your data.
- **The log never contains what you said.** It records timings, word counts
  and errors. Only a developer flag (`--log-text`) writes dictated text to it,
  and it prints a warning when used. Do not use it with private dictations.
- **The window's server** listens only on `127.0.0.1`. It answers only
  requests that carry the **access key**, a random code made fresh every
  time the app starts and useless once the app quits. The browser keeps the
  access key in its page storage while the window is open; where the browser
  stores that is up to the browser.
- **The clipboard:** the pasted text is put on the clipboard marked so that
  Windows keeps it out of clipboard history (Windows+V) and cloud clipboard
  sync, then your own clipboard is put back about half a second later.
  Programs that ignore those markers can still read it while it is there.
  History > Copy uses the same markers.
- **Other programs under your own Windows account** are not kept out: such a
  program could ask the app to quit, or stop it starting. Windows does not
  separate programs of one account from each other, and neither does this app.
  The window's server refuses other websites, other computers and programs
  that lack the access key. It drops slow connections, which limits (but does
  not rule out) a program that keeps opening connections.

## Known limitations

- It cannot paste into windows that run **as administrator** (Windows blocks
  it). Start the app as administrator too, if you must.
- A mouse button used as the hold-to-talk key stops doing its normal job
  while the app is ready. Left and right buttons cannot be used.
- Some **full-screen games** may hide the on-screen indicator; dictation
  still works. This was not tested.
- On a **shared PC**, see "How it decides whether to trust the program on its
  port". Turn AI cleanup off on shared PCs unless you trust the other
  accounts.
- **The browser** that shows the window makes its own connections (see
  "What leaves your computer").
- **Speech recognition on an NVIDIA card** needs a recent card and driver;
  otherwise the app uses the processor and says so.
- **AI cleanup checks catch many unwanted changes, not all.**
- A multi-line **snippet** pasted into a terminal can run commands; the
  terminal protection covers only line breaks added by AI cleanup.
- **Accuracy:** this project has not measured how accurate the speech
  recognition is, in any language.
- **Tested on:** Python 3.12, Windows 11, one PC. Other versions are accepted
  but untested.

## Updating

There is no update check. To get a newer version, download the new Setup
file from the Releases page and double-click it (or download the new ZIP,
extract it and run its **`install.bat`**). Either way the same window then
works as the updater and says so: it is called
**Update** instead of Setup, its first page says "This updates Hold to talk
from version ... to ... It does not install a second copy", and its button
and headings say **Update**, "Updating Hold to talk" and "Hold to talk is
updated". (If the same version is already installed it says **Repair**.) It
quits the running app, replaces the program files (and removes
files that are no longer part of the app), adds any new libraries, keeps your
settings and history, and refreshes the shortcuts. `CHANGELOG.md` says what
changed.

## Uninstalling

1. Open **Settings > Apps > Installed apps**, find **Hold to talk** and choose
   **Uninstall** (or double-click `uninstall.bat` in
   `%LOCALAPPDATA%\Programs\HoldToTalk`).
2. A window (in the same style as the setup window) asks **"Remove Hold to
   talk?"** and lists what will be removed and what is kept. Choose
   **Remove**. It quits the app and removes the Start with Windows entry, both
   shortcuts, the Installed apps entry, the Python environment, the window's
   browser profile and the program files.
3. The box **"Also delete my settings, dictation history and logs (this cannot
   be undone)"** starts unticked, so your data is kept. If you tick it, the
   window asks **"Delete your settings and history for good?"** (Delete or
   Keep). (Running `uninstall.bat -Yes` or `-Yes -DeleteData` in a Command
   Prompt uses the text uninstaller, which asks the questions "Remove Hold to
   talk now? [Y/n]" and "Delete your settings, dictation history and logs too?
   This cannot be undone. [y/N]" instead.) If you choose Delete, it deletes only the files the app itself
   creates in the data folder, by name: `config.json` and its copies and
   leftovers (`config.json.bad`, `.bad-<time>`, `.copying`, `.migrated`),
   `history.sqlite3` with its side files and set-aside copies
   (`history.sqlite3.corrupt-<time>`, `.migrated`, `.copying`),
   `terms-accepted.json` (the record of accepting the Terms of Use), and
   `logs\dictate.log` and `.1`. Anything else found there is left in place
   and listed.
4. The window has three steps, listed on its left: Confirm, Removing and
   Finish. The last page is headed **"Hold to talk has been removed"** and
   says what was kept and what it left on purpose.

**Remove every trace** (the uninstaller leaves things that are not part of
the app, because other programs may use them):

- [ ] Uninstall as above. This removes `%LOCALAPPDATA%\Programs\HoldToTalk`,
  `%LOCALAPPDATA%\HoldToTalk\venv` (this includes the NVIDIA runtime) and the
  browser profile folders `window`, `window-*` and `quit-probe` in
  `%LOCALAPPDATA%\HoldToTalk`; the Start menu and Desktop shortcuts; the
  registry key `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\HoldToTalk`;
  and the `HoldToTalk` value under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` and under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run`.
- [ ] Tick **Also delete my settings, dictation history and logs** in the
  uninstall window (in the text uninstaller: answer **y**), or delete the folder
  `%LOCALAPPDATA%\HoldToTalk\data` yourself. If `%LOCALAPPDATA%\HoldToTalk`
  is then empty, the uninstaller removes it.
- [ ] **Speech models:** open `%USERPROFILE%\.cache\huggingface\hub` (press
  Win+R, paste it, Enter) and delete the folders whose names start with
  `models--` and contain `whisper` (here
  `models--Systran--faster-whisper-small` and
  `models--mobiuslabsgmbh--faster-whisper-large-v3-turbo`). If you set the
  environment variable `HF_HUB_CACHE` or `HF_HOME`, they are there instead.
  Other programs may use the same cache.
- [ ] **pip's download cache:** delete `%LOCALAPPDATA%\pip\Cache`, or run
  `python -m pip cache purge`. It empties pip's cache for all your Python
  projects, not just this one.
- [ ] **Ollama, if you installed it for this:** in the Command Prompt run
  `ollama list`, then `ollama rm gemma4:e2b-it-qat`; then uninstall Ollama in
  Settings > Apps. Ollama keeps its own folders; check its documentation.
  If it is still running, quit it from its tray icon first.
- [ ] **Python,** if you installed it only for this: Settings > Apps.
- [ ] **The downloaded ZIP and the extracted folder:** delete them yourself.

## Troubleshooting

How to open a Command Prompt: press the Windows key, type `cmd`, press
Enter. To open it in a folder: open the folder in File Explorer, click the
address bar, type `cmd`, press Enter.

- **The installer says no suitable Python was found.** Choose **Install
  Python for me** in the setup window. Or do Step 0 yourself, then choose
  **Check again** (or run Setup or `install.bat` again). The message lists
  what it looked at.
- **The installer stopped part-way** (no internet, window closed): choose
  **Try again** on the last page, or run Setup (or `install.bat`) again; it
  carries on.
- **The setup window does not open.** If you used the Setup file, it shows a
  message saying so; download the ZIP from the project's page instead and
  run its `install.bat`. If you started from the ZIP, `install.bat` carries
  on as text in the black window and says why in one line. You can also ask
  for the text installer yourself: `install.bat -Console` (see "The text
  installer").
- **Windows or antivirus blocks the installer.** The Setup file and the
  scripts are not digitally signed. For the ZIP, unblock it first (Installing,
  step 2). A SmartScreen "Windows protected your PC" screen has a "More info"
  link and a "Run anyway" button; use them only if you trust where you
  downloaded the files from. Some antivirus programs distrust any unsigned
  script; the project has not tested against any.
- **Nothing happens when I hold the key.** Look at the tray icon. Amber: still
  loading. Grey: paused or a problem (open the window; it says why). Teal:
  check Windows lets desktop apps use the microphone, and that the right
  microphone is chosen (Settings > Microphone and language, with the level
  test). Another program may be using the same key combination.
- **The icon is teal but nothing is pasted.** The program in front may run as
  administrator (see Known limitations), or may not accept pasted text. The
  dictation is still in History; use Copy there. Also check that you let go of
  the key: pasting waits until no modifier key is held.
- **The icon stays amber for a very long time.** The speech model is probably
  still downloading (you skipped it at install, or it was missing). Quit the
  app, run Setup (or `install.bat`) again and leave the model download ticked so you see
  progress. If the window says the model failed to load, it offers a retry.
- **The text comes out in the wrong language or garbled.** Choose your
  language in Settings > Microphone and language instead of detecting it.
  Check the microphone with the level test.
- **AI cleanup does nothing.** Settings > AI cleanup shows what the app sees:
  Ollama not running, model not installed (`ollama pull gemma4:e2b-it-qat`),
  or a blocked port. Dictations of fewer than 4 words get no cleanup.
- **The window says "Can't dictate right now".** The speech model could not
  load; the window gives the reason and a retry button. The log has details.
- **Where is the log?** `%LOCALAPPDATA%\HoldToTalk\data\logs\dictate.log`, or
  in the window: Settings > About > Show recent log.
- **See what it is doing.** Quit the tray copy, open a Command Prompt in
  `%LOCALAPPDATA%\Programs\HoldToTalk` and run `start.bat console`. A detailed
  log appears in that window; Ctrl+C quits.
- **Quit it from outside.** In a Command Prompt run
  `"%LOCALAPPDATA%\HoldToTalk\venv\Scripts\python.exe" "%LOCALAPPDATA%\Programs\HoldToTalk\dictate.py" --quit`.

## For developers

`ARCHITECTURE.md` explains how the program works, its rules, and how to add a
pipeline stage, a setting or a key name. Tests: `python -B -m unittest
discover tests`. Third-party software and model licences are in
`THIRD-PARTY-NOTICES.md`.

## Reporting a bug or asking a question

Open an issue on this project's GitHub page (the **Issues** tab, then **New
issue**) and pick the **Bug report** or the **Question or idea** form.
Everyone can read issues, so leave out anything private: text you dictated,
your history, and your Windows account name in folder paths. No reply is
promised.

## Reporting a security problem

Please do not put the details of a security problem in a public issue.
`SECURITY.md` says how to report it privately.

## Licence

Hold to talk is free to use, but it is not open-source software. `LICENSE`
(the Hold to talk Licence) has the rules. In short: you may install and use
it on your own computers, or one you have been given to use, for any lawful
purpose including work, and change the code for your own use; you may not give, send or publish the program or its code to anyone
else, changed or unchanged, or sell it. To share it, send people the link to
this project's page so that they download it there themselves. The Terms of
Use (`TERMS.md`) apply in addition.
Hold to talk is an independent
project and is not affiliated with, endorsed by or sponsored by the makers of
any product or service named in its documents; those names belong to their
owners. Libraries and models
downloaded by the installer have
their own licences, including NVIDIA's for the optional graphics-card
runtime: see `THIRD-PARTY-NOTICES.md`.
