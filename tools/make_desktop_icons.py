"""Generate the icons of the Windows and Mac app (desktop/) from Hold to talk's
own mark: the same three sound bars on a disc that tray.make_icon draws.

    python -B tools/make_desktop_icons.py

Writes, under desktop/src-tauri:
  icons/      the app icon at every size Tauri bundles (PNG, .ico, .icns)
  resources/  the tray icons: colour (ready, listening, working, needs
              attention) and one-colour versions for light and dark taskbars
              and the Mac menu bar

Only Pillow is needed, so it runs on any system. Run it again only if the
mark changes; the generated files are committed.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
TAURI = ROOT / "desktop" / "src-tauri"

# The same colours as tray._ICON_COLORS.
TEAL, CORAL, AMBER, GREY = "#137a70", "#e0533d", "#c98a12", "#7c7f86"
BIG = 1024                     # drawn large, then downscaled


def mark(disc, bars=(255, 255, 255, 255), slash=False, knockout=False):
    """BIG x BIG RGBA image of the mark.

    knockout=True cuts the bars out of the disc (they become transparent),
    which is how the one-colour tray icons show them."""
    s = BIG / 256
    img = Image.new("RGBA", (BIG, BIG), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4 * s, 4 * s, BIG - 5 * s, BIG - 5 * s), fill=disc)
    if knockout:
        cut = Image.new("L", (BIG, BIG), 255)
        cd = ImageDraw.Draw(cut)
    for x, half in ((80, 34), (128, 66), (176, 46)):
        box = ((x - 14) * s, (128 - half) * s, (x + 14) * s, (128 + half) * s)
        if knockout:
            cd.rounded_rectangle(box, radius=14 * s, fill=0)
        else:
            d.rounded_rectangle(box, radius=14 * s, fill=bars)
    if knockout:
        alpha = img.getchannel("A")
        img.putalpha(Image.composite(alpha, Image.new("L", (BIG, BIG), 0), cut))
    if slash:
        d = ImageDraw.Draw(img)
        d.line((56 * s, 56 * s, 200 * s, 200 * s), fill=(30, 30, 30, 255),
               width=int(28 * s))
        d.line((56 * s, 56 * s, 200 * s, 200 * s), fill=(255, 255, 255, 255),
               width=int(12 * s))
    return img


def sized(img, size):
    return img.resize((size, size), Image.LANCZOS)


def app_icons(out):
    icon = mark(TEAL)
    pngs = {
        "32x32.png": 32, "64x64.png": 64, "128x128.png": 128,
        "128x128@2x.png": 256, "icon.png": 512, "logo.png": 1024,
        "Square30x30Logo.png": 30, "Square44x44Logo.png": 44,
        "Square71x71Logo.png": 71, "Square89x89Logo.png": 89,
        "Square107x107Logo.png": 107, "Square142x142Logo.png": 142,
        "Square150x150Logo.png": 150, "Square284x284Logo.png": 284,
        "Square310x310Logo.png": 310, "StoreLogo.png": 50,
    }
    for name, size in pngs.items():
        sized(icon, size).save(out / name)
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    sized(icon, 256).save(out / "icon.ico", format="ICO",
                          sizes=[(n, n) for n in ico_sizes])
    icon.save(out / "icon.icns", format="ICNS")
    return len(pngs) + 2


def tray_icons(out, size=64):
    white, black = (255, 255, 255, 255), (0, 0, 0, 255)
    files = {
        # Colour set
        "handy.png": mark(TEAL),
        "recording.png": mark(CORAL),
        "transcribing.png": mark(AMBER),
        "handy_warning.png": mark(GREY, slash=True),
        # For dark taskbars: white mark; state colours stay so they read at
        # a glance, as in the Windows app today.
        "tray_idle.png": mark(white, knockout=True),
        "tray_recording.png": mark(CORAL),
        "tray_transcribing.png": mark(AMBER),
        "tray_idle_warning.png": mark(white, knockout=True, slash=True),
        # For light taskbars and the Mac menu bar: black mark.
        "tray_idle_dark.png": mark(black, knockout=True),
        "tray_recording_dark.png": mark(CORAL),
        "tray_transcribing_dark.png": mark(AMBER),
        "tray_idle_warning_dark.png": mark(black, knockout=True, slash=True),
    }
    for name, img in files.items():
        sized(img, size).save(out / name)
    return len(files)


def main():
    n = app_icons(TAURI / "icons")
    m = tray_icons(TAURI / "resources")
    print(f"wrote {n} app icons and {m} tray icons under {TAURI}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
