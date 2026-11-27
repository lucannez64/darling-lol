"""mldr must map and zero the whole tail of a segment whose vmsize is not page-aligned.

The zero-fill mapping rounded its start down and its size up separately, which
left the segment's last page unmapped whenever vmsize was not page-aligned.
The file mapping also covered its whole last page, so past filesize that page
held whatever followed in the file where the segment must read as zeros (the
start of __bss/__thread_bss), as the XNU loader guarantees. Backport of
upstream darling 7192fa6d.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/darling.patch'


def new_side(path):
    """Context and added lines of `path`'s section in the darling patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class SegmentTailTests(unittest.TestCase):
    def setUp(self):
        self.text = new_side('src/startup/mldr/loader.c')

    def test_zero_fill_spans_from_file_end_to_segment_end(self):
        self.assertIn('uintptr_t zf_start = PAGE_ALIGN(seg->vmaddr + seg->filesize);', self.text)
        self.assertIn('uintptr_t zf_end = PAGE_ROUNDUP(seg->vmaddr + seg->vmsize);', self.text)
        self.assertIn('rv = compatible_mmap((void*) zf_start, zf_end - zf_start, useprot,', self.text)

    def test_file_page_past_filesize_is_zeroed(self):
        start = self.text.index('if (seg->filesize < seg->vmsize)\n')
        block = self.text[start:start + 900]
        self.assertIn('uintptr_t tail = addr + seg->filesize;', block)
        self.assertIn('memset((void*) tail, 0, tail_end - tail);', block)
        self.assertLess(block.index('useprot | PROT_WRITE'), block.index('memset('))


if __name__ == '__main__':
    unittest.main()
