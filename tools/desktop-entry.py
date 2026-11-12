#!/usr/bin/env python3
"""Create and install the GNOME / FreeDesktop desktop entry and launcher icon."""
import os
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path("@PROJECT_ROOT@")
HOST_HOME = Path("@HOST_HOME@")

# Marks a launcher entry created by this project.  Previous versions pointed
# Exec at this checkout's desktop-run.sh, so an entry without that marker
# belongs to someone else and is left alone.
LEGACY_RUN_SCRIPT = "scripts/desktop-run.sh"

# Distinctive fragments of the artwork the previous helper wrote.
LEGACY_SVG_MARKERS = ('id="goldGrad"', 'M172 108 L242 108')

# Characters that force Exec quoting, per the Desktop Entry specification.
EXEC_RESERVED = set(' \t\n"\'\\><~|&;$*?#()`')


def escape_string(value: str) -> str:
    """Escape a value of Desktop Entry type ``string``."""
    return (value.replace("\\", "\\\\").replace("\t", "\\t")
            .replace("\n", "\\n").replace("\r", "\\r"))


def quote_exec_argument(value: str) -> str:
    """Escape and quote one Exec argument as the specification requires."""
    escaped = value.replace("%", "%%")
    if not any(char in EXEC_RESERVED for char in escaped):
        return escaped
    escaped = "".join("\\" + char if char in '"`$\\' else char for char in escaped)
    return f'"{escaped}"'


def legacy_entry_owned(path: Path) -> bool:
    """Return True when a launcher entry was created by this project."""
    marker = str(PROJECT_ROOT / LEGACY_RUN_SCRIPT)
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        # A dangling symlink still counts when it points at this checkout.
        try:
            text = os.readlink(path)
        except OSError:
            return False
    return marker in text or marker.replace("%", "%%") in text


def remove_legacy_entry(path: Path) -> bool:
    """Remove a previous launcher entry only when this project created it."""
    if not (path.is_symlink() or path.exists()):
        return False
    if not legacy_entry_owned(path):
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def legacy_svg_owned(path: Path) -> bool:
    """Return True when an icon is the artwork the previous helper wrote."""
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return False
    return all(marker in text for marker in LEGACY_SVG_MARKERS)


def shortcut_directories() -> list[Path]:
    """Return the desktop directories that hold launcher shortcuts."""
    directories: list[Path] = []
    try:
        result = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            path = Path(result.stdout.strip())
            if path.is_dir():
                directories.append(path)
    except Exception:
        pass
    for candidate in [HOST_HOME / "Desktop"]:
        if candidate.is_dir() and candidate not in directories:
            directories.append(candidate)
    return directories


