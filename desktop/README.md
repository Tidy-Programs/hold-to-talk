# Hold to talk for Windows and Mac (in progress)

This folder is the new version of Hold to talk that runs on **Windows and
macOS** as one app, with a normal installer and no Python to install. It is
not finished: the app you install today is still the Python one at the top of
this repository.

## Where it comes from

It starts from **Handy** (<https://github.com/cjpais/Handy>), a free dictation
app by CJ Pais and contributors, under the MIT licence. The copy here was
taken from Handy's `main` branch at commit
`939c9573a346859016de1f9d87369a9765af1671` (version 0.10.1, 21 November 2026).
Handy's licence is in [`LICENSE`](LICENSE) and ships inside the app as
`resources/notices/Handy-LICENSE.txt`; Handy's own README is in
[`docs/HANDY-README.md`](docs/HANDY-README.md).

Changes made for Hold to talk so far (step 1 of the plan):

- Named **Hold to talk**, version 0.1.0, app id `com.tidyprograms.holdtotalk`,
  so it installs beside Handy rather than over it.
- Hold to talk's mark (three bars on a teal disc) for the app icon, the tray
  or menu-bar icon and the sidebar. `tools/make_desktop_icons.py` at the top of
  the repository draws them.
- **No update checks**: the updater is switched off in code and has no
  address to check.
- **Not signed yet**: Handy's own Windows signing step is removed. The Mac
  build is signed "ad hoc" only, so macOS will ask before opening it.
- The app's name in every language of the interface.

Still Handy's for now, and replaced in later steps: the window's screens and
mascot, the default key (Ctrl+Space on Windows, Option+Space on Mac), the
"Source code" and "Donate" links in About, and where speech models are
downloaded from (Handy's server, `blob.handy.computer`).

## Building it

GitHub builds it: the "Desktop app" workflow
(`.github/workflows/desktop.yml`) makes a Windows installer and a Mac disk
image on every pull request that changes this folder, and you download them
from the run's page under "Artifacts".

To build on your own computer you need Rust, Bun and the platform tools that
[`BUILD.md`](BUILD.md) lists; then, in this folder, `bun install` and
`bun run tauri build`.
