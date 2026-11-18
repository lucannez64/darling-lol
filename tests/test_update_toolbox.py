"""Unit coverage for the Toolbox re-exec block in scripts/update.sh.

The real update.sh runs in a temporary repo (only scripts/update.sh is needed:
every re-exec path exits through the fake toolbox, and the remaining paths exit
at the "No existing build" check), with a fake `toolbox` binary first on PATH.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATE = ROOT / 'scripts/update.sh'
SH = shutil.which('sh')
REEXEC_ARGV = 'run --container {name} env DARLING_LOL_UPDATE_REEXEC=1 sh {repo}/scripts/update.sh'

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


def tool_sandbox(folder, *names):
    """Return a PATH dir exposing only the given host tools."""
    bindir = Path(folder) / 'tools'
    bindir.mkdir()
    for name in names:
        resolved = shutil.which(name)
        if resolved is None:
            raise unittest.SkipTest(f'{name} is unavailable')
        os.symlink(resolved, bindir / name)
    return bindir


class UpdateToolboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Path('/run/.toolboxenv').exists():
            raise unittest.SkipTest('test host runs inside a Toolbox container')
        if SH is None:
            raise unittest.SkipTest('sh is unavailable')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='update-toolbox ')
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        self.repo = folder / 'repo'
        self.home = folder / 'home'
        self.bindir = folder / 'bin'
        self.log = folder / 'toolbox.log'
        (self.repo / 'scripts').mkdir(parents=True)
        self.home.mkdir()
        self.bindir.mkdir()
        shutil.copy2(UPDATE, self.repo / 'scripts' / 'update.sh')
        (self.bindir / 'toolbox').write_text(FAKE_TOOLBOX)
        (self.bindir / 'toolbox').chmod(0o755)
        self.env = dict(os.environ)
        self.env.update(HOME=str(self.home), FAKE_TOOLBOX_LOG=str(self.log))
        self.env['PATH'] = f'{self.bindir}:{self.env["PATH"]}'
        self.env.pop('DARLING_WORKSPACE', None)
        self.env.pop('DARLING_LOL_UPDATE_REEXEC', None)

    def run_update(self, **overrides):
        env = dict(self.env)
        env.update(overrides)
        return subprocess.run(
            [SH, str(self.repo / 'scripts' / 'update.sh')],
            capture_output=True, text=True, check=False, env=env,
        )

    def argv_lines(self):
        if not self.log.exists():
            return []
        return self.log.read_text().splitlines()

    def write_marker(self, name):
        build = self.repo / 'build'
        build.mkdir(exist_ok=True)
        (build / 'toolbox-container').write_text(name + '\n')

    def write_native_marker(self):
        build = self.repo / 'build'
        build.mkdir(exist_ok=True)
        (build / 'native-install').write_text('')

    def write_desktop_entry(self, container, script_repo=None):
        """Write the launcher entry as tools/desktop-entry.py would for script_repo."""
        script_repo = Path(self.repo if script_repo is None else script_repo)
        apps = self.home / '.local/share/applications'
        apps.mkdir(parents=True, exist_ok=True)
        (apps / 'darling-launcher.desktop').write_text(
            '[Desktop Entry]\n'
            f'Exec=toolbox run -c {container} {script_repo}/scripts/desktop-run.sh\n'
        )

    def assert_reexec_into(self, name):
        self.assertIn(REEXEC_ARGV.format(name=name, repo=self.repo), self.argv_lines())

    def assert_no_reexec(self):
        self.assertFalse(any('--container' in line for line in self.argv_lines()),
                         self.argv_lines())

    def test_reexec_from_marker(self):
        self.write_marker('mycont')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('mycont')

    def test_marker_takes_precedence_over_desktop_entry(self):
        self.write_marker('a')
        self.write_desktop_entry('b')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('a')

    def test_reexec_from_desktop_entry_of_this_checkout(self):
        self.write_desktop_entry('deskcont')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('deskcont')

    def test_ignores_desktop_entry_of_another_checkout(self):
        self.write_desktop_entry('other', script_repo=Path('/outro/checkout'))
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn('No existing build', result.stderr)
        self.assert_no_reexec()

    def test_native_install_marker_blocks_all_detection(self):
        self.write_native_marker()
        self.write_marker('mycont')
        self.write_desktop_entry('deskcont')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1', FAKE_TOOLBOX_HAS_DARLING='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('No existing build', result.stderr)
        self.assertEqual(self.argv_lines(), [])

    def test_legacy_checkout_falls_back_to_default_container(self):
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1', FAKE_TOOLBOX_HAS_DARLING='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('darling-lol')

    def test_no_reexec_when_nothing_matches(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn('No existing build', result.stderr)
        self.assert_no_reexec()

    def test_container_unreachable_exits_1_with_hint(self):
        self.write_marker('gone')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='0')
        self.assertEqual(result.returncode, 1)
        self.assertIn('that container is not', result.stderr)
        self.assertIn('toolbox enter gone', result.stderr)
        self.assert_no_reexec()

    def test_toolbox_missing_gives_distinct_message(self):
        self.write_marker('mycont')
        # PATH must not contain the fake toolbox (nor a real one): only the
        # tools update.sh needs before its first toolbox call.
        bindir = tool_sandbox(Path(self.env['FAKE_TOOLBOX_LOG']).parent,
                              'cat', 'sed', 'grep', 'head', 'dirname')
        result = self.run_update(PATH=str(bindir))
        self.assertEqual(result.returncode, 1)
        self.assertIn('is not installed on this host', result.stderr)
        self.assertIn('toolbox enter mycont', result.stderr)
        self.assertEqual(self.argv_lines(), [])

    def test_reexec_guard_prevents_loop(self):
        self.write_marker('mycont')
        result = self.run_update(DARLING_LOL_UPDATE_REEXEC='1', FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('No existing build', result.stderr)
        self.assertEqual(self.argv_lines(), [])

    def test_workspace_branch_unaffected(self):
        result = self.run_update(DARLING_WORKSPACE='/nix/store/abc/share/darling-lol')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.argv_lines(), [])


if __name__ == '__main__':
    unittest.main()
