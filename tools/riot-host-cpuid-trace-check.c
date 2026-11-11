// Host-only check for darling-lol's cpuid execute-breakpoint tracer
// (emulation/src/linux_premigration/signal/cpuid_trace.c). The REAL arming and
// handler code are compiled and linked unchanged; only the guest-only helpers
// linux_syscall() and __simple_snprintf() are shimmed to the host, and the flag
// and log paths are redirected to check-local temporary files.
//
// The test:
//   * arms a hardware execute breakpoint on a cpuid instruction in this binary,
//   * runs that cpuid from the main thread and from a thread created AFTER
//     arming (exercising perf inherit=1),
//   * verifies the emulated results equal a real, untrapped cpuid,
//   * verifies the per-site hit count equals the number of executions,
//   * verifies the instruction bytes (0f a2) are never modified, and
//   * verifies a second, non-armed cpuid site is unaffected and uncounted,
//   * verifies fake mode rewrites only the identity leaves (vendor, signature,
//     brand string, address sizes) and passes all other leaves through.
//
// Exit status is non-zero on any failure. Requires a kernel with perf sigtrap
// breakpoints (>= 5.13) and permission to open a user-only breakpoint event.
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <ucontext.h>
#include <unistd.h>

#include <darling/emulation/common/cpu_identity.h>
#include <darling/emulation/linux_premigration/signal/cpuid_trace.h>

// ---------------------------------------------------------------------------
// Shims for the guest-only symbols the compiled tracer references.

long linux_syscall(long a1, long a2, long a3, long a4, long a5, long a6, int nr) {
    long r = syscall(nr, a1, a2, a3, a4, a5, a6);
    return r == -1 ? -errno : r;
}

int __simple_snprintf(char* buffer, size_t max_length, const char* format, ...) {
    va_list args;
    va_start(args, format);
    int ret = vsnprintf(buffer, max_length, format, args);
    va_end(args);
    return ret;
}

// ---------------------------------------------------------------------------
// Two cpuid sites with exact, externally-visible labels on the cpuid byte.
// SysV args: edi=eax, esi=ecx, rdx=out[4].

extern void rclol_test_cpuid(unsigned eax, unsigned ecx, unsigned* out);
extern char rclol_test_cpuid_insn[];
extern void rclol_test_cpuid2(unsigned eax, unsigned ecx, unsigned* out);
extern char rclol_test_cpuid2_insn[];

__asm__(
    ".text\n"
    ".globl rclol_test_cpuid\n"
    ".globl rclol_test_cpuid_insn\n"
    "rclol_test_cpuid:\n"
    "  pushq %rbx\n"
    "  movl %edi, %eax\n"
    "  movl %esi, %ecx\n"
    "  movq %rdx, %r8\n"
    "rclol_test_cpuid_insn:\n"
    "  cpuid\n"
    "  movl %eax, 0(%r8)\n"
    "  movl %ebx, 4(%r8)\n"
    "  movl %ecx, 8(%r8)\n"
    "  movl %edx, 12(%r8)\n"
    "  popq %rbx\n"
    "  ret\n"

    ".globl rclol_test_cpuid2\n"
    ".globl rclol_test_cpuid2_insn\n"
    "rclol_test_cpuid2:\n"
    "  pushq %rbx\n"
    "  movl %edi, %eax\n"
    "  movl %esi, %ecx\n"
    "  movq %rdx, %r8\n"
    "rclol_test_cpuid2_insn:\n"
    "  cpuid\n"
    "  movl %eax, 0(%r8)\n"
    "  movl %ebx, 4(%r8)\n"
    "  movl %ecx, 8(%r8)\n"
    "  movl %edx, 12(%r8)\n"
    "  popq %rbx\n"
    "  ret\n"
);

// A plain reference cpuid (never trapped) for comparison.
static void ref_cpuid(unsigned eax, unsigned ecx, unsigned out[4]) {
    __asm__ volatile("cpuid"
        : "=a"(out[0]), "=b"(out[1]), "=c"(out[2]), "=d"(out[3])
        : "a"(eax), "c"(ecx));
}

// ---------------------------------------------------------------------------
// SIGTRAP handler: forwards to the real tracer. The guest siginfo/ucontext
// layouts mirror the kernel ABI, so the host's siginfo_t/ucontext_t can be
// reinterpreted directly.

static volatile sig_atomic_t spurious_traps = 0;

static void on_sigtrap(int sig, siginfo_t* info, void* uctx) {
    (void) sig;
    if (rclol_cpuid_trace_handle((struct linux_siginfo*) info, (struct linux_ucontext*) uctx))
        return;
    // Not ours: nothing good can come of resuming on the same instruction.
    spurious_traps++;
    _exit(97);
}

