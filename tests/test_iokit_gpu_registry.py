"""Offline contracts for the synthetic IORegistry GPU entries.

Dawn's Metal backend resolves -[MTLDevice registryID] through
IOServiceGetMatchingService(IORegistryEntryIDMatching(...)), reads vendor-id and
device-id from the accelerator's parent and only then stops logging
"Failed to get the IO registry entry for the accelerator". iokitd publishes one
entry per host card at registryIdFor(card PCI IDs), and Metal reports the same
function of its Vulkan device's PCI IDs, so the lookup finds the card Metal
renders on even on a multi-GPU host. These checks read the patches, so they run
without a prepared Darling tree.
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
METAL_INFO = 'private-include/Metal/MTLDeviceInfo.h'
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


def registry_function(source):
    start = source.index('kRegistryIdBase = ')
    end = source.index('\n}\n', source.index('registryIdFor(uint32_t vendor, uint32_t device)'))
    # Drop qualifiers so the two copies compare on their logic alone.
    body = re.sub(r'\b(static|inline)\s+', '', source[start:end])
    return ' '.join(body.split())


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

    def test_registry_ids_are_derived_identically_in_metal(self):
        mine = registry_function(added_lines(IDS))
        theirs = registry_function(added_lines(METAL_INFO, 'patches/metal.patch'))
        self.assertEqual(mine, theirs)
        device = added_lines('src/Metal/MTLDevice.mm', 'patches/metal.patch')
        self.assertIn('- (uint64_t)registryID { return _registryID; }', device)
        self.assertIn('MTLDeviceInfo::registryIdFor(priv->properties().vendorID, '
                      'priv->properties().deviceID)', device)
        # The single shared constant is gone: a fixed ID can only name one card.
        self.assertNotIn('kIOGraphicsAcceleratorRegistryID', added_lines('src/IOGraphicsAccelerator.h'))

    def test_integrated_rule_is_identical_in_metal(self):
        def rule(source):
            start = source.index('bool presentedAsIntegrated(')
            end = source.index('\n}\n', start)
            return ' '.join(source[start:end].split())
        self.assertEqual(rule(added_lines(IDS)), rule(added_lines(METAL_INFO, 'patches/metal.patch')))
        device = added_lines('src/Metal/MTLDevice.mm', 'patches/metal.patch')
        self.assertIn('MTLDeviceInfo::macGpuName(priv->properties().vendorID, '
                      'priv->properties().deviceID, cards)', device)
        self.assertIn('presented_as_integrated "$card" && vendor=8086', PROFILER.read_text())

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

    def test_every_card_is_published_with_its_own_ids(self):
        source = added_lines(IDS)
        for value in ('RCLOL_SYSTEM_ROOT', '"/sys/class/drm"', 'RCLOL_SYSTEM_PROP',
                      '".gpu.name"', '"mac"', '0x7340', '0x3e9b', '0x10de'):
            self.assertIn(value, source)
        accelerator = added_lines('src/IOGraphicsAccelerator.mm')
        body = accelerator.split('void IOGraphicsAccelerator::registerSelf', 1)[1]
        self.assertIn('for (const auto& gpu : IOGraphicsAcceleratorIds::accelerators())', body)
        self.assertIn('new IOPCIDevice(gpu.presented.vendor, gpu.presented.device)', body)
        self.assertIn('dataForWord(m_vendor)', accelerator)
        # The ID is pinned before anything is registered, so a refused ID leaves
        # no orphan PCI device behind.
        pin = body.index('accelerator->assignId(gpu.registryId)')
        self.assertLess(pin, body.index('new IOPCIDevice'))
        self.assertLess(pin, body.index('registerService'))

    def test_selector_guessing_is_gone(self):
        # Metal's choice no longer has to be guessed from /tmp/metal-gpu: the ID
        # names the card Metal actually uses.
        source = added_lines(IDS)
        for name in ('readGpuSelector', 'cardForSelector', 'acceleratorIdsFor',
                     'getenv("METAL_INDIUM_GPU")', '"/tmp/metal-gpu"'):
            self.assertNotIn(name, source)
        self.assertNotIn('card0', source)

    def test_profiler_resolution_is_shared(self):
        source = added_lines(IDS)
        # Last value wins and empty means host.
        self.assertIn('readLastValue', source)
        self.assertIn('name.empty() ? std::string("host")', source)
        self.assertIn('host.vendor == 0x1002 || host.vendor == 0x10de', source)

    def test_compat_pci_ids_match_system_profiler(self):
        source = added_lines(IDS)
        profiler = (ROOT / 'tools/darling-system_profiler.sh').read_text()
        for vendor, device in (('0x1002', '0x7340'), ('0x8086', '0x3e9b')):
            self.assertIn(vendor, source)
            self.assertIn(device, source)
            self.assertIn(device[2:], profiler)
        self.assertIn("1002|10de) vendor=1002; device=7340", profiler)

    def test_probe_exercises_the_registry_path(self):
        probe = (ROOT / 'tools/riot-registry-compatibility-check.mm').read_text()
        self.assertIn('static void checkGpuRegistry()', probe)
        self.assertIn('IOGraphicsAcceleratorIds::accelerators()', probe)
        self.assertIn('IOGraphicsAcceleratorIds::hostCards()', probe)
        self.assertIn('IOGraphicsAcceleratorIds::registryIdFor(card.host.vendor, card.host.device)', probe)
        self.assertIn('assert(none == nullptr);', probe)
        self.assertNotIn('kIOGraphicsAcceleratorRegistryID', probe)
        self.assertIn('checkGpuRegistry();', probe)
        self.assertIn('gpu_registry=1', probe)


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class IokitGpuIdsTests(unittest.TestCase):
    """Compile the host-side identity logic with the real patched headers."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / 'IOGraphicsAcceleratorIds.h').write_text(added_lines(IDS))
        (self.root / 'MTLDeviceInfo.h').write_text(added_lines(METAL_INFO, 'patches/metal.patch'))
        self.source = self.root / 'check.cc'
        self.source.write_text(r'''
#include <cstdio>
#include <cstring>
#include "IOGraphicsAcceleratorIds.h"
#include "MTLDeviceInfo.h"
namespace G = IOGraphicsAcceleratorIds;

int main(int argc, char** argv)
{
	if (argc > 1 && std::strcmp(argv[1], "agree") == 0)
	{
		const uint32_t vendors[] = { 0x1002, 0x10de, 0x8086, 0x10005, 0 };
		for (uint32_t vendor : vendors)
			for (uint32_t device = 0; device <= 0xffff; ++device)
				if (G::registryIdFor(vendor, device) != MTLDeviceInfo::registryIdFor(vendor, device))
					return 1;
		return 0;
	}
	printf("%s", G::hostGpuMode().c_str());
	for (const auto& gpu : G::accelerators())
	{
		const char* metal = MTLDeviceInfo::macGpuName(gpu.host.vendor, gpu.host.device,
		                                              MTLDeviceInfo::hostCards());
		printf(" %llx:%04x:%04x:%llx:%s", (unsigned long long) gpu.registryId,
		       gpu.presented.vendor, gpu.presented.device,
		       (unsigned long long) MTLDeviceInfo::registryIdFor(gpu.host.vendor, gpu.host.device),
		       metal ? (std::strstr(metal, "Intel") ? "intel" : "amd") : "none");
	}
	printf("\n");
	return 0;
}
''')
        self.binary = self.root / 'check'
        result = subprocess.run(['c++', '-std=c++17', '-O0', '-Wall', '-I', str(self.root),
                                 str(self.source), '-o', str(self.binary)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self.folder.cleanup()

    def card(self, directory, vendor, device, vram=()):
        path = self.root / directory / 'sys/class/drm'
        for index, (vendor_id, device_id) in enumerate(zip(vendor, device)):
            device_dir = path / f'card{index}/device'
            device_dir.mkdir(parents=True)
            (device_dir / 'vendor').write_text(vendor_id + '\n')
            (device_dir / 'device').write_text(device_id + '\n')
            # amdgpu's mem_info_vram_total in bytes; None leaves it out (NVIDIA).
            if index < len(vram) and vram[index] is not None:
                (device_dir / 'mem_info_vram_total').write_text(f'{vram[index]}\n')
        return self.root / directory

    def prop(self, name, text):
        path = self.root / name
        path.write_text(text)
        return path

    def run_ids(self, root, prop, **env):
        """(mode, [(registry id, presented vendor, presented device)])."""
        child = {'PATH': os.environ['PATH'],
                 'RCLOL_SYSTEM_ROOT': str(root),
                 'RCLOL_SYSTEM_PROP': str(prop)}
        child.update(env)
        result = subprocess.run([str(self.binary)], env=child, check=True,
                                capture_output=True, text=True)
        mode, *entries = result.stdout.split()
        gpus = []
        for entry in entries:
            registry, vendor, device, metal_registry, metal_name = entry.split(':')
            # Metal derives the same ID for this card's Vulkan device...
            self.assertEqual(registry, metal_registry)
            # ...and, in mac mode, the name of the GPU iokitd presents.
            if mode == 'mac' and metal_name != 'none':
                self.assertEqual(metal_name, 'intel' if vendor == '8086' else 'amd')
            gpus.append((int(registry, 16), int(vendor, 16), int(device, 16)))
        return mode, gpus

    def presented(self, root, prop, **env):
        return [(vendor, device) for _, vendor, device in self.run_ids(root, prop, **env)[1]]

    def profiler_ids(self, root, prop):
        env = {'PATH': os.environ['PATH'], 'USER': 'player',
               'RCLOL_SYSTEM_ROOT': str(root), 'RCLOL_SYSTEM_PROP': str(prop)}
        report = subprocess.run(['sh', str(PROFILER), '-xml', 'SPDisplaysDataType'],
                                env=env, check=True, capture_output=True, text=True)
        ids = []
        for item in plistlib.loads(report.stdout.encode())[0]['_items']:
            device = re.fullmatch(r'0x([0-9a-f]+)', item['spdisplays_device-id'])
            vendor = re.search(r'0x([0-9a-f]+)', item['spdisplays_vendor'])
            ids.append((int(vendor.group(1), 16), int(device.group(1), 16)))
        return ids

    def test_registry_ids_agree_with_metal_for_every_device_id(self):
        result = subprocess.run([str(self.binary), 'agree'])
        self.assertEqual(result.returncode, 0)

    def test_single_gpu_matches_the_profiler_in_host_and_mac_modes(self):
        cases = [
            (('0x8086',), ('0x1234',), 'compat.gpu.name=mac\n', 0x8086, 0x3e9b),
            (('0x1002',), ('0x67ff',), 'compat.gpu.name=mac\n', 0x1002, 0x7340),
            (('0x10de',), ('0x1f91',), 'compat.gpu.name=mac\n', 0x1002, 0x7340),
            (('0x8086',), ('0x1234',), 'compat.gpu.name=host\n', 0x8086, 0x1234),
            (('0x10de',), ('0x1f91',), 'compat.gpu.name=host\n', 0x10de, 0x1f91),
            (('0x1002',), ('0x67ff',), 'compat.gpu.name=\n', 0x1002, 0x67ff),
            (('0x1002',), ('0x67ff',), 'profile=host\nhost.gpu.name=host\n', 0x1002, 0x67ff),
        ]
        for index, (vendors, devices, text, vendor, device) in enumerate(cases):
            with self.subTest(index=index):
                root = self.card(f'host{index}', vendors, devices)
                prop = self.prop(f'prop{index}', 'profile=compat\n' + text)
                self.assertEqual(self.presented(root, prop), [(vendor, device)])
                self.assertEqual(self.profiler_ids(root, prop), [(vendor, device)])

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
        self.assertEqual(self.run_ids(root, prop)[0], 'mac')
        self.assertEqual(self.presented(root, prop), [(0x8086, 0x3e9b)])

    def test_env_override_wins_like_the_profiler(self):
        root = self.card('host', ('0x8086',), ('0x1234',))
        prop = self.prop('prop', 'profile=compat\ncompat.gpu.name=mac\n')
        self.assertEqual(self.run_ids(root, prop, RCLOL_GPU_NAME='custom')[0], 'custom')
        self.assertEqual(self.presented(root, prop, RCLOL_GPU_NAME='mac'), [(0x8086, 0x3e9b)])
        # The mac environment fallback only applies when the name is empty.
        empty = self.prop('empty', 'profile=compat\ncompat.gpu.name=\n')
        self.assertEqual(self.run_ids(root, empty, DARLING_METAL_MAC_GPU_NAME='1')[0], 'mac')
        self.assertEqual(self.run_ids(root, empty, DARLING_METAL_MAC_GPU_NAME='0')[0], 'host')
        # The launcher hands the profile over, so the file name is not read.
        self.assertEqual(self.run_ids(root, prop, RCLOL_PLATFORM_PROFILE='compat')[0], 'host')

    def test_hybrid_host_publishes_every_card_like_the_profiler(self):
        prop = self.prop('prop', 'profile=compat\ncompat.gpu.name=mac\n')
        gib = 1 << 30
        mac_pair = [(0x8086, 0x3e9b), (0x1002, 0x7340)]
        cases = [
            # Intel iGPU + NVIDIA: the MacBookPro16,1 pair, whichever Metal picks.
            (('0x8086', '0x10de'), ('0x9a49', '0x2520'), (), mac_pair),
            # Ryzen 7000 APU (512 MiB carve-out) + RX 7800 XT: the APU is the
            # Mac's integrated GPU, never a second Radeon Pro 5500M (issue #31).
            (('0x1002', '0x1002'), ('0x164e', '0x747e'), (gib // 2, 16 * gib), mac_pair),
            # The dGPU probed first: the order of the cards does not matter.
            (('0x1002', '0x1002'), ('0x747e', '0x164e'), (16 * gib, gib // 2), mac_pair[::-1]),
            # AMD APU + NVIDIA (no mem_info_vram_total on NVIDIA).
            (('0x1002', '0x10de'), ('0x1681', '0x2560'), (gib // 2, None), mac_pair),
            # Two AMD cards that cannot be told apart stay two 5500Ms.
            (('0x1002', '0x1002'), ('0x164e', '0x744c'), (), [(0x1002, 0x7340), (0x1002, 0x7340)]),
        ]
        for index, (vendors, devices, vram, expected) in enumerate(cases):
            with self.subTest(index=index):
                root = self.card(f'hybrid{index}', vendors, devices, vram)
                mode, gpus = self.run_ids(root, prop)
                self.assertEqual([(v, d) for _, v, d in gpus], expected)
                self.assertEqual(len({registry for registry, _, _ in gpus}), len(gpus))
                self.assertEqual(self.profiler_ids(root, prop), expected)
        # In host mode every card keeps its own IDs.
        host = self.prop('host', 'profile=compat\ncompat.gpu.name=host\n')
        root = self.card('hybrid-host', ('0x8086', '0x10de'), ('0x9a49', '0x2520'))
        self.assertEqual(self.presented(root, host), [(0x8086, 0x9a49), (0x10de, 0x2520)])

    def test_lone_apu_stays_the_5500m(self):
        prop = self.prop('prop', 'profile=compat\ncompat.gpu.name=mac\n')
        root = self.card('apu', ('0x1002',), ('0x164e',), (1 << 29,))
        self.assertEqual(self.presented(root, prop), [(0x1002, 0x7340)])
        self.assertEqual(self.profiler_ids(root, prop), [(0x1002, 0x7340)])

    def test_identical_cards_share_one_entry(self):
        prop = self.prop('prop', 'profile=compat\ncompat.gpu.name=host\n')
        root = self.card('twins', ('0x10de', '0x10de'), ('0x2204', '0x2204'))
        self.assertEqual(self.presented(root, prop), [(0x10de, 0x2204)])


if __name__ == '__main__':
    unittest.main()
