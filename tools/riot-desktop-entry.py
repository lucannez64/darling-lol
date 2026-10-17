#!/usr/bin/env python3
"""Create and install the GNOME / FreeDesktop desktop entry and launcher icon."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path("@PROJECT_ROOT@")
HOST_HOME = Path("@HOST_HOME@")
DPREFIX = Path("@DPREFIX@")

SVG_ICON_CONTENT = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#091428"/>
      <stop offset="50%" stop-color="#0a1c36"/>
      <stop offset="100%" stop-color="#030811"/>
    </linearGradient>
    <linearGradient id="borderGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#f0e6d2"/>
      <stop offset="40%" stop-color="#c8aa6e"/>
      <stop offset="80%" stop-color="#785a28"/>
      <stop offset="100%" stop-color="#c89b3c"/>
    </linearGradient>
    <linearGradient id="goldGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#fff2c6"/>
      <stop offset="25%" stop-color="#f0e6d2"/>
      <stop offset="50%" stop-color="#c8aa6e"/>
      <stop offset="85%" stop-color="#9a7a35"/>
      <stop offset="100%" stop-color="#785a28"/>
    </linearGradient>
    <linearGradient id="goldHighlight" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#ffffff" stop-opacity="0.6"/>
      <stop offset="100%" stop-color="#c8aa6e" stop-opacity="0"/>
    </linearGradient>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="6" result="blur"/>
      <feComposite in="SourceGraphic" in2="blur" operator="over"/>
    </filter>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="8" stdDeviation="14" flood-color="#000000" flood-opacity="0.85"/>
    </filter>
  </defs>

  <!-- Background Shield / Squircle -->
  <rect x="24" y="24" width="464" height="464" rx="104" fill="url(#bgGrad)" stroke="url(#borderGrad)" stroke-width="12" filter="url(#shadow)"/>
  <rect x="40" y="40" width="432" height="432" rx="90" fill="none" stroke="#c8aa6e" stroke-width="2" stroke-opacity="0.4"/>

  <!-- Crest Emblem -->
  <g filter="url(#glow)">
    <path d="M172 108 L242 108 L242 316 L364 316 L352 388 L156 388 L156 124 Z"
          fill="url(#goldGrad)" stroke="#463714" stroke-width="3"/>
    <path d="M 180 120 L 230 120 L 230 250 L 180 200 Z"
          fill="url(#goldHighlight)"/>
    <path d="M 242 328 L 348 328 L 340 376 L 172 376 L 172 356 L 242 356 Z"
          fill="#c8aa6e" opacity="0.45"/>
    <polygon points="256,76 264,96 284,96 268,108 274,128 256,116 238,128 244,108 228,96 248,96" fill="#f0e6d2"/>
  </g>
</svg>
"""


def extract_best_png_from_icns(icns_path: Path) -> bytes | None:
    """Extract the highest-resolution embedded PNG from an Apple ICNS file."""
    try:
        data = icns_path.read_bytes()
        if not data.startswith(b"icns"):
            return None
        offset = 8
        best_png = None
        best_len = 0
        while offset + 8 <= len(data):
            size = int.from_bytes(data[offset + 4 : offset + 8], "big")
            if size < 8 or offset + size > len(data):
                break
            chunk = data[offset + 8 : offset + size]
            if chunk.startswith(b"\x89PNG\r\n\x1a\n") and len(chunk) > best_len:
                best_png = chunk
                best_len = len(chunk)
            offset += size
        return best_png
    except Exception:
        return None


def find_candidate_icns() -> Path | None:
    """Find any available Riot or League .icns icon files."""
    candidates = [
        *list((PROJECT_ROOT / "build/installer").glob("*League of Legends*.app/Contents/Resources/*.icns")),
        *list(DPREFIX.glob("Users/Shared/Riot Games/Riot Client.app/Contents/Resources/*.icns")),
        *list(DPREFIX.glob("Applications/Riot Client.app/Contents/Resources/*.icns")),
        *list(DPREFIX.glob("Applications/League of Legends.app/Contents/Resources/*.icns")),
    ]
    for c in candidates:
        if c.is_file() and c.stat().st_size > 1024:
            return c
    return None


