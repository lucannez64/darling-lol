"""Signals must only reach threads that are checked in with darlingserver.

A thread can handle signals only while it is checked in. A new thread could
take a process signal before checking in, and the main thread parked in
sigsuspend() after dispatch_main() accepted every signal: without a server
connection the default action ran and killed the process, even for an ignored
signal. New threads now start with all signals blocked and take the creator's
mask after checking in; a terminating thread blocks all signals before it
checks out, and the parked main thread keeps them blocked. Backport of upstream
darling 0224d389.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/darling.patch'
THREADS = 'src/startup/mldr/elfcalls/threads.c'


def new_side(path):
    """Context and added lines of `path`'s section in the darling patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class ThreadSignalMaskTests(unittest.TestCase):
    def setUp(self):
        self.text = new_side(THREADS)

    def test_new_threads_start_with_signals_blocked(self):
        start = self.text.index('pthread_sigmask(SIG_SETMASK, &all, &args.sigmask);')
        create = self.text.index('pthread_create(&nativeLibcThread, &attr, darling_thread_entry, &args);', start)
        restore = self.text.index('pthread_sigmask(SIG_SETMASK, &args.sigmask, NULL);', create)
        self.assertLess(start, create)
        self.assertLess(create, restore)
        self.assertIn('sigset_t sigmask;', self.text)

    def test_thread_takes_the_mask_after_checking_in(self):
        port = self.text.index('args.port = thread_self_port;')
        self.assertLess(port, self.text.index('pthread_sigmask(SIG_SETMASK, &args.sigmask, NULL);', port))

    def test_terminating_thread_blocks_signals_before_checkout(self):
        block = self.text.index('pthread_sigmask(SIG_SETMASK, &all, NULL);')
        self.assertLess(block, self.text.index('dserver_rpc_explicit_checkout(t_server_socket, -1, false);'))
        self.assertIn('sigsuspend(&all);', self.text)
        self.assertNotIn('sigsuspend(&mask);', self.text)


if __name__ == '__main__':
    unittest.main()