// Read the 48-byte brand string through the armed site (fake mode on).
static void read_fake_brand(char out[49]) {
    for (int i = 0; i < 3; ++i) {
        unsigned o[4];
        rclol_test_cpuid(0x80000002u + (unsigned) i, 0, o);
        memcpy(out + i * 16, o, sizeof(o));
    }
    out[48] = '\0';
}

static int brand_matches(const char* expected) {
    char got[49];
    size_t length = strlen(expected);
    read_fake_brand(got);
    if (memcmp(got, expected, length) != 0)
        return 0;
    for (size_t i = length; i < 48; ++i)
        if (got[i] != '\0')
            return 0;
    return 1;
}

// The launcher's profile and brand are read from /proc/self/environ, so they
// have to be present when this process was exec'd (the builder sets them).
static int profile_mode(int argc, char** argv) {
    const char* expect_profile = argc > 2 ? argv[2] : "host";
    const char* expect_brand = argc > 3 ? argv[3] : "Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz";
    int compat = darling_compat_profile_enabled();

    unsigned long cookie = 0x100ABCD1UL;
    if (rclol_cpuid_trace_arm((unsigned long) rclol_test_cpuid_insn, cookie) < 0) {
        fprintf(stderr, "FAIL: profile mode could not arm the site\n");
        return 2;
    }
    rclol_cpuid_trace_set_fake(1);

    if (strcmp(compat ? "compat" : "host", expect_profile) != 0) {
        fprintf(stderr, "FAIL: profile=%s expected %s\n", compat ? "compat" : "host", expect_profile);
        return 1;
    }
    if (!brand_matches(expect_brand)) {
        char got[49];
        read_fake_brand(got);
        fprintf(stderr, "FAIL: brand '%s' expected '%s'\n", got, expect_brand);
        return 1;
    }
    printf("HOST_CPUID_PROFILE pass=1 profile=%s brand=\"%s\"\n", expect_profile, expect_brand);
    return 0;
}

static int failures = 0;
#define CHECK(cond, msg) do { if (!(cond)) { fprintf(stderr, "FAIL: %s\n", msg); failures++; } \
                              else { fprintf(stderr, "ok: %s\n", msg); } } while (0)

static unsigned g_out[4];
static const int kThreadRuns = 5;

static void* thread_fn(void* arg) {
    (void) arg;
    for (int i = 0; i < kThreadRuns; ++i) {
        unsigned o[4];
        rclol_test_cpuid(1, 0, o); // leaf 1 from an inherited thread
    }
    return NULL;
}

