"""A guest socket must answer SIOCGIFCONF.

The request used to fall through to Linux under its Darwin number and fail, and a caller
that does not expect that failure can leave its socket open: the Riot client leaked one
descriptor per call until RLIMIT_NOFILE (#226).
"""
import unittest
from pathlib import Path

PATCH = Path(__file__).resolve().parent.parent / 'patches/xnu.patch'
FILE = 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/helper/ioctl/socket.c'


def added_lines():
    text = PATCH.read_text()
    start = text.index('diff --git a/' + FILE + ' ')
    end = text.find('\ndiff --git ', start)
    return [line[1:].strip() for line in text[start:end].splitlines() if line.startswith('+') and not line.startswith('+++')]


class SocketInterfaceList(unittest.TestCase):
    def setUp(self):
        self.lines = added_lines()

    def test_request_is_handled_with_the_linux_number(self):
        self.assertIn('case SIOCGIFCONF:', self.lines)
        self.assertIn('#define LINUX_SIOCGIFCONF 0x8912', self.lines)
        self.assertIn('status = __real_ioctl(fd, LINUX_SIOCGIFCONF, &lifc);', self.lines)

    def test_each_interface_gets_a_link_record(self):
        self.assertIn('dl->sdl_family = AF_LINK;', self.lines)
        self.assertIn('dl->sdl_type = IFT_ETHER;', self.lines)
        self.assertIn('if (__real_ioctl(fd, LINUX_SIOCGIFHWADDR, &hw) == 0 && hw.lifr_addr.sa_family == LINUX_ARPHRD_ETHER)', self.lines)

    def test_ipv4_record_is_a_darwin_sockaddr(self):
        self.assertIn('ifr->ifr_addr.sa_len = sizeof(struct sockaddr_in);', self.lines)
        self.assertIn('ifr->ifr_addr.sa_family = AF_INET;', self.lines)

    def test_only_whole_records_are_returned(self):
        self.assertIn('if ((size_t) (space - written) < record)', self.lines)
        self.assertIn('if ((size_t) (space - written) < sizeof(struct ifreq))', self.lines)
        self.assertIn('ifc->ifc_len = written;', self.lines)


if __name__ == '__main__':
    unittest.main()
