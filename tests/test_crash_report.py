"""Crash/hang bundle: process table, log tails, redaction; never starts anything."""
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/crash-report.py'
SPEC = importlib.util.spec_from_file_location('crash_report', SCRIPT)
crash_report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(crash_report)


def make_process(proc, pid, ppid, state, comm, argv):
    folder = Path(proc) / str(pid)
    folder.mkdir(parents=True)
    (folder / 'stat').write_text('%d (%s) %s %d 0 0 0\n' % (pid, comm, state, ppid))
    (folder / 'comm').write_text(comm + '\n')
    (folder / 'cmdline').write_bytes(b'\0'.join(part.encode() for part in argv) + b'\0')
    return folder


class ProcessSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.proc = Path(self.folder.name)
        make_process(self.proc, 100, 1, 'S', 'systemd', ['/usr/lib/systemd/systemd', '--user'])
        make_process(self.proc, 101, 100, 'S', 'mldr', ['/Volumes/mldr', 'RiotClientServices'])
        make_process(self.proc, 102, 101, 'Z', 'League of Legends', ['/game/League of Legends'])
        (self.proc / 'self').mkdir()
        (self.proc / 'uptime').write_text('1.0 2.0\n')

    def tearDown(self):
        self.folder.cleanup()

    def test_snapshot_parses_stat_with_spaces_in_comm(self):
        rows = crash_report.process_snapshot(self.proc)
        self.assertEqual([row['pid'] for row in rows], [100, 101, 102])
        game = rows[-1]
        self.assertEqual(game['comm'], 'League of Legends')
        self.assertEqual(game['ppid'], 101)
        self.assertEqual(game['state'], 'Z')
        self.assertIn('League of Legends', game['argv'])

    def test_relevant_selects_runtime_client_and_zombies(self):
        rows = crash_report.process_snapshot(self.proc)
        relevant = crash_report.relevant_processes(rows)
        self.assertEqual([row['pid'] for row in relevant], [101, 102])

    def test_format_defaults_to_the_runtime_and_client_tree(self):
        rows = crash_report.process_snapshot(self.proc)
        text = crash_report.format_processes(rows, crash_report.relevant_processes(rows))
        self.assertNotIn('# all processes owned by this user', text)
        self.assertIn('# runtime, client and zombie processes', text)
        self.assertIn('101\t100\tS\tmldr', text)
        self.assertNotIn('100\t1\tS\tsystemd', text)

    def test_format_includes_everything_on_request(self):
        rows = crash_report.process_snapshot(self.proc)
        text = crash_report.format_processes(rows, crash_report.relevant_processes(rows),
                                             all_processes=True)
        self.assertIn('# all processes owned by this user', text)
        self.assertIn('100\t1\tS\tsystemd', text)


