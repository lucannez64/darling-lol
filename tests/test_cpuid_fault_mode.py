"""cpuid fault mode: trap every cpuid of the process, not a few armed sites.

With the flag token fault=1 the tracer turns on CPUID faulting
(arch_prctl(ARCH_SET_CPUID, 0)) instead of arming breakpoints, and emulates
each cpuid from the SIGSEGV it raises. The host test compiles the patched
handler and runs it for real when arch_prctl accepts CPUID faulting; where the
CPU, the kernel or a sandbox refuses it, it checks the fallback. Set
RCLOL_REQUIRE_CPUID_FAULT=1 to fail instead when the mode cannot be turned on.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EMULATION = 'darling/src/libsystem_kernel/emulation'
TRACE = f'{EMULATION}/src/linux_premigration/signal/cpuid_trace.c'


def patched_lines(path):
    patch = (ROOT / 'patches/xnu.patch').read_text()
    section = patch.split(f'diff --git a/{path} b/{path}\n', 1)[1].split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith(('+', ' ')) and not line.startswith('+++'))


class CpuidFaultModeTests(unittest.TestCase):
    def test_setup_tries_fault_mode_before_arming_and_only_on_request(self):
        trace = patched_lines(TRACE)
        requested = trace[trace.index('static int rclol_fault_requested('):]
        requested = requested[:requested.index('\n}\n')]
        self.assertIn('rclol_flag_value(contents, "fault=")', requested)
        self.assertIn('got = rclol_whole_tokens(contents, got, (long) sizeof(contents) - 1);\n'
                      '\t\tcontents[got] = \'\\0\';', trace)
        setup = trace[trace.index('void rclol_cpuid_trace_setup(void)'):]
        # the League branch: fault mode, then the breakpoint groups as fallback
        fault = setup.rindex('if (rclol_fault_requested(contents)) {')
        self.assertLess(fault, setup.index('const unsigned long* chosen = rclol_group1;'))
        self.assertIn('if (on)\n\t\t\treturn;', setup[fault:fault + 500])
        # the client branch (mvg.framework only): same order, and a refusal is logged
        client = setup[:setup.index('rclol_arm_mvg_cpuinfo((const void*) mvg_base, mvg_base);')]
        self.assertIn('if (rclol_fault_requested(contents)) {\n\t\t\tif (rclol_cpuid_fault_enable()) {', client)
        self.assertIn('fault mode unavailable (mvg process), arming breakpoints', client)

    def test_signal_handler_serves_the_fault_before_mach_exception_handling(self):
        sigexc = patched_lines(f'{EMULATION}/src/linux_premigration/signal/sigexc.c')
        hook = 'if (linux_signum == LINUX_SIGSEGV && rclol_cpuid_fault_handle(info, ctxt))\n\t\treturn;'
        handler = sigexc[sigexc.index('void sigexc_handler('):]
        self.assertIn(hook, handler)
        self.assertLess(handler.index(hook), handler.index('dserver_rpc_interrupt_enter();'))

    def test_guest_masks_never_block_sigsegv_in_fault_mode(self):
        mask = patched_lines(f'{EMULATION}/src/xnu_syscall/bsd/impl/signal/sigprocmask.c')
        self.assertIn('if (set != NULL && linux_how != 1 /* LINUX_SIG_UNBLOCK */)\n'
                      '\t\tin = rclol_cpuid_fault_mask(in);', mask)
        self.assertLess(mask.index('rclol_cpuid_fault_mask('), mask.index('__NR_rt_sigprocmask'))

    def test_guest_handler_mask_never_blocks_sigsegv_in_fault_mode(self):
        # sigexc installs the guest's sa_mask with a raw rt_sigprocmask before
        # calling the guest handler: a sigfillset mask, or SIGSEGV kept blocked
        # for a SIGSEGV handler, would make a cpuid in the handler fatal.
        sigexc = patched_lines(f'{EMULATION}/src/linux_premigration/signal/sigexc.c')
        forward = sigexc[sigexc.index('if (!(sig_flags[linux_signum] & LINUX_SA_NODEFER))'):]
        forward = forward[:forward.index('&set, NULL, sizeof(linux_sigset_t));')]
        nodefer = forward.index('set |= (1ull << (linux_signum-1));')
        strip = forward.index('set = rclol_cpuid_fault_mask(set);')
        self.assertLess(nodefer, strip)
        self.assertLess(strip, forward.index('LINUX_SYSCALL(__NR_rt_sigprocmask, 2 /* LINUX_SIG_SETMASK */,'))

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_fault_mask_helper_clears_only_sigsegv_and_only_when_on(self):
        header = patched_lines(f'{EMULATION}/include/linux_premigration/signal/cpuid_trace.h')
        helper = header[header.index('static inline unsigned long long rclol_cpuid_fault_mask('):]
        helper = helper[:helper.index('\n}\n') + 3]
        harness = f"""
