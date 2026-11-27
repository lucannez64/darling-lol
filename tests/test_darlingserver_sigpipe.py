"""A client that exits mid-write must not kill darlingserver with SIGPIPE.

The server writes to sockets and pipes its clients own: kqchan replies, the
push_reply pipe, debug pipes and log writers. When the client side had already
closed, the write raised SIGPIPE, and the default action killed the server and
every process in the prefix with it. The server now ignores SIGPIPE once
launchd's process exists (an ignored SIGPIPE survives execve, so launchd must
start before it), and sendmmsg passes MSG_NOSIGNAL. Backport of upstream
darlingserver 1414604.
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


class DarlingserverSigpipeTests(unittest.TestCase):
    def test_server_ignores_sigpipe_after_dropping_privileges(self):
        text = new_side('src/darlingserver.cpp')
        ignore = text.index('signal(SIGPIPE, SIG_IGN);')
        self.assertLess(text.index('perma_drop_privileges(originalUID, originalGID);'), ignore)
        # Ignoring it before launchd is cloned (the old darling-lol M58f2 change)
        # would hand every guest process an ignored SIGPIPE through execve.
        self.assertEqual(text.count('signal(SIGPIPE, SIG_IGN);'), 1)
        self.assertNotIn('M58f2: never die from SIGPIPE', PATCH.read_text())

    def test_batched_replies_do_not_raise_sigpipe(self):
        text = new_side('src/message.cpp')
        self.assertIn('sendmmsg(socket, mmsgs, len, MSG_DONTWAIT | MSG_NOSIGNAL);', text)


if __name__ == '__main__':
    unittest.main()
