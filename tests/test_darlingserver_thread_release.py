"""A thread that exits through `checkout` must be released by darlingserver.

`Checkout::processCall` sends its reply directly through the server, bypassing
`Thread::pushCallReply`, the only place that deactivates a thread's call. A dead
thread is released only once it has no active call, and until then it keeps its
process alive: the process's Mach ports are never destroyed and its peers never
get the dead-name or no-senders notification. The League client UX waits forever
for a GPU helper that had any short-lived thread.
"""
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def patched_function(patch, path, name):
    """`name` as the patch leaves `path`: only its hunks (context and added lines)."""
    sections = (ROOT / patch).read_text().split('diff --git ')
    section = next(s for s in sections if s.startswith('a/' + path + ' b/'))
    new_side = '\n'.join(line[1:] for line in section.splitlines()
                         if line[:1] in (' ', '+') and not line.startswith('+++'))
    match = re.search(r'^void DarlingServer::' + re.escape(name) + r'\(\) \{[\s\S]*?^\};', new_side, re.M)
    if not match:
        raise AssertionError(f'{name} is not in the {path} patch')
    return match.group()


class CheckoutReleasesThreadTests(unittest.TestCase):
    def setUp(self):
        self.checkout = patched_function('patches/darlingserver.patch', 'src/call.cpp',
                                         'Call::Checkout::processCall')

    def test_dead_thread_call_is_deactivated_after_the_direct_reply(self):
        body = self.checkout
        reply = body.index('_sendReply(code);')
        deactivate = body.index('deactivateCall(')
        self.assertLess(body.index('_thread.reset();'), reply)
        self.assertGreater(deactivate, reply, 'deactivate only after the reply was sent')
        # Only the thread that notifyDead() marked dead, and only while this call is its active one.
        self.assertRegex(body, r'notifyDead\(\);\s*\n\s*exitingThread = thread;')
        self.assertIn('exitingThread->activeCall().get() == this', body)

    def test_exec_checkout_keeps_its_thread(self):
        # An exec checkout keeps the thread running in the new image; nothing to release.
        # The patch only carries the hunks around the change, so check the one assignment.
        self.assertEqual(len(re.findall(r'exitingThread = ', self.checkout)), 1)

    def test_mismatched_call_does_not_take_the_server_down(self):
        # _deactivateCallLocked throws when the thread was interrupted for a signal
        # with another call saved; that must not abort darlingserver.
        self.assertRegex(self.checkout, r'try \{[^}]*deactivateCall\([^}]*\} catch \(const std::exception&')


if __name__ == '__main__':
    unittest.main()