def main():
    icons_base = HOST_HOME / ".local/share/icons"
    apps_base = HOST_HOME / ".local/share/applications"

    scalable_dir = icons_base / "hicolor/scalable/apps"
    png_dir = icons_base / "hicolor/128x128/apps"
    scalable_dir.mkdir(parents=True, exist_ok=True)
    png_dir.mkdir(parents=True, exist_ok=True)
    apps_base.mkdir(parents=True, exist_ok=True)

    source_svg = PROJECT_ROOT / "darling-lol.svg"
    source_png = PROJECT_ROOT / "darling-lol.png"
    if not source_svg.is_file() or not source_png.is_file():
        raise SystemExit("launcher icon is missing: expected darling-lol.svg and darling-lol.png")

    # Install the project's original icon under a neutral name.  Do not extract
    # or install an icon from the game/client installation.
    shutil.copyfile(source_svg, scalable_dir / "darling-launcher.svg")
    shutil.copyfile(source_png, png_dir / "darling-launcher.png")

    # Remove artifacts created by older versions of this helper.  Only files
    # proven to be ours are deleted: an entry that pointed at this checkout's
    # desktop-run.sh, and the SVG with the old artwork.  The old PNG was
    # extracted from the client and cannot be verified, so it is removed only
    # when one of those proofs succeeded.
    owned = remove_legacy_entry(apps_base / "league-of-legends.desktop")
    desktop_dirs = shortcut_directories()
    for directory in desktop_dirs:
        owned = remove_legacy_entry(directory / "league-of-legends.desktop") or owned
    legacy_svgs = (scalable_dir / "league-of-legends.svg", icons_base / "league-of-legends.svg")
    if any(legacy_svg_owned(path) for path in legacy_svgs):
        owned = True

    if owned:
        for legacy_icon in (
            *legacy_svgs,
            icons_base / "league-of-legends.png",
            icons_base / "hicolor/512x512/apps/league-of-legends.png",
        ):
            try:
                legacy_icon.unlink(missing_ok=True)
            except OSError:
                pass

    # Create the desktop entry.  Keep StartupWMClass: Darling's AppKit sets
    # WM_CLASS from -[NSProcessInfo processName], which is the basename of
    # argv[0], and the launcher starts .../RiotClientServices.  It is an
    # interoperability identifier, not a user-visible name.
    # Terminal stays false here because this entry runs desktop-run.sh, which
    # saves session output in build/desktop-launch.log; the packaged entry runs
    # the session directly and therefore needs a terminal.
    exec_cmd = quote_exec_argument(str(PROJECT_ROOT / LEGACY_RUN_SCRIPT))
    project_path = escape_string(str(PROJECT_ROOT))
    desktop_content = f"""[Desktop Entry]
Version=1.0
Type=Application
Name=Darling Launcher
GenericName=Game
Comment=Unofficial launcher for the macOS game client via Darling
Exec={exec_cmd}
Path={project_path}
Icon=darling-launcher
Terminal=false
Categories=Game;
Keywords=Darling;Launcher;Compatibility;
StartupWMClass=RiotClientServices
StartupNotify=false
"""

    desktop_file = apps_base / "darling-launcher.desktop"
    desktop_file.write_text(desktop_content)
    os.chmod(desktop_file, 0o755)
    print(f"Created application launcher: {desktop_file}")

    for directory in desktop_dirs:
        desktop_shortcut = directory / "darling-launcher.desktop"
        desktop_shortcut.write_text(desktop_content)
        os.chmod(desktop_shortcut, 0o755)
        gio = shutil.which("gio")
        if gio:
            subprocess.run([gio, "set", str(desktop_shortcut), "metadata::trusted", "true"], check=False)
        print(f"Created desktop shortcut: {desktop_shortcut}")

    # Link handler for sign-in: Google and other social sign-in finish in the host
    # browser at a riotclient:// URL, which open-guest-url.sh hands to the client
    # running in Darling. Hidden from menus and never placed on the desktop.
    # Declaring the MimeType makes it the handler only while no other app claims
    # the scheme; the user's explicit choices in mimeapps.list are left alone.
    handler_exec = quote_exec_argument(str(PROJECT_ROOT / "scripts/open-guest-url.sh"))
    handler_file = apps_base / "darling-url-handler.desktop"
    handler_file.write_text(f"""[Desktop Entry]
Version=1.0
Type=Application
Name=Darling Link Handler
Comment=Opens sign-in links in the running Darling session
Exec={handler_exec} %u
Path={project_path}
Icon=darling-launcher
Terminal=false
NoDisplay=true
MimeType=x-scheme-handler/riotclient;
StartupNotify=false
""")
    os.chmod(handler_file, 0o755)
    print(f"Created link handler: {handler_file}")

    # Refresh icon and desktop databases
    update_desktop = shutil.which("update-desktop-database")
    if update_desktop:
        subprocess.run([update_desktop, str(apps_base)], check=False)

    update_icons = shutil.which("gtk-update-icon-cache")
    if update_icons:
        subprocess.run([update_icons, "-f", "-t", str(icons_base / "hicolor")], check=False)

    print("GNOME application launcher and icon installed successfully.")


if __name__ == "__main__":
    main()
