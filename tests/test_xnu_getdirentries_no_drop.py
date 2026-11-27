"""getdirentries must never drop directory entries the kernel already returned.

Both calls read Linux records into a buffer sized from the caller's length,
convert them, and stop at the first converted record that does not fit. The
records past that point were lost: the directory offset had already moved on.
A getdirentries64 record (8-byte aligned, as on macOS) is up to 8 bytes longer
than its Linux record, so the host read is now sized for that worst case; the
old 32-bit call reads at most len bytes (its records are never larger), with a
floor of one longest Linux record. The proc enumeration buffer grows to fit a
longest record too. Backport of upstream darling-xnu 5cc9425, 42e995a and
35d3284.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/xnu.patch'
EMU = 'darling/src/libsystem_kernel/emulation/'


def new_side(path):
    """Context and added lines of `path`'s section in the xnu patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


GETDIRENTRIES = EMU + 'src/xnu_syscall/bsd/impl/dirent/getdirentries.c'


class GetdirentriesTests(unittest.TestCase):
    def setUp(self):
        self.text = new_side(GETDIRENTRIES)

    def test_64_bit_records_are_8_byte_aligned(self):
        self.assertIn('reclen = ALIGN(__builtin_offsetof(struct bsd_dirent64, d_name) + slen + 1, 8);', self.text)
        self.assertRegex(self.text, r'if \(len - opos < reclen\)\s*break;')

    def test_64_bit_read_is_sized_for_the_worst_case(self):
        self.assertIn('#define BSD_DIRENT64_MAX_SIZE_PER_LINUX_MIN 32', self.text)
        self.assertIn('unsigned int max_entry_count = len / BSD_DIRENT64_MAX_SIZE_PER_LINUX_MIN;', self.text)

    def test_32_bit_read_never_exceeds_the_callers_length(self):
        self.assertRegex(self.text, r'unsigned int hostlen = len;\s*if \(hostlen < LINUX_DIRENT64_LARGEST_RECORD\)')
        self.assertIn('ret = LINUX_SYSCALL(__NR_getdents64, fd, buf, hostlen);', self.text)

    def test_proc_enumeration_buffer_holds_a_longest_record(self):
        text = new_side(EMU + 'src/xnu_syscall/bsd/helper/misc/sysctl_proc.c')
        self.assertIn('char dents[1024];', text)


if __name__ == '__main__':
    unittest.main()
