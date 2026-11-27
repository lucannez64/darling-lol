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
if [ "${1:-}" = --version ]; then
  if [ "${FAKE_TOOLBOX_BAD_VERSION:-0}" = 1 ]; then
    echo "toolbox: unrecognized option '--version'" >&2
    exit 1
  fi
  printf 'toolbox version 0.0.99.5\\n'
  exit 0
fi
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

FAKE_DISTROBOX = """#!/bin/sh
# Fake distrobox: records argv and DBX_CONTAINER_MANAGER, then behaves per FAKE_TOOLBOX_*.
printf '%s\\n' "${DBX_CONTAINER_MANAGER:-}" >> "${FAKE_DISTROBOX_LOG:?}.manager"
printf '%s\\n' "$*" >> "${FAKE_DISTROBOX_LOG:?}"
case "${1:-}" in enter) shift ;; *) exit 0 ;; esac
while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-tty) shift ;;
    --name) shift 2 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
case "${1:-}" in
  true) [ "${FAKE_TOOLBOX_CONTAINER_OK:-0}" = 1 ] || exit 1 ;;
  sh) [ "${FAKE_TOOLBOX_HAS_DARLING:-0}" = 1 ] || exit 1 ;;
esac
exit 0
"""

FAKE_PODMAN = """#!/bin/sh
case "${1:-}:${2:-}" in
  container:exists) [ "${FAKE_PODMAN_EXISTS:-1}" = 1 ] ;;
  container:inspect)
    [ "${FAKE_PODMAN_EXISTS:-1}" = 1 ] || exit 1
    printf '%s\\n' "${FAKE_PODMAN_MANAGER-distrobox}"
    ;;
  *) exit 1 ;;
esac
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
        self.distrobox_log = folder / 'distrobox.log'
        (self.repo / 'scripts').mkdir(parents=True)
        self.home.mkdir()
        self.bindir.mkdir()
        shutil.copy2(CLEAN, self.repo / 'scripts' / 'clean-prefix.sh')
        (self.bindir / 'toolbox').write_text(FAKE_TOOLBOX)
        (self.bindir / 'toolbox').chmod(0o755)
        (self.bindir / 'distrobox').write_text(FAKE_DISTROBOX)
        (self.bindir / 'distrobox').chmod(0o755)
        (self.bindir / 'podman').write_text(FAKE_PODMAN)
        (self.bindir / 'podman').chmod(0o755)
        self.env = dict(os.environ)
        self.env.update(HOME=str(self.home), FAKE_TOOLBOX_LOG=str(self.log),
                        FAKE_DISTROBOX_LOG=str(self.distrobox_log),
                        DARLING_TOOLBOX_ENV=str(folder / 'missing-toolboxenv'),
                        DARLING_CONTAINER_ENV=str(folder / 'missing-containerenv'))
        self.env['PATH'] = f'{self.bindir}:{self.env["PATH"]}'
        for name in ('DARLING_WORKSPACE', 'CLEAN_PREFIX_REEXEC', 'DPREFIX',
                     'RIOT_GUEST_HOME', 'CLEAN_PREFIX_PROC_ROOT',
                     'CLEAN_PREFIX_RUNTIME_ROOT', 'NO_COLOR', 'CONTAINER_ID'):
            self.env.pop(name, None)

    def run_clean(self, *args, cwd=None, stdin=None, **overrides):
        env = dict(self.env)
        env.update(overrides)
        return subprocess.run(
            [SH, str(self.repo / 'scripts' / 'clean-prefix.sh'), *args],
            cwd=cwd, stdin=stdin, capture_output=True, text=True, check=False, env=env,
        )

    def argv_lines(self):
        if not self.log.exists():
            return []
        return self.log.read_text().splitlines()

    def distrobox_lines(self):
        if not self.distrobox_log.exists():
            return []
        return self.distrobox_log.read_text().splitlines()

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
            NO_COLOR='1',
            FAKE_TOOLBOX_CONTAINER_OK='1',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(line for line in self.argv_lines() if 'clean-prefix.sh' in line)
        words = line.split()
        # `toolbox run` executes its first argument, so the assignments must follow `env`
        self.assertEqual(words[:4], ['run', '--container', 'mycont', 'env'], line)
        assignments = words[4:words.index('sh')]
        self.assertEqual(sorted(assignments), sorted([
            'CLEAN_PREFIX_REEXEC=1',
            'DPREFIX=/custom/prefix',
            'RIOT_GUEST_HOME=/custom/prefix/Users/tester',
            'CLEAN_PREFIX_PROC_ROOT=/custom/proc',
            'CLEAN_PREFIX_RUNTIME_ROOT=/custom/runtime',
            'NO_COLOR=1',
        ]), line)

    def test_distrobox_marker_and_desktop_entry_reexec_into_distrobox(self):
        self.write_marker('my-dbx')
        (self.repo / 'build' / 'toolbox-backend').write_text('distrobox\n')
        result = self.run_clean('--yes', stdin=subprocess.DEVNULL, FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = (
            f'enter --no-tty --name my-dbx -- env CLEAN_PREFIX_REEXEC=1 '
            f'sh {self.repo}/scripts/clean-prefix.sh --yes'
        )
        self.assertIn(expected, self.distrobox_lines())
        self.assertEqual(self.argv_lines(), [])
        self.assertEqual(
            set(Path(f'{self.distrobox_log}.manager').read_text().splitlines()),
            {'podman'},
        )

    def test_missing_or_non_distrobox_container_skips_distrobox_enter(self):
        self.write_marker('my-dbx')
        (self.repo / 'build' / 'toolbox-backend').write_text('distrobox\n')
        missing = self.run_clean(stdin=subprocess.DEVNULL, FAKE_PODMAN_EXISTS='0')
        self.assertEqual(missing.returncode, 1)
        self.assertIn('is not reachable', missing.stderr)
        self.assertEqual(self.distrobox_lines(), [])

        wrong_manager = self.run_clean(stdin=subprocess.DEVNULL, FAKE_PODMAN_MANAGER='')
        self.assertEqual(wrong_manager.returncode, 1)
        self.assertEqual(self.distrobox_lines(), [])

        (self.repo / 'build' / 'toolbox-container').unlink()
        (self.repo / 'build' / 'toolbox-backend').unlink()
        legacy = self.run_clean(
            '--yes', stdin=subprocess.DEVNULL,
            FAKE_TOOLBOX_BAD_VERSION='1',
            FAKE_PODMAN_EXISTS='0',
        )
        self.assertNotEqual(legacy.returncode, 0)
        self.assertEqual(self.distrobox_lines(), [])

    def test_distrobox_desktop_entry_reexecs_without_marker(self):
        apps = self.home / '.local/share/applications'
        apps.mkdir(parents=True, exist_ok=True)
        (apps / 'darling-launcher.desktop').write_text(
            '[Desktop Entry]\n'
            f'Exec=env DBX_CONTAINER_MANAGER=podman distrobox enter --no-tty --name deskdbx -- {self.repo}/scripts/desktop-run.sh\n'
        )
        result = self.run_clean('--yes', stdin=subprocess.DEVNULL, FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            any('enter --no-tty --name deskdbx -- ' in line for line in self.distrobox_lines()),
            self.distrobox_lines(),
        )

    def test_distrobox_missing_and_unreachable_give_distrobox_hints(self):
        self.write_marker('my-dbx')
        (self.repo / 'build' / 'toolbox-backend').write_text('distrobox\n')
        unreachable = self.run_clean(stdin=subprocess.DEVNULL, FAKE_TOOLBOX_CONTAINER_OK='0')
        self.assertEqual(unreachable.returncode, 1)
        self.assertIn('distrobox enter my-dbx', unreachable.stderr)

        bindir = tool_sandbox(self.folder, 'cat', 'dirname')
        missing = self.run_clean(stdin=subprocess.DEVNULL, PATH=str(bindir))
        self.assertEqual(missing.returncode, 1)
        self.assertIn('Distrobox is not installed on this host', missing.stderr)
        self.assertIn('distrobox enter my-dbx', missing.stderr)

    def test_legacy_checkout_skips_microos_toolbox_and_uses_distrobox(self):
        result = self.run_clean(
            '--yes', stdin=subprocess.DEVNULL,
            FAKE_TOOLBOX_BAD_VERSION='1',
            FAKE_TOOLBOX_CONTAINER_OK='1',
            FAKE_TOOLBOX_HAS_DARLING='1',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.argv_lines(), [])
        self.assertTrue(
            any('enter --no-tty --name darling-lol -- ' in line for line in self.distrobox_lines()),
            self.distrobox_lines(),
        )

    def test_inside_distrobox_skips_reexec(self):
        self.write_marker('my-dbx')
        (self.repo / 'build' / 'toolbox-backend').write_text('distrobox\n')
        containerenv = self.folder / 'containerenv'
        containerenv.write_text('name="my-dbx"\n')
        result = self.run_clean(
            '--help', stdin=subprocess.DEVNULL,
            DARLING_TOOLBOX_ENV=str(self.folder / 'missing-toolboxenv'),
            DARLING_CONTAINER_ENV=str(containerenv),
            CONTAINER_ID='my-dbx',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.distrobox_lines(), [])


if __name__ == '__main__':
    unittest.main()
