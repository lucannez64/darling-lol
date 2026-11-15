"""The launcher's host display and GPU setup, run from its own text with fake cookies and a fake DRM tree."""
import importlib.util
import os
from pathlib import Path
import shutil
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
