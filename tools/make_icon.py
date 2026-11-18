"""Generate app.ico (the Windows icon for the shortcuts and the Installed apps
entry) from the app's own mark: tray.make_icon draws it.

    python -B tools\\make_icon.py            writes app.ico next to the code

The icon is the "ready" (teal) mark at 16, 24, 32, 48, 64, 128 and 256
pixels, each size drawn from the 256 pixel original by make_icon itself.
Run it again only if the mark in tray.py changes; app.ico is committed.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SIZES = (16, 24, 32, 48, 64, 128, 256)


def build(path):
    import tray                       # the mark lives here
    images = [tray.make_icon("ready", size) for size in SIZES]
    biggest = images[-1]
    biggest.save(path, format="ICO", sizes=[(s, s) for s in SIZES],
                 append_images=images[:-1])
    return path


def main(argv=None):
    out = Path(argv[0]) if argv else ROOT / "app.ico"
    build(out)
    print(f"wrote {out.name} ({out.stat().st_size} bytes), sizes {SIZES}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
