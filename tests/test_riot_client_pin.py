"""Fixture-tree tests for tools/riot-client-pin.py: no guest, no real prefix, no copy of 1 GB."""
import importlib.util
from pathlib import Path
import tempfile
import subprocess
import os
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('riot_client_pin', ROOT / 'tools/riot-client-pin.py')
pin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pin)

KNOWN = '42E1358C87FB3ABC'

SYSTEM_YAML = """---
allow_insecure_content: true
app:
  app_name: Riot Client
  respawn: null
enable_self_update: true
insecure_tls_versions: true
"""


def make_prefix(root, release=KNOWN, self_update='enable_self_update: true\n', partial=True):
    """A prefix shaped like an instal: bundle, metadata and a staged .partial-* directory."""
    shared = Path(root) / 'Users/Shared/Riot Games'
    app = shared / 'Riot Client.app'
    (app / 'Contents/Resources').mkdir(parents=True)
    (app / 'Contents/Resources/system.yaml').write_text(SYSTEM_YAML.replace(
        'enable_self_update: true\n', self_update))
    (app / 'Contents/MacOS').mkdir()
    (app / 'Contents/MacOS/RiotClientServices').write_text('binary\n')
    metadata = shared / 'Metadata/Riot Client'
    metadata.mkdir(parents=True)
    if release:
        (metadata / 'Riot Client.ok').write_text(
            f'https://ks-foundation.secure.dyn.riotcdn.net/channels/public/releases/{release}.manifest\n')
        (metadata / 'Riot Client.manifest').write_bytes(b'RMAN')
    if partial:
        (shared / 'Riot Client.app.partial-2336/Contents').mkdir(parents=True)
    return Path(root), app, metadata


