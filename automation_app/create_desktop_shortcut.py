"""
Creates the "Coach Vee YouTube Automator" icon on the Windows Desktop.

The shortcut starts app_gui.py with the app venv's pythonw.exe (no console window).
Also draws coachvee.ico the first time (needs Pillow, which is in requirements.txt).
Run:  .venv\\Scripts\\python.exe create_desktop_shortcut.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ICON = APP_DIR / "coachvee.ico"
NAME = "Coach Vee YouTube Automator"


def make_icon() -> None:
    if ICON.exists():
        return
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("Pillow not installed - shortcut will use the default Python icon.")
        return
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):                                   # rose -> violet gradient
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=(int(236 + (124 - 236) * t), int(72 + (58 - 72) * t), int(153 + (237 - 153) * t), 255))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([8, 8, S - 8, S - 8], radius=56, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 112)
    except OSError:
        font = ImageFont.load_default()
    d.text((S / 2, 104), "CV", font=font, fill="white", anchor="mm")
    d.polygon([(104, 168), (104, 222), (152, 195)], fill="white")     # small play button
    d.rounded_rectangle([70, 160, 186, 230], radius=16, outline="white", width=7)
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Icon written: {ICON}")


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def create_shortcut() -> Path:
    pyw = APP_DIR / ".venv" / "Scripts" / "pythonw.exe"
    if not pyw.exists():
        pyw = Path(sys.executable).with_name("pythonw.exe")
    target_script = APP_DIR / "app_gui.py"
    ps = (
        "$desk = [Environment]::GetFolderPath('Desktop'); "
        f"$lnk = Join-Path $desk {_ps_quote(NAME + '.lnk')}; "
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk); "
        f"$s.TargetPath = {_ps_quote(pyw)}; "
        f"$s.Arguments = {_ps_quote(chr(34) + str(target_script) + chr(34))}; "
        f"$s.WorkingDirectory = {_ps_quote(APP_DIR)}; "
        f"$s.Description = {_ps_quote('Coach Vee weekly Shorts: Drive -> Whisper -> review -> YouTube schedule')}; "
        + (f"$s.IconLocation = {_ps_quote(str(ICON) + ',0')}; " if ICON.exists() else "")
        + "$s.Save(); Write-Output $lnk"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"Could not create the shortcut:\n{r.stderr}")
    return Path(r.stdout.strip())


if __name__ == "__main__":
    make_icon()
    print(f"Desktop shortcut created: {create_shortcut()}")
