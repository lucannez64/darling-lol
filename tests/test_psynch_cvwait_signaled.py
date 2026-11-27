"""A condition wait that a signal already consumed must still drop the mutex.

`_psynch_cvwait` returned EINVAL when the S (signal) sequence of the condition
variable had already caught up with the L (lock) sequence of the wait. It
returned before releasing the mutex, so the caller kept holding it and every
other thread that needed that mutex blocked forever. It now drops the mutex as
the normal wait path does and reports a completed wakeup. Backport of upstream
darlingserver b81eb72.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/darlingserver.patch'
SYNCH = 'duct-tape/pthread/kern_synch.c'


def new_side(path):
    """Context and added lines of `path`'s section in the darlingserver patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class SignaledCondWaitTests(unittest.TestCase):
    def setUp(self):
        text = new_side(SYNCH)
        start = text.index('if (is_seqhigher_eq(csgen, lockseq) != 0) {')
        self.branch = text[start:text.index('PTHREAD_TRACE(psynch_cvar_kwait | DBG_FUNC_START', start)]

    def test_already_signaled_wait_drops_the_mutex(self):
        self.assertIn('_psynch_mutexdrop(NULL, mutex, mgen, ugen, 0, flags,', self.branch)
        self.assertIn('if (mutex != 0) {', self.branch)

    def test_already_signaled_wait_reports_a_wakeup_not_einval(self):
        self.assertNotIn('return EINVAL;', self.branch)
        self.assertRegex(self.branch, r'\*retval = 0;\s*return 0;')


if __name__ == '__main__':
    unittest.main()
