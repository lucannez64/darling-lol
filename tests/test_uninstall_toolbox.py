"""Verify scripts/uninstall.sh re-enters a Toolbox install like clean-prefix.sh."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNINSTALL = ROOT / 'scripts/uninstall.sh'
SH = shutil.which('sh')
REEXEC_ARGV = 'run --container {name} env UNINSTALL_REEXEC=1 sh {repo}/scripts/uninstall.sh'

FAKE_TOOLBOX = """#!/bin/sh
# Fake toolbox: records argv, then behaves per the FAKE_TOOLBOX_* knobs.
printf '%s\\n' "$*" >> "${FAKE_TOOLBOX_LOG:?}"
case "${1:-}" in run) shift ;; *) exit 0 ;; esac
case "${1:-}" in
  -c) container=$2; shift 2 ;;
  --container) container=$2; shift 2 ;;
  *) exit 0 ;;
esac
case "${1:-}" in
  true) [ "${FAKE_TOOLBOX_CONTAINER_OK:-0}" = 1 ] || exit 1 ;;
  sh) [ "${FAKE_TOOLBOX_HAS_DARLING:-0}" = 1 ] || exit 1 ;;
esac
exit 0
"""


class UninstallToolboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Path('/run/.toolboxenv').exists():
            raise unittest.SkipTest('test host runs inside a Toolbox container')
        if SH is None:
            raise unittest.SkipTest('sh is unavailable')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='uninstall-toolbox-')
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        # A space in the checkout path must survive the re-exec.
        self.repo = folder / 'my repo'
        self.home = folder / 'home'
        self.bindir = folder / 'bin'
        self.log = folder / 'toolbox.log'
        (self.repo / 'scripts').mkdir(parents=True)
        self.home.mkdir()
        self.bindir.mkdir()
        shutil.copy2(UNINSTALL, self.repo / 'scripts/uninstall.sh')
        # The host run must re-exec before configuring; a configure step here
        # would mean the host went on to stop the runtime itself.
        (self.repo / 'scripts/configure.py').write_text('raise SystemExit("configured on host")\n')
        (self.bindir / 'toolbox').write_text(FAKE_TOOLBOX)
        (self.bindir / 'toolbox').chmod(0o755)
        self.env = dict(os.environ)
        self.env.update(HOME=str(self.home), FAKE_TOOLBOX_LOG=str(self.log))
        self.env['PATH'] = f'{self.bindir}:{self.env["PATH"]}'
        for name in ('DARLING_WORKSPACE', 'UNINSTALL_REEXEC', 'DPREFIX',
                     'RIOT_GUEST_HOME', 'UNINSTALL_PROC_ROOT'):
            self.env.pop(name, None)

    def run_uninstall(self, *args, **overrides):
        env = dict(self.env)
        env.update(overrides)
        return subprocess.run(
            [SH, str(self.repo / 'scripts/uninstall.sh'), *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            check=False, env=env, timeout=60,
        )

    def argv_lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def write_build_file(self, name, text=''):
        build = self.repo / 'build'
        build.mkdir(exist_ok=True)
        (build / name).write_text(text)

    def assert_no_reexec(self):
        self.assertFalse(any('--container' in line for line in self.argv_lines()),
                         self.argv_lines())

    def test_host_run_reenters_recorded_container_with_same_args(self):
        self.write_build_file('toolbox-container', 'mycont\n')
        result = self.run_uninstall('--all', '--yes', FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(REEXEC_ARGV.format(name='mycont', repo=self.repo) + ' --all --yes',
                      self.argv_lines())
        self.assertNotIn('configured on host', result.stderr)

    def test_reexec_from_desktop_entry_of_this_checkout(self):
        apps = self.home / '.local/share/applications'
        apps.mkdir(parents=True)
        (apps / 'darling-launcher.desktop').write_text(
            f'[Desktop Entry]\nExec=toolbox run -c deskcont {self.repo}/scripts/desktop-run.sh\n')
        result = self.run_uninstall('--keep-game', FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(REEXEC_ARGV.format(name='deskcont', repo=self.repo) + ' --keep-game',
                      self.argv_lines())

    def test_legacy_checkout_falls_back_to_default_container(self):
        result = self.run_uninstall(FAKE_TOOLBOX_CONTAINER_OK='1', FAKE_TOOLBOX_HAS_DARLING='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(REEXEC_ARGV.format(name='darling-lol', repo=self.repo),
                      self.argv_lines())

    def test_environment_overrides_follow_env(self):
        self.write_build_file('toolbox-container', 'mycont\n')
        result = self.run_uninstall('--yes', DPREFIX='/custom/prefix',
                                    RIOT_GUEST_HOME='/custom/prefix/Users/tester',
                                    UNINSTALL_PROC_ROOT='/custom/proc',
                                    FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(line for line in self.argv_lines() if 'uninstall.sh' in line)
        words = line.split()
        self.assertEqual(words[:4], ['run', '--container', 'mycont', 'env'], line)
        self.assertEqual(sorted(words[4:words.index('sh')]), sorted([
            'UNINSTALL_REEXEC=1', 'DPREFIX=/custom/prefix',
            'RIOT_GUEST_HOME=/custom/prefix/Users/tester', 'UNINSTALL_PROC_ROOT=/custom/proc',
        ]), line)

    def test_native_install_is_never_redirected(self):
        self.write_build_file('native-install')
        self.write_build_file('toolbox-container', 'mycont\n')
        result = self.run_uninstall('--yes', FAKE_TOOLBOX_CONTAINER_OK='1',
                                    FAKE_TOOLBOX_HAS_DARLING='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('configured on host', result.stderr)
        self.assertEqual(self.argv_lines(), [])

    def test_reexec_guard_prevents_loop(self):
        self.write_build_file('toolbox-container', 'mycont\n')
        result = self.run_uninstall('--yes', UNINSTALL_REEXEC='1', FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertIn('configured on host', result.stderr)
        self.assertEqual(self.argv_lines(), [])

    def test_unreachable_container_stops_with_hint(self):
        self.write_build_file('toolbox-container', 'gone\n')
        result = self.run_uninstall('--yes', FAKE_TOOLBOX_CONTAINER_OK='0')
        self.assertEqual(result.returncode, 1)
        self.assertIn('toolbox enter gone', result.stderr)
        self.assertNotIn('configured on host', result.stderr)
        self.assert_no_reexec()

    def test_help_does_not_touch_the_container(self):
        self.write_build_file('toolbox-container', 'mycont\n')
        self.run_uninstall('--help', FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(self.argv_lines(), [])


if __name__ == '__main__':
    unittest.main()
