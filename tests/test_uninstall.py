"""Verify what scripts/uninstall.sh deletes and refuses, in a disposable tree."""
import os
import pty
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/uninstall.sh'


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='uninstall-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        (self.repo / 'scripts').mkdir(parents=True)
        (self.repo / 'tools').mkdir()
        # The script resolves the prefix through the real configure step.
        for name in ('uninstall.sh', 'configure.py', 'common.py'):
            shutil.copy2(ROOT / 'scripts' / name, self.repo / 'scripts' / name)
        for name in ('runtime-paths.sh', 'runtime_paths.py'):
            shutil.copy2(ROOT / 'tools' / name, self.repo / 'tools' / name)
        # Stands in for the real stop helper, which is never run here.
        self.stop_log = self.root / 'stop.log'
        (self.repo / 'tools/stop-darling-runtime.py').write_text(
            f'open({str(self.stop_log)!r}, "a").write("STOP\\n")\n')
        self.home = self.root / 'home'
        self.home.mkdir()
        # An empty process table: the host's own Riot processes stay unseen.
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        # A space in the path: an unquoted expansion would split it into
        # `<root>/my` and a path relative to the checkout.
        self.prefix = self.root / 'my prefix'
        self.guest = self.prefix / 'Users/tester'
        self.outside = self.root / 'my'
        self.build_prefix()

    def build_prefix(self):
        prefix, guest, outside = self.prefix, self.guest, self.outside
        for folder in (
            prefix / 'private/etc',
            prefix / 'Applications/League of Legends.app/Contents',
            prefix / 'Applications/Utilities',
            prefix / 'Users/Shared/Riot Games/Riot Client.app',
            prefix / 'Users/Shared/Riot Games/Metadata/league_of_legends.live',
            prefix / 'Users/Shared/Riot Games/Metadata/Riot Client',
            guest / 'Library/Application Support/Riot Games',
            guest / 'Library/Application Support/Other',
            guest / 'Library/Logs/Riot Games',
            guest / 'Library/Caches/com.riotgames.RiotClient',
            guest / 'Library/Caches/com.riotgames.LeagueofLegends',
            guest / 'Library/Caches/mesa',
            guest / 'Library/Preferences',
            outside / 'real-riot',
            outside / 'Users/tester/Library/Caches/com.riotgames.RiotClient',
        ):
            folder.mkdir(parents=True)
        # The marker scripts/install-runtime.sh leaves in every prefix.
        (prefix / 'private/etc/darling-system.prop').write_text('x\n')
        (prefix / 'Applications/League of Legends.app/Contents/game').write_text('game\n')
        (guest / 'Library/Logs/Riot Games/a.log').write_text('log\n')
        (guest / 'Library/Preferences/com.riotgames.RiotClient.plist').write_text('x\n')
        (guest / 'Library/Preferences/other.plist').write_text('x\n')
        (guest / 'Library/Caches/mesa/shader').write_text('x\n')
        os.symlink('../Users/Shared/Riot Games/Riot Client.app', prefix / 'Applications/Riot Client.app')
        os.symlink('../Users/Shared/Riot Games/Riot Client.app', prefix / 'Applications/Cliente Riot.app')
        # A link that leaves the prefix: the link goes, its target stays.
        os.symlink(outside / 'real-riot', guest / 'Library/Application Support/Riot Client')
        for canary in (outside / 'canary', outside / 'real-riot/canary',
                       outside / 'Users/tester/Library/Caches/com.riotgames.RiotClient/canary'):
            canary.write_text('keep\n')

    def run_script(self, *arguments, answer=None, prefix=None, **extra):
        """Run the script; `answer` is typed at a terminal, otherwise stdin is not one."""
        env = dict(os.environ, HOME=str(self.home), DPREFIX=str(prefix or self.prefix),
                   RIOT_GUEST_HOME=str((prefix or self.prefix) / 'Users/tester'),
                   UNINSTALL_PROC_ROOT=str(self.proc), **extra)
        env.pop('DARLING_WORKSPACE', None)
        command = ['sh', str(self.repo / 'scripts/uninstall.sh'), *arguments]
        if answer is None:
            return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, check=False, env=env, timeout=60)
        master, slave = pty.openpty()
        try:
            os.write(master, answer.encode() + b'\n')
            return subprocess.run(command, stdin=slave, capture_output=True,
                                  text=True, check=False, env=env, timeout=60)
        finally:
            os.close(master)
            os.close(slave)

    def stops(self):
        return self.stop_log.read_text().count('STOP') if self.stop_log.exists() else 0

    def assert_outside_untouched(self):
        for canary in ('canary', 'real-riot/canary',
                       'Users/tester/Library/Caches/com.riotgames.RiotClient/canary'):
            self.assertEqual((self.outside / canary).read_text(), 'keep\n', canary)

    def assert_nothing_removed(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.stops(), 0, 'the runtime was stopped')
        for kept in ('Applications/League of Legends.app/Contents/game',
                     'Applications/Riot Client.app',
                     'Users/Shared/Riot Games/Riot Client.app',
                     'Users/tester/Library/Application Support/Riot Games',
                     'Users/tester/Library/Caches/com.riotgames.RiotClient',
                     'Users/tester/Library/Preferences/com.riotgames.RiotClient.plist'):
            self.assertTrue(os.path.lexists(self.prefix / kept), kept)
        self.assertNotIn('Removed', result.stdout)
        self.assert_outside_untouched()

    def assert_client_removed(self):
        for gone in ('Applications/Riot Client.app',
                     'Applications/Cliente Riot.app',
                     'Users/Shared/Riot Games/Riot Client.app',
                     'Users/Shared/Riot Games/Metadata/Riot Client',
                     'Users/tester/Library/Application Support/Riot Games',
                     'Users/tester/Library/Application Support/Riot Client',
                     'Users/tester/Library/Logs/Riot Games',
                     'Users/tester/Library/Caches/com.riotgames.RiotClient',
                     'Users/tester/Library/Caches/com.riotgames.LeagueofLegends',
                     'Users/tester/Library/Preferences/com.riotgames.RiotClient.plist'):
            self.assertFalse(os.path.lexists(self.prefix / gone), gone)
        for kept in ('private/etc/darling-system.prop',
                     'Applications/Utilities',
                     'Users/tester/Library/Application Support/Other',
                     'Users/tester/Library/Caches/mesa/shader',
                     'Users/tester/Library/Preferences/other.plist'):
            self.assertTrue((self.prefix / kept).exists(), kept)
        self.assert_outside_untouched()

    def test_a_prefix_path_with_a_space_loses_only_what_is_inside_it(self):
        result = self.run_script('--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_client_removed()
        self.assertFalse((self.prefix / 'Applications/League of Legends.app').exists())
        self.assertFalse((self.prefix / 'Users/Shared/Riot Games').exists())
        self.assertEqual(self.stops(), 1)
        self.assertIn('Riot software removed', result.stdout)
        self.assertEqual(len(list((self.repo / 'build/uninstall-log-backups').glob('*.tar.gz'))), 1)

    def test_keep_game_keeps_the_game_and_its_metadata(self):
        result = self.run_script('--keep-game', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_client_removed()
        self.assertEqual(
            (self.prefix / 'Applications/League of Legends.app/Contents/game').read_text(), 'game\n')
        self.assertTrue((self.prefix / 'Users/Shared/Riot Games/Metadata/league_of_legends.live').is_dir())
        self.assertIn('Will be kept: League of Legends', result.stdout)

    def test_a_running_riot_process_is_refused(self):
        for command_line in (b'/x/Applications/Riot Client.app/Contents/MacOS/RiotClientServices\0',
                             b'/x/League of Legends.app/Contents/LoL/LeagueClient.app/Contents/MacOS/LeagueClient\0--x\0'):
            with self.subTest(command_line=command_line):
                shutil.rmtree(self.proc)
                (self.proc / '4242').mkdir(parents=True)
                (self.proc / '4242/cmdline').write_bytes(command_line)
                (self.proc / 'self').mkdir()
                for arguments in (('--yes',), ('--all', '--yes')):
                    result = self.run_script(*arguments)
                    self.assert_nothing_removed(result)
                    self.assertIn('A Riot Client or League process is running', result.stderr)

    def test_no_terminal_and_no_yes_is_refused(self):
        for arguments in ((), ('--keep-game',), ('--all',)):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assert_nothing_removed(result)
                self.assertIn('pass --yes', result.stderr)
                # What would have been removed is still announced.
                self.assertIn(f'Darling prefix: {self.prefix}', result.stdout)
                self.assertIn('Will be removed', result.stdout)

    def test_declining_the_question_leaves_the_runtime_running(self):
        for arguments, answer in (((), 'n'), ((), ''), (('--keep-game',), 'no'),
                                  (('--all',), 'y'), (('--all',), str(self.prefix) + 'x')):
            with self.subTest(arguments=arguments, answer=answer):
                result = self.run_script(*arguments, answer=answer)
                self.assert_nothing_removed(result)
                self.assertIn('Aborted.', result.stderr)

    def test_accepting_the_question_removes_the_client(self):
        result = self.run_script(answer='y')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('League of Legends (the whole game', result.stdout)
        self.assert_client_removed()
        self.assertEqual(self.stops(), 1)

    def test_all_removes_the_prefix_and_nothing_else(self):
        result = self.run_script('--all', answer=str(self.prefix))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.prefix.exists())
        self.assertEqual(self.stops(), 1)
        self.assert_outside_untouched()

    def test_a_directory_that_is_not_a_prefix_is_refused(self):
        plain = self.root / 'documents'
        (plain / 'Users/tester').mkdir(parents=True)
        (plain / 'thesis.txt').write_text('keep\n')
        for arguments in (('--all', '--yes'), ('--yes',)):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments, prefix=plain)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('is not a Darling prefix', result.stderr)
                self.assertEqual((plain / 'thesis.txt').read_text(), 'keep\n')
                self.assertEqual(self.stops(), 0)

    def test_the_home_directory_is_refused_even_with_a_marker(self):
        (self.home / 'private/etc').mkdir(parents=True)
        (self.home / 'private/etc/darling-system.prop').write_text('x\n')
        (self.home / 'Users/tester').mkdir(parents=True)
        (self.home / 'notes.txt').write_text('keep\n')
        link = self.root / 'link-to-home'
        link.symlink_to(self.home)
        for prefix in (self.home, link):
            with self.subTest(prefix=prefix):
                result = self.run_script('--all', '--yes', prefix=prefix)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('home directory', result.stderr)
                self.assertEqual((self.home / 'notes.txt').read_text(), 'keep\n')
                self.assertEqual(self.stops(), 0)

    def test_a_missing_stop_helper_aborts_before_any_deletion(self):
        (self.repo / 'tools/stop-darling-runtime.py').unlink()
        for arguments in (('--yes',), ('--all', '--yes')):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assert_nothing_removed(result)
                self.assertIn('darling shutdown', result.stderr)

    def test_an_empty_prefix_reports_that_nothing_was_installed(self):
        shutil.rmtree(self.prefix)
        (self.prefix / 'private/etc').mkdir(parents=True)
        (self.prefix / 'private/etc/darling-system.prop').write_text('x\n')
        (self.prefix / 'Applications').mkdir()
        self.guest.mkdir(parents=True)
        for arguments in (('--yes',), ('--keep-game', '--yes')):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('Removed', result.stdout)
                self.assertNotIn('Riot software removed', result.stdout)
                self.assertIn('Nothing of the Riot software was installed', result.stdout)

    def test_the_old_flag_name_is_gone(self):
        result = self.run_script('--with-game', '--yes')
        self.assert_nothing_removed(result)
        self.assertIn('--keep-game', result.stderr)


if __name__ == '__main__':
    unittest.main()
