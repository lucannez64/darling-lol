"""A persistent S > L state must not make every condition signal fail.

`_pthread_psynch_cond_signal` treats a snapshot whose S (signal) count is
above its L (lock) count as a race with a waiter still publishing a newer L:
it yields and retries. After 8192 retries it returned EAGAIN, so a state that
never resolved made every later signal on that condition variable fail the
same way, and its waiters were never woken. It now repairs the S count with
an atomic compare-and-swap of both words and retries. Backport of upstream
darling-libpthread b60b2cd.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/libpthread.patch'


def new_side(path):
    """Context and added lines of `path`'s section in the libpthread patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class CondSignalRepairTests(unittest.TestCase):
    def test_exhausted_retries_repair_the_signal_count(self):
        text = new_side('src/pthread_cond.c')
        start = text.index('if (retry_count > 8192) {')
        branch = text[start:text.index('} else if (is_seqhigher((ucntval', start)]
        self.assertNotIn('return EAGAIN;', branch)
        self.assertIn('(lcntval & PTHRW_COUNT_MASK);', branch)
        self.assertIn('os_atomic_cmpxchg(c_lsseqaddr, old_ls, new_ls, seq_cst);', branch)
        self.assertRegex(branch, r'retry_count = 0;.*retry = true;\s*continue;'.replace('.*', r'[\s\S]*'))


if __name__ == '__main__':
    unittest.main()
