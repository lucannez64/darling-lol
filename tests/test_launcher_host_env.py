"""The launcher's host display and GPU setup, run from its own text with fake cookies and a fake DRM tree."""
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'tools/run-riot-client.sh'
SH = shutil.which('sh')


def launcher_text(start, end):
    """The launcher source from the first `start` up to the next `end`."""
    source = LAUNCHER.read_text()
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


def load_preflight():
    spec = importlib.util.spec_from_file_location('preflight', ROOT / 'scripts/preflight.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HostEnvFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='launcher host env ')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / 'home'
        self.runtime = root / 'runtime dir'
        self.bin = root / 'bin'
        for folder in (self.home, self.runtime, self.bin):
            folder.mkdir()
        # Only these tools are visible, so the host's own xauth or nvidia-smi is never run.
        for tool in ('awk', 'sed', 'cat'):
            (self.bin / tool).symlink_to(shutil.which(tool))

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def run_block(self, block, tail, *arguments, **extra):
        script = Path(self.tmp.name) / 'block.sh'
        script.write_text(block)
        env = {'PATH': str(self.bin), 'HOME': str(self.home)}
        env.update(extra)
        result = subprocess.run([SH, '-euc', '. "$1"; ' + tail, 'sh', str(script), *arguments],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()


class RefreshRate(HostEnvFixture):
    BLOCK = launcher_text('# --- darling-lol perf:', '# Vulkan WSI & Mesa optimizations')

    def refresh(self, **extra):
        return self.run_block(self.BLOCK, SH + ' -c \'printf "%s" "${RCLOL_REFRESH_HZ-unset}"\'', **extra)

    def test_explicit_value_is_exported_without_querying_xrandr(self):
        calls = self.bin / 'xrandr-calls'
        self.stub('xrandr', f': > "{calls}"\nexit 99')
        for value in ('144', '59.94', 'invalid', '0'):
            with self.subTest(value=value):
                self.assertEqual(self.refresh(RCLOL_REFRESH_HZ=value), value)
        self.assertFalse(calls.exists())

    def test_primary_current_mode_is_rounded_and_exported(self):
        self.stub('xrandr', '''[ "$*" = --current ] || exit 99
cat <<'MODES'
DP-1 connected 1920x1080+0+0
   1920x1080 59.94*+ 120.00
DP-2 connected primary 2560x1440+1920+0
   2560x1440 143.98*+ 60.00
MODES''')
        self.assertEqual(self.refresh(), '144')
        self.assertEqual(self.refresh(RCLOL_REFRESH_HZ=''), '144')

    def test_without_primary_the_first_active_mode_wins(self):
        self.stub('xrandr', '''cat <<'MODES'
DP-1 connected 1920x1080+0+0
   1920x1080 59.94*+ 120.00
DP-2 connected 2560x1440+1920+0
   2560x1440 143.98*+ 60.00
MODES''')
        self.assertEqual(self.refresh(), '60')

    def test_missing_failed_or_empty_xrandr_leaves_the_rate_unset(self):
        self.assertEqual(self.refresh(), 'unset')
        self.stub('xrandr', 'exit 1')
        self.assertEqual(self.refresh(), 'unset')
        self.stub('xrandr', 'echo "DP-1 disconnected"')
        self.assertEqual(self.refresh(), 'unset')


class WaylandGameWindow(HostEnvFixture):
    """RCLOL_WAYLAND plumbing: the launcher's own block, then its guest `env` assignments."""
    BLOCK = launcher_text('wayland_socket_path() {', '# The client saves its locale')
    ASSIGNMENTS = launcher_text('  XMODIFIERS=', '  "$darling_bin" "$A"')
    GUEST_PATH = '/.darling/guest'
    SEEN = 'printf "%s\\n" "${RCLOL_WAYLAND_SOCKET-unset}" "${DARLING_WAYLAND_LIBRARY-unset}" ' \
           '"${RCLOL_WAYLAND_ACTIVATION_TOKEN-unset}" "${RCLOL_WAYLAND_SCOPE-unset}"'
    OFF = ['unset', 'unset', 'unset', 'unset']

    def setUp(self):
        super().setUp()
        self.env_tool = shutil.which('env')

    def listen(self, path):
        """A real AF_UNIX socket file at path; the file stays after the server closes."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        return str(path)

    def launch(self, **env):
        """Run the block and the guest env list; return (guest values, stderr)."""
        script = Path(self.tmp.name) / 'wayland launch.sh'
        script.write_text(
            'set -eu\nguest_path=' + self.GUEST_PATH + '\n' + self.BLOCK +
            self.env_tool + ' \\\n' + self.ASSIGNMENTS +
            '  ' + SH + " -c '" + self.SEEN + "'\n")
        base = {'PATH': str(self.bin), 'HOME': str(self.home)}
        base.update(env)
        result = subprocess.run([SH, str(script)], env=base, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines(), result.stderr

    def test_unset_flag_passes_nothing_even_with_a_socket(self):
        self.listen(self.runtime / 'wayland-1')
        values, stderr = self.launch(WAYLAND_DISPLAY='wayland-1', XDG_RUNTIME_DIR=str(self.runtime))
        self.assertEqual(values, self.OFF)
        self.assertEqual(stderr, '')

    def test_inherited_guest_values_do_not_survive_when_the_flag_is_off(self):
        values, _ = self.launch(RCLOL_WAYLAND='0', RCLOL_WAYLAND_SOCKET='/stale', XDG_RUNTIME_DIR=str(self.runtime),
                                RCLOL_WAYLAND_ACTIVATION_TOKEN='stale', XDG_ACTIVATION_TOKEN='fresh')
        self.assertEqual(values, self.OFF)

    def test_flag_on_passes_the_host_socket_library_and_token(self):
        socket_path = self.listen(self.runtime / 'wayland-1')
        values, stderr = self.launch(RCLOL_WAYLAND='game', WAYLAND_DISPLAY='wayland-1',
                                     XDG_RUNTIME_DIR=str(self.runtime), XDG_ACTIVATION_TOKEN='token-1')
        self.assertEqual(values, [socket_path, self.GUEST_PATH + '/libDarlingWayland.dylib', 'token-1', 'game'])
        self.assertEqual(stderr, 'wayland: experimental native Wayland for the game window '
                                 '(socket %s)\n' % socket_path)
        self.assertNotIn('token-1', stderr)

    def test_flag_one_passes_scope_all_and_the_game_and_client_line(self):
        socket_path = self.listen(self.runtime / 'wayland-1')
        values, stderr = self.launch(RCLOL_WAYLAND='1', WAYLAND_DISPLAY='wayland-1',
                                     XDG_RUNTIME_DIR=str(self.runtime), XDG_ACTIVATION_TOKEN='token-1')
        self.assertEqual(values, [socket_path, self.GUEST_PATH + '/libDarlingWayland.dylib', 'token-1', 'all'])
        self.assertEqual(stderr, 'wayland: experimental native Wayland for the game and client windows '
                                 '(socket %s)\n' % socket_path +
                         'wayland: the client-window idle stop cannot see Wayland windows; it is off for this session\n')
        self.assertNotIn('token-1', stderr)

    def idle_stop_seconds(self, **env):
        """Run only the launcher block; return RCLOL_STOP_AFTER_CLIENT_SECONDS as it leaves the block."""
        script = Path(self.tmp.name) / 'wayland idle.sh'
        script.write_text('set -eu\nguest_path=' + self.GUEST_PATH + '\n' + self.BLOCK +
                          'printf "%s\\n" "${RCLOL_STOP_AFTER_CLIENT_SECONDS-unset}"\n')
        base = {'PATH': str(self.bin), 'HOME': str(self.home), 'XDG_RUNTIME_DIR': str(self.runtime),
                'WAYLAND_DISPLAY': 'wayland-1'}
        base.update(env)
        result = subprocess.run([SH, str(script)], env=base, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_client_scope_turns_off_the_x11_window_idle_stop(self):
        self.listen(self.runtime / 'wayland-1')
        self.assertEqual(self.idle_stop_seconds(RCLOL_WAYLAND='1'), '0')
        self.assertEqual(self.idle_stop_seconds(RCLOL_WAYLAND='1', RCLOL_STOP_AFTER_CLIENT_SECONDS='90'), '0')
        # The game scope keeps client windows on X11, where the supervisor can see them.
        self.assertEqual(self.idle_stop_seconds(RCLOL_WAYLAND='game'), 'unset')
        self.assertEqual(self.idle_stop_seconds(RCLOL_WAYLAND='game', RCLOL_STOP_AFTER_CLIENT_SECONDS='90'), '90')
        self.assertEqual(self.idle_stop_seconds(RCLOL_WAYLAND='0'), 'unset')

    def test_flag_game_passes_scope_game_with_the_game_only_line(self):
        socket_path = self.listen(self.runtime / 'wayland-1')
        values, stderr = self.launch(RCLOL_WAYLAND='game', WAYLAND_DISPLAY='wayland-1',
                                     XDG_RUNTIME_DIR=str(self.runtime))
        self.assertEqual(values[0], socket_path)
        self.assertEqual(values[3], 'game')
        self.assertEqual(stderr, 'wayland: experimental native Wayland for the game window '
                                 '(socket %s)\n' % socket_path)

    def test_scope_is_removed_when_off_or_without_a_socket(self):
        self.listen(self.runtime / 'wayland-1')
        inherited = {'RCLOL_WAYLAND_SCOPE': 'all', 'XDG_RUNTIME_DIR': str(self.runtime),
                     'WAYLAND_DISPLAY': 'wayland-1'}
        values, stderr = self.launch(RCLOL_WAYLAND='0', **inherited)
        self.assertEqual((values, stderr), (self.OFF, ''))
        values, stderr = self.launch(**inherited)
        self.assertEqual((values, stderr), (self.OFF, ''))
        values, _ = self.launch(RCLOL_WAYLAND='game', RCLOL_WAYLAND_SCOPE='all', XDG_RUNTIME_DIR=str(self.runtime),
                                WAYLAND_DISPLAY='wayland-missing')
        self.assertEqual(values, self.OFF)
        values, _ = self.launch(RCLOL_WAYLAND='1', RCLOL_WAYLAND_SCOPE='game', XDG_RUNTIME_DIR=str(self.runtime),
                                WAYLAND_DISPLAY='wayland-missing')
        self.assertEqual(values, self.OFF)

    def test_game_without_a_socket_warns_and_passes_nothing(self):
        path = self.runtime / 'wayland-1'
        values, stderr = self.launch(RCLOL_WAYLAND='game', WAYLAND_DISPLAY='wayland-1',
                                     XDG_RUNTIME_DIR=str(self.runtime))
        self.assertEqual(values, self.OFF)
        self.assertEqual(stderr, 'wayland: RCLOL_WAYLAND=game but no Wayland socket at %s; '
                                 'the game stays on X11\n' % path)

    def test_absolute_display_is_used_as_it_is_and_the_default_name_is_wayland_0(self):
        absolute = self.listen(Path(self.tmp.name) / 'compositor dir' / 'wl')
        values, _ = self.launch(RCLOL_WAYLAND='1', WAYLAND_DISPLAY=absolute, XDG_RUNTIME_DIR=str(self.runtime))
        self.assertEqual(values[0], absolute)
        default = self.listen(self.runtime / 'wayland-0')
        values, _ = self.launch(RCLOL_WAYLAND='1', XDG_RUNTIME_DIR=str(self.runtime))
        self.assertEqual(values[0], default)

    def test_missing_or_non_socket_path_warns_and_passes_nothing(self):
        path = self.runtime / 'wayland-1'
        expected = ('wayland: RCLOL_WAYLAND=1 but no Wayland socket at %s; the game stays on X11\n' % path)
        for setup in (lambda: None, lambda: path.write_text('')):
            setup()
            values, stderr = self.launch(RCLOL_WAYLAND='1', WAYLAND_DISPLAY='wayland-1',
                                         XDG_RUNTIME_DIR=str(self.runtime), XDG_ACTIVATION_TOKEN='token-1')
            self.assertEqual(values, self.OFF)
            self.assertEqual(stderr, expected)
            path.unlink(missing_ok=True)

    def test_other_values_warn_and_mean_off(self):
        self.listen(self.runtime / 'wayland-1')
        values, stderr = self.launch(RCLOL_WAYLAND='yes', WAYLAND_DISPLAY='wayland-1',
                                     XDG_RUNTIME_DIR=str(self.runtime))
        self.assertEqual(values, self.OFF)
        self.assertIn('RCLOL_WAYLAND=yes is not 0, 1 or game; treating it as 0', stderr)
        values, stderr = self.launch(RCLOL_WAYLAND='Game', WAYLAND_DISPLAY='wayland-1',
                                     XDG_RUNTIME_DIR=str(self.runtime))
        self.assertEqual(values, self.OFF)
        self.assertIn('RCLOL_WAYLAND=Game is not 0, 1 or game; treating it as 0', stderr)
        values, stderr = self.launch(RCLOL_WAYLAND='', WAYLAND_DISPLAY='wayland-1',
                                     XDG_RUNTIME_DIR=str(self.runtime))
        self.assertEqual((values, stderr), (self.OFF, ''))

    def test_library_path_can_be_overridden(self):
        self.listen(self.runtime / 'wayland-1')
        values, _ = self.launch(RCLOL_WAYLAND='1', WAYLAND_DISPLAY='wayland-1', XDG_RUNTIME_DIR=str(self.runtime),
                                DARLING_WAYLAND_LIBRARY='/opt/custom/libDarlingWayland.dylib')
        self.assertEqual(values[1], '/opt/custom/libDarlingWayland.dylib')

    def test_the_guest_env_list_keeps_the_xshm_library_line(self):
        self.assertIn('DARLING_XSHM_LIBRARY="${DARLING_XSHM_LIBRARY:-$guest_path/libDarlingXShm.dylib}"',
                      self.ASSIGNMENTS)


class XwaylandCookie(HostEnvFixture):
    BLOCK = launcher_text('newest_xwayland_cookie() {', '# Capture ownership before')

    def cookie(self, name, age, entry='host/unix: MIT-MAGIC-COOKIE-1 00'):
        path = self.runtime / name
        path.write_text(entry + '\n')
        os.utime(path, (1_700_000_000 - age, 1_700_000_000 - age))
        return str(path)

    def exported(self, **extra):
        env = {'DISPLAY': ':0', 'XDG_RUNTIME_DIR': str(self.runtime)}
        env.update(extra)
        return self.run_block(self.BLOCK, 'printf "%s\\n" "${XAUTHORITY-unset}"', **env)

    def test_newest_cookie_wins_whatever_its_name_and_preflight_agrees(self):
        self.cookie('.mutter-Xwaylandauth.AAAAAA', age=10)
        newest = self.cookie('.mutter-Xwaylandauth.BBBBBB', age=5)
        self.assertEqual(self.exported(), newest)
        # A stale cookie of an earlier login sorts last and is still not chosen.
        self.cookie('.mutter-Xwaylandauth.ZZZZZZ', age=3600)
        self.cookie('xauth_stale', age=7200)
        self.assertEqual(self.exported(), newest)
        newest = self.cookie('.mutter-Xwaylandauth.AAAAAA', age=1)
        self.assertEqual(self.exported(), newest)
        preflight = load_preflight()
        self.assertEqual(preflight.discover_xauthority({'XDG_RUNTIME_DIR': str(self.runtime)}), newest)

    def test_xauth_drops_cookies_for_another_display(self):
        self.stub('xauth', 'cat "$2"')
        matching = self.cookie('.mutter-Xwaylandauth.AAAAAA', age=10, entry='host/unix:1 MIT-MAGIC-COOKIE-1 00')
        self.cookie('.mutter-Xwaylandauth.BBBBBB', age=5, entry='host/unix:7 MIT-MAGIC-COOKIE-1 00')
        self.assertEqual(self.exported(DISPLAY=':1.0'), matching)
        any_display = self.cookie('xauth_any', age=2)
        self.assertEqual(self.exported(DISPLAY=':1.0'), any_display)

    def test_readable_xauthority_is_kept_and_no_cookie_changes_nothing(self):
        self.assertEqual(self.exported(), 'unset')
        self.assertEqual(self.exported(XAUTHORITY='/nonexistent/cookie'), '/nonexistent/cookie')
        newest = self.cookie('.mutter-Xwaylandauth.AAAAAA', age=5)
        mine = Path(self.tmp.name) / 'my cookie'
        mine.write_text('')
        self.assertEqual(self.exported(XAUTHORITY=str(mine)), str(mine))
        self.assertEqual(self.exported(XAUTHORITY='/nonexistent/cookie'), newest)
        env = {'XDG_RUNTIME_DIR': str(self.runtime)}
        self.assertEqual(self.run_block(self.BLOCK, 'printf "%s\\n" "${XAUTHORITY-unset}"', **env), 'unset')

    def test_home_xauthority_is_never_written(self):
        self.cookie('.mutter-Xwaylandauth.AAAAAA', age=5)
        self.exported()
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertNotIn('.Xauthority', LAUNCHER.read_text().replace('~/.Xauthority', ''))


class NvidiaVram(HostEnvFixture):
    BLOCK = launcher_text('host_gpus_all_nvidia() {', 'if [ "$profile" = compat ]; then')
    FUNCTIONS = launcher_text('host_gpus_all_nvidia() {', 'if [ -z "${RCLOL_HOST_GPU_VRAM_MB:-}" ]; then')

    def setUp(self):
        super().setUp()
        self.calls = Path(self.tmp.name) / 'nvidia-smi calls'
        self.smi('echo 8192')

    def smi(self, body):
        self.stub('nvidia-smi', f'echo "$*" >> "{self.calls}"\n{body}')

    def drm(self, *vendors):
        root = Path(tempfile.mkdtemp(dir=self.tmp.name)) / 'drm'
        for index, vendor in enumerate(vendors):
            device = root / f'card{index}' / 'device'
            device.mkdir(parents=True)
            (device / 'vendor').write_text(vendor + '\n')
        (root / 'card0-DP-1').mkdir(parents=True)
        return str(root)

    def vram(self, *vendors):
        return self.run_block(self.FUNCTIONS, 'host_nvidia_vram_mb "$2"', self.drm(*vendors))

    def test_only_an_all_nvidia_host_runs_nvidia_smi(self):
        for vendors in ((), ('0x1002',), ('0x8086', '0x10de'), ('0x10de', '0x1002')):
            self.assertEqual(self.vram(*vendors), '', vendors)
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.vram('0x10de'), '8192')
        self.assertEqual(self.calls.read_text().count('memory.total'), 1)

    def test_several_gpus_a_failure_or_garbage_export_nothing(self):
        self.smi('echo 8192; echo 8192')
        self.assertEqual(self.vram('0x10de', '0x10de'), '')
        self.smi('echo 8192; exit 9')
        self.assertEqual(self.vram('0x10de'), '')
        self.smi('echo "[N/A]"')
        self.assertEqual(self.vram('0x10de'), '')

    def test_nvidia_smi_runs_under_a_time_limit_when_timeout_exists(self):
        limits = Path(self.tmp.name) / 'timeout calls'
        self.stub('timeout', f'echo "$*" >> "{limits}"\nwhile [ "$1" != nvidia-smi ]; do shift; done\n"$@"')
        self.assertEqual(self.vram('0x10de'), '8192')
        self.assertIn('5 nvidia-smi', limits.read_text())
        # A run that hit the limit ends with status 124 whatever it printed before.
        self.stub('timeout', 'echo 8192; exit 124')
        self.assertEqual(self.vram('0x10de'), '')

    def test_a_user_value_wins_and_this_host_decides_the_export(self):
        tail = 'printf "%s\\n" "${RCLOL_HOST_GPU_VRAM_MB-unset}"'
        self.assertEqual(self.run_block(self.BLOCK, tail, RCLOL_HOST_GPU_VRAM_MB='4096'), '4096')
        self.assertFalse(self.calls.exists())
        vendors = {path.read_text().strip() for path in Path('/sys/class/drm').glob('card*/device/vendor')}
        expected = '8192' if vendors == {'0x10de'} else 'unset'
        self.assertEqual(self.run_block(self.BLOCK, tail), expected)

    def test_the_icd_pin_uses_the_same_condition(self):
        source = LAUNCHER.read_text()
        self.assertEqual(source.count('/device/vendor'), 2)
        pin = source[source.index('NVIDIA Vulkan ICD pin'):source.index('newest_xwayland_cookie() {')]
        self.assertIn('if host_gpus_all_nvidia; then', pin)


if __name__ == '__main__':
    unittest.main()
