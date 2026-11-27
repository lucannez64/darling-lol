"""_simple_asl_send must not block on a full log socket.

It wrote each message to the syslog datagram socket with a plain blocking
sendto. When something holds /var/run/syslog (launchd does) and nobody drains
it, the queue fills (10 datagrams by default on Linux) and every later caller
sleeps in sendto forever: any process that logs through libplatform
(libdispatch, libxpc and others) hangs. Logging is best effort, so the send is
now MSG_DONTWAIT and a full queue drops the message. Backport of upstream
darling-libplatform 5df459e.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/libplatform.patch'


def new_side(path):
    """Context and added lines of `path`'s section in the libplatform patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class AslNonblockingTests(unittest.TestCase):
    def test_log_send_does_not_wait(self):
        text = new_side('src/simple/asl.c')
        self.assertIn('__sendto(asl_fd, cp, strlen(cp), MSG_DONTWAIT, NULL, 0);', text)
        self.assertNotIn('__sendto(asl_fd, cp, strlen(cp), 0, NULL, 0);', text)


if __name__ == '__main__':
    unittest.main()
