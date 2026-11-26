"""Tests for iokitd IODeviceTree:/options (IODTNVRAM) and host UEFI Secure Boot reporting."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/iokitd.patch'
HEADER = 'src/HostSecureBoot.h'


def new_file_from_patch(relpath):
    patch = PATCH.read_text()
    section = re.search(
        r'(?ms)^diff --git a/%s b/%s\n.*?^@@ [^\n]*\n(.*?)(?=^diff --git |\Z)'
        % (re.escape(relpath), re.escape(relpath)),
        patch,
    )
    if not section:
        raise AssertionError(f'{relpath} is not a new file in patches/iokitd.patch')
    lines = section.group(1).splitlines(keepends=True)
    if not all(line.startswith('+') for line in lines):
        raise AssertionError(f'{relpath} section has non-added lines')
    return ''.join(line[1:] for line in lines)


class IokitSecureBootTests(unittest.TestCase):
    def test_iodtnvram_registered_at_iodevicetree_options(self):
        patch = PATCH.read_text()
        nvram_mm = new_file_from_patch('src/IODTNVRAM.mm')
        self.assertIn('nvram->registerInPlane(kIODeviceTreePlane, "options", IORegistryEntry::root());', nvram_mm)
        self.assertIn('94b73556-2197-4702-82a8-3e1337dafbfb:AppleSecureBootPolicy', nvram_mm)
        self.assertIn('HostSecureBoot::hostPolicyByte()', nvram_mm)
        self.assertIn('+	src/IODTNVRAM.mm', patch)
        self.assertIn('+	IODTNVRAM::registerSelf(registry);', patch)

    @unittest.skipUnless(shutil.which('c++'), 'no C++ compiler')
    def test_host_secure_boot_efivar_parsing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'HostSecureBoot.h').write_text(new_file_from_patch(HEADER))

            # Fixture 1: UEFI SecureBoot enabled (4-byte attrs + 0x01).
            enabled_root = root / 'enabled'
            efivar_rel = Path('sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c')
            (enabled_root / efivar_rel).parent.mkdir(parents=True)
            (enabled_root / efivar_rel).write_bytes(b'\x06\x00\x00\x00\x01')

            # Fixture 2: UEFI SecureBoot disabled (4-byte attrs + 0x00).
            disabled_root = root / 'disabled'
            (disabled_root / efivar_rel).parent.mkdir(parents=True)
            (disabled_root / efivar_rel).write_bytes(b'\x06\x00\x00\x00\x00')

            # Fixture 3: Truncated efivar (< 5 bytes).
            trunc_root = root / 'truncated'
            (trunc_root / efivar_rel).parent.mkdir(parents=True)
            (trunc_root / efivar_rel).write_bytes(b'\x06\x00\x00')

            # Fixture 4: Missing efivar.
            missing_root = root / 'missing'
            missing_root.mkdir()

            cpp = root / 'check.cpp'
            exe = root / 'check'
            cpp.write_text(f'''
#include "HostSecureBoot.h"
#include <cassert>

int main() {{
    assert(HostSecureBoot::policyByteForSystemRoot("{enabled_root}", false) == 0x02);
    assert(HostSecureBoot::policyByteForSystemRoot("{disabled_root}", false) == 0x00);
    assert(HostSecureBoot::policyByteForSystemRoot("{trunc_root}", false) == 0x00);
    assert(HostSecureBoot::policyByteForSystemRoot("{missing_root}", false) == 0x00);
    return 0;
}}
''')
            subprocess.run(['c++', '-std=c++17', '-Wall', '-Werror', str(cpp), '-o', str(exe)], check=True)
            subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    unittest.main()
