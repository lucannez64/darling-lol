"""Compile the added interface-media handler with inert ioctl fixtures."""
from pathlib import Path
import shutil
import unittest

import test_runtime_compatibility as sources


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class InterfaceMediaTests(unittest.TestCase):
    run_c = sources.CompatibilityChecks.run_c

    def test_media_queries_preserve_buffers_and_exclude_loopback(self):
        patch = (Path(__file__).resolve().parents[1] / 'patches/xnu.patch').read_text()
        section = patch.split('diff --git a/darling/src/libsystem_kernel/emulation/src/'
                              'xnu_syscall/bsd/helper/ioctl/socket.c ', 1)[1].split('\ndiff --git ', 1)[0]
        added = '\n'.join(line[1:] for line in section.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        handler = added.split('\t\tcase SIOCGIFMEDIA:', 1)[1]
        self.run_c(r'''
#include <assert.h>
#include <errno.h>
#include <string.h>
#define SIOCGIFMEDIA 1
#define SIOCGIFXMEDIA 2
#define IOCTL_HANDLED 1
#define IOCTL_PASS 0
#define IFF_UP 1
#define IFF_LOOPBACK 8
#define IFF_RUNNING 64
#define IFM_ETHER 0x20
#define IFM_IEEE80211 0x80
#define IFM_AUTO 0
#define IFM_AVALID 1
#define IFM_ACTIVE 2
#define LINUX_SIOCGIFFLAGS 0x8913
#define LINUX_IFNAMSIZ 16
struct linux_ifreq { char lifr_name[16]; short lifr_flags; char pad[22]; };
struct ifmediareq {
    char ifm_name[16];
    int ifm_current, ifm_mask, ifm_status, ifm_active, ifm_count;
    int *ifm_ulist;
};
static short flags;
static int ioctl_result;
static int __real_ioctl(int fd, int command, struct linux_ifreq *request) {
    assert(fd == 3 && command == LINUX_SIOCGIFFLAGS);
    request->lifr_flags = flags; return ioctl_result;
}
static int handle_socket(int fd, unsigned cmd, void *arg, int *retval) {
    switch (cmd) { case SIOCGIFMEDIA:
''' + handler + r'''
    }
    return IOCTL_PASS;
}
int main(void) {
    struct ifmediareq request = {.ifm_name = "eth0"};
    int result = 99, list[] = {-1, -2};
    flags = IFF_UP | IFF_RUNNING;
    assert(handle_socket(3, SIOCGIFMEDIA, &request, &result) == IOCTL_HANDLED);
    assert(result == 0 && request.ifm_count == 1 && request.ifm_ulist == NULL);
    assert(request.ifm_current == IFM_ETHER && request.ifm_active == IFM_ETHER);
    assert(request.ifm_status == (IFM_AVALID | IFM_ACTIVE));
    request.ifm_count = 2; request.ifm_ulist = list;
    assert(handle_socket(3, SIOCGIFXMEDIA, &request, &result) == IOCTL_HANDLED);
    assert(result == 0 && request.ifm_count == 1 && request.ifm_ulist == list);
    assert(list[0] == IFM_ETHER && list[1] == -2);
    strcpy(request.ifm_name, "wlan0"); request.ifm_count = 1;
    handle_socket(3, SIOCGIFMEDIA, &request, &result);
    assert(result == 0 && list[0] == IFM_IEEE80211);
    flags = 0;
    handle_socket(3, SIOCGIFMEDIA, &request, &result);
    assert(result == 0 && request.ifm_status == IFM_AVALID);
    flags = IFF_UP | IFF_RUNNING | IFF_LOOPBACK;
    list[0] = -1;
    handle_socket(3, SIOCGIFMEDIA, &request, &result);
    assert(result == -ENOTSUP && list[0] == -1 && request.ifm_ulist == list);
    flags = IFF_UP | IFF_RUNNING; request.ifm_count = -1;
    handle_socket(3, SIOCGIFMEDIA, &request, &result);
    assert(result == -EINVAL);
    request.ifm_count = 1; request.ifm_ulist = NULL;
    handle_socket(3, SIOCGIFMEDIA, &request, &result);
    assert(result == -EFAULT);
    handle_socket(3, SIOCGIFMEDIA, NULL, &result);
    assert(result == -EINVAL);
    request.ifm_count = 0; ioctl_result = -ENODEV;
    handle_socket(3, SIOCGIFMEDIA, &request, &result);
    assert(result == -ENODEV);
}
''')


if __name__ == '__main__':
    unittest.main()
