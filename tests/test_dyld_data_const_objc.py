"""objc must be able to rewrite references in __DATA_CONST while it maps images.

link() and _main make SG_READ_ONLY __DATA_CONST segments PROT_READ before
objc's mapped callback runs. Current Apple ld puts objc reference sections
such as __objc_protorefs and __objc_superrefs in __DATA_CONST, and objc's
_read_images faulted when it had to rewrite one (remapProtocolRef for a
protocol a system library also defines). dyld2 now makes those segments
writable around the objc mapped notification and protects them again after.
Backport of upstream darling-dyld 467de62.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/dyld.patch'


def new_side(path):
    """Context and added lines of `path`'s section in the dyld patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class DataConstObjcTests(unittest.TestCase):
    def test_protection_can_be_lifted_and_restored(self):
        text = new_side('src/ImageLoaderMachOCompressed.cpp')
        self.assertIn('void ImageLoaderMachOCompressed::setReadOnlyDataWritable(bool writable) const', text)
        self.assertIn('::mprotect((void*)start, size, writable ? (PROT_READ | PROT_WRITE) : PROT_READ);', text)

    def test_objc_mapping_runs_with_the_segments_writable(self):
        text = new_side('src/dyld2.cpp')
        lift = text.index('image->setReadOnlyDataWritable(true);')
        notify = text.index('ImageLoader::fgTotalObjCSetupTime += (t1-t0);')
        restore = text.index('unprotected[i]->setReadOnlyDataWritable(false);')
        self.assertLess(lift, notify)
        self.assertLess(notify, restore)
        self.assertIn('if ( image->madeDataReadOnly() ) {', text[:lift])


if __name__ == '__main__':
    unittest.main()
