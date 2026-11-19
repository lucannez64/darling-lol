#!/usr/bin/env python3
"""Create, update, inspect or remove the GNOME / FreeDesktop desktop entries.

The non-interactive flow lives in scripts/create-desktop-entry.sh; the
interactive create-shortcuts.sh wraps this helper to ask for a launcher name,
window entries, custom launch flags and a Toolbox or host target.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path("@PROJECT_ROOT@")
HOST_HOME = Path("@HOST_HOME@")

# Marks a launcher entry created by this project.  Previous versions pointed
# Exec at this checkout's desktop-run.sh, so an entry without one of these
# markers belongs to someone else and is left alone.
LEGACY_RUN_SCRIPT = "scripts/desktop-run.sh"
# Current Exec target: it starts the Riot Client installer automatically when
# the client is missing, then delegates to desktop-run.sh.
LAUNCH_SHORTCUT_SCRIPT = "scripts/launch-shortcut.sh"
URL_HANDLER_SCRIPT = "scripts/open-guest-url.sh"

# Distinctive fragments of the artwork the previous helper wrote.
LEGACY_SVG_MARKERS = ('id="goldGrad"', 'M172 108 L242 108')

# Opt-in window-association entries (--window-entries / DARLING_LOL_WINDOW_ENTRIES=1).
# GNOME matches a window to an application by StartupWMClass, so a hidden entry
# per client window class gives those windows a proper name and icon in the
# Activities overview and the dash.  The game runs in its own window with the
# LeagueofLegends class, so it gets an entry too.  They carry third-party
# product names, so they are never created unless the user asks, and the
# default launcher stays neutral.  Each tuple is (file name, display name,
# WM_CLASS).
GAME_ENTRY_NAME = "darling-league-game.desktop"
GAME_ENTRY_MARKER = "X-Darling-LoL-WindowEntry=game"
WINDOW_ENTRIES = (
    ("darling-league-client.desktop", "League of Legends", "LeagueClientUx"),
    (GAME_ENTRY_NAME, "League of Legends", "LeagueofLegends"),
    ("darling-riot-client.desktop", "Riot Client", "Riot Client"),
)
WINDOW_ENTRY_KEYWORDS = "LoL;League;League of Legends;Riot;"

# Characters that force Exec quoting, per the Desktop Entry specification.
EXEC_RESERVED = set(' \t\n"\'\\><~|&;$*?#()`')


def escape_string(value: str) -> str:
    """Escape a value of Desktop Entry type ``string``."""
    return (value.replace("\\", "\\\\").replace("\t", "\\t")
            .replace("\n", "\\n").replace("\r", "\\r"))


def unescape_string(value: str) -> str:
    """Inverse of :func:`escape_string` for values this helper wrote."""
    escapes = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            replacement = escapes.get(value[index + 1])
            if replacement is not None:
                result.append(replacement)
                index += 2
                continue
        result.append(char)
        index += 1
    return "".join(result)


def quote_exec_argument(value: str) -> str:
    """Escape and quote one Exec argument as the specification requires."""
    escaped = value.replace("%", "%%")
    if not any(char in EXEC_RESERVED for char in escaped):
        return escaped
    escaped = "".join("\\" + char if char in '"`$\\' else char for char in escaped)
    return f'"{escaped}"'


def exec_command(script: str, toolbox: str | None = None, env=()) -> str:
    """Return the Exec value for a project script, optionally through Toolbox.

    The native installer runs inside Toolbox when it is invoked with
    ``--toolbox``; every entry must then re-enter the same container instead
    of running on the host, where neither Darling nor its dependencies are
    installed.  That holds for the launcher and for the sign-in link handler.
    Launch flags arrive as ``env`` NAME=VALUE assignments ahead of the script;
    inside the Toolbox wrapper they are set in the container, so the guest
    session inherits them.
    """
    command = str(PROJECT_ROOT / script)
    parts: list[str] = []
    if env:
        parts.append("env")
        parts.extend(quote_exec_argument(item) for item in env)
    parts.append(quote_exec_argument(command))
    if toolbox is not None:
        parts = ["toolbox", "run", "-c", quote_exec_argument(toolbox), *parts]
    return " ".join(parts)


# One token of a Desktop Entry Exec value, per the specification's quoting
# rules: a double-quoted string with backslash escapes, or a bare run that
# stops at whitespace or a double quote.
EXEC_TOKEN = r'"(?:\\.|[^"\\])*"|[^\s"]+'
# An environment assignment as this helper writes it into Exec: NAME=VALUE,
# where the value may be quoted (spaces), empty or ordinary.  Ownership only
# recognises assignments, so a foreign command such as
# ``env FOO=1 /usr/bin/printf ".../launch-shortcut.sh"`` is never ours.
ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)


def unquote_exec_argument(value: str) -> str:
    """Inverse of quote_exec_argument for the wrappers this helper generates."""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        body = value[1:-1]
        body = (body.replace("\\\\", "\x00").replace('\\"', '"')
                .replace("\\`", "`").replace("\\$", "$").replace("\x00", "\\"))
        return body.replace("%%", "%")
    return value.replace("%%", "%")


def unwrap_exec(command: str) -> tuple[str | None, list[str], str]:
    """Split an Exec command into (toolbox container, env assignments, command).

    Recognises exactly the wrappers this helper generates: an optional
    ``toolbox run -c CONTAINER`` prefix and an optional ``env`` prefix whose
    arguments are all NAME=VALUE assignments.  Any other command comes back
    with ``toolbox=None``, no assignments and itself as the final fragment.
    """
    toolbox = None
    wrapper = re.fullmatch(rf"toolbox run -c ({EXEC_TOKEN}) (.+)", command)
    if wrapper:
        toolbox = unquote_exec_argument(wrapper[1])
        command = wrapper[2]
    env: list[str] = []
    while True:
        match = re.fullmatch(rf"env ({EXEC_TOKEN}) (.+)", command)
        if match is None and env:
            # Assignments after the first one are not preceded by `env` again.
            match = re.fullmatch(rf"({EXEC_TOKEN}) (.+)", command)
        if match is None:
            break
        # Decode the complete token first: a quoted value may contain spaces.
        assignment = unquote_exec_argument(match[1])
        if ENV_ASSIGNMENT.fullmatch(assignment) is None:
            break
        env.append(assignment)
        command = match[2]
    return toolbox, env, command


def exec_marker_variants() -> set[str]:
    """Every Exec fragment that proves an entry runs this checkout's scripts."""
    variants: set[str] = set()
    for script in (LEGACY_RUN_SCRIPT, LAUNCH_SHORTCUT_SCRIPT):
        marker = str(PROJECT_ROOT / script)
        variants.update((marker, marker.replace("%", "%%"), quote_exec_argument(marker)))
    # The link handler launches with the %u URL field code appended.
    handler = str(PROJECT_ROOT / URL_HANDLER_SCRIPT)
    for marker in (handler, handler.replace("%", "%%"), quote_exec_argument(handler)):
        variants.add(marker + " %u")
    return variants


