"""Drive installer.sh against stubs: retry, interrupt, lock and prefix handling.

Nothing here reaches the network, a Darling prefix or the real runner: the
fetcher, unzip, notify-send and scripts/run.sh are stubs in a disposable tree.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = 'Users/Shared/Riot Games/Riot Client.app'
METADATA = 'Users/Shared/Riot Games/Metadata/Riot Client'


def stub_script(path, body):
    path.write_text(f'#!/bin/sh\n{body}\n')
    path.chmod(0o755)


def snapshot(folder):
    """Every path under folder with its type, link target or content."""
    state = {}
    for path in sorted(folder.rglob('*')):
        name = str(path.relative_to(folder))
        if path.is_symlink():
            state[name] = ('link', os.readlink(path))
        elif path.is_dir():
            state[name] = ('dir',)
        else:
            state[name] = ('file', path.read_bytes(), path.stat().st_mtime_ns)
    return state


class InstallerRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='installer-retry-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        tools = self.repo / 'build/runtime/tools'
        tools.mkdir(parents=True)
        (self.repo / 'scripts').mkdir()
        shutil.copy(ROOT / 'scripts/installer.sh', self.repo / 'scripts')
        (self.repo / 'scripts/configure.py').write_text('')
        self.configured = self.root / 'configured-prefix'
        shutil.copy(ROOT / 'tools/runtime_paths.py', tools)
        (tools / 'runtime-paths.sh').write_text(
            (ROOT / 'tools/runtime-paths.sh').read_text()
            .replace('@DPREFIX@', str(self.configured))
            .replace('@GUEST_HOME@', '/Users/guest')
            .replace('@PROJECT_ROOT@', str(self.repo)))
        (tools / 'fetch-installer.sh').write_text('echo "FETCH $1" >> "$TEST_LOG"\n')
        # installer.sh puts <repo>/usr-local/bin first on PATH.
        stubs = self.repo / 'usr-local/bin'
        stubs.mkdir(parents=True)
        stub_script(stubs / 'unzip', ':')
        stub_script(stubs / 'notify-send', 'echo "NOTIFY $*" >> "$TEST_LOG"')
        self.log = self.root / 'log'
        self.log.write_text('')
        self.prefix = self.root / 'prefix'
        self.lock = self.repo / 'build/installer.lock'

    def run_stub(self, body):
        """scripts/run.sh stand-in; installer runs see RIOT_LAUNCHER_HOST."""
        stub_script(self.repo / 'scripts/run.sh',
                    'if [ -n "${RIOT_LAUNCHER_HOST:-}" ]; then echo INSTALLER >> "$TEST_LOG"\n'
                    f'{body}\n'
                    'else echo CLIENT >> "$TEST_LOG"; fi')

    def env(self, prefix=None):
        env = dict(os.environ, TEST_LOG=str(self.log), HOME=str(self.root / 'home'))
        for name in ('DARLING_WORKSPACE', 'DPREFIX', 'RIOT_GUEST_HOME'):
            env.pop(name, None)
        if prefix is not None:
            env['DPREFIX'] = str(prefix)
        return env

    def install(self, prefix='default'):
        prefix = self.prefix if prefix == 'default' else prefix
        return subprocess.run(['sh', str(self.repo / 'scripts/installer.sh'), 'euw'],
                              env=self.env(prefix), capture_output=True, text=True, timeout=30)

    def events(self, name):
        return [line for line in self.log.read_text().splitlines() if line.startswith(name)]

    def make_partial(self, prefix=None):
        prefix = prefix or self.prefix
        (prefix / BUNDLE / 'Contents/Frameworks').mkdir(parents=True)
        (prefix / METADATA).mkdir(parents=True)
        (prefix / METADATA / 'partial.db').write_text('partial')
        (prefix / 'Applications').mkdir(parents=True)
        os.symlink(prefix / BUNDLE, prefix / 'Applications/Riot Client.app')

    def make_complete(self, prefix=None):
        prefix = prefix or self.prefix
        (prefix / BUNDLE / 'Contents/MacOS').mkdir(parents=True)
        (prefix / BUNDLE / 'Contents/MacOS/RiotClientServices').write_text('client')
        (prefix / METADATA).mkdir(parents=True)
        (prefix / METADATA / 'release.json').write_text('{"release": "pinned"}')
        (prefix / 'Applications').mkdir(parents=True)
        os.symlink(prefix / BUNDLE, prefix / 'Applications/Riot Client.app')

    def test_an_install_that_never_completes_stops_after_three_attempts(self):
        self.make_partial()
        # Every attempt leaves a partial bundle behind again.
        self.run_stub(f'mkdir -p "$DPREFIX/{BUNDLE}/Contents/Frameworks"')
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.events('INSTALLER')), 3)
        self.assertEqual(self.events('CLIENT'), [])
        self.assertIn('after 3 attempt(s)', result.stderr)
        self.assertIn('retrying (2 of 3)', result.stderr)
        self.assertIn('retrying (3 of 3)', result.stderr)
        self.assertEqual(len(self.events('NOTIFY')), 3)
        self.assertIn('Riot Client install failed', self.events('NOTIFY')[-1])
        # The leftovers of the first crash were removed before retrying.
        self.assertFalse((self.prefix / METADATA).exists())
        self.assertFalse((self.prefix / 'Applications/Riot Client.app').is_symlink())
        self.assertFalse(self.lock.exists())

    def test_a_partial_install_is_cleaned_and_a_second_attempt_can_finish(self):
        self.make_partial()
        self.run_stub(f'''if [ -e "$TEST_LOG.second" ]; then
  [ ! -e "$DPREFIX/{BUNDLE}" ] && [ ! -e "$DPREFIX/{METADATA}" ] && echo CLEAN >> "$TEST_LOG"
  mkdir -p "$DPREFIX/{BUNDLE}/Contents/MacOS"
else
  [ ! -e "$DPREFIX/{BUNDLE}" ] && [ ! -e "$DPREFIX/{METADATA}" ] && echo CLEAN >> "$TEST_LOG"
  : > "$TEST_LOG.second"; mkdir -p "$DPREFIX/{BUNDLE}/Contents/Frameworks"; exit 139
fi''')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        # A crash status (sig 11) is retried; both attempts started clean.
        self.assertEqual(len(self.events('INSTALLER')), 2)
        self.assertEqual(len(self.events('CLEAN')), 2)
        self.assertEqual(len(self.events('CLIENT')), 1)
        self.assertIn('Riot Client installed', self.events('NOTIFY')[-1])
        self.assertFalse(self.lock.exists())

    def test_a_complete_install_is_not_retried_and_not_touched(self):
        self.make_complete()
        before = snapshot(self.prefix)
        self.run_stub('exit 1')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.events('INSTALLER')), 1)
        self.assertEqual(len(self.events('CLIENT')), 1)
        self.assertNotIn('Removed', result.stdout)
        self.assertEqual(snapshot(self.prefix), before)
        self.assertEqual((self.prefix / METADATA / 'release.json').read_text(),
                         '{"release": "pinned"}')

    def test_the_prefix_from_dprefix_is_honoured(self):
        custom = self.root / 'custom prefix'
        self.make_partial(self.configured)
        configured = snapshot(self.configured)
        self.run_stub(f'echo "PREFIX $DPREFIX" >> "$TEST_LOG"; mkdir -p "$DPREFIX/{BUNDLE}/Contents/MacOS"')
        result = self.install(custom)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events('PREFIX'), [f'PREFIX {custom}'])
        self.assertEqual(len(self.events('INSTALLER')), 1)
        self.assertEqual(len(self.events('CLIENT')), 1)
        self.assertTrue((custom / BUNDLE / 'Contents/MacOS').is_dir())
        # The configured prefix was not the target: its leftovers are intact.
        self.assertEqual(snapshot(self.configured), configured)

    def test_the_configured_prefix_is_used_without_dprefix(self):
        self.run_stub('exit 0')
        result = self.install(None)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f'{self.configured}/Users/<guest user>/Library/Logs', result.stderr)

    def test_an_interrupted_run_stops_without_retry_or_wipe(self):
        for status in (130, 143, 129):
            with self.subTest(status=status):
                shutil.rmtree(self.prefix, ignore_errors=True)
                self.log.write_text('')
                self.run_stub(f'mkdir -p "$DPREFIX/{BUNDLE}/Contents/Frameworks" "$DPREFIX/{METADATA}"\n'
                              f'exit {status}')
                result = self.install()
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(len(self.events('INSTALLER')), 1)
                self.assertEqual(self.events('NOTIFY'), [])
                self.assertIn('interrupted', result.stderr)
                self.assertNotIn('did not install', result.stderr)
                # What the interrupted attempt wrote is still there.
                self.assertTrue((self.prefix / BUNDLE / 'Contents/Frameworks').is_dir())
                self.assertTrue((self.prefix / METADATA).is_dir())
                self.assertFalse(self.lock.exists())

    def test_a_signal_to_the_installer_stops_without_retry_or_wipe(self):
        for signal, status in (('INT', 130), ('TERM', 143)):
            with self.subTest(signal=signal):
                shutil.rmtree(self.prefix, ignore_errors=True)
                self.log.write_text('')
                # The run itself ends normally; only the installer is signalled.
                self.run_stub(f'mkdir -p "$DPREFIX/{BUNDLE}/Contents/Frameworks"\n'
                              f'kill -{signal} "$PPID"; exit 0')
                result = self.install()
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(len(self.events('INSTALLER')), 1)
                self.assertNotIn('did not install', result.stderr)
                self.assertTrue((self.prefix / BUNDLE / 'Contents/Frameworks').is_dir())
                self.assertFalse(self.lock.exists())

    def test_a_second_install_exits_without_deleting_anything(self):
        self.make_partial()
        started, release = self.root / 'started', self.root / 'release'
        # The first install is mid-download: it has recreated a partial bundle.
        self.run_stub(f'mkdir -p "$DPREFIX/{BUNDLE}/Contents/Frameworks"\n'
                      f': > "{started}"\n'
                      f'while [ ! -e "{release}" ]; do sleep 0.05; done\n'
                      f'mkdir -p "$DPREFIX/{BUNDLE}/Contents/MacOS"')
        first = subprocess.Popen(['sh', str(self.repo / 'scripts/installer.sh'), 'euw'],
                                 env=self.env(self.prefix), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 20
            while not started.exists():
                self.assertLess(time.monotonic(), deadline, 'first install never started')
                self.assertIsNone(first.poll())
                time.sleep(0.02)
            self.assertEqual((self.lock / 'pid').read_text().strip(), str(first.pid))
            before = snapshot(self.prefix)
            fetched = len(self.events('FETCH'))
            second = self.install()
            self.assertEqual(second.returncode, 1, second.stderr)
            self.assertIn(f'already running (pid {first.pid})', second.stderr)
            self.assertNotIn('Removed', second.stdout)
            self.assertEqual(snapshot(self.prefix), before)
            # It stopped before fetching, unpacking or running anything.
            self.assertEqual(len(self.events('FETCH')), fetched)
            self.assertEqual(len(self.events('INSTALLER')), 1)
            self.assertIn('already running', self.events('NOTIFY')[-1])
            # The first install still owns its lock.
            self.assertEqual((self.lock / 'pid').read_text().strip(), str(first.pid))
        finally:
            release.write_text('')
            out, err = first.communicate(timeout=20)
        self.assertEqual(first.returncode, 0, err)
        self.assertEqual(len(self.events('CLIENT')), 1)
        self.assertFalse(self.lock.exists())

    READ_ONLY = ('terminate called after throwing an instance of \'std::system_error\'\n'
                 '  what():  Failed to bind socket: Read-only file system')

    def test_a_read_only_prefix_overlay_is_not_retried_and_names_the_workdir(self):
        self.make_partial()
        workdir = Path(f'{self.prefix}.workdir')
        workdir.mkdir()
        self.run_stub(f'echo "{self.READ_ONLY}" >&2; exit 1')
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stderr)
        # The cause is persistent, so there is exactly one attempt.
        self.assertEqual(len(self.events('INSTALLER')), 1)
        self.assertNotIn('retrying', result.stderr)
        self.assertNotIn('after 3 attempt(s)', result.stderr)
        # The darlingserver message still reaches the terminal, followed by the fix.
        self.assertIn('Failed to bind socket: Read-only file system', result.stderr)
        self.assertIn(f'mv "{self.prefix}.workdir" "{self.prefix}.workdir.bak"', result.stderr)
        self.assertIn('Riot Client install failed', self.events('NOTIFY')[-1])
        # The workdir is only named, never moved or removed on the user's behalf.
        self.assertTrue(workdir.is_dir())
        self.assertFalse(self.lock.exists())

    def test_a_read_only_prefix_overlay_without_a_workdir_points_at_the_filesystem(self):
        self.run_stub(f'echo "{self.READ_ONLY}" >&2; exit 1')
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.events('INSTALLER')), 1)
        self.assertNotIn('mv "', result.stderr)
        self.assertIn(f'There is no {self.prefix}.workdir', result.stderr)
        self.assertIn('DPREFIX', result.stderr)
        self.assertFalse(self.lock.exists())

    def test_each_attempt_starts_with_an_empty_error_capture(self):
        self.make_partial()
        # The first attempt crashes with an error of its own; the second one
        # installs. The first attempt's stderr must not be shown or checked again.
        self.run_stub(f'''if [ -e "$TEST_LOG.second" ]; then
  mkdir -p "$DPREFIX/{BUNDLE}/Contents/MacOS"
else
  echo "first attempt error" >&2
  : > "$TEST_LOG.second"; mkdir -p "$DPREFIX/{BUNDLE}/Contents/Frameworks"; exit 139
fi''')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.events('INSTALLER')), 2)
        self.assertEqual(result.stderr.count('first attempt error'), 1)
        self.assertNotIn('read-only', result.stderr)

    def test_other_run_errors_are_shown_and_still_retried(self):
        self.make_partial()
        self.run_stub(f'echo "Failed to bind socket: Address already in use" >&2\n'
                      f'mkdir -p "$DPREFIX/{BUNDLE}/Contents/Frameworks"')
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.events('INSTALLER')), 3)
        self.assertEqual(result.stderr.count('Address already in use'), 3)
        self.assertNotIn('read-only', result.stderr)

    def test_a_stale_lock_is_taken_over(self):
        gone = subprocess.Popen(['true'])
        gone.wait()
        self.lock.mkdir(parents=True)
        (self.lock / 'pid').write_text(f'{gone.pid}\n')
        self.run_stub(f'mkdir -p "$DPREFIX/{BUNDLE}/Contents/MacOS"')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('stale install lock', result.stderr)
        self.assertEqual(len(self.events('CLIENT')), 1)
        self.assertFalse(self.lock.exists())

    def test_the_lock_is_released_before_the_client_session(self):
        # The final exec keeps the installer's pid for the whole session.
        stub_script(self.repo / 'scripts/run.sh',
                    'if [ -n "${RIOT_LAUNCHER_HOST:-}" ]; then '
                    f'mkdir -p "$DPREFIX/{BUNDLE}/Contents/MacOS"\n'
                    f'else [ -e "{self.lock}" ] && echo LOCKED >> "$TEST_LOG"; echo CLIENT >> "$TEST_LOG"; fi')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events('CLIENT'), ['CLIENT'])
        self.assertEqual(self.events('LOCKED'), [])


if __name__ == '__main__':
    unittest.main()
