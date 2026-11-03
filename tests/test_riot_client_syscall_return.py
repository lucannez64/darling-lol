"""The BSD syscall trampoline must return zero in %rdx (except pipe()).

XNU returns a second result value in %rdx. Chromium's sendto wrapper reads it
and reports ERR_NETWORK_CHANGED when it is not zero, which made Riot Client 142's
UX fail to load its local page. pipe() is the one BSD syscall that returns a
second descriptor there, so it keeps the register.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/xnu.patch'
FILE = 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/bsd_syscall.S'


def patched_text(path):
    """Context and added lines of path's section: the file after the patch."""
    patch = PATCH.read_text()
    match = re.search(
        r'(?ms)^diff --git [ai]/%s [bw]/%s\n(.*?)(?=^diff --git |\Z)'
        % (re.escape(path), re.escape(path)), patch)
    if not match:
        raise AssertionError(path + ' is not in patches/xnu.patch')
    body = match.group(1)
    return ''.join(line[1:] if line[:1] in ('+', ' ') else ''
                   for line in body.splitlines(keepends=True))


class BsdSyscallReturnTests(unittest.TestCase):
    def setUp(self):
        self.source = patched_text(FILE)

    def test_rdx_is_cleared_after_the_call(self):
        self.assertIn('xorl\t%edx, %edx', self.source)

    def test_pipe_keeps_its_second_result(self):
        self.assertIn('cmpq\t$42, %rax', self.source)
        self.assertIn('.keep_rdx:', self.source)
        keep = self.source.split('.keep_rdx:', 1)[1].split('.std_ret:', 1)[0]
        self.assertNotIn('xorl\t%edx, %edx', keep)

    def test_clearing_happens_before_the_shared_return(self):
        self.assertLess(self.source.index('xorl\t%edx, %edx'), self.source.index('.std_ret:'))


if __name__ == '__main__':
    unittest.main()
