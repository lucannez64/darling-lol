"""Offline contracts for the synthetic IORegistry GPU entry.

Dawn's Metal backend resolves -[MTLDevice registryID] through
IOServiceGetMatchingService(IORegistryEntryIDMatching(...)), reads vendor-id and
device-id from the accelerator's parent and only then stops logging
"Failed to get the IO registry entry for the accelerator". These checks read the
patches, so they run without a prepared Darling tree.
"""
from pathlib import Path
import json
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = 'patches/iokitd.patch'


def patch_section(path, patch=PATCH):
    """Every section for path, joined: a path may appear more than once."""
    marker = f'diff --git a/{path} b/{path}\n'
    parts = (ROOT / patch).read_text().split(marker)[1:]
    return '\n'.join(part.split('\ndiff --git ', 1)[0] for part in parts)


def added_lines(path, patch=PATCH):
    """Added file content: one diff marker plus, for new files, the content marker."""
    section = patch_section(path, patch)
    new_file = 'new file mode' in section
    lines = []
    for line in section.splitlines():
        if not line.startswith('+') or line.startswith('+++'):
            continue
        lines.append(line[2:] if new_file else line[1:])
    return '\n'.join(lines)


class IokitGpuRegistryTests(unittest.TestCase):
    def test_new_files_are_tracked_by_the_manifest(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        component = next(c for c in manifest['components'] if c['patch'] == PATCH)
        for path in ('src/IOGraphicsAccelerator.h', 'src/IOGraphicsAccelerator.mm'):
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
        source = added_lines('src/IOGraphicsAccelerator.mm')
        for value in ('@"vendor-id"', '@"device-id"', 'dataForWord(m_vendor)', 'dataForWord(m_device)'):
            self.assertIn(value, source)
        self.assertIn('"IOPCIDevice"', source)
        self.assertIn('"IOGraphicsAccelerator2"', source)
        self.assertIn('"IOAccelerator"', source)
        self.assertIn('registerInPlane(kIOServicePlane, "IOAccelerator", pciDevice)', source)
        self.assertIn('/Volumes/SystemRoot/sys/class/drm', source)
        self.assertIn('/private/etc/darling-system.prop', source)

    def test_compat_pci_ids_match_system_profiler(self):
        source = added_lines('src/IOGraphicsAccelerator.mm')
        profiler = (ROOT / 'tools/darling-system_profiler.sh').read_text()
        for vendor, device in (('0x1002', '0x7340'), ('0x8086', '0x3e9b')):
            self.assertIn(vendor, source)
            self.assertIn(device, source)
            self.assertIn(device[2:], profiler)

    def test_probe_exercises_the_registry_path(self):
        probe = (ROOT / 'tools/riot-registry-compatibility-check.mm').read_text()
        self.assertIn('static void checkGpuRegistry()', probe)
        self.assertIn('kIOGraphicsAcceleratorRegistryID', probe)
        self.assertIn('checkGpuRegistry();', probe)
        self.assertIn('gpu_registry=1', probe)


if __name__ == '__main__':
    unittest.main()
