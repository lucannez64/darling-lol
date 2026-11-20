"""Verify scripts/clean-prefix.sh re-enters a Toolbox install like update.sh."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLEAN = ROOT / 'scripts/clean-prefix.sh'
SH = shutil.which('sh')
REEXEC_ARGV = 'run --container {name} env CLEAN_PREFIX_REEXEC=1 sh {repo}/scripts/clean-prefix.sh'

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


class CleanPrefixToolboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Path('/run/.toolboxenv').exists():
            raise unittest.SkipTest('test host runs inside a Toolbox container')
        if SH is None:
            raise unittest.SkipTest('sh is unavailable')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='clean-prefix-toolbox-')
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        self.folder = folder
        self.repo = folder / 'repo'
        self.home = folder / 'home'
        self.bindir = folder / 'bin'
        self.log = folder / 'toolbox.log'
        (self.repo / 'scripts').mkdir(parents=True)
        self.home.mkdir()
        self.bindir.mkdir()
        shutil.copy2(CLEAN, self.repo / 'scripts' / 'clean-prefix.sh')
        (self.bindir / 'toolbox').write_text(FAKE_TOOLBOX)
        (self.bindir / 'toolbox').chmod(0o755)
        self.env = dict(os.environ)
        self.env.update(HOME=str(self.home), FAKE_TOOLBOX_LOG=str(self.log))
        self.env['PATH'] = f'{self.bindir}:{self.env["PATH"]}'
        for name in ('DARLING_WORKSPACE', 'CLEAN_PREFIX_REEXEC', 'DPREFIX',
                     'RIOT_GUEST_HOME', 'CLEAN_PREFIX_PROC_ROOT',
                     'CLEAN_PREFIX_RUNTIME_ROOT'):
            self.env.pop(name, None)

    def run_clean(self, *args, cwd=None, **overrides):
        env = dict(self.env)
        env.update(overrides)
        return subprocess.run(
            [SH, str(self.repo / 'scripts' / 'clean-prefix.sh'), *args],
            cwd=cwd, capture_output=True, text=True, check=False, env=env,
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
        script_repo = Path(self.repo if script_repo is None else script_repo)
        apps = self.home / '.local/share/applications'
        apps.mkdir(parents=True, exist_ok=True)
        (apps / 'darling-launcher.desktop').write_text(
            '[Desktop Entry]\n'
            f'Exec=toolbox run -c {container} {script_repo}/scripts/desktop-run.sh\n'
        )

    def assert_reexec_into(self, name, suffix=''):
        self.assertIn(REEXEC_ARGV.format(name=name, repo=self.repo) + suffix,
                      self.argv_lines())

    def assert_no_reexec(self):
        self.assertFalse(any('--container' in line for line in self.argv_lines()),
                         self.argv_lines())

    def test_reexec_from_marker_and_forwards_options(self):
        self.write_marker('mycont')
        result = self.run_clean(
            '--keep-identity', '--keep-game-settings', '--backup-dir', '/tmp/backups', '--yes',
            FAKE_TOOLBOX_CONTAINER_OK='1',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into(
            'mycont', ' --keep-identity --keep-game-settings --backup-dir /tmp/backups --yes')

    def test_relative_backup_dir_is_resolved_before_reexec(self):
        self.write_marker('mycont')
        caller_cwd = self.folder / 'caller cwd'
        caller_cwd.mkdir()
        result = self.run_clean('--backup-dir', 'rel-backups', '--yes',
                                cwd=str(caller_cwd), FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into(
            'mycont', f' --backup-dir {caller_cwd}/rel-backups --yes')

    def test_marker_takes_precedence_over_desktop_entry(self):
        self.write_marker('a')
        self.write_desktop_entry('b')
        result = self.run_clean(FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('a')

    def test_reexec_from_desktop_entry_of_this_checkout(self):
        self.write_desktop_entry('deskcont')
        result = self.run_clean(FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('deskcont')

    def test_ignores_desktop_entry_of_another_checkout(self):
        self.write_desktop_entry('other', script_repo=Path('/outro/checkout'))
        result = self.run_clean()
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_reexec()

    def test_native_install_marker_blocks_all_detection(self):
        self.write_native_marker()
        self.write_marker('mycont')
        self.write_desktop_entry('deskcont')
        result = self.run_clean(FAKE_TOOLBOX_CONTAINER_OK='1', FAKE_TOOLBOX_HAS_DARLING='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.argv_lines(), [])

    def test_legacy_checkout_falls_back_to_default_container(self):
        result = self.run_clean(FAKE_TOOLBOX_CONTAINER_OK='1', FAKE_TOOLBOX_HAS_DARLING='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('darling-lol')

    def test_no_reexec_when_nothing_matches(self):
        result = self.run_clean()
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_reexec()

    def test_container_unreachable_exits_with_hint(self):
        self.write_marker('gone')
        result = self.run_clean(FAKE_TOOLBOX_CONTAINER_OK='0')
        self.assertEqual(result.returncode, 1)
        self.assertIn('that container is not', result.stderr)
        self.assertIn('toolbox enter gone', result.stderr)
        self.assertNotIn('$name', result.stderr)
        self.assert_no_reexec()

    def test_toolbox_missing_gives_distinct_message(self):
        self.write_marker('mycont')
        bindir = tool_sandbox(self.folder, 'cat', 'dirname')
        result = self.run_clean(PATH=str(bindir))
        self.assertEqual(result.returncode, 1)
        self.assertIn('is not installed on this host', result.stderr)
        self.assertIn('toolbox enter mycont', result.stderr)
        self.assertNotIn('$name', result.stderr)
        self.assertEqual(self.argv_lines(), [])

    def test_reexec_guard_prevents_loop(self):
        self.write_marker('mycont')
        result = self.run_clean(CLEAN_PREFIX_REEXEC='1', FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.argv_lines(), [])

    def test_workspace_branch_unaffected(self):
        result = self.run_clean(DARLING_WORKSPACE='/nix/store/abc/share/darling-lol')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.argv_lines(), [])

    def test_help_exits_without_touching_the_container(self):
        result = self.run_clean('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--keep-game-settings', result.stdout)
        self.assertEqual(self.argv_lines(), [])

    def test_environment_overrides_are_forwarded(self):
        self.write_marker('mycont')
        result = self.run_clean(
            '--yes',
            DPREFIX='/custom/prefix',
            RIOT_GUEST_HOME='/custom/prefix/Users/tester',
            CLEAN_PREFIX_PROC_ROOT='/custom/proc',
            CLEAN_PREFIX_RUNTIME_ROOT='/custom/runtime',
            FAKE_TOOLBOX_CONTAINER_OK='1',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(line for line in self.argv_lines() if 'clean-prefix.sh' in line)
        for pair in ('DPREFIX=/custom/prefix',
                     'RIOT_GUEST_HOME=/custom/prefix/Users/tester',
                     'CLEAN_PREFIX_PROC_ROOT=/custom/proc',
                     'CLEAN_PREFIX_RUNTIME_ROOT=/custom/runtime'):
            self.assertIn(pair, line)


if __name__ == '__main__':
    unittest.main()
