"""Exercise the real patched descriptor helper and frequency conversion on the host."""
import unittest
import test_runtime_compatibility as sources


class ShellCPUChecks(unittest.TestCase):
    patched = sources.CompatibilityChecks.patched
    run_c = sources.CompatibilityChecks.run_c

    def test_frequency_units_width_and_invalid_inputs(self):
        root = 'darling/src/libsystem_kernel/emulation/'
        header = self.patched('xnu', root + 'include/common/cpu_frequency.h')
        original = self.patched('xnu', root + 'include/xnu_syscall/bsd/impl/misc/sysctl.h')
        size_macro = original[original.index('#define sysctl_handle_size'):original.index('typedef long')]
        source = self.patched('xnu', root + 'src/xnu_syscall/bsd/helper/misc/sysctl_hw.c')
        start = source.index('static long cpu_frequency(')
        handler = source[start:source.index('\nsysctl_handler(handle_cpu64bitcapable)', start)]
        self.run_c(header + '''
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <string.h>
static unsigned reads;
static bool read_string(const char* path, char* dst, int size) {
    ++reads; strcpy(dst, "5600000\\n"); return true;
}
/* The compat hardware profile short-circuits the probe; a switch here so both
 * answers are exercised. */
struct darling_cpu_identity { uint32_t base_mhz; };
static int compat_on;
static const struct darling_cpu_identity* darling_compat_cpu_identity(void) {
    static const struct darling_cpu_identity identity = { 2600 };
    return &identity;
}
static int darling_compat_profile_enabled(void) { return compat_on; }
#define sysctl_handler(name) long name(void* old, unsigned long* oldlen)
''' + size_macro + handler + '''
int main(void) {
    uint64_t hz = 17;
    assert(darling_frequency_hz("2400000\\n", &hz) && hz == 2400000000ULL);
    assert(darling_frequency_hz("5600000", &hz) && hz == 5600000000ULL);
    const char *bad[] = {"", "0", "-1", " 123", "12MHz", "18446744073709551616", "18446744073709552"};
    for (unsigned i=0; i<sizeof(bad)/sizeof(bad[0]); ++i) {
        hz = 17; assert(!darling_frequency_hz(bad[i], &hz) && hz == 17);
    }
    unsigned long length = 0;
    assert(handle_cpufrequency(NULL, &length) == 0 && length == 8 && reads == 0);
    length = 4; hz = 17;
    assert(handle_cpufrequency(&hz, &length) == -EINVAL && hz == 17 && reads == 0);
    length = sizeof(hz);
    assert(handle_cpufrequency(&hz, &length) == 0 && hz == 5600000000ULL && length == 8);
    uint32_t legacy = 0; length = sizeof(legacy);
    assert(handle_cpufrequency_compat(&legacy, &length) == 0 && length == 4 && legacy == (uint32_t)5600000000ULL);
    compat_on = 1; reads = 0; hz = 17; length = sizeof(hz);
    assert(handle_cpufrequency(&hz, &length) == 0 && hz == 2600000000ULL && length == 8 && reads == 0);
    length = 4; hz = 17;
    assert(handle_cpufrequency(&hz, &length) == -EINVAL && hz == 17);
}
''')

    def test_stdio_aliases_cleanup_and_failure_do_not_close_other_descriptors(self):
        header = self.patched('darling', 'src/shellspawn/shellspawn-fds.h')
        self.run_c('#define _GNU_SOURCE\n' + header + '''
#include <assert.h>
#include <sys/stat.h>
#include <sys/wait.h>
static unsigned open_count(void) {
    unsigned n = 0; for (int fd=3; fd<256; ++fd) if (fcntl(fd, F_GETFD) >= 0) ++n; return n;
}
int main(void) {
    pid_t child = fork(); assert(child >= 0);
    if (!child) {
        int p[3][2], sentinel[2];
        for (int i=0; i<3; ++i) assert(pipe(p[i]) == 0);
        assert(pipe(sentinel) == 0);
        for (int i=0; i<3; ++i) assert(dup2(p[i][0], i) == i);
        struct stat expected[3], actual;
        int alias[3] = {2,0,1};
        for (int i=0; i<3; ++i) assert(fstat(alias[i], &expected[i]) == 0);
        assert(shellspawn_remap_stdio(alias) == 0);
        for (int i=0; i<3; ++i) {
            assert(fstat(i, &actual) == 0 && actual.st_ino == expected[i].st_ino);
            assert(!(fcntl(i, F_GETFD) & FD_CLOEXEC));
        }
        int streams[3] = {p[0][0],p[1][0],p[2][0]};
        shellspawn_close_streams(streams);
        for (int i=0; i<3; ++i) assert(fcntl(p[i][0], F_GETFD) == -1 && streams[i] == -1);
        assert(fcntl(sentinel[0], F_GETFD) >= 0);
        streams[0] = streams[1] = streams[2] = p[0][1];
        shellspawn_close_streams(streams); shellspawn_close_streams(streams);
        unsigned before = open_count();
        int invalid[3] = {sentinel[0],-1,sentinel[1]};
        for (int i=0; i<1000; ++i) assert(shellspawn_remap_stdio(invalid) == -1 && errno == EBADF);
        assert(open_count() == before && fcntl(sentinel[0], F_GETFD) >= 0);
        _exit(0);
    }
    int status; assert(waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
}
''')