#include <assert.h>
#include <signal.h>
static int g_on;
static int rclol_cpuid_fault_active(void) {{ return g_on; }}
{helper}
int main(void)
{{
    unsigned long long all = ~0ull, segv = 1ull << (SIGSEGV - 1);
    assert(rclol_cpuid_fault_mask(all) == all);
    g_on = 1;
    assert(rclol_cpuid_fault_mask(all) == (all & ~segv));
    assert(rclol_cpuid_fault_mask(segv) == 0);
    assert(rclol_cpuid_fault_mask(1ull << (SIGTRAP - 1)) == 1ull << (SIGTRAP - 1));
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'mask.c'
            binary = Path(folder) / 'mask'
            source.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_failed_relock_is_logged_once_and_counted(self):
        trace = patched_lines(TRACE)
        relock = trace[trace.index('static void rclol_fault_relock(void)'):]
        relock = relock[:relock.index('\n}\n')]
        self.assertEqual(relock.count('rclol_sys(__NR_arch_prctl, RCLOL_ARCH_SET_CPUID, 0, 0, 0, 0, 0) == 0'), 2)
        self.assertIn('__atomic_fetch_add(&g_fault_relock_failures, 1, __ATOMIC_RELAXED) == 0', relock)
        self.assertIn('cannot restore faulting tid=%ld', relock)
        handler = trace[trace.index('bool rclol_cpuid_fault_handle('):]
        handler = handler[:handler.index('\n}\n')]
        # the result is still written back after a failed relock
        self.assertLess(handler.index('rclol_fault_relock();'), handler.index('r->rip += length;'))
        self.assertNotIn('rclol_sys(__NR_arch_prctl, RCLOL_ARCH_SET_CPUID, 0,', handler)

    def test_clock_probe_runs_no_cpuid_once_faulting_is_on(self):
        # The probe is lazy and its callers may have SIGSEGV blocked (the RPC
        # guard masks it without going through sys_sigprocmask).
        clock = patched_lines(f'{EMULATION}/src/linux_premigration/ext/clock_gettime.c')
        probe = clock[clock.index('static int fc_probe(void)'):]
        probe = probe[:probe.index('\n}\n')]
        check = 'if (LINUX_SYSCALL(__NR_arch_prctl, 0x1011 /* ARCH_GET_CPUID */, 0) == 0) {'
        self.assertIn(check, probe)
        self.assertLess(probe.index(check), probe.index('"cpuid"'))
        self.assertIn('fc_state = -1;', probe[probe.index(check):probe.index('"cpuid"')])

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_handler_emulates_every_cpuid_of_the_process(self):
        trace = patched_lines(TRACE)
        start = trace.index('#define RCLOL_ARCH_GET_CPUID 0x1011')
        code = trace[start:trace.index('void rclol_cpuid_trace_setup(void)', start)]
        flag_value = trace[trace.index('static const char* rclol_flag_value('):]
        flag_value = flag_value[:flag_value.index('\n}\n') + 3]
        harness = f"""
#define _GNU_SOURCE
#include <assert.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

/* the kernel's x86-64 signal frame, as the emulation's headers describe it */
struct linux_siginfo {{ int si_signo; int si_errno; int si_code; }};
struct linux_gregset {{
    unsigned long r8, r9, r10, r11, r12, r13, r14, r15, rdi, rsi, rbp, rbx, rdx, rax, rcx, rsp, rip;
}};
struct linux_ucontext {{
    unsigned long uc_flags; void* uc_link;
    struct {{ void* ss_sp; int ss_flags; size_t ss_size; }} uc_stack;
    struct {{ struct linux_gregset gregs; }} uc_mcontext;
}};

#define RCLOL_FAULT_LOG_FIRST_N 4
#define __simple_snprintf snprintf
static int g_fake = 1;
static int g_log_enabled = 1;
static int g_logged;
static int g_sites_logged;
static int g_refused;
static int g_deny_lift;
static int g_deny_relock;
static int g_identity_filled;
static char g_last_site[256];
static long rclol_sys(long nr, long a1, long a2, long a3, long a4, long a5, long a6)
{{
    /* a filter that lets faulting be turned on but not lifted */
    if (g_deny_lift && nr == SYS_arch_prctl && a1 == 0x1012 && a2 == 1)
        return -1;
    /* and one that refuses to turn it back on */
    if (g_deny_relock && nr == SYS_arch_prctl && a1 == 0x1012 && a2 == 0)
        return -1;
    long rc = syscall(nr, a1, a2, a3, a4, a5, a6);
    return rc < 0 ? -1 : rc;
}}
static unsigned long long rclol_monotonic_ns(void) {{ return 0; }}
static long rclol_gettid(void) {{ return syscall(SYS_gettid); }}
static int g_relock_logged;
static void rclol_write_str(const char* line)
{{
    if (strncmp(line, "cpuid-trace: cannot restore faulting tid=", 41) == 0) {{
        g_relock_logged++;
    }} else if (strncmp(line, "cpuid-site ", 11) == 0) {{
        g_sites_logged++;
        snprintf(g_last_site, sizeof(g_last_site), "%s", line);
    }} else if (strncmp(line, "cpuid-fault ", 12) == 0) {{
        g_logged++;
    }} else {{
        g_refused++;
    }}
}}
struct darling_cpu_identity;
static const struct darling_cpu_identity* rclol_identity(void) {{ g_identity_filled = 1; return NULL; }}
/* stands for the compat transform: leaf 0 becomes GenuineIntel */
static void rclol_cpuid_trace_spoof(uint32_t leaf, uint32_t subleaf,
    uint32_t* eax, uint32_t* ebx, uint32_t* ecx, uint32_t* edx)
{{
    (void) subleaf; (void) eax;
    /* the transform may read the host's cpuid; inside the handler that must not fault */
    {{
        uint32_t a, b, c, d;
        __asm__ volatile("cpuid" : "=a"(a), "=b"(b), "=c"(c), "=d"(d) : "0"(0), "2"(0));
        (void) a; (void) b; (void) c; (void) d;
    }}
    if (leaf == 0) {{ *ebx = 0x756e6547; *edx = 0x49656e69; *ecx = 0x6c65746e; }}
}}
{flag_value}
{code}

static volatile int g_unhandled;
static void on_segv(int sig, siginfo_t* info, void* context)
{{
    (void) sig;
    if (!rclol_cpuid_fault_handle((struct linux_siginfo*) info, (struct linux_ucontext*) context)) {{
        g_unhandled = 1;
        _exit(3);
    }}
}}

static void vendor(char out[13])
{{
    uint32_t a, b, c, d;
    __asm__ volatile("cpuid" : "=a"(a), "=b"(b), "=c"(c), "=d"(d) : "0"(0), "2"(0));
    memcpy(out, &b, 4); memcpy(out + 4, &d, 4); memcpy(out + 8, &c, 4); out[12] = 0;
}}

/* push rbx; mov eax,edi; xor ecx,ecx; cpuid; store eax..edx at rsi; pop rbx; ret */
static const unsigned char cpuid_code[] = {{
    0x53, 0x89, 0xf8, 0x31, 0xc9, 0x0f, 0xa2, 0x89, 0x06, 0x89, 0x5e, 0x04,
    0x89, 0x4e, 0x08, 0x89, 0x56, 0x0c, 0x5b, 0xc3,
}};
typedef void (*cpuid_fn)(uint32_t leaf, uint32_t out[4]);

/* cpuid from a mapping of a file that is not ELF, as guest code is */
static cpuid_fn map_code(const char* folder, const char* name)
{{
    char path[512];
    snprintf(path, sizeof(path), "%s/%s", folder, name);
    int fd = open(path, O_CREAT | O_RDWR | O_TRUNC, 0700);
    assert(fd >= 0 && write(fd, cpuid_code, sizeof(cpuid_code)) == (ssize_t) sizeof(cpuid_code));
    void* code = mmap(NULL, 4096, PROT_READ | PROT_EXEC, MAP_PRIVATE, fd, 0);
    assert(code != MAP_FAILED);
    close(fd);
    return (cpuid_fn) code;
}}

static void vendor_from(cpuid_fn fn, char out[13])
{{
    uint32_t r[4];
    fn(0, r);
    memcpy(out, &r[1], 4); memcpy(out + 4, &r[3], 4); memcpy(out + 8, &r[2], 4); out[12] = 0;
}}

static cpuid_fn g_guest;
static void* thread_main(void* arg) {{ vendor_from(g_guest, arg); return NULL; }}

int main(int argc, char** argv)
{{
    char host[13], seen[13];
    assert(argc == 2);
    g_guest = map_code(argv[1], "guest-code");
    cpuid_fn emulation = map_code(argv[1], "libsystem_kernel.dylib");
    struct linux_siginfo info = {{ SIGSEGV, 0, RCLOL_SI_KERNEL }};
    struct linux_ucontext frame;
    static const unsigned char nops[] = {{ 0x90, 0x90 }};
    static const unsigned char cpuid_bytes[] = {{ 0x0f, 0xa2 }};

    vendor(host);
    memset(&frame, 0, sizeof(frame));

    /* off: nothing is claimed, even a cpuid-shaped fault */
    assert(!rclol_cpuid_fault_active());
    frame.uc_mcontext.gregs.rip = (unsigned long) cpuid_bytes;
    assert(!rclol_cpuid_fault_handle(&info, &frame));

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = on_segv;
    sa.sa_flags = SA_SIGINFO;
    sigaction(SIGSEGV, &sa, NULL);

    /* only the exact token turns the mode on */
    assert(rclol_fault_requested("fault=1") && rclol_fault_requested("fault=1\\n"));
    assert(rclol_fault_requested("fake=1 fault=1") && rclol_fault_requested("fault=1,fake=1"));
    assert(!rclol_fault_requested("") && !rclol_fault_requested("fault=0"));
    assert(!rclol_fault_requested("fault=10") && !rclol_fault_requested("fault=1x"));
    assert(!rclol_fault_requested("fault=yes") && !rclol_fault_requested("nofault=1"));
    /* a 127-byte read of 120 spaces and "fault=10" ends in "fault=1": the cut token is dropped */
    {{
        char cut[128];
        memset(cut, ' ', 120);
        memcpy(cut + 120, "fault=1", 7);
        long kept = rclol_whole_tokens(cut, 127, 127);
        assert(kept == 120);
        cut[kept] = 0;
        assert(!rclol_fault_requested(cut));
        /* a shorter read is whole, and so is a full one that ends on a delimiter */
        memcpy(cut, "fault=1", 8);
        assert(rclol_whole_tokens(cut, 7, 127) == 7 && rclol_fault_requested(cut));
        memset(cut, ' ', 127);
        memcpy(cut + 119, "fault=1", 7);
        assert(rclol_whole_tokens(cut, 127, 127) == 127);
        cut[127] = 0;
        assert(rclol_fault_requested(cut));
    }}

    /* ARCH_GET_CPUID, as the clock probe asks it: 1 while cpuid runs freely */
    long free_before = syscall(SYS_arch_prctl, RCLOL_ARCH_GET_CPUID, 0UL);
    int on = rclol_cpuid_fault_enable();
    /* the identity is filled before any handler can run */
    assert(g_identity_filled);
    if (!on) {{
        /* no CPUID faulting on this CPU or kernel, or a sandbox refuses it: the mode stays off */
        assert(!rclol_cpuid_fault_active());
        vendor(seen);
        assert(strcmp(seen, host) == 0);
        puts("unsupported");
        return 0;
    }}
    assert(rclol_cpuid_fault_active());
    assert(free_before == 1 && syscall(SYS_arch_prctl, RCLOL_ARCH_GET_CPUID, 0UL) == 0);

    /* every cpuid now goes through the handler; guest code gets the transform */
    vendor_from(g_guest, seen);
    assert(strcmp(seen, "GenuineIntel") == 0);
    assert(g_fault_hits == 1 && g_logged == 1);

    /* the site is described once, by file name and offset, without its path */
    assert(g_sites_logged == 1);
    assert(strstr(g_last_site, " guest file=guest-code\\n") && !strchr(g_last_site, '/'));
    {{
        unsigned long rip = 0, off = 0;
        assert(sscanf(g_last_site, "cpuid-site rip=0x%lx off=0x%lx", &rip, &off) == 2);
        assert(rip == (unsigned long) g_guest + 5 && off == 5);
    }}
    vendor_from(g_guest, seen);
    assert(g_sites_logged == 1 && g_logged == 2);

    /* a host library (this ELF binary) keeps the host's answer */
    vendor(seen);
    assert(strcmp(seen, host) == 0);
    assert(g_sites_logged == 2 && strstr(g_last_site, " host file=fault-harness\\n"));

    /* so does the emulation layer, which applies the compat view itself */
    vendor_from(emulation, seen);
    assert(strcmp(seen, host) == 0);
    assert(g_sites_logged == 3 && strstr(g_last_site, " emulation file=libsystem_kernel.dylib\\n"));

    /* other leaves keep the host's values; the registers and rip are restored */
    {{
        uint32_t a, b, c, d, a2, b2, c2, d2;
        __asm__ volatile("cpuid" : "=a"(a), "=b"(b), "=c"(c), "=d"(d) : "0"(1), "2"(0));
        syscall(SYS_arch_prctl, RCLOL_ARCH_SET_CPUID, 1UL);
        __asm__ volatile("cpuid" : "=a"(a2), "=b"(b2), "=c"(c2), "=d"(d2) : "0"(1), "2"(0));
        syscall(SYS_arch_prctl, RCLOL_ARCH_SET_CPUID, 0UL);
        assert(a == a2 && c == c2 && d == d2 && (b & 0x00ffffff) == (b2 & 0x00ffffff));
    }}

    /* a thread created afterwards is covered too: its cpuid faults as well */
    pthread_t thread;
    char in_thread[13];
    unsigned long hits = g_fault_hits;
    pthread_create(&thread, NULL, thread_main, in_thread);
    pthread_join(thread, NULL);
    assert(strcmp(in_thread, "GenuineIntel") == 0);
    assert(g_fault_hits == hits + 1);

    /* so is a forked child */
    pid_t pid = fork();
    if (pid == 0) {{
        hits = g_fault_hits;
        vendor_from(g_guest, seen);
        _exit(strcmp(seen, "GenuineIntel") == 0 && g_fault_hits == hits + 1 ? 0 : 1);
    }}
    int status;
    waitpid(pid, &status, 0);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);

    /* only cpuid faults are claimed: not a memory fault, not another instruction */
    info.si_code = 1; /* SEGV_MAPERR */
    frame.uc_mcontext.gregs.rip = (unsigned long) cpuid_bytes;
    assert(!rclol_cpuid_fault_handle(&info, &frame));
    info.si_code = RCLOL_SI_KERNEL;
    frame.uc_mcontext.gregs.rip = (unsigned long) nops;
    assert(!rclol_cpuid_fault_handle(&info, &frame));
    assert(!rclol_cpuid_fault_handle(NULL, &frame) && !rclol_cpuid_fault_handle(&info, NULL));

    /* the log is bounded */
    for (int i = 0; i < 20; i++) vendor(seen);
    assert(g_logged == RCLOL_FAULT_LOG_FIRST_N);
    /* the three files and the leaf 1 check above: four sites, however often they run */
    assert(g_sites_logged == 4);
    assert(!g_unhandled);

    /* faulting cannot be lifted: the handler declines instead of running cpuid in here */
    g_deny_lift = 1;
    frame.uc_mcontext.gregs.rip = (unsigned long) cpuid_bytes;
    frame.uc_mcontext.gregs.rax = 0x1234;
    assert(g_refused == 0);
    assert(!rclol_cpuid_fault_handle(&info, &frame));
    assert(g_refused == 1);
    assert(frame.uc_mcontext.gregs.rip == (unsigned long) cpuid_bytes && frame.uc_mcontext.gregs.rax == 0x1234);
    g_deny_lift = 0;

    /* a REX- or 0x66-prefixed cpuid is served and skipped whole */
    {{
        static const unsigned char rex_cpuid[] = {{ 0x48, 0x0f, 0xa2 }};
        static const unsigned char two_prefixes[] = {{ 0x66, 0x48, 0x0f, 0xa2 }};
        static const unsigned char prefixed_nop[] = {{ 0x48, 0x90, 0x90 }};
        frame.uc_mcontext.gregs.rax = 0;
        frame.uc_mcontext.gregs.rip = (unsigned long) rex_cpuid;
        assert(rclol_cpuid_fault_handle(&info, &frame));
        assert(frame.uc_mcontext.gregs.rip == (unsigned long) rex_cpuid + 3);
        frame.uc_mcontext.gregs.rax = 0;
        frame.uc_mcontext.gregs.rip = (unsigned long) two_prefixes;
        assert(rclol_cpuid_fault_handle(&info, &frame));
        assert(frame.uc_mcontext.gregs.rip == (unsigned long) two_prefixes + 4);
        frame.uc_mcontext.gregs.rip = (unsigned long) prefixed_nop;
        assert(!rclol_cpuid_fault_handle(&info, &frame));
        assert(frame.uc_mcontext.gregs.rip == (unsigned long) prefixed_nop);
    }}

    /* faulting cannot be restored: the cpuid is still served, the thread is logged once */
    {{
        unsigned long served = g_fault_hits;
        g_deny_relock = 1;
        for (int i = 0; i < 2; i++) {{
            frame.uc_mcontext.gregs.rax = 0;
            frame.uc_mcontext.gregs.rip = (unsigned long) cpuid_bytes;
            assert(rclol_cpuid_fault_handle(&info, &frame));
            assert(frame.uc_mcontext.gregs.rip == (unsigned long) cpuid_bytes + 2);
        }}
        g_deny_relock = 0;
        assert(g_fault_hits == served + 2);
        assert(g_relock_logged == 1 && g_fault_relock_failures == 2);
        /* this thread really runs unfaulted now; the mode itself stays on */
        assert(syscall(SYS_arch_prctl, RCLOL_ARCH_GET_CPUID, 0UL) == 1);
        assert(rclol_cpuid_fault_active());
        syscall(SYS_arch_prctl, RCLOL_ARCH_SET_CPUID, 0UL);
    }}
    puts("supported");
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'fault.c'
            binary = Path(folder) / 'fault-harness'
            source.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-pthread', str(source), '-o', str(binary)],
                           check=True)
            result = subprocess.run([str(binary), folder], check=True, capture_output=True, text=True)
            self.assertIn(result.stdout.strip(), ('supported', 'unsupported'))
            # arch_prctl decides: a sandbox may refuse the mode on a CPU that has it
            if os.environ.get('RCLOL_REQUIRE_CPUID_FAULT') == '1':
                self.assertEqual(result.stdout.strip(), 'supported')


if __name__ == '__main__':
    unittest.main()