class ReleaseTests(unittest.TestCase):
    def test_release_id_reads_the_manifest_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, metadata = make_prefix(tmp)
            self.assertEqual(pin.release_id(metadata), KNOWN)

    def test_release_id_is_upper_case_and_tolerates_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, metadata = make_prefix(tmp, release=KNOWN.lower())
            (metadata / 'Riot Client.ok').write_text(
                f'https://cdn/releases/{KNOWN.lower()}.manifest\r\n')
            self.assertEqual(pin.release_id(metadata), KNOWN)

    def test_missing_or_unrelated_metadata_is_not_a_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, metadata = make_prefix(tmp, release=None)
            self.assertIsNone(pin.release_id(metadata))
            (metadata / 'Riot Client.ok').write_text('https://cdn/channels/public\n')
            self.assertIsNone(pin.release_id(metadata))
            self.assertIsNone(pin.release_id(Path(tmp) / 'nowhere'))

    def test_known_good_release_is_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, _, _ = make_prefix(tmp)
            state = pin.describe(prefix)
            self.assertTrue(state['installed'])
            self.assertTrue(state['known_good'])

    def test_other_release_is_reported_as_not_known_good(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, _, _ = make_prefix(tmp, release='6E813D498F26C657')
            self.assertFalse(pin.describe(prefix)['known_good'])


class SelfUpdateTests(unittest.TestCase):
    def test_reads_the_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, _ = make_prefix(tmp)
            self.assertTrue(pin.self_update_enabled(app))
            (app / 'Contents/Resources/system.yaml').write_text(
                SYSTEM_YAML.replace('enable_self_update: true', 'enable_self_update: false'))
            self.assertFalse(pin.self_update_enabled(app))

    def test_missing_setting_or_bundle_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, _ = make_prefix(tmp, self_update='')
            self.assertIsNone(pin.self_update_enabled(app))
            self.assertIsNone(pin.self_update_enabled(Path(tmp) / 'nowhere'))

    def test_pin_rewrites_only_that_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, _ = make_prefix(tmp)
            before = (app / 'Contents/Resources/system.yaml').read_text().splitlines()
            self.assertFalse(pin.set_self_update(app, False))
            after = (app / 'Contents/Resources/system.yaml').read_text()
            changed = [line for line in after.splitlines() if line.startswith('enable_self_update')]
            self.assertEqual(changed, ['enable_self_update: false'])
            self.assertEqual(len(before), len(after.splitlines()))
            self.assertFalse((app / 'Contents/Resources/system.yaml.new').exists())
            self.assertFalse(pin.self_update_enabled(app))

    def test_pin_is_idempotent_and_unpin_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, _ = make_prefix(tmp)
            pin.set_self_update(app, False)
            pin.set_self_update(app, False)
            self.assertFalse(pin.self_update_enabled(app))
            pin.set_self_update(app, True)
            self.assertTrue(pin.self_update_enabled(app))

    def test_a_bundle_without_the_setting_is_refused(self):
        """Riot's generator can rename the key; silently doing nothing would be worse."""
        with tempfile.TemporaryDirectory() as tmp:
            _, app, _ = make_prefix(tmp, self_update='')
            with self.assertRaises(SystemExit):
                pin.set_self_update(app, False)

    def test_utf8_settings_survive_pinning_in_an_ascii_locale(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, _ = make_prefix(tmp)
            path = app / 'Contents/Resources/system.yaml'
            content = 'label: Français\nenable_self_update: true\n'
            path.write_bytes(content.encode('utf-8'))
            code = ('import importlib.util,sys; '
                    's=importlib.util.spec_from_file_location("pin",sys.argv[1]); '
                    'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                    'm.set_self_update(sys.argv[2],False)')
            subprocess.run([sys.executable, '-c', code, str(ROOT / 'tools/riot-client-pin.py'),
                            str(app)], check=True, capture_output=True,
                           env=dict(os.environ, LC_ALL='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0'))
            self.assertEqual(path.read_bytes(), content.replace('true', 'false').encode('utf-8'))

    def test_nested_self_update_keeps_its_parent_and_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, _ = make_prefix(tmp)
            path = app / 'Contents/Resources/system.yaml'
            text = 'updater:\n  enable_self_update: true\n  channel: live\nother: unchanged\n'
            path.write_text(text, encoding='utf-8')
            pin.set_self_update(app, False)
            self.assertEqual(path.read_text(encoding='utf-8'),
                             text.replace('enable_self_update: true', 'enable_self_update: false'))
            self.assertFalse(pin.self_update_enabled(app))
            pin.set_self_update(app, True)
            self.assertEqual(path.read_text(encoding='utf-8'), text)

    def test_trailing_newline_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, _ = make_prefix(tmp)
            path = app / 'Contents/Resources/system.yaml'
            path.write_text('a: 1\nenable_self_update: true')
            pin.set_self_update(app, False)
            self.assertEqual(path.read_text(), 'a: 1\nenable_self_update: false')


class StagedUpdateTests(unittest.TestCase):
    def test_staged_bundles_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, _, _ = make_prefix(tmp)
            self.assertEqual([p.name for p in pin.staged_updates(pin.install_paths(prefix)[0])],
                             ['Riot Client.app.partial-2336'])

    def test_no_staged_bundle_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, _, _ = make_prefix(tmp, partial=False)
            self.assertEqual(pin.staged_updates(pin.install_paths(prefix)[0]), [])


class RunningTests(unittest.TestCase):
    def proc_tree(self, root, *commands):
        """Fake /proc entries from argv lists; paths contain spaces, so no shell parsing."""
        for index, argv in enumerate(commands, start=100):
            entry = Path(root) / str(index)
            entry.mkdir()
            (entry / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in argv) + b'\0')
        return Path(root)

    def test_a_client_executable_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.proc_tree(tmp, ['/Users/Shared/Riot Games/Riot Client.app/Contents/MacOS/RiotClientServices',
                                        '--app-port=1'])
            self.assertTrue(pin.running(root))

    def test_electron_main_and_helper_names_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.proc_tree(tmp, ['Riot Client.app/Contents/MacOS/Riot Client', '--type=renderer'])
            self.assertTrue(pin.running(root))

    def test_naming_the_bundle_without_running_it_is_not_running(self):
        """A shell, editor or grep that mentions the path used to block every write."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.proc_tree(
                tmp,
                ['/usr/bin/bash', '-c', 'grep enable_self_update "/Users/Shared/Riot Games/Riot Client.app/Contents/Resources/system.yaml"'],
                ['python3', 'tools/riot-client-pin.py', '--pin'],
                ['/usr/bin/cp', '-a', '/Users/Shared/Riot Games/Riot Client.app', '/backup'])
            self.assertFalse(pin.running(root))

    def test_an_empty_or_unreadable_entry_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '55').mkdir()
            (root / 'not-a-pid').mkdir()
            self.assertFalse(pin.running(root))

    def test_unrelated_process_table_is_not_reported_as_a_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.proc_tree(tmp, ['/usr/bin/python3', 'tools/riot-client-pin.py'])
            self.assertFalse(pin.running(root))


class BackupTests(unittest.TestCase):
    def test_backup_keeps_bundle_metadata_and_a_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, metadata = make_prefix(tmp)
            target = pin.backup(app, metadata, Path(tmp) / 'kept')
            self.assertEqual(target.name, KNOWN)
            self.assertTrue((target / 'Riot Client.app/Contents/MacOS/RiotClientServices').is_file())
            self.assertTrue((target / 'Metadata/Riot Client.manifest').is_file())
            note = (target / 'PIN.txt').read_text()
            self.assertIn('release 42E1358C87FB3ABC', note)
            self.assertIn('self_update true', note)

    def test_backup_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, metadata = make_prefix(tmp)
            pin.backup(app, metadata, Path(tmp) / 'kept')
            with self.assertRaises(SystemExit):
                pin.backup(app, metadata, Path(tmp) / 'kept')

    def test_backup_without_metadata_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, metadata = make_prefix(tmp, release=None)
            target = pin.backup(app, metadata, Path(tmp) / 'kept')
            self.assertTrue(target.name.startswith('unknown-'))

    def test_dry_run_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, metadata = make_prefix(tmp)
            pin.backup(app, metadata, Path(tmp) / 'kept', dry_run=True)
            self.assertFalse((Path(tmp) / 'kept').exists())


class RestoreTests(unittest.TestCase):
    def test_restore_swaps_the_bundle_and_keeps_the_old_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, metadata = make_prefix(tmp)
            pin.backup(app, metadata, Path(tmp) / 'kept')
            # The updater replaced the working bundle with a newer, broken one.
            (app / 'Contents/Resources/system.yaml').write_text(
                SYSTEM_YAML.replace('enable_self_update: true', 'enable_self_update: true') + '# 142\n')
            replaced = pin.restore(app, Path(tmp) / 'kept')
            self.assertTrue(replaced.is_dir())
            self.assertIn('# 142', (replaced / 'Contents/Resources/system.yaml').read_text())
            self.assertNotIn('# 142', (app / 'Contents/Resources/system.yaml').read_text())
            self.assertTrue((app / 'Contents/MacOS/RiotClientServices').is_file())

    def test_restore_pairs_the_bundle_with_its_release_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, app, metadata = make_prefix(tmp)
            saved = pin.backup(app, metadata, Path(tmp) / 'kept')
            (metadata / 'Riot Client.ok').write_text(
                'https://cdn/releases/6E813D498F26C657.manifest')
            replaced = pin.restore(app, saved)
            self.assertEqual(pin.release_id(metadata), KNOWN)
            self.assertTrue(pin.describe(prefix)['known_good'])
            self.assertEqual(pin.release_id(replaced.with_name(
                replaced.name + '.metadata')), '6E813D498F26C657')

    def test_restore_without_saved_metadata_clears_the_old_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, metadata = make_prefix(tmp)
            saved = Path(tmp) / 'saved'
            saved.mkdir()
            pin.copy_tree(app, saved / app.name)
            pin.restore(app, saved)
            self.assertIsNone(pin.release_id(metadata))

    def test_copy_failures_leave_the_installed_release_unchanged(self):
        for failing_copy in (1, 2):
            with self.subTest(copy=failing_copy), tempfile.TemporaryDirectory() as tmp:
                _, app, metadata = make_prefix(tmp)
                saved = pin.backup(app, metadata, Path(tmp) / 'kept')
                (app / 'Contents/Resources/system.yaml').write_text('current bundle')
                (metadata / 'Riot Client.ok').write_text('current metadata')
                copy = pin.copy_tree
                calls = 0

                def interrupted(source, destination):
                    nonlocal calls
                    calls += 1
                    if calls == failing_copy:
                        Path(destination).mkdir()
                        (Path(destination) / 'partial').write_text('incomplete')
                        raise subprocess.CalledProcessError(1, ['cp'])
                    copy(source, destination)

                with patch.object(pin, 'copy_tree', side_effect=interrupted):
                    with self.assertRaises(subprocess.CalledProcessError):
                        pin.restore(app, saved)
                self.assertEqual((app / 'Contents/Resources/system.yaml').read_text(), 'current bundle')
                self.assertEqual((metadata / 'Riot Client.ok').read_text(), 'current metadata')
                self.assertEqual(list(app.parent.glob('.riot-client-restore-*')), [])
                self.assertEqual(list(app.parent.glob('Riot Client.app.replaced-*')), [])

    def test_metadata_publish_failure_rolls_back_both_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, metadata = make_prefix(tmp)
            saved = pin.backup(app, metadata, Path(tmp) / 'kept')
            (app / 'Contents/Resources/system.yaml').write_text('current bundle')
            (metadata / 'Riot Client.ok').write_text('current metadata')
            rename = Path.rename

            def interrupted(source, destination):
                if source.name == 'Metadata' and destination == metadata:
                    raise OSError('metadata publish failed')
                return rename(source, destination)

            with patch.object(Path, 'rename', interrupted):
                with self.assertRaisesRegex(OSError, 'metadata publish failed'):
                    pin.restore(app, saved)
            self.assertEqual((app / 'Contents/Resources/system.yaml').read_text(), 'current bundle')
            self.assertEqual((metadata / 'Riot Client.ok').read_text(), 'current metadata')
            self.assertEqual(list(app.parent.glob('Riot Client.app.replaced-*')), [])

    def test_repeated_restores_keep_each_displaced_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, metadata = make_prefix(tmp)
            saved = pin.backup(app, metadata, Path(tmp) / 'kept')
            with patch.object(pin.time, 'strftime', return_value='same-second'):
                first = pin.restore(app, saved)
                second = pin.restore(app, saved)
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())

    def test_restore_rejects_a_directory_without_a_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, _ = make_prefix(tmp)
            empty = Path(tmp) / 'kept'
            empty.mkdir()
            with self.assertRaises(SystemExit):
                pin.restore(app, empty)


class CommandLineTests(unittest.TestCase):
    def test_status_flag_reports_and_changes_nothing(self):
        """Docs tell people to run --status, so it must be a real flag and stay read-only."""
        with tempfile.TemporaryDirectory() as tmp:
            prefix, app, _ = make_prefix(tmp)
            with patch.object(pin, 'running', return_value=False):
                self.assertEqual(pin.main(['--prefix', str(prefix), '--status']), 0)
            self.assertTrue(pin.self_update_enabled(app))
            self.assertEqual(len(pin.staged_updates(app)), 1)

    def test_status_on_a_missing_prefix_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pin.main(['--prefix', str(Path(tmp) / 'empty')]), 1)

    def test_a_write_is_refused_while_the_client_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, app, _ = make_prefix(tmp)
            with patch.object(pin, 'running', return_value=True):
                self.assertEqual(pin.main(['--prefix', str(prefix), '--pin']), 1)
            self.assertTrue(pin.self_update_enabled(app))

    def test_pin_and_discard_partial_through_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, app, _ = make_prefix(tmp)
            with patch.object(pin, 'running', return_value=False):
                self.assertEqual(pin.main(['--prefix', str(prefix), '--pin', '--discard-partial']), 0)
            self.assertFalse(pin.self_update_enabled(app))
            self.assertEqual(pin.staged_updates(app), [])

    def test_pin_and_backup_run_together(self):
        """The documented command keeps a copy and pins in one go."""
        with tempfile.TemporaryDirectory() as tmp:
            prefix, app, _ = make_prefix(tmp)
            kept = Path(tmp) / 'kept'
            with patch.object(pin, 'running', return_value=False):
                self.assertEqual(pin.main(['--prefix', str(prefix), '--backup', str(kept),
                                           '--pin', '--discard-partial']), 0)
            self.assertFalse(pin.self_update_enabled(app))
            self.assertTrue((kept / KNOWN / 'Riot Client.app').is_dir())
            self.assertTrue(pin.self_update_enabled(kept / KNOWN / 'Riot Client.app'))
            self.assertEqual(pin.staged_updates(app), [])

    def test_restore_then_pin_in_existing_and_fresh_prefixes(self):
        for fresh in (False, True):
            with self.subTest(fresh=fresh), tempfile.TemporaryDirectory() as tmp:
                prefix, app, metadata = make_prefix(tmp)
                saved = pin.backup(app, metadata, Path(tmp) / 'kept')
                if fresh:
                    prefix = Path(tmp) / 'fresh'
                    app, metadata = pin.install_paths(prefix)
                with patch.object(pin, 'running', return_value=False):
                    self.assertEqual(pin.main(['--prefix', str(prefix), '--restore',
                                               str(saved), '--pin']), 0)
                self.assertFalse(pin.self_update_enabled(app))
                self.assertEqual(pin.release_id(metadata), KNOWN)
                self.assertTrue(pin.self_update_enabled(saved / app.name))

    def test_restore_pin_dry_run_in_a_fresh_prefix_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, app, metadata = make_prefix(tmp)
            saved = pin.backup(app, metadata, Path(tmp) / 'kept')
            prefix = Path(tmp) / 'fresh'
            with patch.object(pin, 'running', return_value=False):
                self.assertEqual(pin.main(['--prefix', str(prefix), '--restore',
                                           str(saved), '--pin', '--dry-run']), 0)
            self.assertFalse(prefix.exists())
            self.assertTrue(pin.self_update_enabled(saved / app.name))

    def test_failed_backup_does_not_pin_or_discard_staged_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, app, metadata = make_prefix(tmp)
            kept = Path(tmp) / 'kept'
            pin.backup(app, metadata, kept)
            with patch.object(pin, 'running', return_value=False):
                with self.assertRaises(SystemExit):
                    pin.main(['--prefix', str(prefix), '--backup', str(kept),
                              '--pin', '--discard-partial'])
            self.assertTrue(pin.self_update_enabled(app))
            self.assertEqual(len(pin.staged_updates(app)), 1)

    def test_status_with_an_action_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, _, _ = make_prefix(tmp)
            with self.assertRaises(SystemExit), patch.object(pin, 'running', return_value=False):
                pin.main(['--prefix', str(prefix), '--status', '--pin'])

    def test_discard_partial_alone_is_refused(self):
        """It deletes a directory, so it may not be a bare accident."""
        with tempfile.TemporaryDirectory() as tmp:
            prefix, _, _ = make_prefix(tmp)
            with self.assertRaises(SystemExit), patch.object(pin, 'running', return_value=False):
                pin.main(['--prefix', str(prefix), '--discard-partial'])

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, app, _ = make_prefix(tmp)
            with patch.object(pin, 'running', return_value=False):
                self.assertEqual(pin.main(['--prefix', str(prefix), '--pin', '--dry-run']), 0)
            self.assertTrue(pin.self_update_enabled(app))
            self.assertEqual(len(pin.staged_updates(app)), 1)


if __name__ == '__main__':
    unittest.main()
