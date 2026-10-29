// Vivox's network reachability provider calls SIOCGIFMEDIA on each interface
// and drops every interface whose ioctl fails. Darling used to pass the Darwin
// request through to Linux, which returns ENOTTY.
#include <ifaddrs.h>
#include <net/if.h>
#include <net/if_media.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/sockio.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int check(const char *name, int required) {
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    struct ifmediareq ifmr;
    memset(&ifmr, 0, sizeof(ifmr));
    strncpy(ifmr.ifm_name, name, sizeof(ifmr.ifm_name) - 1);
    errno = 0;
    int rc = ioctl(sock, SIOCGIFMEDIA, &ifmr);
    close(sock);
    int active = (ifmr.ifm_status & (IFM_AVALID | IFM_ACTIVE)) == (IFM_AVALID | IFM_ACTIVE);
    int known_type = IFM_TYPE(ifmr.ifm_current) == IFM_ETHER ||
                     IFM_TYPE(ifmr.ifm_current) == IFM_IEEE80211;
    printf("IFMEDIA interface=%s rc=%d errno=%d current=0x%x status=0x%x active=%d type=%d\n",
           name, rc, errno, ifmr.ifm_current, ifmr.ifm_status, active, known_type);
    if (rc != 0) return required;
    return required && (!active || !known_type);
}

int main(int argc, char **argv) {
    int failures = 0;
    if (argc > 1) {
        for (int i = 1; i < argc; i++) failures += check(argv[i], 1);
    } else {
        struct ifaddrs *interfaces = NULL;
        char name[IFNAMSIZ] = {0};
        if (getifaddrs(&interfaces) != 0) {
            printf("IFMEDIA getifaddrs failed errno=%d\n", errno);
            return 1;
        }
        for (struct ifaddrs *entry = interfaces; entry; entry = entry->ifa_next) {
            if (!entry->ifa_addr || entry->ifa_addr->sa_family != AF_INET) continue;
            if (entry->ifa_flags & IFF_LOOPBACK) continue;
            strncpy(name, entry->ifa_name, sizeof(name) - 1);
            break;
        }
        freeifaddrs(interfaces);
        if (!name[0]) {
            printf("IFMEDIA no non-loopback IPv4 interface\n");
            return 1;
        }
        failures += check(name, 1);
    }
    printf("IFMEDIA_CHECK failures=%d\n", failures);
    return failures ? 1 : 0;
}
