"""exec-guest.py: inherited descriptors and the opt-in host environment filter."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'tools/exec-guest.py'
spec = importlib.util.spec_from_file_location('exec_guest', HELPER)
exec_guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exec_guest)

# What the guest and the host libraries Darling loads still need.
REQUIRED = {
    'DISPLAY': ':0', 'XAUTHORITY': '/run/user/1000/.mutter-Xwaylandauth.A', 'XCURSOR_SIZE': '24',
    'XDG_RUNTIME_DIR': '/run/user/1000', 'XDG_DATA_DIRS': '/usr/share', 'XDG_CONFIG_HOME': '/home/u/.config',
    'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus', 'PULSE_SERVER': 'unix:/run/pulse',
    'PIPEWIRE_LATENCY': '128/48000', 'ALSA_CONFIG_PATH': '/usr/share/alsa/alsa.conf',
    'DRI_PRIME': '1', 'MESA_NO_ERROR': '1', 'mesa_glthread': 'false', '__GL_THREADED_OPTIMIZATIONS': '0',
    '__NV_PRIME_RENDER_OFFLOAD': '1', '__EGL_VENDOR_LIBRARY_FILENAMES': '/usr/share/glvnd/egl_vendor.d/10_nvidia.json',
    'VK_ICD_FILENAMES': '/usr/share/vulkan/icd.d/nvidia_icd.json', 'LIBGL_ALWAYS_SOFTWARE': '0',
    'LD_PRELOAD': 'libgamemodeauto.so.0', 'LD_LIBRARY_PATH': '/run/opengl-driver/lib',
    'HOME': '/home/u', 'USER': 'u', 'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8',
    'DPREFIX': '/home/u/.darling', 'RCLOL_REFRESH_HZ': '144', 'DYLD_LIBRARY_PATH': '/Users/u/RuntimeLibraries',
}
SESSION = {
    'TERM': 'xterm', 'STARSHIP_SESSION_KEY': 'host-prompt', 'AWS_PROFILE': 'host-profile',
    'GIT_CONFIG_GLOBAL': '/home/u/.gitconfig', 'EDITOR': '/bin/host-editor',
    'WAYLAND_DISPLAY': 'wayland-0', 'XDG_CURRENT_DESKTOP': 'GNOME', 'XDG_SESSION_TYPE': 'wayland',
    'XDG_SESSION_ID': '2', 'XDG_SEAT': 'seat0', 'DESKTOP_SESSION': 'gnome', 'GDMSESSION': 'gnome',
    'GDM_LANG': 'en_US.UTF-8', 'SESSION_MANAGER': 'local/host:@/tmp/.ICE-unix/1', 'GNOME_SETUP_DISPLAY': ':1',
    'KDE_FULL_SESSION': 'true', 'SSH_AUTH_SOCK': '/run/user/1000/ssh', 'SYSTEMD_EXEC_PID': '1',
    'INVOCATION_ID': 'abc', 'JOURNAL_STREAM': '8:1', 'MANAGERPID': '1', 'NOTIFY_SOCKET': '/run/notify',
    'LISTEN_FDS': '1', 'MEMORY_PRESSURE_WATCH': '/sys/fs/cgroup', 'QT_QPA_PLATFORM': 'wayland',
    'GTK_MODULES': 'canberra', 'GDK_BACKEND': 'wayland', 'GIO_LAUNCHED_DESKTOP_FILE': '/x.desktop',
    'FLATPAK_ID': 'x', 'SNAP': '/snap/x', 'APPIMAGE': '/x.AppImage', 'VIRTUAL_ENV': '/home/u/.venv',
    'COLORTERM': 'truecolor', 'VTE_VERSION': '7600', 'WINDOWID': '1', 'TERM_PROGRAM': 'vscode',
    'LS_COLORS': 'di=01;34', 'SHLVL': '2', 'BASH_FUNC_f%%': '() { :; }',
}


class FilterTests(unittest.TestCase):
    def test_strips_session_metadata_and_keeps_runtime_variables(self):
        environ = {**REQUIRED, **SESSION, 'SHELL': '/bin/fish'}
        removed = exec_guest.filter_host_environment(environ)
        self.assertEqual(set(removed), set(SESSION))
        self.assertEqual(environ, {**REQUIRED, 'SHELL': '/bin/zsh', 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'})

    def test_explicit_keep_preserves_host_path_and_terminal(self):
        environ = {'PATH': '/custom/bin', 'TERM': 'xterm',
                   'RCLOL_KEEP_HOST_ENV': 'PATH TERM', 'RCLOL_FILTER_HOST_ENV': '1'}
        self.assertEqual(exec_guest.filter_host_environment(environ), [])
        self.assertEqual(environ, {'PATH': '/custom/bin', 'TERM': 'xterm', 'SHELL': '/bin/zsh'})

    def test_keep_list(self):
        environ = {'WAYLAND_DISPLAY': 'wayland-0', 'SSH_AUTH_SOCK': '/s', 'SHELL': '/bin/fish',
                   'RCLOL_KEEP_HOST_ENV': 'WAYLAND_DISPLAY, SHELL'}
        self.assertEqual(exec_guest.filter_host_environment(environ), ['SSH_AUTH_SOCK'])
        self.assertEqual(environ['WAYLAND_DISPLAY'], 'wayland-0')
        self.assertEqual(environ['SHELL'], '/bin/fish')
        self.assertNotIn('RCLOL_KEEP_HOST_ENV', environ)


class ExecTests(unittest.TestCase):
    DUMP = ('import json, os; print(json.dumps({"env": dict(os.environ), "fds": sorted('
            'fd for fd in map(int, os.listdir("/proc/self/fd")) if fd < 3 or os.path.exists("/proc/self/fd/%d" % fd))}))')

    def run_helper(self, environ):
        leaked = list(os.pipe())
        leaked.append(os.dup2(leaked[0], 41, inheritable=False))
        try:
            result = subprocess.run([sys.executable, str(HELPER), sys.executable, '-c', self.DUMP],
                                    env=environ, pass_fds=leaked, capture_output=True, text=True, check=True)
        finally:
            for fd in leaked:
                os.close(fd)
        return json.loads(result.stdout)

    def test_closes_descriptors_and_keeps_environment_by_default(self):
        environ = {**REQUIRED, **SESSION, 'SHELL': '/bin/fish'}
        child = self.run_helper(environ)
        self.assertEqual(child['fds'], [0, 1, 2])
        for name, value in environ.items():
            self.assertEqual(child['env'].get(name), value, name)

    def test_opt_in_filter(self):
        child = self.run_helper({**REQUIRED, **SESSION, 'SHELL': '/bin/fish', 'RCLOL_FILTER_HOST_ENV': '1'})
        self.assertEqual(child['fds'], [0, 1, 2])
        self.assertFalse(set(SESSION) & set(child['env']))
        for name, value in REQUIRED.items():
            expected = '/usr/bin:/bin:/usr/sbin:/sbin' if name == 'PATH' else value
            self.assertEqual(child['env'].get(name), expected, name)
        self.assertEqual(child['env']['SHELL'], '/bin/zsh')
        self.assertNotIn('RCLOL_FILTER_HOST_ENV', child['env'])
        self.assertNotIn('RCLOL_KEEP_HOST_ENV', child['env'])


if __name__ == '__main__':
    unittest.main()
