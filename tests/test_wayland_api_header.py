import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tools/darling-wayland-api.h'
GUEST_COPY = ROOT / 'build/darling-src/src/external/cocotron/AppKit/X11.backend/DarlingWaylandAPI.h'


class WaylandApiHeader(unittest.TestCase):
    def test_header_defines_contract(self):
        text = SOURCE.read_text()
        self.assertIn('#define DWL_API_VERSION 3', text)
        self.assertIn('DarlingWaylandGetAPI', text)

    @unittest.skipUnless(GUEST_COPY.is_file(), 'cocotron DarlingWaylandAPI.h copy not present')
    def test_guest_copy_is_identical(self):
        self.assertEqual(SOURCE.read_bytes(), GUEST_COPY.read_bytes())

    def test_cocotron_patch_copy_is_identical(self):
        patch = (ROOT / 'patches/cocotron.patch').read_text()
        marker = 'diff --git a/AppKit/X11.backend/DarlingWaylandAPI.h b/AppKit/X11.backend/DarlingWaylandAPI.h\n'
        self.assertIn(marker, patch)
        section = patch.split(marker, 1)[1].split('diff --git ', 1)[0]
        lines = section.splitlines(keepends=True)
        hunk_index = next(i for i, line in enumerate(lines) if line.startswith('@@ '))
        extracted = []
        for line in lines[hunk_index + 1:]:
            if line.startswith('+'):
                extracted.append(line[1:])
            elif line == '\\ No newline at end of file\n' and extracted:
                extracted[-1] = extracted[-1].rstrip('\n')
        self.assertEqual(SOURCE.read_bytes(), ''.join(extracted).encode('utf-8'))


if __name__ == '__main__':
    unittest.main()