class HelperTests(unittest.TestCase):
    def test_scrub_replaces_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            text = '%s/secret joe on desktop-1' % home
            cleaned = crash_report.scrub(text, home=home, username='joe', hostname='desktop-1')
            self.assertEqual(cleaned, '~/secret <user> on <host>')
            self.assertEqual(crash_report.scrub('', home=home), '')

    def test_read_tail_keeps_last_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'dserver.log'
            log.write_text(''.join('line %d\n' % index for index in range(10)))
            text, error = crash_report.read_tail(log, limit=3)
            self.assertIsNone(error)
            self.assertEqual(text, 'line 7\nline 8\nline 9\n')

    def test_read_tail_reports_missing_file(self):
        text, error = crash_report.read_tail('/nonexistent/dserver.log')
        self.assertIsNone(text)
        self.assertIn('No such file', error)

    def test_mask_argv_hides_credentials(self):
        masked = crash_report.mask_argv([
            'RiotClientServices', '--token', 'abc123', '--password=hunter2',
            'https://user:secret@example.test/path', '--region=EUW',
            'AUTH=topsecret', '--bearer', 'xyz',
        ])
        self.assertNotIn('abc123', masked)
        self.assertNotIn('hunter2', masked)
        self.assertNotIn('secret', masked)
        self.assertNotIn('topsecret', masked)
        self.assertNotIn('xyz', masked)
        self.assertIn('--region=EUW', masked)

    def test_qualified_credential_flags_in_both_formats(self):
        for flag in ('--remoting-auth-token', '--riotclient-auth-token',
                     '--access_token', '--client-password', '--api-key', '--authToken'):
            for arguments in ([flag + '=synthetic-value'], [flag, 'synthetic-value']):
                with self.subTest(arguments=arguments):
                    masked = crash_report.mask_argv(['LeagueClientUx', *arguments, '--region=EUW'])
                    self.assertNotIn('synthetic-value', masked)
                    self.assertIn('--region=EUW', masked)

    def test_scrub_uses_whole_tokens(self):
        self.assertEqual(
            crash_report.scrub('al and alps at /mnt/al on al-host', home=Path('/mnt/al'),
                               username='al', hostname='not-present'),
            '<user> and alps at ~ on <user>-host')

    def test_window_listing_drops_titles(self):
        listing = crash_report.window_listing(json.dumps([
            {'address': '0x1', 'class': 'Riot Client', 'title': 'inbox - secret subject',
             'initialTitle': 'old secret', 'size': [100, 100]},
        ]))
        self.assertIn('Riot Client', listing)
        self.assertNotIn('secret', listing)

    def test_label_is_restricted(self):
        with self.assertRaises(ValueError):
            crash_report.bundle_name('../../escape')
        with self.assertRaises(ValueError):
            crash_report.bundle_name('hang report')
        self.assertTrue(crash_report.bundle_name('hang-2').endswith('-hang-2.tar.gz'))

    def test_default_launch_log_prefers_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            log = workspace / 'build/desktop-launch.log'
            log.parent.mkdir()
            log.write_text('launcher output\n')
            self.assertEqual(crash_report.default_launch_log({'DARLING_WORKSPACE': folder}), log)

    def test_human_size(self):
        self.assertEqual(crash_report.human_size(512), '512 B')
        self.assertEqual(crash_report.human_size(2048), '2 KiB')
        self.assertEqual(crash_report.human_size(3 * 1024 ** 2), '3 MiB')

    def test_bundle_name_has_label(self):
        name = crash_report.bundle_name('hang')
        self.assertTrue(name.startswith('lol-report-'))
        self.assertTrue(name.endswith('-hang.tar.gz'))


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.prefix = self.root / 'prefix'
        self.proc = self.root / 'proc'
        self.out = self.root / 'reports'
        self.proc.mkdir()
        make_process(self.proc, 200, 1, 'S', 'darlingserver', ['/usr/libexec/darlingserver'])
        make_process(self.proc, 201, 200, 'Z', 'mldr', ['/mldr'])
        log = self.prefix / 'private/var/log/dserver.log'
        log.parent.mkdir(parents=True)
        log.write_text(str(Path.home()) + '/.darling running\nsecret marker\n')
        self.launch = self.root / 'launch.log'
        self.launch.write_text('launcher line\n')
        self.env = {'USER': 'joe', 'HOME': str(Path.home()), 'SECRET_TOKEN': 'do-not-publish'}

    def tearDown(self):
        self.folder.cleanup()

    def build(self, **overrides):
        options = dict(prefix=self.prefix, out_dir=self.out, launch_log=self.launch,
                       proc_root=self.proc, which=lambda name: None, env=self.env)
        options.update(overrides)
        return crash_report.build(**options)

    def test_bundle_contains_expected_files_and_summary(self):
        archive, manifest = self.build()
        self.assertTrue(archive.is_file())
        self.assertEqual(manifest['processes'], {'total': 2, 'relevant': 2, 'zombies': 1,
                                                  'included': 'runtime-client-zombies'})
        self.assertEqual(manifest['redacted'], True)
        self.assertEqual(manifest['prefix'], '<prefix>')
        with tarfile.open(archive) as tar:
            names = set(tar.getnames())
            self.assertIn('lol-report/README.txt', names)
            self.assertIn('lol-report/report.json', names)
            self.assertIn('lol-report/processes.txt', names)
            self.assertIn('lol-report/logs/dserver.log.tail', names)
            self.assertIn('lol-report/logs/kernel.txt', names)
            self.assertIn('lol-report/coredumps.txt', names)
            report = json.loads(tar.extractfile('lol-report/report.json').read())
            self.assertEqual(report['doctor']['checks'][0]['id'], 'kernel')

    def test_bundle_redacts_home_and_secrets(self):
        archive, _ = self.build()
        with tarfile.open(archive) as tar:
            combined = ''
            for member in tar.getmembers():
                if member.isfile():
                    combined += tar.extractfile(member).read().decode()
        self.assertNotIn(str(Path.home()), combined)
        self.assertNotIn('do-not-publish', combined)
        self.assertIn('~/.darling running', combined)

    def test_bundle_omits_riot_command_line_tokens(self):
        make_process(self.proc, 202, 200, 'S', 'LeagueClientUx',
                     ['LeagueClientUx', '--remoting-auth-token=synthetic-remote',
                      '--riotclient-auth-token', 'synthetic-riot'])
        archive, _ = self.build()
        with tarfile.open(archive) as tar:
            text = tar.extractfile('lol-report/processes.txt').read().decode()
        self.assertIn('LeagueClientUx', text)
        self.assertNotIn('synthetic-remote', text)
        self.assertNotIn('synthetic-riot', text)

    def test_workspace_paths_are_redacted(self):
        archive, _ = self.build(launch_log=None,
                                env=dict(self.env, DARLING_WORKSPACE=str(self.root / 'work')))
        with tarfile.open(archive) as tar:
            text = tar.extractfile('lol-report/logs/launch.log.tail').read().decode()
        self.assertNotIn(str(self.root), text)
        self.assertIn('<workspace>', text)

    def test_archive_is_private_and_has_no_owner_metadata(self):
        archive, _ = self.build()
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, '', ''))

    def test_all_processes_is_opt_in(self):
        archive, manifest = self.build(all_processes=True)
        self.assertEqual(manifest['processes']['included'], 'all')
        with tarfile.open(archive) as tar:
            text = tar.extractfile('lol-report/processes.txt').read().decode()
        self.assertIn('# all processes owned by this user', text)

    def test_no_redact_keeps_paths(self):
        archive, manifest = self.build(redact=False)
        self.assertEqual(manifest['redacted'], False)
        self.assertEqual(manifest['prefix'], str(self.prefix))
        with tarfile.open(archive) as tar:
            text = tar.extractfile('lol-report/logs/dserver.log.tail').read().decode()
        self.assertIn(str(Path.home()), text)

    def test_missing_logs_are_explained(self):
        archive, manifest = self.build(launch_log=None)
        self.assertIn('logs/launch.log.tail', manifest['files'])
        self.assertEqual(manifest['processes']['included'], 'runtime-client-zombies')
        with tarfile.open(archive) as tar:
            text = tar.extractfile('lol-report/logs/launch.log.tail').read().decode()
        self.assertIn('desktop-launch.log', text)
        self.assertIn('No such file', text)


if __name__ == '__main__':
    unittest.main()
