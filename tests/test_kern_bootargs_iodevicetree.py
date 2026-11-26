"""Tests for kern.bootargs sysctl and IODeviceTree plane registration.

- kern.bootargs returns an empty string (matching a standard macOS boot without
  custom boot arguments) instead of ENOENT.
- IOPlatformExpertDevice is registered in both kIOServicePlane and
  kIODeviceTreePlane so IODeviceTree traversal and path queries succeed.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SYSCTL_KERN = 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/helper/misc/sysctl_kern.c'
PLATFORM_DEVICE = 'src/IOPlatformExpertDevice.mm'


def patch_section(patch_rel, target):
    patch = (ROOT / patch_rel).read_text()
    marker = f'diff --git a/{target} b/{target}\n'
    return patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def patch_added(patch_rel, target):
    return '\n'.join(
        line[1:]
        for line in patch_section(patch_rel, target).splitlines()
        if line.startswith('+') and not line.startswith('+++')
    )


class KernBootargsAndIODeviceTreeTests(unittest.TestCase):
    def test_kern_bootargs_sysctl_registered_and_returns_empty_string(self):
        added = patch_added('patches/xnu.patch', SYSCTL_KERN)
        self.assertIn('_KERN_BOOTARGS,', added)
        self.assertIn(
            '{ .oid = _KERN_BOOTARGS, .type = CTLTYPE_STRING, .exttype = "S", .name = "bootargs", .handler = handle_bootargs },',
            added,
        )
        self.assertIn('static sysctl_handler(handle_bootargs) {', added)
        self.assertIn('copyout_string("", (char*)old, oldlen);', added)

    def test_platform_expert_registered_in_iodevicetree_plane(self):
        section = patch_section('patches/iokitd.patch', PLATFORM_DEVICE)
        added = patch_added('patches/iokitd.patch', PLATFORM_DEVICE)
        self.assertIn(
            'device->registerInPlane(kIOServicePlane, "Platform", IORegistryEntry::root());',
            section,
        )
        self.assertIn(
            'device->registerInPlane(kIODeviceTreePlane, "Platform", IORegistryEntry::root());',
            added,
        )


if __name__ == '__main__':
    unittest.main()