def legacy_entry_owned(path: Path) -> bool:
    """Recognize entries managed by this checkout."""
    markers = [str(PROJECT_ROOT / script) for script in (LEGACY_RUN_SCRIPT,
                                                         LAUNCH_SHORTCUT_SCRIPT)]
    variants = exec_marker_variants()
    try:
        # Ownership is the entry's Exec target.  The path in Name, Comment or any
        # other field proves nothing: a foreign file can mention this checkout.
        if path.name == GAME_ENTRY_NAME and path.is_symlink():
            return False
        lines = path.read_text(errors="ignore").splitlines()
        if path.name == GAME_ENTRY_NAME and GAME_ENTRY_MARKER not in lines:
            return False
        commands = [line[5:].strip() for line in lines if line.startswith("Exec=")]
    except OSError:
        if path.name == GAME_ENTRY_NAME:
            return False
        # A dangling symlink still counts when it points at this checkout.
        try:
            return os.readlink(path) in markers
        except OSError:
            return False
    # Match the entire command, including the wrappers we generate (Toolbox
    # re-entry and env launch flags).  A script path passed as an argument to
    # an unrelated program proves no ownership.
    for command in commands:
        if unwrap_exec(command)[2] in variants:
            return True
    return False


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


def main(toolbox: str | None = None, window_entries: bool = False,
         name: str = "Darling Launcher", env=(), icon: str | None = None):
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
    # Terminal stays false here because this entry runs launch-shortcut.sh,
    # which saves session output in build/desktop-launch.log; the packaged
    # entry runs the session directly and therefore needs a terminal.
    exec_cmd = exec_command(LAUNCH_SHORTCUT_SCRIPT, toolbox, env)
    project_path = escape_string(str(PROJECT_ROOT))
    keywords = "Darling;Launcher;Compatibility;"
    if window_entries:
        keywords += WINDOW_ENTRY_KEYWORDS
    # A custom icon is written as an absolute path (allowed by the Desktop
    # Entry specification); otherwise the neutral theme icon installed above.
    icon_value = escape_string(icon) if icon else "darling-launcher"
    desktop_content = f"""[Desktop Entry]
Version=1.0
Type=Application
Name={escape_string(name)}
GenericName=Game
Comment=Unofficial launcher for the macOS game client via Darling
Exec={exec_cmd}
Path={project_path}
Icon={icon_value}
Terminal=false
Categories=Game;
Keywords={keywords}
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

    # Window-association entries.  Hidden from menus (NoDisplay) and never put on
    # the desktop: they only let the shell name and icon the client windows.
    # Without the opt-in, remove only entries owned by this checkout.
    window_write_failed = False
    for file_name, display_name, wm_class in WINDOW_ENTRIES:
        window_file = apps_base / file_name
        if not window_entries:
            if remove_legacy_entry(window_file):
                print(f"Removed window entry: {window_file}")
            continue
        if (window_file.is_symlink() or window_file.exists()) and not legacy_entry_owned(window_file):
            print(f"Window entry {window_file} already exists and was not created by this "
                  "project; leaving it untouched.", file=sys.stderr)
            continue
        marker = f"{GAME_ENTRY_MARKER}\n" if file_name == GAME_ENTRY_NAME else ""
        try:
            # Replace an owned link itself.
            if window_file.is_symlink():
                window_file.unlink()
            window_file.write_text(f"""[Desktop Entry]
