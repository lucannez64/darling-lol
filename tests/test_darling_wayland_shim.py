import ctypes
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ['build-darling-wayland.sh', 'darling-wayland.c', 'darling-wayland-api.h',
         'darling-wayland-wrapper.c', 'test-darling-wayland.c']


def missing_dependencies():
    missing = [tool for tool in ('cc', 'pkg-config', 'wayland-scanner', 'nm') if not shutil.which(tool)]
    if not missing:
        modules = 'wayland-client wayland-egl xkbcommon wayland-protocols egl x11'.split()
        if subprocess.run(['pkg-config', '--exists', *modules]).returncode:
            missing.append('pkg-config modules ' + ' '.join(modules))
    return missing


@unittest.skipIf(missing_dependencies(), 'Wayland shim build dependencies missing: %s' % missing_dependencies())
class WaylandShimBuild(unittest.TestCase):
    def test_host_build_exports_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'tools').mkdir()
            for name in FILES:
                shutil.copy(ROOT / 'tools' / name, project / 'tools' / name)
            result = subprocess.run(['sh', str(project / 'tools/build-darling-wayland.sh'), '--host-only'],
                                    capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr)
            if 'skipping the experimental Wayland transport' in result.stderr:
                self.skipTest(result.stderr.strip())
            self.assertEqual(result.stderr.count('warning:'), 0, result.stderr)
            library = project / 'build/libdarling-wayland.next.so'
            self.assertTrue(library.is_file())
            self.assertTrue((project / 'build/test-darling-wayland').is_file())
            symbols = subprocess.run(['nm', '-D', '--defined-only', str(library)], capture_output=True, text=True,
                                     check=True).stdout
            self.assertIn(' DarlingWaylandAPI', symbols)
            self.assertNotIn(' dwl_', symbols)
            self.check_api_table(library)

    def check_api_table(self, library):
        header = (ROOT / 'tools/darling-wayland-api.h').read_text()
        declared = int(re.search(r'#define DWL_API_VERSION (\d+)', header).group(1))
        self.assertEqual(declared, 3)
        # struct dwl_api is a version word followed by function pointers, all 8 bytes on x86-64.
        body = header.split('struct dwl_api {', 1)[1].split('};', 1)[0]
        names = re.findall(r'\(\*(\w+)\)\(', body)
        lib = ctypes.CDLL(str(library))
        lib.DarlingWaylandAPI.restype = ctypes.c_void_p
        base = lib.DarlingWaylandAPI()
        self.assertTrue(base)
        self.assertEqual(ctypes.c_uint32.from_address(base).value, declared)
        slots = (ctypes.c_void_p * (len(names) + 1)).from_address(base)
        table = dict(zip(names, slots[1:]))
        for name in ('popup_create', 'popup_reposition', 'window_move', 'window_resize', 'window_set_parent',
                     'window_set_maximized', 'window_set_minimized'):
            self.assertIn(name, table)
        for name, pointer in table.items():
            self.assertTrue(pointer, 'api.%s is NULL' % name)

    def test_build_script_rejects_unknown_option(self):
        result = subprocess.run(['sh', str(ROOT / 'tools/build-darling-wayland.sh'), '--bogus'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_shm_buffering_flushing_and_handoff_tokens(self):
        shim = (ROOT / 'tools/darling-wayland.c').read_text()
        header = (ROOT / 'tools/darling-wayland-api.h').read_text()
        buffers = int(re.search(r'#define SHM_BUFFERS (\d+)', shim).group(1))
        self.assertGreaterEqual(buffers, 3)
        self.assertIn('flush_best_effort', shim)
        self.assertIn('read_pending_nonblocking', shim)
        self.assertIn('darling-wayland-handoff-', shim)
        self.assertIn('maybe_mint_handoff_token', shim)
        self.assertIn('#define DWL_CONFIGURE_SUSPENDED', header)
        self.assertIn('XDG_TOPLEVEL_STATE_SUSPENDED', shim)
        self.assertIn('DWL_CONFIGURE_SUSPENDED', shim)
        self.assertIn('window_update_opaque_region', shim)
        self.assertIn('wl_surface_set_opaque_region', shim)


if __name__ == '__main__':
    unittest.main()
