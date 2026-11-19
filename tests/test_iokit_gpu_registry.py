"""Offline contracts for the synthetic IORegistry GPU entry.

Dawn's Metal backend resolves -[MTLDevice registryID] through
IOServiceGetMatchingService(IORegistryEntryIDMatching(...)), reads vendor-id and
device-id from the accelerator's parent and only then stops logging
"Failed to get the IO registry entry for the accelerator". These checks read the
patches, so they run without a prepared Darling tree.
"""
from pathlib import Path
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = 'patches/iokitd.patch'
IDS = 'src/IOGraphicsAcceleratorIds.h'
PROFILER = ROOT / 'tools/darling-system_profiler.sh'


def patch_section(path, patch=PATCH):
    """Every section for path, joined: a path may appear more than once."""
    marker = f'diff --git a/{path} b/{path}\n'
    parts = (ROOT / patch).read_text().split(marker)[1:]
    return '\n'.join(part.split('\ndiff --git ', 1)[0] for part in parts)


def added_lines(path, patch=PATCH):
    """The file content the patch adds."""
    section = patch_section(path, patch)
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


class IokitGpuRegistryTests(unittest.TestCase):
    def test_new_files_are_tracked_by_the_manifest(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        component = next(c for c in manifest['components'] if c['patch'] == PATCH)
        for path in ('src/IOGraphicsAccelerator.h', 'src/IOGraphicsAccelerator.mm', IDS):
            self.assertIn(path, component['new_files'])
            self.assertIn('new file mode', patch_section(path))
        files = set(re.findall(r'^diff --git \w/(\S+) ', (ROOT / PATCH).read_text(), re.M))
        self.assertEqual(component['modified_files'], len(files - set(component['new_files'])))

    def test_accelerator_is_built_and_registered(self):
        self.assertIn('src/IOGraphicsAccelerator.mm', added_lines('CMakeLists.txt'))
        main = added_lines('src/main.mm')
        self.assertIn('#include "IOGraphicsAccelerator.h"', main)
        self.assertIn('IOGraphicsAccelerator::registerSelf(registry);', main)

    def test_registry_id_matches_the_metal_device(self):
        mine = re.search(r'kIOGraphicsAcceleratorRegistryID = (0x[0-9a-fA-F]+)ULL;',
                         added_lines('src/IOGraphicsAccelerator.h'))
        theirs = re.search(r'kDefaultRegistryID = (0x[0-9a-fA-F]+)ULL;',
                           added_lines('private-include/Metal/MTLDeviceInfo.h', 'patches/metal.patch'))
        self.assertIsNotNone(mine)
        self.assertIsNotNone(theirs)
        self.assertEqual(mine.group(1).lower(), theirs.group(1).lower())

    def test_id_matching_uses_the_entry_id(self):
        service = added_lines('src/IOService.mm')
        self.assertIn('kIORegistryEntryIDKey', service)
        branch = service.split('kIORegistryEntryIDKey', 1)[1].split('if ([ourProp', 1)[0]
        self.assertIn('isKindOfClass: [NSNumber class]', branch)
        self.assertIn('[expectedValue unsignedLongLongValue] != id()', branch)

    def test_pinned_ids_are_exclusive(self):
        entry = added_lines('src/IORegistryEntry.h') + added_lines('src/IORegistryEntry.mm')
        self.assertIn('bool assignId(uint64_t id);', entry)
        method = entry.split('bool IORegistryEntry::assignId(uint64_t id)', 1)[1].split('\n}', 1)[0]
        self.assertIn('id == 0 || id == std::numeric_limits<uint64_t>::max()', method)
        self.assertIn('existing->second != this', method)
        self.assertIn('m_nextId = id + 1;', method)

    def test_accelerator_reports_the_host_or_compat_pci_ids(self):
        source = added_lines(IDS)
        for value in ('RCLOL_SYSTEM_ROOT', '"/sys/class/drm"', 'RCLOL_SYSTEM_PROP',
                      '".gpu.name"', '"mac"', '0x7340', '0x3e9b', '0x10de'):
            self.assertIn(value, source)
        accelerator = added_lines('src/IOGraphicsAccelerator.mm')
        self.assertIn('IOGraphicsAcceleratorIds::acceleratorIds()', accelerator)
        self.assertIn('dataForWord(m_vendor)', accelerator)

    def test_unknown_identity_publishes_no_entry(self):
        # Dawn trusts any entry it finds and reads zero IDs as vendor 0; only a
        # missing entry makes it fall back to the Metal device name.
        accelerator = added_lines('src/IOGraphicsAccelerator.mm')
        body = accelerator.split('void IOGraphicsAccelerator::registerSelf', 1)[1]
        guard = body.index('if (ids.vendor == 0 && ids.device == 0)\n\t\treturn;')
        self.assertLess(guard, body.index('new IOGraphicsAccelerator'))
        self.assertLess(guard, body.index('new IOPCIDevice'))
        self.assertLess(guard, body.index('registerService'))
        probe = (ROOT / 'tools/riot-registry-compatibility-check.mm').read_text()
        self.assertIn('assert(gpu == nullptr);', probe)

    def test_selection_follows_metal_and_keeps_unknown_unknown(self):
        source = added_lines(IDS)
        # The same selector sources Indium uses, and no unconditional card0.
        self.assertIn('"/tmp/metal-gpu"', source)
        self.assertIn('METAL_INDIUM_GPU', source)
        self.assertIn('return { 0, 0 };', source)
        self.assertNotIn('hostGpuIds', source)
        self.assertNotIn('card0', source)

    def test_profiler_resolution_is_shared(self):
        source = added_lines(IDS)
        # Last value wins, empty means host, NVIDIA keeps its own IDs.
        self.assertIn('readLastValue', source)
        self.assertIn('name.empty() ? std::string("host")', source)
        self.assertIn('selected.vendor != 0x1002 && selected.vendor != 0x8086', source)

    def test_compat_pci_ids_match_system_profiler(self):
        source = added_lines(IDS)
        profiler = (ROOT / 'tools/darling-system_profiler.sh').read_text()
        for vendor, device in (('0x1002', '0x7340'), ('0x8086', '0x3e9b')):
            self.assertIn(vendor, source)
            self.assertIn(device, source)
            self.assertIn(device[2:], profiler)

    def test_probe_exercises_the_registry_path(self):
        probe = (ROOT / 'tools/riot-registry-compatibility-check.mm').read_text()
        self.assertIn('static void checkGpuRegistry()', probe)
        self.assertIn('kIOGraphicsAcceleratorRegistryID', probe)
        self.assertIn('IOGraphicsAcceleratorIds::acceleratorIds()', probe)
        self.assertIn('IOGraphicsAcceleratorIds::hostCards()', probe)
        self.assertNotIn('vendorId == 0 || vendorId == 0x1002', probe)
        self.assertIn('checkGpuRegistry();', probe)
        self.assertIn('gpu_registry=1', probe)


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class IokitGpuIdsTests(unittest.TestCase):
    """Compile the host-side identity logic with the real patched header."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        header = self.root / 'IOGraphicsAcceleratorIds.h'
        header.write_text(added_lines(IDS))
        self.source = self.root / 'check.cc'
        self.source.write_text(r'''
#include <cstdio>
#include <cstdlib>
#include "IOGraphicsAcceleratorIds.h"
namespace G = IOGraphicsAcceleratorIds;

int main()
{
	const char* selector = getenv("TEST_SELECTOR");
	G::PciId ids = G::acceleratorIdsFor(G::hostCards(), selector ? selector : "",
	                                    G::hostGpuMode());
	printf("%s %04x %04x\n", G::hostGpuMode().c_str(), ids.vendor, ids.device);
	return 0;
}
''')
        self.binary = self.root / 'check'
        result = subprocess.run(['c++', '-std=c++17', '-O0', '-I', str(self.root),
                                 str(self.source), '-o', str(self.binary)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self.folder.cleanup()

    def card(self, directory, vendor, device):
        path = self.root / directory / 'sys/class/drm'
        for index, (vendor_id, device_id) in enumerate(zip(vendor, device)):
            device_dir = path / f'card{index}/device'
            device_dir.mkdir(parents=True)
            (device_dir / 'vendor').write_text(vendor_id + '\n')
            (device_dir / 'device').write_text(device_id + '\n')
        return self.root / directory

    def prop(self, name, text):
        path = self.root / name
        path.write_text(text)
        return path

    def run_ids(self, root, prop, selector='', **env):
        child = {'PATH': os.environ['PATH'],
                 'RCLOL_SYSTEM_ROOT': str(root),
                 'RCLOL_SYSTEM_PROP': str(prop)}
        child.update(env)
        if selector:
            child['TEST_SELECTOR'] = selector
        result = subprocess.run([str(self.binary)], env=child, check=True,
                                capture_output=True, text=True)
        mode, vendor, device = result.stdout.split()
        return mode, int(vendor, 16), int(device, 16)

    def profiler_ids(self, root, prop):
        env = {'PATH': os.environ['PATH'], 'USER': 'player',
               'RCLOL_SYSTEM_ROOT': str(root), 'RCLOL_SYSTEM_PROP': str(prop)}
        report = subprocess.run(['sh', str(PROFILER), '-xml', 'SPDisplaysDataType'],
                                env=env, check=True, capture_output=True, text=True)
        items = plistlib.loads(report.stdout.encode())[0]['_items']
        self.assertEqual(len(items), 1)
        device = re.fullmatch(r'0x([0-9a-f]+)', items[0]['spdisplays_device-id'])
        vendor = re.search(r'0x([0-9a-f]+)', items[0]['spdisplays_vendor'])
        return int(vendor.group(1), 16), int(device.group(1), 16)

    def test_single_gpu_matches_the_profiler_in_host_and_mac_modes(self):
        cases = [
            (('0x8086',), ('0x1234',), 'compat.gpu.name=mac\n', 0x8086, 0x3e9b),
            (('0x1002',), ('0x67ff',), 'compat.gpu.name=mac\n', 0x1002, 0x7340),
            (('0x10de',), ('0x1f91',), 'compat.gpu.name=mac\n', 0x10de, 0x1f91),
            (('0x8086',), ('0x1234',), 'compat.gpu.name=host\n', 0x8086, 0x1234),
            (('0x1002',), ('0x67ff',), 'compat.gpu.name=\n', 0x1002, 0x67ff),
            (('0x1002',), ('0x67ff',), 'profile=host\nhost.gpu.name=host\n', 0x1002, 0x67ff),
        ]
        for index, (vendors, devices, text, vendor, device) in enumerate(cases):
            with self.subTest(index=index):
                root = self.card(f'host{index}', vendors, devices)
                prop = self.prop(f'prop{index}', 'profile=compat\n' + text)
                mode, found_vendor, found_device = self.run_ids(root, prop)
                self.assertEqual((found_vendor, found_device), (vendor, device))
                self.assertEqual(self.profiler_ids(root, prop), (vendor, device))
                self.assertIn(mode, ('host', 'mac'))

    def test_last_gpu_name_wins_and_empty_means_host(self):
        root = self.card('host', ('0x8086',), ('0x3e9b',))
        cases = [
            ('profile=compat\ncompat.gpu.name=host\ncompat.gpu.name=mac\n', 'mac'),
            ('profile=compat\ncompat.gpu.name=mac\ncompat.gpu.name=host\n', 'host'),
            ('profile=compat\ncompat.gpu.name=\n', 'host'),
            ('profile=host\nhost.gpu.name=mac\n', 'mac'),
        ]
        for index, (text, mode) in enumerate(cases):
            with self.subTest(index=index):
                prop = self.prop(f'last{index}', text)
                self.assertEqual(self.run_ids(root, prop)[0], mode)

    def test_empty_profile_is_compat(self):
        root = self.card('host', ('0x8086',), ('0x1234',))
        prop = self.prop('empty', 'compat.gpu.name=mac\n')
        self.assertEqual(self.run_ids(root, prop), ('mac', 0x8086, 0x3e9b))

    def test_env_override_wins_like_the_profiler(self):
        root = self.card('host', ('0x8086',), ('0x1234',))
        prop = self.prop('prop', 'profile=compat\ncompat.gpu.name=mac\n')
        self.assertEqual(self.run_ids(root, prop, RCLOL_GPU_NAME='custom')[0], 'custom')
        self.assertEqual(self.run_ids(root, prop, RCLOL_GPU_NAME='mac')[1:], (0x8086, 0x3e9b))
        # The mac environment fallback only applies when the name is empty.
        empty = self.prop('empty', 'profile=compat\ncompat.gpu.name=\n')
        self.assertEqual(self.run_ids(root, empty, DARLING_METAL_MAC_GPU_NAME='1')[0], 'mac')
        self.assertEqual(self.run_ids(root, empty, DARLING_METAL_MAC_GPU_NAME='0')[0], 'host')
        # The launcher hands the profile over, so the file name is not read.
        self.assertEqual(self.run_ids(root, prop, RCLOL_PLATFORM_PROFILE='compat')[0], 'host')

    def test_hybrid_selection_never_publishes_the_other_card(self):
        root = self.card('hybrid', ('0x8086', '0x10de'), ('0x1234', '0x1f91'))
        prop = self.prop('prop', 'profile=compat\ncompat.gpu.name=mac\n')
        # Without a selector Metal's choice is unknown, not card0.
        self.assertEqual(self.run_ids(root, prop)[1:], (0, 0))
        self.assertEqual(self.run_ids(root, prop, selector='1')[1:], (0, 0))
        self.assertEqual(self.run_ids(root, prop, selector='llvmpipe')[1:], (0, 0))
        self.assertEqual(self.run_ids(root, prop, selector='amd')[1:], (0, 0))
        # A unique vendor hint maps back to its card; NVIDIA keeps its host IDs.
        self.assertEqual(self.run_ids(root, prop, selector='nvidia')[1:], (0x10de, 0x1f91))
        self.assertEqual(self.run_ids(root, prop, selector='Intel')[1:], (0x8086, 0x3e9b))
        # Two cards of the hinted vendor stay unknown.
        dual = self.card('dual', ('0x10de', '0x10de'), ('0x1f91', '0x1f92'))
        self.assertEqual(self.run_ids(dual, prop, selector='nvidia')[1:], (0, 0))
        # A single card is unambiguous only without a selector: '1' or
        # 'llvmpipe' can name the software device listed after it.
        single = self.card('single', ('0x10de',), ('0x1f91',))
        self.assertEqual(self.run_ids(single, prop)[1:], (0x10de, 0x1f91))
        self.assertEqual(self.run_ids(single, prop, selector='1')[1:], (0, 0))
        self.assertEqual(self.run_ids(single, prop, selector='llvmpipe')[1:], (0, 0))
        self.assertEqual(self.run_ids(single, prop, selector='nvidia')[1:], (0x10de, 0x1f91))


if __name__ == '__main__':
    unittest.main()
