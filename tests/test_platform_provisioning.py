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
            self.assertEqual(value['ROM'][0] & 3, 2)
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
        # A prefix without identity files is not created or provisioned.
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

    def test_standalone_fields_derived_from_known_uuid_are_replaced(self):
        known_id = uuid.UUID(platform.KNOWN_UUID).bytes_le
        for supplied in [{'system-id': known_id}, {'provisioning_UDID': platform.KNOWN_UUID}]:
            value = platform.provision(supplied)
            self.assertNotEqual(value['IOPlatformUUID'], platform.KNOWN_UUID)
            self.assertNotEqual(value['system-id'], known_id)