def main():
    icons_base = HOST_HOME / ".local/share/icons"
    apps_base = HOST_HOME / ".local/share/applications"
    desktop_dir = HOST_HOME / "Desktop"

    scalable_dir = icons_base / "hicolor/scalable/apps"
    png_dir = icons_base / "hicolor/512x512/apps"
    scalable_dir.mkdir(parents=True, exist_ok=True)
    png_dir.mkdir(parents=True, exist_ok=True)
    apps_base.mkdir(parents=True, exist_ok=True)

    # 1. Install scalable SVG icon
    svg_target = scalable_dir / "league-of-legends.svg"
    svg_target.write_text(SVG_ICON_CONTENT)
    shutil.copyfile(svg_target, icons_base / "league-of-legends.svg")

    # 2. Extract or generate PNG icon
    png_target = png_dir / "league-of-legends.png"
    icns_file = find_candidate_icns()
    extracted = False
    if icns_file:
        png_data = extract_best_png_from_icns(icns_file)
        if png_data:
            png_target.write_bytes(png_data)
            shutil.copyfile(png_target, icons_base / "league-of-legends.png")
            extracted = True
            print(f"Extracted official icon from {icns_file.name} to {png_target}")

    if not extracted and not png_target.exists():
        # Try to rasterize SVG via rsvg-convert or magick
        rsvg = shutil.which("rsvg-convert")
        magick = shutil.which("magick") or shutil.which("convert")
        if rsvg:
            subprocess.run([rsvg, "-w", "512", "-h", "512", str(svg_target), "-o", str(png_target)], check=False)
            if png_target.exists():
                shutil.copyfile(png_target, icons_base / "league-of-legends.png")
        elif magick:
            subprocess.run([magick, "-background", "none", "-resize", "512x512", str(svg_target), str(png_target)], check=False)
            if png_target.exists():
                shutil.copyfile(png_target, icons_base / "league-of-legends.png")

    # 3. Create .desktop file
    run_script = PROJECT_ROOT / "scripts/desktop-run.sh"
    exec_path = str(run_script).replace("%", "%%")
    exec_cmd = f'"{exec_path}"' if " " in exec_path else exec_path
    desktop_content = f"""[Desktop Entry]
Version=1.0
Type=Application
Name=League of Legends
GenericName=MOBA Game
Comment=Play League of Legends on Linux with darling-lol
Exec={exec_cmd}
Path={PROJECT_ROOT}
Icon=league-of-legends
Terminal=false
Categories=Game;ActionGame;StrategyGame;
Keywords=riot;league;lol;darling;games;
StartupWMClass=Riot Client
StartupNotify=false
"""

    desktop_file = apps_base / "league-of-legends.desktop"
    desktop_file.write_text(desktop_content)
    os.chmod(desktop_file, 0o755)
    print(f"Created application launcher: {desktop_file}")

    target_desktop_dirs = []
    try:
        res = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            d = Path(res.stdout.strip())
            if d.is_dir() and d not in target_desktop_dirs:
                target_desktop_dirs.append(d)
    except Exception:
        pass
    for cand in [HOST_HOME / "Desktop"]:
        if cand.is_dir() and cand not in target_desktop_dirs:
            target_desktop_dirs.append(cand)

    for ddir in target_desktop_dirs:
        desktop_shortcut = ddir / "league-of-legends.desktop"
        desktop_shortcut.write_text(desktop_content)
        os.chmod(desktop_shortcut, 0o755)
        gio = shutil.which("gio")
        if gio:
            subprocess.run([gio, "set", str(desktop_shortcut), "metadata::trusted", "true"], check=False)
        print(f"Created desktop shortcut: {desktop_shortcut}")

    # 4. Refresh icon and desktop databases
    update_desktop = shutil.which("update-desktop-database")
    if update_desktop:
        subprocess.run([update_desktop, str(apps_base)], check=False)

    update_icons = shutil.which("gtk-update-icon-cache")
    if update_icons:
        subprocess.run([update_icons, "-f", "-t", str(icons_base / "hicolor")], check=False)

    print("GNOME application launcher and icon installed successfully.")


if __name__ == "__main__":
    main()
