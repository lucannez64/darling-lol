"""The guest interface list (NET_RT_IFLIST) must give each link entry its BSD type.

Consumers that look for a hardware address pick the Ethernet link entry by sdl_type;
a zero type makes every interface look like none of them.
"""
import unittest
from pathlib import Path

PATCH = Path(__file__).resolve().parent.parent / 'patches/xnu.patch'
FILE = 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/helper/misc/sysctl_net.c'


def added_lines():
    text = PATCH.read_text()
    start = text.index('diff --git a/' + FILE + ' ')
    end = text.find('\ndiff --git ', start)
    return [line[1:].strip() for line in text[start:end].splitlines() if line.startswith('+') and not line.startswith('+++')]


class InterfaceListLinkType(unittest.TestCase):
    def setUp(self):
        self.lines = added_lines()

    def test_ethernet_gets_type_and_address(self):
        self.assertIn('#define BSD_IFT_ETHER 0x6', self.lines)
        self.assertIn('dl->sdl_type = BSD_IFT_ETHER;', self.lines)
        self.assertIn('if (tmp.lifr_hwaddr.sa_family == LINUX_ARPHRD_ETHER) {', self.lines)

    def test_other_links_have_no_address(self):
        self.assertIn('#define BSD_IFT_LOOP 0x18', self.lines)
        self.assertIn('dl->sdl_alen = 0;', self.lines)

    def test_interface_data_matches_link_entry(self):
        self.assertIn('ifm->ifm_data.ifi_type = dl->sdl_type;', self.lines)
        self.assertIn('ifm->ifm_data.ifi_addrlen = dl->sdl_alen;', self.lines)


if __name__ == '__main__':
    unittest.main()
