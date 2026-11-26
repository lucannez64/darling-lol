"""Verify darlingserver duct-tape kext_request() implementation.

Compiles duct-tape/src/kext_info.c directly from patches/darlingserver.patch
in standalone mode (-DDTAPE_KEXT_INFO_STANDALONE) and exercises every read-only
OSKext predicate, filtering path, XML edge case, and privilege rejection path.
"""
import base64
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/darlingserver.patch'

KEXT_ERR_NOT_PRIVILEGED = 0xDC008004 - (1 << 32)
KEXT_ERR_INVALID_ARGUMENT = 0xDC008005 - (1 << 32)
KEXT_ERR_NOT_FOUND = 0xDC008006 - (1 << 32)
KEXT_ERR_UNSUPPORTED = 0xDC008009 - (1 << 32)
PROFILE_ENV = 'RCLOL_PLATFORM_PROFILE'
GET_LOADED = (
    b'<dict><key>Kext Request Predicate</key>'
    b'<string>Get Loaded Kext Info</string></dict>\0'
)


def lookup_request(value_xml: str, prefix_xml: str = '') -> bytes:
    """A Get Kext UUID by Address request whose lookup address is value_xml."""
    return (
        '<dict>' + prefix_xml +
        '<key>Kext Request Predicate</key>'
        '<string>Get Kext UUID by Address</string>'
        '<key>Kext Request Arguments</key><dict>'
        f'<key>Kext Request Lookup Address</key>{value_xml}'
        '</dict></dict>\0'
    ).encode('utf-8')


def patch_section(target: str) -> str:
    text = PATCH.read_text()
    marker = f'diff --git a/{target} b/{target}\n'
    return text.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def extract_new_file_from_patch(target: str) -> str:
    section = patch_section(target)
    lines = []
    in_hunk = False
    for line in section.splitlines():
        if line.startswith('@@ '):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith('+'):
            lines.append(line[1:])
        elif line.startswith('\\'):
            continue
    return '\n'.join(lines) + '\n'


def parse_osserialize_xml(xml_text: str):
    """Wrap bare OSSerialize XML in <plist> for Python's plistlib."""
    wrapped = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<plist version="1.0">' + xml_text + '</plist>'
    )
    return plistlib.loads(wrapped.encode('utf-8'))


HARNESS_C = r'''
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern int32_t dtape_kext_handle_request(
    const char* request_xml,
    size_t request_len,
    bool is_privileged,
    char** response_out,
    size_t* response_len_out);
extern bool dtape_kext_catalog_enabled(void);

int main(int argc, char** argv) {
    bool is_priv = (argc > 1 && strcmp(argv[1], "priv") == 0);
    /* kext_request() returns KERN_NOT_SUPPORTED before parsing when this is false. */
    if (!dtape_kext_catalog_enabled()) {
        printf("not-supported\n");
        return 0;
    }
    size_t cap = 4096;
    size_t len = 0;
    char* buf = (char*)malloc(cap);
    if (!buf) {
        return 2;
    }
    for (;;) {
        if (len == cap) {
            cap *= 2;
            char* next = (char*)realloc(buf, cap);
            if (!next) {
                free(buf);
                return 2;
            }
            buf = next;
        }
        size_t n = fread(buf + len, 1, cap - len, stdin);
        len += n;
        if (n == 0) {
            break;
        }
    }

    char* resp = NULL;
    size_t resp_len = 0;
    int32_t op_res = dtape_kext_handle_request(buf, len, is_priv, &resp, &resp_len);

    int nul_ok = 1;
    if (resp != NULL && resp_len > 0) {
        if (resp[resp_len - 1] != '\0' || strlen(resp) + 1 != resp_len) {
            nul_ok = 0;
        }
    }

    printf("%d %zu %d\n", op_res, resp_len, nul_ok);
    if (resp != NULL && resp_len > 1) {
        fwrite(resp, 1, resp_len - 1, stdout);
    }
    free(resp);
    free(buf);
    return 0;
}
'''


class KextRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = None
        cls._bin = None
        if shutil.which('cc'):
            cls._tmpdir = tempfile.TemporaryDirectory()
            td = Path(cls._tmpdir.name)
            src = td / 'kext_info.c'
            harness = td / 'harness.c'
            binary = td / 'test_kext_info'
            src.write_text(extract_new_file_from_patch('duct-tape/src/kext_info.c'))
            harness.write_text(HARNESS_C)
            command = [
                # GCC folds the signed negation UBSan has to see; prefer clang.
                'clang' if shutil.which('clang') else 'cc',
                '-std=c11',
                '-Wall',
                '-Wextra',
                '-Werror',
                '-DDTAPE_KEXT_INFO_STANDALONE',
                str(src),
                str(harness),
                '-o',
                str(binary),
            ]
            # Undefined behaviour aborts the harness when the toolchain ships UBSan.
            sanitized = subprocess.run(
                command[:1] + ['-fsanitize=undefined', '-fno-sanitize-recover=all'] + command[1:],
                capture_output=True,
                text=True,
            )
            if sanitized.returncode != 0:
                subprocess.run(command, check=True, capture_output=True, text=True)
            cls._bin = binary

    @classmethod
    def tearDownClass(cls):
        if cls._tmpdir is not None:
            cls._tmpdir.cleanup()

    def run_harness(self, payload: bytes, privileged: bool = False, profile='compat'):
        self.assertIsNotNone(self._bin, 'C compiler required')
        args = [str(self._bin)]
        if privileged:
            args.append('priv')
        env = {k: v for k, v in os.environ.items() if k != PROFILE_ENV}
        if profile is not None:
            env[PROFILE_ENV] = profile
        return subprocess.run(
            args,
            input=payload,
            check=True,
            capture_output=True,
            env=env,
        )

    def invoke(self, payload: bytes, privileged: bool = False):
        proc = self.run_harness(payload, privileged)
        header, _, body = proc.stdout.partition(b'\n')
        op_res_s, resp_len_s, nul_ok_s = header.decode('ascii').split()
        return int(op_res_s), int(resp_len_s), int(nul_ok_s), body.decode('utf-8')

    def test_patch_wires_kext_info_into_duct_tape(self):
        cmake_sec = patch_section('duct-tape/CMakeLists.txt')
        self.assertIn('+\tsrc/kext_info.c', cmake_sec)
        stubs_sec = patch_section('duct-tape/src/stubs.c')
        self.assertIn('-kern_return_t kext_request(', stubs_sec)
        kext_src = extract_new_file_from_patch('duct-tape/src/kext_info.c')
        self.assertIn('kern_return_t kext_request(', kext_src)
        self.assertIn('vm_map_copy_discard(req_copy);', kext_src)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_get_loaded_kext_info_full_catalog_invariants(self):
        req = (
            b'<dict><key>Kext Request Predicate</key>'
            b'<string>Get Loaded Kext Info</string></dict>\0'
        )
        op_res, resp_len, nul_ok, xml = self.invoke(req)
        self.assertEqual(op_res, 0)
        self.assertEqual(nul_ok, 1)
        self.assertEqual(resp_len, len(xml.encode('utf-8')) + 1)

        # IOCFUnserialize requires strictly lowercase hex in <integer> tags.
        hex_ints = re.findall(r'<integer[^>]*>([^<]+)</integer>', xml)
        self.assertGreater(len(hex_ints), 200)
        for val in hex_ints:
            self.assertRegex(val, r'^0x[0-9a-f]+$')

        # No Darling branding anywhere in guest-visible output.
        self.assertNotIn('darling', xml.lower())

        info = parse_osserialize_xml(xml)
        self.assertIsInstance(info, dict)
        self.assertGreaterEqual(len(info), 65)

        # Expected MacBookPro16,1 / macOS 15.5 (Darwin 24.5.0) standard Apple kexts.
        for required in (
            '__kernel__',
            'com.apple.kpi.bsd',
            'com.apple.kpi.dsep',
            'com.apple.kpi.iokit',
            'com.apple.kpi.libkern',
            'com.apple.kpi.mach',
            'com.apple.kpi.private',
            'com.apple.kpi.unsupported',
            'com.apple.kec.corecrypto',
            'com.apple.driver.AppleMobileFileIntegrity',
            'com.apple.security.sandbox',
            'com.apple.filesystems.apfs',
            'com.apple.driver.AppleIntelCFLGraphicsFramebuffer',
            'com.apple.kext.AMDRadeonX6000',
            'com.apple.driver.AppleSEPManager',
            'com.apple.driver.AirPort.BrcmNIC',
        ):
            self.assertIn(required, info)

        kernel_uuid = info['__kernel__']['OSBundleUUID']
        self.assertEqual(len(kernel_uuid), 16)
        self.assertEqual(info['__kernel__']['CFBundleVersion'], '24.5.0')
        # Shared with Apple silicon: versions measured on macOS 15.5 (24F74).
        self.assertEqual(info['com.apple.filesystems.apfs']['CFBundleVersion'], '2332.120.31')
        self.assertEqual(info['com.apple.iokit.IOSurface']['CFBundleVersion'], '372.5.2')
        self.assertTrue(info['__kernel__']['OSBundleKextsInKernelText'])

        tags = set()
        ranges = []
        for bundle_id, entry in info.items():
            self.assertEqual(entry['CFBundleIdentifier'], bundle_id)
            self.assertEqual(entry['CFBundlePackageType'], 'KEXT')
            self.assertTrue(
                bundle_id == '__kernel__' or bundle_id.startswith('com.apple.'),
                f'Unexpected non-Apple kext: {bundle_id}',
            )
            tag = entry['OSBundleLoadTag']
            self.assertNotIn(tag, tags)
            tags.add(tag)
            self.assertTrue(entry['OSBundleStarted'])
            self.assertEqual(entry['_BundleCollectionType'], 'Primary')
            self.assertEqual(len(entry['OSBundleUUID']), 16)
            # RFC 4122 version 5 / variant 1 check.
            self.assertEqual(entry['OSBundleUUID'][6] & 0xF0, 0x50)
            self.assertEqual(entry['OSBundleUUID'][8] & 0xC0, 0x80)

            if entry['OSBundleIsInterface']:
                self.assertTrue(entry['OSKernelResource'])
                self.assertEqual(entry['CFBundleVersion'], '24.5.0')
                self.assertEqual(entry['OSBundleLoadAddress'], 0)
                self.assertEqual(entry['OSBundleLoadSize'], 0)
                self.assertEqual(entry['OSBundleUUID'], kernel_uuid)
            else:
                self.assertEqual(entry['OSBundleCPUType'], 0x01000007)
                self.assertEqual(entry['OSBundleCPUSubtype'], 3)
                addr = entry['OSBundleLoadAddress']
                size = entry['OSBundleLoadSize']
                self.assertGreaterEqual(addr, 0xFFFFFF8000200000)
                self.assertEqual(addr % 0x1000, 0)
                self.assertGreater(size, 0)
                self.assertEqual(size % 0x1000, 0)
                self.assertLessEqual(entry['OSBundleExecLoadSize'], size)
                self.assertEqual(entry['OSBundleWiredSize'], size - 0x1000)
                ranges.append((addr, addr + size, bundle_id))
                if bundle_id != '__kernel__':
                    self.assertTrue(
                        entry['OSBundlePath'].startswith('/System/Library/Extensions/')
                    )
                    self.assertTrue(
                        entry['OSBundleExecutablePath'].startswith(
                            entry['OSBundlePath'] + '/Contents/MacOS/'
                        )
                    )

            deps = entry.get('OSBundleDependencies', [])
            if deps:
                self.assertEqual(deps, sorted(deps, reverse=True))
                for dep_tag in deps:
                    self.assertLess(dep_tag, tag)

        # Tags are contiguous 0..N-1.
        self.assertEqual(tags, set(range(len(info))))

        # Non-overlapping memory ranges.
        ranges.sort()
        for idx in range(1, len(ranges)):
            prev_start, prev_end, prev_id = ranges[idx - 1]
            cur_start, _, cur_id = ranges[idx]
            self.assertLessEqual(
                prev_end,
                cur_start,
                f'Overlapping kext ranges: {prev_id} ({prev_start:#x}..{prev_end:#x}) vs {cur_id} ({cur_start:#x})',
            )

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_filters_and_idref_support(self):
        # Single string CFBundleIdentifier + Kext Request Info Keys + ID/IDREF.
        req = (
            b'<dict ID="0">'
            b'<key>Kext Request Predicate</key><string ID="1">Get Loaded Kext Info</string>'
            b'<key>Kext Request Arguments</key><dict ID="2">'
            b'<key>CFBundleIdentifier</key><array ID="3">'
            b'<string ID="4">com.apple.filesystems.apfs</string>'
            b'<string ID="5">com.apple.kext.AMDRadeonX6000</string>'
            b'<string IDREF="4"/>'
            b'</array>'
            b'<key>Kext Request Info Keys</key><array ID="6">'
            b'<string>CFBundleVersion</string>'
            b'<string>OSBundleLoadTag</string>'
            b'</array>'
            b'<key>Delay Autounload</key><true/>'
            b'</dict></dict>\0'
        )
        op_res, _, nul_ok, xml = self.invoke(req)
        self.assertEqual(op_res, 0)
        self.assertEqual(nul_ok, 1)
        info = parse_osserialize_xml(xml)
        self.assertEqual(
            set(info.keys()),
            {'com.apple.filesystems.apfs', 'com.apple.kext.AMDRadeonX6000'},
        )
        apfs = info['com.apple.filesystems.apfs']
        self.assertEqual(
            set(apfs.keys()),
            {'CFBundleIdentifier', 'CFBundlePackageType', 'CFBundleVersion', 'OSBundleLoadTag'},
        )
        self.assertEqual(apfs['CFBundleVersion'], '2332.120.31')

        # Unknown bundle identifier returns empty dict.
        miss_req = (
            b'<dict><key>Kext Request Predicate</key><string>Get Loaded Kext Info</string>'
            b'<key>Kext Request Arguments</key><dict>'
            b'<key>CFBundleIdentifier</key><string>com.example.nonexistent</string>'
            b'</dict></dict>\0'
        )
        op_res, _, _, xml = self.invoke(miss_req)
        self.assertEqual(op_res, 0)
        self.assertEqual(parse_osserialize_xml(xml), {})

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_uuid_collection_and_address_predicates(self):
        # Fetch full info first to get a known UUID and load address.
        full_req = (
            b'<dict><key>Kext Request Predicate</key>'
            b'<string>Get Loaded Kext Info</string></dict>\0'
        )
        op_res, _, _, full_xml = self.invoke(full_req)
        self.assertEqual(op_res, 0)
        full = parse_osserialize_xml(full_xml)
        apfs = full['com.apple.filesystems.apfs']
        apfs_uuid_bytes = apfs['OSBundleUUID']
        apfs_uuid_hex = apfs_uuid_bytes.hex()
        apfs_uuid_str = (
            f'{apfs_uuid_hex[0:8]}-{apfs_uuid_hex[8:12]}-{apfs_uuid_hex[12:16]}-'
            f'{apfs_uuid_hex[16:20]}-{apfs_uuid_hex[20:32]}'
        )

        # Get Loaded Kext Info By UUID (all) has no duplicate keys.
        by_uuid_all = (
            b'<dict><key>Kext Request Predicate</key>'
            b'<string>Get Loaded Kext Info By UUID</string></dict>\0'
        )
        op_res, _, _, uuid_xml = self.invoke(by_uuid_all)
        self.assertEqual(op_res, 0)
        uuid_dict = parse_osserialize_xml(uuid_xml)
        self.assertIn(apfs_uuid_str.upper(), uuid_dict)
        # 7 KPI interfaces share __kernel__'s UUID, so len(uuid_dict) == len(full) - 7.
        self.assertEqual(len(uuid_dict), len(full) - 7)

        # Case-insensitive UUID filter.
        by_uuid_one = (
            '<dict><key>Kext Request Predicate</key>'
            '<string>Get Loaded Kext Info By UUID</string>'
            '<key>Kext Request Arguments</key><dict>'
            f'<key>CFBundleIdentifier</key><array><string>{apfs_uuid_str.lower()}</string></array>'
            '</dict></dict>\0'
        ).encode('utf-8')
        op_res, _, _, one_xml = self.invoke(by_uuid_one)
        self.assertEqual(op_res, 0)
        one_dict = parse_osserialize_xml(one_xml)
        self.assertEqual(list(one_dict.keys()), [apfs_uuid_str.upper()])
        self.assertEqual(one_dict[apfs_uuid_str.upper()]['CFBundleIdentifier'], 'com.apple.filesystems.apfs')

        # Get Kext UUID by Address (hit and miss).
        addr = apfs['OSBundleLoadAddress'] + 0x100
        addr_req = (
            '<dict><key>Kext Request Predicate</key>'
            '<string>Get Kext UUID by Address</string>'
            '<key>Kext Request Arguments</key><dict>'
            f'<key>Kext Request Lookup Address</key><integer size="64">{hex(addr)}</integer>'
            '</dict></dict>\0'
        ).encode('utf-8')
        op_res, _, _, addr_xml = self.invoke(addr_req)
        self.assertEqual(op_res, 0)
        self.assertEqual(parse_osserialize_xml(addr_xml), apfs_uuid_bytes)

        miss_addr_req = (
            b'<dict><key>Kext Request Predicate</key>'
            b'<string>Get Kext UUID by Address</string>'
            b'<key>Kext Request Arguments</key><dict>'
            b'<key>Kext Request Lookup Address</key><integer size="64">0x1000</integer>'
            b'</dict></dict>\0'
        )
        op_res, resp_len, _, _ = self.invoke(miss_addr_req)
        self.assertEqual(op_res, KEXT_ERR_NOT_FOUND)
        self.assertEqual(resp_len, 0)

        # Get Kexts in Collection: Primary returns full set, Auxiliary returns empty.
        for coll, expected_nonempty in (('Primary', True), ('Any', True), ('Auxiliary', False)):
            coll_req = (
                '<dict><key>Kext Request Predicate</key>'
                '<string>Get Kexts in Collection</string>'
                '<key>Kext Request Arguments</key><dict>'
                f'<key>Collection Type</key><string>{coll}</string>'
                '</dict></dict>\0'
            ).encode('utf-8')
            op_res, _, _, coll_xml = self.invoke(coll_req)
            self.assertEqual(op_res, 0)
            parsed = parse_osserialize_xml(coll_xml)
            self.assertEqual(bool(parsed), expected_nonempty)

        # Get All Load Requests returns <set/>.
        load_reqs = (
            b'<dict><key>Kext Request Predicate</key>'
            b'<string>Get All Load Requests</string></dict>\0'
        )
        op_res, _, _, set_xml = self.invoke(load_reqs)
        self.assertEqual(op_res, 0)
        self.assertEqual(set_xml, '<set/>')

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_privileged_and_malformed_requests_rejected(self):
        # XNU's OSKext::handleRequest() refuses Daemon Ready without host_priv too.
        for pred in ('Load', 'Unload', 'Start', 'Stop', 'Get Kernel Requests', 'Daemon Ready'):
            req = (
                '<dict><key>Kext Request Predicate</key>'
                f'<string>{pred}</string></dict>\0'
            ).encode('utf-8')
            op_unpriv, len_unpriv, _, _ = self.invoke(req, privileged=False)
            self.assertEqual(op_unpriv, KEXT_ERR_NOT_PRIVILEGED)
            self.assertEqual(len_unpriv, 0)
            op_priv, len_priv, _, _ = self.invoke(req, privileged=True)
            self.assertEqual(op_priv, KEXT_ERR_UNSUPPORTED)
            self.assertEqual(len_priv, 0)

        # Binary MKXT magic rejected.
        op_res, _, _, _ = self.invoke(b'MKXT\x00\x00\x00\x00', privileged=False)
        self.assertEqual(op_res, KEXT_ERR_NOT_PRIVILEGED)

        # Empty / malformed XML rejected safely.
        for bad in (b'', b'not xml', b'<dict><key>Kext Request Predicate</key>', b'<dict/>'):
            op_res, resp_len, _, _ = self.invoke(bad)
            self.assertEqual(op_res, KEXT_ERR_INVALID_ARGUMENT)
            self.assertEqual(resp_len, 0)

    def test_kext_request_checks_profile_before_serving(self):
        kext_src = extract_new_file_from_patch('duct-tape/src/kext_info.c')
        body = kext_src.split('kern_return_t kext_request(', 1)[1]
        gate = body.index('if (!dtape_kext_catalog_enabled()) {')
        # The gate runs first and answers exactly like the stub it replaced.
        self.assertLess(gate, body.index('*op_result ='))
        self.assertLess(gate, body.index('dtape_kext_handle_request('))
        self.assertIn(
            'if (!dtape_kext_catalog_enabled()) {\n'
            '\t\t/* Same answer as the stub this file replaced. */\n'
            '\t\tdtape_stub_safe();\n'
            '\t\treturn KERN_NOT_SUPPORTED;\n'
            '\t}',
            body,
        )
        self.assertIn('getenv(DTAPE_KEXT_PROFILE_ENV)', kext_src)
        self.assertIn(f'#define DTAPE_KEXT_PROFILE_ENV "{PROFILE_ENV}"', kext_src)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_catalog_served_only_under_compat_profile(self):
        # profile=host, no profile and anything but the exact "compat" keep
        # kext_request() unsupported, as before the catalog existed.
        for profile in ('host', None, '', 'Compat', 'compat ', 'compatx'):
            with self.subTest(profile=profile):
                proc = self.run_harness(GET_LOADED, profile=profile)
                self.assertEqual(proc.stdout, b'not-supported\n')
        proc = self.run_harness(GET_LOADED, profile='compat')
        self.assertTrue(proc.stdout.startswith(b'0 '))

    def apfs_entry(self):
        op_res, _, _, xml = self.invoke(GET_LOADED)
        self.assertEqual(op_res, 0)
        return parse_osserialize_xml(xml)['com.apple.filesystems.apfs']

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_lookup_address_number_edge_cases(self):
        apfs = self.apfs_entry()
        addr = apfs['OSBundleLoadAddress'] + 0x100
        # A kernel address read as a signed 64-bit decimal is negative.
        for text in (
            str(addr - (1 << 64)),
            str(addr),
            hex(addr),
            '0x' + '0' * 8 + f'{addr:016x}',
            f' {hex(addr)} ',
        ):
            with self.subTest(text=text):
                op_res, _, _, xml = self.invoke(lookup_request(f'<integer size="64">{text}</integer>'))
                self.assertEqual(op_res, 0)
                self.assertEqual(parse_osserialize_xml(xml), apfs['OSBundleUUID'])
        # INT64_MIN and UINT64_MAX are valid values with no kext behind them;
        # negating INT64_MIN is checked by UBSan when the toolchain has it.
        for text in ('-9223372036854775808', '18446744073709551615', '0xffffffffffffffff'):
            with self.subTest(text=text):
                op_res, resp_len, _, _ = self.invoke(lookup_request(f'<integer>{text}</integer>'))
                self.assertEqual(op_res, KEXT_ERR_NOT_FOUND)
                self.assertEqual(resp_len, 0)
        # Values that do not fit in 64 bits or are not numbers are malformed,
        # not wrapped into some other address.
        for text in (
            '-9223372036854775809',
            '18446744073709551616',
            '0x1' + '0' * 16,
            f'0x1{addr:016x}',
            '12abc',
            '0x',
            '-',
        ):
            with self.subTest(text=text):
                op_res, resp_len, _, _ = self.invoke(lookup_request(f'<integer>{text}</integer>'))
                self.assertEqual(op_res, KEXT_ERR_INVALID_ARGUMENT)
                self.assertEqual(resp_len, 0)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_lookup_address_idref_resolves_earlier_integer(self):
        apfs = self.apfs_entry()
        addr = apfs['OSBundleLoadAddress'] + 0x100
        earlier = f'<key>Earlier</key><integer ID="9" size="64">{hex(addr)}</integer>'
        for ref in ('<integer IDREF="9"/>', '<reference IDREF="9"/>'):
            with self.subTest(ref=ref):
                op_res, _, _, xml = self.invoke(lookup_request(ref, earlier))
                self.assertEqual(op_res, 0)
                self.assertEqual(parse_osserialize_xml(xml), apfs['OSBundleUUID'])
        # An ID on the lookup address itself is accepted.
        op_res, _, _, xml = self.invoke(lookup_request(f'<integer ID="3">{hex(addr)}</integer>'))
        self.assertEqual(op_res, 0)
        self.assertEqual(parse_osserialize_xml(xml), apfs['OSBundleUUID'])
        # A reference to no earlier value, or to a string, is not address 0.
        string_id = '<key>Earlier</key><string ID="9">0x1000</string>'
        for ref, prefix in (
            ('<integer IDREF="42"/>', ''),
            ('<integer IDREF="42"/>', earlier),
            ('<integer IDREF="9"/>', string_id),
        ):
            with self.subTest(ref=ref, prefix=prefix):
                op_res, resp_len, _, _ = self.invoke(lookup_request(ref, prefix))
                self.assertEqual(op_res, KEXT_ERR_INVALID_ARGUMENT)
                self.assertEqual(resp_len, 0)
        # An integer ID is not a string: a CFBundleIdentifier reference to it fails.
        req = (
            '<dict>' + earlier +
            '<key>Kext Request Predicate</key><string>Get Loaded Kext Info</string>'
            '<key>Kext Request Arguments</key><dict>'
            '<key>CFBundleIdentifier</key><string IDREF="9"/>'
            '</dict></dict>\0'
        ).encode('utf-8')
        op_res, _, _, _ = self.invoke(req)
        self.assertEqual(op_res, KEXT_ERR_INVALID_ARGUMENT)


if __name__ == '__main__':
    unittest.main()
