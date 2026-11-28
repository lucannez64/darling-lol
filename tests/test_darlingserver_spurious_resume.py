"""A microthread resumed without a wakeup must keep waiting.

`thread_unblock` schedules the woken microthread to run. When a signal
handler's interrupt_enter arrived first, the interrupted call ran to its end
from there and the scheduled run was left over; if it came while the thread
was in another wait, it resumed that wait although nobody ended it.
`thread_block_parameter` then returned THREAD_WAITING and callers panicked
("semaphore_block" in semaphore_convert_wait_result). Both the blocking and the
continuation paths now go back to waiting. Backport of upstream darlingserver
757f254.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/darlingserver.patch'


def new_side(path):
    """Context and added lines of `path`'s section in the darlingserver patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class SpuriousResumeTests(unittest.TestCase):
    def setUp(self):
        self.text = new_side('duct-tape/src/thread.c')

    def test_continuation_keeps_waiting(self):
        start = self.text.index('wait_result = thread->xnu_thread.wait_result;')
        self.assertRegex(
            self.text[start:],
            r'(?s)^wait_result = thread->xnu_thread\.wait_result;\s*'
            r'if \(wait_result == THREAD_WAITING\) \{.*?'
            r'thread->xnu_thread\.state \|= TH_WAIT;\s*'
            r'thread_unlock\(&thread->xnu_thread\);\s*'
            r'thread_block_parameter\(continuation, parameter\);',
        )

    def test_blocking_wait_loops_instead_of_returning_waiting(self):
        start = self.text.index('again:\n\tthread_lock(&thread->xnu_thread);')
        body = self.text[start:]
        self.assertRegex(
            body,
            r'(?s)if \(waiting && wait_result == THREAD_WAITING\) \{.*?'
            r'thread->xnu_thread\.state \|= TH_WAIT;\s*'
            r'thread_unlock\(&thread->xnu_thread\);\s*goto again;',
        )


if __name__ == '__main__':
    unittest.main()
