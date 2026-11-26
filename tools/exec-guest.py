#!/usr/bin/env python3
"""Run a command without the host file descriptors this launcher inherited (anything above stderr).

It also gives the guest the session environment a macOS app starts with: host desktop-session,
container and shell variables are dropped (see host_session_variable) and launchd's COMMAND_MODE,
XPC_FLAGS, XPC_SERVICE_NAME and __CF_USER_TEXT_ENCODING are added. RCLOL_FILTER_HOST_ENV=0 turns
this off; RCLOL_KEEP_HOST_ENV lists names to keep anyway, separated by spaces or commas.
"""
import os
import sys

# Desktop-session, service-manager, terminal and packaging metadata that nothing in the guest reads.
# Everything Darling and the host libraries it loads need is kept: DISPLAY, XAUTHORITY, XCURSOR_*,
# XDG_RUNTIME_DIR, XDG_DATA_DIRS and the other XDG base directories (the Vulkan loader searches
# them), DBUS_SESSION_BUS_ADDRESS, PULSE_*, PIPEWIRE_*, ALSA_*, DRI_PRIME, MESA_*, mesa_glthread,
# __GL_*, __NV_*, __EGL_*, VK_*, LIBGL_*, LD_PRELOAD (gamemoderun) and LD_LIBRARY_PATH.
SESSION_PREFIXES = (
    'XDG_SESSION_', 'XDG_SEAT', 'XDG_VTNR', 'XDG_CURRENT_DESKTOP', 'XDG_MENU_PREFIX',
    'GNOME_', 'KDE_', 'GDM', 'SSH_', 'SYSTEMD_', 'MEMORY_PRESSURE_', 'QT_', 'GTK_', 'GDK_',
    'GIO_', 'GSETTINGS_', 'GJS_', 'FLATPAK_', 'SNAP_', 'KONSOLE_', 'TERM_PROGRAM', 'BASH_FUNC_',
    # Host shell, prompt and developer-tool metadata. None of it exists on a Mac and
    # several entries name Linux paths, so the guest must not inherit them.
    'STARSHIP_', 'ILLOGICAL_IMPULSE_', 'INIR_', 'PNPM_', 'YARN_', 'npm_config_', 'PIP_',
    'CARGO_', 'GIT_', 'LESS_TERMCAP_', '_P9K_', 'VSSCRIPT_', 'ORCA_', 'CLAUDE', 'TF_',
    'CUDA_', 'COMPOSER_', 'DEBIAN_', 'FZF_', 'BAT_', 'MYSQL_', 'AWS_', 'LESS',
    'CLOUDSDK_', 'GH_', 'GLAB_', 'HOMEBREW_', 'ELECTRON_', 'DELTA_', 'HIST', 'GPG_',
    # Container and host toolchain metadata (distrobox/toolbox/podman, language SDKs).
    'DISTROBOX_', 'TOOLBOX_', 'BUN_', 'GOPATH', 'SGX_', 'LIBVIRT_', 'ZEITGEIST_',
)
SESSION_NAMES = {
    'WAYLAND_DISPLAY', 'DESKTOP_SESSION', 'DESKTOP_STARTUP_ID', 'SESSION_MANAGER',
    'INVOCATION_ID', 'JOURNAL_STREAM', 'MANAGERPID', 'MANAGERPIDFDID', 'NOTIFY_SOCKET',
    'LISTEN_FDS', 'LISTEN_PID', 'LISTEN_FDNAMES', 'COLORTERM', 'VTE_VERSION', 'WINDOWID',
    'WINDOWPATH', 'CHROME_DESKTOP', 'NO_AT_BRIDGE', 'MOTD_SHOWN', 'SNAP', 'APPIMAGE', 'APPDIR',
    'OWD', 'VIRTUAL_ENV', 'DEBUGINFOD_URLS', 'LS_COLORS', 'COLORFGBG', 'OLDPWD', 'SHLVL',
    'AGENT', 'NO_COLOR', 'TERM', 'VISUAL', 'PROMPT_COMMAND', 'HISTFILE', 'HISTSIZE',
    'SAVEHIST', 'HISTORY_IGNORE', 'PYTHONUNBUFFERED', 'LINES', 'COLUMNS', '_',
    'CI', 'EDITOR', 'PAGER', 'GIT_PAGER', 'SYSTEMD_PAGER', 'MANPAGER',
    'container', 'CONTAINER_ID', 'HOSTNAME', 'INFOPATH', 'MANPATH', 'PKG_CONFIG_PATH',
    'TERMINFO_DIRS', 'USERNAME', 'PWD', 'NoDefaultCurrentDirectoryInExePath',
    # Host-side path of the guest home, read only by the host install tools.
    'RIOT_GUEST_HOME',
}
# What launchd gives every GUI application on macOS 15.5 (measured: COMMAND_MODE=unix2003,
# XPC_FLAGS=0x0, XPC_SERVICE_NAME=0, __CF_USER_TEXT_ENCODING=0x<uid>:0x0:0x0).
LAUNCHD_ENVIRONMENT = {'COMMAND_MODE': 'unix2003', 'XPC_FLAGS': '0x0', 'XPC_SERVICE_NAME': '0'}
GUEST_SHELL = '/bin/zsh'


def host_session_variable(name):
    # The guest locale is LANG/LC_ALL (RCLOL_GUEST_LOCALE); per-category host values are not used.
    if name.startswith('LC_') and name != 'LC_ALL':
        return True
    return name in SESSION_NAMES or name.startswith(SESSION_PREFIXES)


def filter_host_environment(environ):
    """Return the names removed from environ (a mutable mapping), honoring RCLOL_KEEP_HOST_ENV."""
    keep = set(environ.get('RCLOL_KEEP_HOST_ENV', '').replace(',', ' ').split())
    removed = sorted(name for name in environ if host_session_variable(name) and name not in keep)
    for name in removed:
        del environ[name]
    # The host login shell (for example /bin/fish) does not exist in the guest.
    if 'SHELL' not in keep:
        environ['SHELL'] = GUEST_SHELL
    # A host PATH names Linux directories; the guest always has the Mac layout.
    if 'PATH' not in keep:
        environ['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin'
    for name, value in LAUNCHD_ENVIRONMENT.items():
        environ.setdefault(name, value)
    environ.setdefault('__CF_USER_TEXT_ENCODING', '0x%X:0x0:0x0' % os.getuid())
    environ.pop('RCLOL_KEEP_HOST_ENV', None)
    environ.pop('RCLOL_FILTER_HOST_ENV', None)
    return removed


def close_inherited_fds():
    """A terminal, IDE or service manager can leak descriptors; the guest must not inherit them."""
    try:
        names = os.listdir('/proc/self/fd')
    except OSError:
        os.closerange(3, 65536)
        return
    for name in names:
        if name.isdigit() and int(name) > 2:
            try:
                os.close(int(name))
            except OSError:
                pass  # the descriptor listdir used is already closed


def main(argv):
    if len(argv) < 2:
        sys.exit('usage: exec-guest.py COMMAND [ARGUMENT]...')
    if os.environ.get('RCLOL_FILTER_HOST_ENV', '1') != '0':
        filter_host_environment(os.environ)
    else:
        os.environ.pop('RCLOL_FILTER_HOST_ENV', None)
    close_inherited_fds()
    os.execvp(argv[1], argv[1:])


if __name__ == '__main__':
    main(sys.argv)
