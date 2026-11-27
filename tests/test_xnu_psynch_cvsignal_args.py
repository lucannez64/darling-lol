"""psynch condition signals must pass their 64-bit arguments whole.

XNU declares psynch_cvsignal with 64-bit cvlsgen and mugen words, but the
emulation took them as uint32_t, dropping the upper halves (the S count of the
L/S pair) before they reached darlingserver. psynch_cvbroad also passed mugen
where the mutex address belongs. Backport of upstream darling-xnu 9903e29.
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


class CvsignalArgumentTests(unittest.TestCase):
    def test_cvsignal_takes_64_bit_sequence_words(self):
        sig = 'long sys_psynch_cvsignal(void* cv, uint64_t cvlsgen, uint32_t cvugen, int thread_port, void* mutex, uint64_t mugen,'
        self.assertIn(sig, new_side(EMU + 'include/xnu_syscall/bsd/impl/psynch/psynch_cvsignal.h'))
        self.assertIn(sig, new_side(EMU + 'src/xnu_syscall/bsd/impl/psynch/psynch_cvsignal.c'))

    def test_cvbroad_passes_the_mutex(self):
        text = new_side(EMU + 'src/xnu_syscall/bsd/impl/psynch/psynch_cvbroad.c')
        self.assertIn('dserver_rpc_psynch_cvbroad(cv, cvlsgen, cvudgen, flags, mutex, mugen, tid, &retval);', text)


if __name__ == '__main__':
    unittest.main()