int main(int argc, char** argv) {
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = on_sigtrap;
    sa.sa_flags = SA_SIGINFO;
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGTRAP, &sa, NULL) != 0) { perror("sigaction"); return 1; }

    if (argc > 1 && strcmp(argv[1], "profile") == 0)
        return profile_mode(argc, argv);

    unsigned char* site = (unsigned char*) rclol_test_cpuid_insn;
    CHECK(site[0] == 0x0f && site[1] == 0xa2, "test cpuid site is 0f a2 before arming");

    // Arm the first site only. cookie is arbitrary here (the real code uses the
    // unslid address); the handler matches on it via siginfo.si_perf_data.
    unsigned long cookie = (unsigned long) 0x100ABCDEUL;
    int fd = rclol_cpuid_trace_arm((unsigned long) site, cookie);
    if (fd < 0) {
        fprintf(stderr, "FAIL: could not arm breakpoint (errno=%d %s). "
                "perf sigtrap breakpoints need kernel >= 5.13 and permission.\n",
                -fd, strerror(-fd));
        return 2;
    }

    // Main-thread runs across several leaves.
    const unsigned leaves[] = { 0, 1, 7, 0x80000000u, 0x80000002u };
    int main_runs = (int)(sizeof(leaves) / sizeof(leaves[0]));
    for (int i = 0; i < main_runs; ++i) {
        unsigned ecx = (leaves[i] == 7) ? 0 : 0;
        rclol_test_cpuid(leaves[i], ecx, g_out);
        unsigned ref[4];
        ref_cpuid(leaves[i], ecx, ref);
        char m[64];
        snprintf(m, sizeof(m), "armed leaf 0x%x emulated == real", leaves[i]);
        CHECK(memcmp(g_out, ref, sizeof(ref)) == 0, m);
    }

    // A thread created AFTER arming must inherit the breakpoint (inherit=1).
    pthread_t th;
    CHECK(pthread_create(&th, NULL, thread_fn, NULL) == 0, "spawn post-arm thread");
    pthread_join(th, NULL);

    unsigned long hits = rclol_cpuid_trace_hits(cookie);
    char hm[96];
    snprintf(hm, sizeof(hm), "hit count %lu == %d (main) + %d (thread)",
             hits, main_runs, kThreadRuns);
    CHECK(hits == (unsigned long)(main_runs + kThreadRuns), hm);
    CHECK(spurious_traps == 0, "no spurious (unmatched) traps");
    CHECK(site[0] == 0x0f && site[1] == 0xa2, "cpuid bytes unchanged after tracing");

    // The non-armed site must still produce correct results and never trap.
    unsigned char* site2 = (unsigned char*) rclol_test_cpuid2_insn;
    unsigned o2[4], ref2[4];
    rclol_test_cpuid2(1, 0, o2);
    ref_cpuid(1, 0, ref2);
    CHECK(memcmp(o2, ref2, sizeof(ref2)) == 0, "non-armed cpuid site unaffected");
    CHECK(site2[0] == 0x0f && site2[1] == 0xa2, "non-armed site bytes intact");
    CHECK(spurious_traps == 0, "non-armed cpuid produced no trap");

    // ---- Fake mode: the trapped site must report the compatibility identity
    // for the identity leaves and pass every other leaf through unchanged.
    rclol_cpuid_trace_set_fake(1);
    static const unsigned brand[3][4] = {
        { 0x65746e49u, 0x2952286cu, 0x726f4320u, 0x4d542865u }, // "Intel(R) Core(TM"
        { 0x37692029u, 0x3537392du, 0x43204830u, 0x40205550u }, // ") i7-9750H CPU @"
        { 0x362e3220u, 0x7a484730u, 0x00000000u, 0x00000000u }, // " 2.60GHz"
    };
    for (int i = 0; i < 3; ++i) {
        unsigned o[4];
        char m[80];
        rclol_test_cpuid(0x80000002u + (unsigned) i, 0, o);
        snprintf(m, sizeof(m), "fake: leaf 0x%x is brand chunk %d", 0x80000002u + (unsigned) i, i);
        CHECK(memcmp(o, brand[i], sizeof(o)) == 0, m);
    }
    {
        unsigned o[4];
        rclol_test_cpuid(1, 0, o);
        CHECK(o[0] == 0x000906eau, "fake: leaf 1 signature is i7-9750H (6/0x9e/0xa)");
        CHECK(((o[1] >> 16) & 0xffu) == 11u, "fake: leaf 1 reports 12 logical processors");
        CHECK(((o[1] >> 8) & 0xffu) == 8u, "fake: leaf 1 keeps CLFLUSH size 8");
    }
    {
        unsigned o[4];
        rclol_test_cpuid(0, 0, o);
        CHECK(o[0] == 0x16u, "fake: leaf 0 max basic is 0x16");
        CHECK(o[1] == 0x756e6547u && o[3] == 0x49656e69u && o[2] == 0x6c65746eu,
              "fake: leaf 0 vendor is GenuineIntel");
    }
    {
        unsigned o[4];
        rclol_test_cpuid(0x80000000u, 0, o);
        CHECK(o[0] == 0x80000008u, "fake: max extended leaf is 0x80000008");
    }
    {
        unsigned o[4];
        rclol_test_cpuid(0x80000008u, 0, o);
        CHECK(o[0] == 0x3027u, "fake: leaf 0x80000008 reports 39/48 address bits");
        CHECK(o[1] == (5u | (11u << 8)), "fake: leaf 0x80000008 reports 6C/12T");
    }
    {
        unsigned o[4];
        rclol_test_cpuid(0x80000001u, 0, o);
        CHECK((o[2] & 0x00003fc4u) == 0, "fake: leaf 0x80000001 clears AMD-only ECX bits");
        CHECK((o[3] & 0xc0400000u) == 0, "fake: leaf 0x80000001 clears AMD-only EDX bits");
    }
    {
        unsigned o[4], ref[4];
        rclol_test_cpuid(7, 0, o);
        ref_cpuid(7, 0, ref);
        CHECK(memcmp(o, ref, sizeof(o)) == 0, "fake: leaf 7 passes through unchanged");
    }
    rclol_cpuid_trace_set_fake(0);
    {
        unsigned o[4], ref[4];
        rclol_test_cpuid(0x80000002u, 0, o);
        ref_cpuid(0x80000002u, 0, ref);
        CHECK(memcmp(o, ref, sizeof(o)) == 0, "fake off: leaf 0x80000002 passes through again");
    }
    CHECK(spurious_traps == 0, "still no spurious traps after fake mode");

    (void) main_runs;
    if (failures) { fprintf(stderr, "\n%d check(s) failed\n", failures); return 1; }
    fprintf(stderr, "\nAll checks passed (armed site traced from main + inherited thread).\n");
    return 0;
}
