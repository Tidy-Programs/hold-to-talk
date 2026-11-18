# Security

## Which version is looked at

Fixes, if any, go into the latest release only. If you use an older one, update first (see
"Updating" in `README.md`) and check whether the problem is still there.

## How to report a security problem

Please report it **privately**, not in a public issue.

On this project's GitHub page, open the **Security** tab and click **Report a
vulnerability**. That is GitHub's private reporting: only you and the
project's maintainers can see it.

If you do not see that button, open an issue with the **Question or idea**
form that says only that you have a security problem to report. Do not put
any details in it. A maintainer will
then tell you how to send the details privately.

Please include:

- what you found and where (the file, the installer step or the part of the
  window);
- the version (Settings > About in the window, or `APP_VERSION` in
  `settings.py`) and your Windows version;
- the steps to repeat it, and what you expected to happen instead;
- what you think an attacker could do with it.

Please do not put real personal data, recordings or dictated text in a
report; invented examples are enough.

## What to expect

This project is unpaid work done in spare time. A maintainer aims to
acknowledge your report and to either fix the problem or explain why it is
not one. No fix and no time frame is promised.

## What is in scope

- The app's own code (the `.py` files and the window in `ui/`).
- The installer and uninstaller (`install.bat`, `install-gui.ps1`,
  `install.ps1`, `uninstall.bat`, `uninstall.ps1`, `htt-common.ps1`).
- The window and the local server that shows it, including the Terms of Use
  screen and the step of accepting the terms (`TERMS.md`, `terms.py`): for
  example a way to make the app dictate without the
  acceptance being recorded, or to make it record an acceptance that was
  not given.

## What is not in scope

- Bugs in Windows, the browser, Ollama, Python or the libraries and models
  the app uses. Please report those to their own projects.
- Attacks that need someone who already controls your Windows account.
  Windows does not separate programs of one account from each other, and
  neither does this app.

The README's "Known limitations" and "Privacy, precisely" sections list what
is already known.

This software comes with no warranty, and using it is subject to the Terms
of Use; see `LICENSE` and `TERMS.md`. The Terms of Use say how security
problems are to be reported (section 18, Contact): privately, as described
above.
