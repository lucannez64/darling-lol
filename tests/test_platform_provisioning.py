"""Provisioning contracts using disposable prefixes and runtime metadata."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/configure-darling-platform.py'
spec = importlib.util.spec_from_file_location('provisioning', SCRIPT)
platform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform)


class ProvisioningChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / 'prefix with spaces'
        self.runtime = self.root / 'runtime with spaces'
        self.version = self.runtime / 'System/Library/CoreServices/SystemVersion.plist'
        self.version.parent.mkdir(parents=True)
        self.metadata = {'ProductName': 'Darling', 'ProductVersion': '11.7.4', 'ProductBuildVersion': 'Darling-test'}
        self.version.write_bytes(plistlib.dumps(self.metadata))
        self.command = [sys.executable, str(SCRIPT), '--prefix', str(self.prefix),
                        '--provision', '--runtime-root', str(self.runtime)]

    def run_cli(self, *args):
        result = subprocess.run([*self.command, *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_generated_shapes_uuid_byte_order_and_prefix_independence(self):
        values = [platform.provision({}) for _ in range(20)]
        for value in values:
            for key, length in [('IOPlatformSerialNumber', 12), ('mlb-serial-number', 17)]:
                self.assertEqual(len(value[key]), length)
                self.assertTrue(set(value[key]) <= set(platform.BASE34))
            parsed = uuid.UUID(value['IOPlatformUUID'])
            self.assertEqual(parsed.version, 4)
            self.assertEqual(parsed.variant, uuid.RFC_4122)
            self.assertEqual(value['system-id'], parsed.bytes_le)
            self.assertEqual(value['provisioning_UDID'], str(parsed).upper())
            self.assertEqual(len(value['ROM']), 6)
            # A Mac's en0 is globally administered unicast under an Apple OUI.
            self.assertEqual(value['ROM'][0] & 3, 0)
            self.assertIn(value['ROM'][:3], platform.APPLE_OUIS)
        self.assertEqual(len({v['IOPlatformUUID'] for v in values}), len(values))

    def test_system_version_normalized_to_one_macos_identity(self):
        stale = {'ProductName': 'macOS', 'ProductVersion': '11.7.4',
                 'ProductUserVisibleVersion': '11.7.4', 'ProductBuildVersion': 'Darling',
                 'iOSSupportVersion': '14.7', 'ProductCopyright': '2012-2023 Lubos Dolezel'}
        out = plistlib.loads(platform.normalize_system_version(stale))
        self.assertEqual(out['ProductVersion'], platform.EMULATED_PRODUCT_VERSION)
        self.assertEqual(out['ProductUserVisibleVersion'], platform.EMULATED_PRODUCT_VERSION)
        self.assertEqual(out['ProductBuildVersion'], platform.EMULATED_BUILD_VERSION)
        self.assertNotIn('Darling', out['ProductBuildVersion'])
        # Unrelated keys survive; the Darling copyright is replaced.
        self.assertEqual(out['iOSSupportVersion'], '14.7')
        self.assertNotIn('Dolezel', out['ProductCopyright'])
        self.assertIn('Apple', out['ProductCopyright'])

    def test_preview_does_not_create_prefix(self):
        preview = json.loads(self.run_cli().stdout)
        self.assertIn('provisioning_UDID', preview['properties'])
        self.assertFalse(self.prefix.exists())

    def test_write_three_files_idempotent_and_importable(self):
        self.run_cli('--write')
        canonical = platform.destination(self.prefix)
        legacy = self.prefix / 'private/etc/darling-smbios.conf'
        version = self.prefix / 'System/Library/CoreServices/SystemVersion.plist'
        self.assertEqual(platform.load(canonical), platform.load(legacy))
        # The plist is normalized to the emulated macOS identity, not copied.
        written = plistlib.loads(version.read_bytes())
        self.assertEqual(written['ProductVersion'], platform.EMULATED_PRODUCT_VERSION)
        self.assertEqual(written['ProductUserVisibleVersion'], platform.EMULATED_PRODUCT_VERSION)
        self.assertEqual(written['ProductBuildVersion'], platform.EMULATED_BUILD_VERSION)
        self.assertEqual(written['ProductName'], 'macOS')
        files = [canonical, legacy, version]
        snapshot = [(f.read_bytes(), f.stat().st_mtime_ns) for f in files]
        self.assertIn('unchanged', self.run_cli('--write').stdout)
        self.assertEqual(snapshot, [(f.read_bytes(), f.stat().st_mtime_ns) for f in files])
        for f in [canonical, legacy]:
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_known_defaults_replaced_and_custom_values_preserved(self):
        legacy = self.prefix / 'private/etc/darling-smbios.conf'
        legacy.parent.mkdir(parents=True)
        original = (f'SMBIOS_SERIAL_NUMBER={platform.KNOWN_SERIAL}\n'
                    f'SMBIOS_PLATFORM_UUID={platform.KNOWN_UUID}\n'
                    'SMBIOS_MLB=CUSTOM-BOARD\nSMBIOS_MODEL=CustomModel\nSMBIOS_ROM=001122334455\n').encode()
        legacy.write_bytes(original)
        self.run_cli('--write')
        result = platform.load(platform.destination(self.prefix))
        self.assertNotEqual(result['IOPlatformSerialNumber'], platform.KNOWN_SERIAL)
        self.assertNotEqual(result['IOPlatformUUID'], platform.KNOWN_UUID)
        self.assertEqual(result['mlb-serial-number'], 'CUSTOM-BOARD')
        self.assertEqual(result['model'], 'CustomModel')
        self.assertEqual(result['ROM'], bytes.fromhex('001122334455'))
        self.assertEqual(legacy.with_name(legacy.name+'.before-platform-change').read_bytes(), original)
        before = result.copy()
        self.run_cli('--write')
        self.assertEqual(platform.load(platform.destination(self.prefix)), before)

    def test_custom_canonical_identity_wins_over_legacy_defaults(self):
        custom = platform.provision({})
        platform.save(self.prefix, custom)
        legacy = self.prefix / 'private/etc/darling-smbios.conf'
        legacy.write_text(f'SMBIOS_SERIAL_NUMBER={platform.KNOWN_SERIAL}\nSMBIOS_PLATFORM_UUID={platform.KNOWN_UUID}\n')
        self.run_cli('--write')
        self.assertEqual(platform.load(platform.destination(self.prefix)), custom)

    def test_explicit_oui_and_existing_rom(self):
        value = platform.provision({}, bytes.fromhex('001122'))
        self.assertEqual(value['ROM'][:3], bytes.fromhex('001122'))
        self.assertEqual(platform.provision(value, bytes.fromhex('AABBCC')), value)
        for oui in ['1234', '011122', 'garbage']:
            result = subprocess.run([*self.command, '--rom-oui', oui, '--write'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(platform.destination(self.prefix).exists())

    def test_version_source_required_before_identity_written(self):
        for raw in [b'not a plist', plistlib.dumps({'ProductVersion': '99.0'})]:
            self.version.write_bytes(raw)
            result = subprocess.run([*self.command, '--write'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(platform.destination(self.prefix).exists())
            self.assertFalse((self.prefix / 'private/etc/darling-smbios.conf').exists())
        self.version.unlink()
        result = subprocess.run([*self.command, '--write'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)

    def test_system_version_backup_and_symlink_preflight(self):
        target = self.prefix / 'System/Library/CoreServices/SystemVersion.plist'
        target.parent.mkdir(parents=True)
        original = plistlib.dumps({'ProductVersion': 'old'})
        target.write_bytes(original)
        self.run_cli('--write')
        self.assertEqual(target.with_name(target.name+'.before-platform-change').read_bytes(), original)
        self.assertEqual(plistlib.loads(target.read_bytes()),
                         plistlib.loads(platform.normalize_system_version(self.metadata)))
        target.unlink()
        target.symlink_to(self.version)
        before = platform.destination(self.prefix).read_bytes()
        result = subprocess.run([*self.command, '--set', 'model=Changed', '--write'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(platform.destination(self.prefix).read_bytes(), before)

    def test_live_prefix_is_not_changed(self):
        self.prefix.mkdir()
        (self.prefix / '.init.pid').write_text(str(os.getpid()))
        result = subprocess.run([*self.command, '--write'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(platform.destination(self.prefix).exists())

    def test_concurrent_writers_share_one_identity(self):
        workers = [subprocess.Popen([*self.command, '--write'], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True) for _ in range(4)]
        for worker in workers:
            output, error = worker.communicate(timeout=15)
            self.assertEqual(worker.returncode, 0, error)
        canonical = platform.destination(self.prefix)
        self.assertEqual(platform.load(canonical), platform.load(self.prefix / 'private/etc/darling-smbios.conf'))
        self.assertFalse(canonical.with_name(canonical.name+'.before-platform-change').exists())

    def test_custom_system_id_is_preserved_and_udid_mismatch_rejected(self):
        value = platform.provision({})
        self.assertEqual(platform.provision({'provisioning_UDID': value['provisioning_UDID']})['IOPlatformUUID'], value['IOPlatformUUID'])
        supplied = {'system-id': value['system-id']}
        self.assertEqual(platform.provision(supplied)['IOPlatformUUID'], value['IOPlatformUUID'])
        with self.assertRaises(ValueError):
            platform.validate({'IOPlatformUUID': value['IOPlatformUUID'], 'provisioning_UDID': str(uuid.uuid4())})

    def test_replace_known_defaults_only_touches_shared_identities(self):
        replace = [sys.executable, str(SCRIPT), '--prefix', str(self.prefix), '--runtime-root',
                   str(self.runtime), '--replace-known-defaults', '--write']
        # A missing prefix is not created or provisioned.
        result = subprocess.run(replace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.prefix.exists())
        # A custom identity stays byte-for-byte unchanged.
        custom = {'IOPlatformSerialNumber': 'MY-SERIAL', 'model': 'CustomModel'}
        platform.save(self.prefix, custom)
        canonical = platform.destination(self.prefix)
        before = canonical.read_bytes()
        result = subprocess.run(replace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(canonical.read_bytes(), before)
        self.assertFalse((self.prefix / 'private/etc/darling-smbios.conf').exists())
        # The shared values are replaced; custom fields survive and a rerun is stable.
        platform.save(self.prefix, {**custom, 'IOPlatformSerialNumber': platform.KNOWN_SERIAL,
                                    'IOPlatformUUID': platform.KNOWN_UUID})
        result = subprocess.run(replace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = platform.load(canonical)
        self.assertNotEqual(value['IOPlatformSerialNumber'], platform.KNOWN_SERIAL)
        self.assertNotEqual(value['IOPlatformUUID'], platform.KNOWN_UUID)
        self.assertFalse(platform.has_known_defaults(value))
        self.assertEqual(value['model'], 'CustomModel')
        self.assertNotIn('board-id', value)
        self.assertNotIn('manufacturer', value)
        self.assertEqual(platform.load(self.prefix / 'private/etc/darling-smbios.conf'), value)
        after = canonical.read_bytes()
        subprocess.run(replace, capture_output=True, text=True, check=True)
        self.assertEqual(canonical.read_bytes(), after)

    def test_replace_known_defaults_provisions_a_prefix_without_identity(self):
        # iokitd reports a DARLING- serial and a host-derived UUID when no
        # identity is saved, so an existing prefix in that state is provisioned.
        (self.prefix / 'private/etc').mkdir(parents=True)
        replace = [sys.executable, str(SCRIPT), '--prefix', str(self.prefix), '--runtime-root',
                   str(self.runtime), '--replace-known-defaults', '--write']
        result = subprocess.run(replace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        canonical = platform.destination(self.prefix)
        value = platform.load(canonical)
        self.assertFalse(platform.has_known_defaults(value))
        self.assertFalse(value['IOPlatformSerialNumber'].startswith('DARLING-'))
        self.assertIn('IOPlatformUUID', value)
        # The new identity is saved, so a rerun changes nothing.
        before = canonical.read_bytes()
        subprocess.run(replace, capture_output=True, text=True, check=True)
        self.assertEqual(canonical.read_bytes(), before)

    def test_standalone_fields_derived_from_known_uuid_are_replaced(self):
        known_id = uuid.UUID(platform.KNOWN_UUID).bytes_le
        for supplied in [{'system-id': known_id}, {'provisioning_UDID': platform.KNOWN_UUID}]:
            value = platform.provision(supplied)
            self.assertNotEqual(value['IOPlatformUUID'], platform.KNOWN_UUID)
            self.assertNotEqual(value['system-id'], known_id)

    def test_normalize_system_version_updates_existing_prefix_without_touching_identity(self):
        normalize = [sys.executable, str(SCRIPT), '--prefix', str(self.prefix),
                     '--runtime-root', str(self.runtime), '--normalize-system-version', '--write']
        # Missing prefix is not created.
        result = subprocess.run(normalize, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.prefix.exists())

        # Existing prefix with no identity files gets normalized SystemVersion.plist only.
        target = self.prefix / 'System/Library/CoreServices/SystemVersion.plist'
        target.parent.mkdir(parents=True)
        stale = plistlib.dumps({'ProductName': 'macOS', 'ProductVersion': '11.7.4',
                                'ProductBuildVersion': 'Darling'})
        target.write_bytes(stale)
        result = subprocess.run(normalize, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('saved', result.stdout)
        self.assertEqual(target.with_name(target.name + '.before-platform-change').read_bytes(), stale)
        self.assertEqual(plistlib.loads(target.read_bytes()),
                         plistlib.loads(platform.normalize_system_version(self.metadata)))
        self.assertFalse(platform.destination(self.prefix).exists())
        self.assertFalse((self.prefix / 'private/etc/darling-smbios.conf').exists())

        # Rerun is idempotent and leaves mtime untouched.
        before_stat = (target.read_bytes(), target.stat().st_mtime_ns)
        result = subprocess.run(normalize, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('unchanged', result.stdout)
        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before_stat)

        # Custom identity survives byte-for-byte while a stale plist is refreshed.
        custom = {'IOPlatformSerialNumber': 'MY-SERIAL', 'model': 'CustomModel'}
        platform.save(self.prefix, custom)
        canonical = platform.destination(self.prefix)
        before_id = canonical.read_bytes()
        target.write_bytes(stale)
        result = subprocess.run(normalize, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(canonical.read_bytes(), before_id)
        self.assertEqual(plistlib.loads(target.read_bytes())['ProductBuildVersion'],
                         platform.EMULATED_BUILD_VERSION)

        # Rejects live prefix and conflicting flags.
        (self.prefix / '.init.pid').write_text(str(os.getpid()))
        live = subprocess.run(normalize, capture_output=True, text=True)
        self.assertNotEqual(live.returncode, 0)
        (self.prefix / '.init.pid').unlink()
        conflict = subprocess.run([*normalize, '--provision'], capture_output=True, text=True)
        self.assertNotEqual(conflict.returncode, 0)

    def test_regenerate_rotates_identifiers_while_preserving_model_and_honouring_overrides(self):
        initial = {
            'IOPlatformSerialNumber': 'CUSTOMSER123',
            'mlb-serial-number': 'CUSTOMMLB12345678',
            'IOPlatformUUID': '11111111-2222-4333-8444-555555555555',
            'ROM': bytes.fromhex('021122334455'),
            'model': 'CustomModel1,1',
            'board-id': 'Mac-CUSTOMBOARD',
            'manufacturer': 'CustomVendor',
        }
        platform.save(self.prefix, initial)
        canonical = platform.destination(self.prefix)
        before_bytes = canonical.read_bytes()

        regen = [sys.executable, str(SCRIPT), '--prefix', str(self.prefix),
                 '--runtime-root', str(self.runtime), '--regenerate']
        # Preview does not touch the prefix.
        preview = subprocess.run(regen, capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(canonical.read_bytes(), before_bytes)
        preview_props = json.loads(preview.stdout)['properties']
        self.assertNotEqual(preview_props['IOPlatformUUID'], initial['IOPlatformUUID'])
        self.assertEqual(preview_props['model'], 'CustomModel1,1')

        # Write rotates all generated identifiers while keeping hardware fields.
        write = subprocess.run([*regen, '--rom-oui', '00:11:22', '--write'],
                               capture_output=True, text=True)
        self.assertEqual(write.returncode, 0, write.stderr)
        after = platform.load(canonical)
        self.assertEqual(after, platform.load(self.prefix / 'private/etc/darling-smbios.conf'))
        self.assertNotEqual(after['IOPlatformSerialNumber'], initial['IOPlatformSerialNumber'])
        self.assertNotEqual(after['mlb-serial-number'], initial['mlb-serial-number'])
        self.assertNotEqual(after['IOPlatformUUID'], initial['IOPlatformUUID'])
        self.assertNotEqual(after['provisioning_UDID'], initial['IOPlatformUUID'])
        self.assertNotEqual(after['system-id'], uuid.UUID(initial['IOPlatformUUID']).bytes_le)
        self.assertEqual(after['ROM'][:3], bytes.fromhex('001122'))
        self.assertEqual(after['model'], 'CustomModel1,1')
        self.assertEqual(after['board-id'], 'Mac-CUSTOMBOARD')
        self.assertEqual(after['manufacturer'], 'CustomVendor')

        # Explicit --set overrides survive --regenerate.
        explicit_uuid = '99999999-8888-4777-8666-555555555555'
        override = subprocess.run([*regen, '--set', f'IOPlatformUUID={explicit_uuid}', '--write'],
                                  capture_output=True, text=True)
        self.assertEqual(override.returncode, 0, override.stderr)
        self.assertEqual(platform.load(canonical)['IOPlatformUUID'], explicit_uuid)

        # --no-backup skips writing *.before-platform-change inside the prefix.
        for backup in (
            canonical.with_name(canonical.name + '.before-platform-change'),
            self.prefix / 'private/etc/darling-smbios.conf.before-platform-change',
        ):
            backup.unlink(missing_ok=True)
        no_backup = subprocess.run([*regen, '--no-backup', '--write'], capture_output=True, text=True)
        self.assertEqual(no_backup.returncode, 0, no_backup.stderr)
        self.assertFalse(canonical.with_name(canonical.name + '.before-platform-change').exists())
        self.assertFalse((self.prefix / 'private/etc/darling-smbios.conf.before-platform-change').exists())

        # Rejects conflicting flags and live prefixes.
        for bad in (['--replace-known-defaults'], ['--normalize-system-version']):
            conflict = subprocess.run([*regen, *bad], capture_output=True, text=True)
            self.assertNotEqual(conflict.returncode, 0)
        (self.prefix / '.init.pid').write_text(str(os.getpid()))
        live = subprocess.run([*regen, '--write'], capture_output=True, text=True)
        self.assertNotEqual(live.returncode, 0)