Version=1.0
Type=Application
Name={display_name}
GenericName=Game
Comment=Window entry for the client started by the Darling Launcher
Exec={exec_cmd}
Path={project_path}
Icon={icon_value}
Terminal=false
NoDisplay=true
Categories=Game;
StartupWMClass={wm_class}
StartupNotify=false
{marker}""")
            os.chmod(window_file, 0o755)
        except OSError as error:
            print(f"Could not write window entry {window_file}: {error}", file=sys.stderr)
            window_write_failed = True
            continue
        print(f"Created window entry: {window_file}")

    # Link handler for sign-in: Google and other social sign-in finish in the host
    # browser at a riotclient:// URL, which open-guest-url.sh hands to the client
    # running in Darling. Hidden from menus and never placed on the desktop.
    # Declaring the MimeType makes it the handler only while no other app claims
    # the scheme; the user's explicit choices in mimeapps.list are left alone.
    # In a Toolbox install open-guest-url.sh needs `darling`, which only exists
    # inside the container, so the handler re-enters it like the launcher does.
    handler_exec = exec_command(URL_HANDLER_SCRIPT, toolbox)
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
    refresh_databases(apps_base, icons_base)

    if window_write_failed:
        return 1
    print("GNOME application launcher and icon installed successfully.")
    return 0


def refresh_databases(apps_base: Path, icons_base: Path) -> None:
    update_desktop = shutil.which("update-desktop-database")
    if update_desktop:
        subprocess.run([update_desktop, str(apps_base)], check=False)

    update_icons = shutil.which("gtk-update-icon-cache")
    if update_icons:
        subprocess.run([update_icons, "-f", "-t", str(icons_base / "hicolor")], check=False)


def remove(remove_url_handler: bool = False) -> None:
    """Remove the shortcuts this project created; foreign files stay untouched.

    The launcher shortcut, its desktop copies and the window entries go; the
    ``riotclient://`` link handler only goes with ``remove_url_handler``, and
    the neutral icons always stay.  Nothing is removed when nothing is ours.
    """
    apps_base = HOST_HOME / ".local/share/applications"
    targets = [("application launcher", apps_base / "darling-launcher.desktop")]
    for directory in shortcut_directories():
        targets.append(("desktop shortcut", directory / "darling-launcher.desktop"))
    for file_name, _display_name, _wm_class in WINDOW_ENTRIES:
        targets.append(("window entry", apps_base / file_name))
    if remove_url_handler:
        targets.append(("link handler", apps_base / "darling-url-handler.desktop"))

    removed = 0
    for label, path in targets:
        if remove_legacy_entry(path):
            print(f"Removed {label}: {path}")
            removed += 1

    if not removed:
        print("No shortcuts created by this project were found.")
        return

    refresh_databases(apps_base, HOST_HOME / ".local/share/icons")
    print(f"Removed {removed} shortcut file(s); files this project did not create stay untouched.")


def show() -> None:
    """Print the installed launcher shortcut's settings as KEY=VALUE lines.

    create-shortcuts.sh parses this to pre-fill its prompts.  ``owned=no``
    means the entry is missing or was not created by this project, so every
    consumer must fall back to its defaults.
    """
    entry = HOST_HOME / ".local/share/applications/darling-launcher.desktop"
    if not (entry.is_symlink() or entry.exists()):
        print("owned=no")
        return
    try:
        lines = entry.read_text(errors="ignore").splitlines()
    except OSError:
        print("owned=no")
        return
    commands = [line[5:].strip() for line in lines if line.startswith("Exec=")]
    owned = bool(commands) and unwrap_exec(commands[0])[2] in exec_marker_variants()
    print(f"owned={'yes' if owned else 'no'}")
    print("name=" + unescape_string(
        next((line[5:] for line in lines if line.startswith("Name=")), "")))
    if not owned:
        return
    toolbox, env, _ = unwrap_exec(commands[0])
    print("icon=" + unescape_string(
        next((line[5:] for line in lines if line.startswith("Icon=")), "")))
    print(f"toolbox={toolbox or ''}")
    for item in env:
        print(f"env={item}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--toolbox", metavar="CONTAINER",
        help="launch the entry through 'toolbox run -c CONTAINER'",
    )
    parser.add_argument(
        "--window-entries", action="store_true",
        help="also create hidden entries that name and icon the client windows "
             "(or set DARLING_LOL_WINDOW_ENTRIES=1)",
    )
    parser.add_argument(
        "--name", default="Darling Launcher", metavar="NAME",
        help="display name of the launcher shortcut (default: Darling Launcher)",
    )
    parser.add_argument(
        "--env", action="append", default=[], metavar="VAR=VALUE",
        help="launch flag written into Exec as an env NAME=VALUE assignment; repeatable",
    )
    parser.add_argument(
        "--icon", metavar="PATH",
        help="custom icon file written into Icon= as an absolute path; "
             "create-shortcuts.sh --icon resolves it to a custom-icon.* file "
             "in the project root",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="print the installed launcher shortcut's settings (owned/name/toolbox/env) "
             "and exit",
    )
    parser.add_argument(
        "--remove", action="store_true",
        help="remove the launcher shortcut, its desktop copies and the window entries "
             "this project created; foreign files stay untouched",
    )
    parser.add_argument(
        "--remove-all", action="store_true",
        help="with --remove, also remove the riotclient:// sign-in link handler",
    )
    arguments = parser.parse_args()
    for item in arguments.env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", item, re.DOTALL):
            parser.error(f"--env expects NAME=VALUE, got {item!r}")
    icon = None
    if arguments.icon is not None:
        icon_path = Path(arguments.icon).expanduser()
        if not icon_path.is_file():
            parser.error(f"--icon expects an existing icon file, got {arguments.icon!r}")
        icon = str(icon_path.resolve())
    # The environment only supplies the default for creation: keeping it out of
    # the parsed flag lets --show/--remove stay usable with it exported.
    window_entries = (arguments.window_entries
                      or os.environ.get("DARLING_LOL_WINDOW_ENTRIES") == "1")
    if arguments.show:
        if (arguments.toolbox is not None or arguments.window_entries
                or arguments.remove or arguments.remove_all or arguments.env
                or arguments.icon is not None
                or arguments.name != "Darling Launcher"):
            parser.error("--show takes no other options")
        show()
    elif arguments.remove:
        if arguments.toolbox is not None or arguments.window_entries or arguments.env \
                or arguments.icon is not None or arguments.name != "Darling Launcher":
            parser.error("--remove takes no creation options")
        remove(arguments.remove_all)
    else:
        if arguments.remove_all:
            parser.error("--remove-all is only valid together with --remove")
        sys.exit(main(arguments.toolbox, window_entries, arguments.name, arguments.env, icon))
