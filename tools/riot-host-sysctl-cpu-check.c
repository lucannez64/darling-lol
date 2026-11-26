// Host-only check for darling-lol's real CPU topology reporting in
// emulation/src/xnu_syscall/bsd/helper/misc/sysctl_hw.c (+ the shared
// common/cpu_topology.h probe it drives).
//
// The real sysctl_hw.c and sysctl_unspec.c sources are linked against host
// shims: read_string() reads the host's actual /sys files, __lolsys_syscall()
// forwards to the host (and can fail sched_getaffinity on request), and the
// Mach calls return sentinel values that must never reach the results. The
// host's own topology is computed independently of the code under test.
//
// Modes (argv[1]):
//   (none)      the host as it is
//   affinity    this process is restricted to four online CPUs
//   no-detect   /sys CPU files and sched_getaffinity both fail -> fixed 8s
//   optout      DARLING_CPU_FIXED_FLAG exists -> the old fixed 8s
#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <mach/host_info.h>
#include <mach/mach_init.h>

#include <darling/emulation/xnu_syscall/bsd/helper/misc/sysctl_hw.h>
#include <darling/emulation/xnu_syscall/bsd/helper/misc/sysctl_machdep.h>
#include <darling/emulation/xnu_syscall/bsd/helper/misc/sysctl_proc.h>
#include <darling/emulation/xnu_syscall/bsd/helper/misc/sysctl_unspec.h>
#include <darling/emulation/linux_premigration/ext/sys/utsname.h>

// ---------------------------------------------------------------------------
// Shims for the compiled emulation sources

static int failDetection = 0; // read_string()/sched_getaffinity() fail on /sys CPU files

bool read_string(const char* path, char* dst, int maxlen) {
    if (failDetection && strncmp(path, "/sys/devices/system/cpu", 23) == 0)
        return false;
    FILE* file = fopen(path, "r");
    if (file == NULL)
        return false;
    int n = (int)fread(dst, 1, (size_t)(maxlen - 1), file);
    fclose(file);
    if (n <= 0)
        return false;
    dst[n] = '\0';
    return true;
}

long __lolsys_syscall(long a1, long a2, long a3, long a4, long a5, long a6, int nr) {
    if (failDetection && nr == SYS_sched_getaffinity)
        return -EINVAL;
    long result = syscall(nr, a1, a2, a3, a4, a5, a6);
    return result == -1 ? -errno : result;
}

unsigned long long __simple_atoi(const char* str, const char** endp) {
    unsigned long long value = 0;
    while (*str >= '0' && *str <= '9')
        value = value * 10 + (unsigned long long)(*str++ - '0');
    if (endp)
        *endp = str;
    return value;
}

int __simple_sprintf(char* buffer, const char* format, ...) {
    va_list args;
    va_start(args, format);
    int ret = vsnprintf(buffer, 4096, format, args);
    va_end(args);
    return ret;
}

void copyout_string(const char* str, char* out, unsigned long* out_len) {
    unsigned long need = strlen(str) + 1;
    if (out == NULL) {
        *out_len = need;
        return;
    }
    if (*out_len < need)
        need = *out_len;
    memcpy(out, str, need);
    out[need - 1] = '\0';
    *out_len = need;
}

// The machdep.cpu.core_count / thread_count handlers fall back to
// /proc/cpuinfo only when /sys detection fails (mode no-detect); the line
// reader and the open/close wrappers only have to link.
struct rdline_buffer;
void _readline_init(struct rdline_buffer* buf) { (void)buf; }
const char* _readline(int fd, struct rdline_buffer* buf) { (void)fd; (void)buf; return NULL; }
long sys_open_nocancel(const char* filename, int flags, unsigned int mode) { (void)filename; (void)flags; (void)mode; return -1; }
long close_internal(int fd) { (void)fd; return -1; }

// common/cpu_identity.c reads /proc/self/environ through the guest syscall
// bridge; forward to the host.
long linux_syscall(long a1, long a2, long a3, long a4, long a5, long a6, int nr) {
    long result = syscall(nr, a1, a2, a3, a4, a5, a6);
    return result == -1 ? -errno : result;
}

// glibc has no strlcpy; sysctl_unspec.c declares it.
size_t strlcpy(char* dst, const char* src, size_t size) {
    size_t len = strlen(src);
    if (size != 0) {
        size_t copy = len < size - 1 ? len : size - 1;
        memcpy(dst, src, copy);
        dst[copy] = '\0';
    }
    return len;
}

struct linux_utsname* need_uname(void) {
    static struct linux_utsname name = { .sysname = "Darwin", .machine = "x86_64" };
    return &name;
}

// The Mach layer: sentinel values that must be overwritten by the topology
// probe. The test fails if any of them survives into an answer.
mach_port_t mach_host_self(void) { return 1; }
mach_port_t mach_task_self(void) { return 1; }
kern_return_t mach_port_deallocate(ipc_space_t task, mach_port_name_t name) { (void)task; (void)name; return 0; }
#define SENTINEL_MEMORY (64ULL << 30) // "host" memory: 64 GiB
static void sentinel_reply(struct host_basic_info* reply) {
    *reply = (struct host_basic_info) { 0 };
    reply->avail_cpus = 99;
    reply->max_cpus = 99;
    reply->memory_size = 0x80000000u;
    reply->physical_cpu = 99;
    reply->physical_cpu_max = 99;
    reply->logical_cpu = 99;
    reply->logical_cpu_max = 99;
    reply->cpu_type = 7;
    reply->max_mem = SENTINEL_MEMORY;
}
// The raw darlingserver reply (libsyscall wraps host_info() around it).
static int rpc_fail = 0; // the next raw RPCs fail without touching the buffer
kern_return_t _kernelrpc_host_info(mach_port_name_t host, int itype, void* hinfo, mach_msg_type_number_t* count) {
    (void)host; (void)itype;
    if (rpc_fail)
        return 5; // KERN_FAILURE
    sentinel_reply(hinfo);
    *count = HOST_BASIC_INFO_COUNT;
    return 0;
}

// The raw HOST_VM_INFO64 reply hw.usermem reads: 1 GiB wired.
kern_return_t _kernelrpc_host_statistics64(mach_port_name_t host, int flavor, void* info, mach_msg_type_number_t* count) {
    (void)host; (void)flavor; (void)count;
    if (rpc_fail)
        return 5; // KERN_FAILURE
    ((struct vm_statistics64*)info)->wire_count = 262144;
    return 0;
}

// The cpuid trap's transform, which the cpuid-decoded machdep.cpu leaves call
// under the compat profile. cpuid_trace.c needs the guest signal headers, so
// the check links this pass-through; tests/test_machdep_cpu_leaves.py runs
// those leaves through the real transform.
void rclol_cpuid_trace_spoof(uint32_t leaf, uint32_t subleaf,
    uint32_t* eax, uint32_t* ebx, uint32_t* ecx, uint32_t* edx) {
    (void)leaf; (void)subleaf; (void)eax; (void)ebx; (void)ecx; (void)edx;
}

// sysctl_hw.c: what libsyscall's host_info wrapper writes into a reply.
void darling_host_basic_info_present(struct host_basic_info* out, mach_msg_type_number_t count);

// dyld calls host_info(HOST_BASIC_INFO) before libc exists, so nothing the
// wrapper reaches may call getenv() (a NULL-environ getenv crashed launchd at
// boot). This getenv interposes libc's and fails the check while forbidden.
extern char** environ;
static int forbid_getenv = 0;
char* getenv(const char* name) {
    if (forbid_getenv) {
        fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=host_info path called getenv(\"%s\")\n", name);
        exit(1);
    }
    size_t len = strlen(name);
    for (char** entry = environ; entry != NULL && *entry != NULL; ++entry)
        if (strncmp(*entry, name, len) == 0 && (*entry)[len] == '=')
            return *entry + len + 1;
    return NULL;
}

// A minimal sysctl tree whose "hw" node is the real one; sysctl_unspec.c
// walks it for sysctlbyname (name -> oid).
static const struct known_sysctl root_children[] = {
    { .oid = 6 /* CTL_HW */, .type = CTLTYPE_NODE, .exttype = "", .name = "hw", .subctls = sysctls_hw },
    { .oid = -1 }
};
const struct known_sysctl sysctls_root = {
    .oid = 0, .type = CTLTYPE_NODE, .exttype = "", .name = "root", .subctls = root_children
};

// ---------------------------------------------------------------------------
// Independent host topology (does not use the code under test)

static unsigned int count_list(const char* list) {
    unsigned int count = 0;
    const char* p = list;
    for (;;) {
        unsigned long long first = 0, last;
        const char* start;
        while (*p == ',' || *p == ' ' || *p == '\t' || *p == '\n')
            ++p;
        start = p;
        while (*p >= '0' && *p <= '9')
            first = first * 10 + (unsigned int)(*p++ - '0');
        if (p == start)
            break;
        last = first;
        if (*p == '-') {
            ++p;
            last = 0;
            while (*p >= '0' && *p <= '9')
                last = last * 10 + (unsigned int)(*p++ - '0');
        }
        if (last >= first && last - first < 4096)
            count += (unsigned int)(last - first + 1);
        if (*p != ',' && *p != ' ' && *p != '\t' && *p != '\n')
            break;
    }
    return count;
}

static unsigned int file_count(const char* path) {
    FILE* file = fopen(path, "r");
    if (file == NULL)
        return 0;
    char buf[256];
    if (fgets(buf, sizeof(buf), file) == NULL) {
        fclose(file);
        return 0;
    }
    fclose(file);
    return count_list(buf);
}

static unsigned int file_uint(const char* path, int* ok) {
    FILE* file = fopen(path, "r");
    if (file == NULL) {
        *ok = 0;
        return 0;
    }
    char buf[64];
    if (fgets(buf, sizeof(buf), file) == NULL) {
        fclose(file);
        *ok = 0;
        return 0;
    }
    fclose(file);
    *ok = 1;
    return (unsigned int)strtoul(buf, NULL, 10);
}

// distinct (package, core) pairs among [0, online), optionally masked
static unsigned int host_cores(const unsigned char* mask, int maskBytes, unsigned int online) {
    unsigned long long pairs[256];
    unsigned int count = 0;
    if (online > 256)
        return 0;
    for (unsigned int cpu = 0; cpu < online; ++cpu) {
        char path[112];
        int ok;
        if (mask != NULL && (mask[cpu >> 3] & (1u << (cpu & 7))) == 0)
            continue;
        snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%u/topology/physical_package_id", cpu);
        unsigned int package = file_uint(path, &ok);
        if (!ok)
            return 0;
        snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%u/topology/core_id", cpu);
        unsigned int core = file_uint(path, &ok);
        if (!ok)
            return 0;
        unsigned long long pair = ((unsigned long long)package << 32) | core;
        int seen = 0;
        for (unsigned int i = 0; i < count; ++i)
            if (pairs[i] == pair)
                seen = 1;
        if (!seen)
            pairs[count++] = pair;
    }
    return count;
}

struct expect {
    unsigned int ncpu, active, logical, logical_max, physical, physical_max;
};

static void host_expectations(struct expect* e) {
    unsigned int online = file_count("/sys/devices/system/cpu/online");
    if (online == 0)
        online = file_count("/sys/devices/system/cpu/present");

    cpu_set_t set;
    CPU_ZERO(&set);
    unsigned int active = online;
    if (sched_getaffinity(0, sizeof(set), &set) == 0)
        active = (unsigned int)CPU_COUNT(&set);

    e->ncpu = online;
    e->active = active;
    e->logical = active;
    e->logical_max = online;
    e->physical_max = host_cores(NULL, 0, online);
    if (e->physical_max == 0)
        e->physical_max = online;
    e->physical = host_cores((const unsigned char*)&set, (int)sizeof(set), online);
    if (e->physical == 0)
        e->physical = e->physical_max;
}

// ---------------------------------------------------------------------------
// Driving the real handlers

static const struct known_sysctl* find_by_name(const char* name) {
    for (const struct known_sysctl* entry = sysctls_hw; entry->oid != -1; ++entry) {
        if (strcmp(entry->name, name) == 0)
            return entry;
    }
    fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=%s missing from sysctls_hw\n", name);
    exit(1);
}

static int int_value(const struct known_sysctl* entry) {
    unsigned long len = 0;
    long rc = entry->handler(NULL, 0, NULL, &len, NULL, 0);
    if (rc != 0 || len != sizeof(int)) {
        fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=%s size rc=%ld len=%lu\n", entry->name, rc, len);
        exit(1);
    }
    int value = 0;
    len = sizeof(value);
    rc = entry->handler(NULL, 0, &value, &len, NULL, 0);
    if (rc != 0) {
        fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=%s call rc=%ld\n", entry->name, rc);
        exit(1);
    }
    if (value == 99) {
        fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=%s leaked the Mach sentinel\n", entry->name);
        exit(1);
    }
    return value;
}

static void require_value(const struct known_sysctl* entry, int expected) {
    int value = int_value(entry);
    if (value != expected) {
        fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=%s value=%d expected=%d\n", entry->name, value, expected);
        exit(1);
    }
}

// sysctlbyname("hw.<name>") through the real name_to_oid handler, then the
// handler selected by the returned OID (what the dispatcher does).
static void require_byname(const char* full_name, int expected) {
    const struct known_sysctl* unspec = NULL;
    for (const struct known_sysctl* entry = sysctls_unspec; entry->oid != -1; ++entry) {
        if (strcmp(entry->name, "name_to_oid") == 0)
            unspec = entry;
    }
    if (unspec == NULL) {
        fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=name_to_oid missing\n");
        exit(1);
    }

    int oid[4] = { 0 };
    unsigned long len = sizeof(oid);
    long rc = unspec->handler(NULL, 0, oid, &len, (void*)full_name, strlen(full_name));
    if (rc != 0 || len != 2 * sizeof(int) || oid[0] != 6) {
        fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=name_to_oid %s rc=%ld len=%lu oid=%d,%d\n",
                full_name, rc, len, oid[0], oid[1]);
        exit(1);
    }

    const struct known_sysctl* entry = NULL;
    for (const struct known_sysctl* probe = sysctls_hw; probe->oid != -1; ++probe) {
        if (probe->oid == oid[1]) {
            entry = probe;
            break;
        }
    }
    if (entry == NULL) {
        fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=name_to_oid %s unknown oid %d\n", full_name, oid[1]);
        exit(1);
    }
    require_value(entry, expected);
}

// ---------------------------------------------------------------------------
// machdep.cpu.* (sysctl_machdep.c): the CPU identity node. In the host profile
// every value is the host's own cpuid view; in the compat profile they come
// from common/cpu_identity.c, the table the cpuid trap rewrites the game's
// identity leaves to. Every one of these OIDs is CTLTYPE_INT: four bytes, not
// a decimal string (they used to be written as strings, and family/model/
// stepping read the wrong cpuid leaf with the wrong bit shifts).

static void ref_cpuid(unsigned int leaf, unsigned int out[4]) {
    __asm__ volatile ("cpuid"
        : "=a"(out[0]), "=b"(out[1]), "=c"(out[2]), "=d"(out[3])
        : "a"(leaf), "c"(0u));
}

static const struct known_sysctl* find_machdep(const char* name) {
    for (const struct known_sysctl* entry = sysctls_machdep[0].subctls; entry->oid != -1; ++entry)
        if (strcmp(entry->name, name) == 0)
            return entry;
    fprintf(stderr, "HOST_SYSCTL_MACHDEP pass=0 error=%s missing from machdep.cpu\n", name);
    exit(1);
}

static void machdep_string(const char* name, char* buf, unsigned long size) {
    const struct known_sysctl* entry = find_machdep(name);
    unsigned long len = size;
    long rc = entry->handler(NULL, 0, buf, &len, NULL, 0);
    if (rc != 0 || len == 0 || len > size) {
        fprintf(stderr, "HOST_SYSCTL_MACHDEP pass=0 error=%s rc=%ld len=%lu\n", name, rc, len);
        exit(1);
    }
    buf[size - 1] = '\0';
}

// xnu's feature_map order and names (osfmk/i386/cpuid.c): leaf 1 EDX, then
// ECX (bits 32..63), as macOS prints machdep.cpu.features.
static const struct { int bit; const char* name; } feature_names[] = {
    {0, "FPU"}, {1, "VME"}, {2, "DE"}, {3, "PSE"}, {4, "TSC"}, {5, "MSR"}, {6, "PAE"},
    {7, "MCE"}, {8, "CX8"}, {9, "APIC"}, {11, "SEP"}, {12, "MTRR"}, {13, "PGE"},
    {14, "MCA"}, {15, "CMOV"}, {16, "PAT"}, {17, "PSE36"}, {18, "PSN"}, {19, "CLFSH"},
    {21, "DS"}, {22, "ACPI"}, {23, "MMX"}, {24, "FXSR"}, {25, "SSE"}, {26, "SSE2"},
    {27, "SS"}, {28, "HTT"}, {29, "TM"}, {31, "PBE"},
    {32, "SSE3"}, {33, "PCLMULQDQ"}, {34, "DTES64"}, {35, "MON"}, {36, "DSCPL"},
    {37, "VMX"}, {38, "SMX"}, {39, "EST"}, {40, "TM2"}, {41, "SSSE3"}, {42, "CID"},
    {44, "FMA"}, {45, "CX16"}, {46, "TPR"}, {47, "PDCM"}, {51, "SSE4.1"},
    {52, "SSE4.2"}, {53, "x2APIC"}, {54, "MOVBE"}, {55, "POPCNT"}, {57, "AES"},
    {49, "PCID"}, {50, "DCA"}, {58, "XSAVE"}, {59, "OSXSAVE"}, {43, "SEGLIM64"},
    {56, "TSCTMR"}, {60, "AVX1.0"}, {62, "RDRAND"}, {61, "F16C"}, {63, "VMM"},
};

// The same list the check builds from the feature words, independently of the
// source under test (the source used to decode the vendor ASCII as bits).
static void feature_string(unsigned int edx, unsigned int ecx, char* out, size_t size) {
    size_t length = 0;
    out[0] = '\0';
    for (size_t i = 0; i < sizeof(feature_names) / sizeof(feature_names[0]); ++i) {
        unsigned int word = feature_names[i].bit < 32 ? edx : ecx;
        if (!(word & (1u << (feature_names[i].bit & 31))))
            continue;
        if (length != 0 && length + 1 < size)
            out[length++] = ' ';
        for (const char* p = feature_names[i].name; *p && length + 1 < size; ++p)
            out[length++] = *p;
    }
    out[length] = '\0';
}

static int machdep_mode(int compat) {
    unsigned int leaf0[4], leaf1[4];
    unsigned int max_basic, signature, edx, ecx;
    char vendor[16], features[512], expected[512], brand[64];
    char label[32];

    ref_cpuid(0, leaf0);
    ref_cpuid(1, leaf1);

    if (compat) {
        // The compat profile: i7-9750H (leaf 1 EAX 0x906ea, 6/0x9e/0xa).
        max_basic = 0x16u;
        signature = 0x000906eau;
        edx = 0xbfebfbffu;
        // The i7-9750H's ECX: its instruction-set bits the host has, plus the
        // ring-0-only bits (VMX, EST, PCID, x2APIC...) whatever the host.
        ecx = (leaf1[2] & 0x7ffafbbfu) | 0x0122c9b4u;
        snprintf(vendor, sizeof(vendor), "GenuineIntel");
        snprintf(label, sizeof(label), "compat");
    } else {
        max_basic = leaf0[0];
        signature = leaf1[0];
        edx = leaf1[3];
        ecx = leaf1[2];
        snprintf(vendor, sizeof(vendor), "%c%c%c%c%c%c%c%c%c%c%c%c",
                 leaf0[1] & 0xff, (leaf0[1] >> 8) & 0xff, (leaf0[1] >> 16) & 0xff, leaf0[1] >> 24,
                 leaf0[3] & 0xff, (leaf0[3] >> 8) & 0xff, (leaf0[3] >> 16) & 0xff, leaf0[3] >> 24,
                 leaf0[2] & 0xff, (leaf0[2] >> 8) & 0xff, (leaf0[2] >> 16) & 0xff, leaf0[2] >> 24);
        snprintf(label, sizeof(label), "host");
    }

    unsigned int base_family = (signature >> 8) & 0xfu;
    unsigned int ext_family = (signature >> 20) & 0xffu;
    unsigned int base_model = (signature >> 4) & 0xfu;
    unsigned int ext_model = (signature >> 16) & 0xfu;
    int family = (int)(base_family == 0xfu ? base_family + ext_family : base_family);
    int model = (int)((base_family == 0x6u || base_family == 0xfu) ? ((ext_model << 4) | base_model) : base_model);
    int stepping = (int)(signature & 0xfu);

    require_value(find_machdep("max_basic"), (int)max_basic);
    require_value(find_machdep("family"), family);
    require_value(find_machdep("model"), model);
    require_value(find_machdep("stepping"), stepping);

    char got_vendor[16];
    machdep_string("vendor", got_vendor, sizeof(got_vendor));
    if (strcmp(got_vendor, vendor) != 0) {
        fprintf(stderr, "HOST_SYSCTL_MACHDEP pass=0 error=vendor '%s' expected '%s'\n", got_vendor, vendor);
        return 1;
    }

    machdep_string("features", features, sizeof(features));
    feature_string(edx, ecx, expected, sizeof(expected));
    if (strcmp(features, expected) != 0) {
        fprintf(stderr, "HOST_SYSCTL_MACHDEP pass=0 error=features '%s' expected '%s'\n", features, expected);
        return 1;
    }
    // On a host with every instruction set a 9750H has (any Zen or recent
    // Intel), compat prints exactly what a real MacBookPro16,1 prints.
    if (compat && (leaf1[2] & 0x7ffafbbfu & ~0x0122c9b4u) == (0x7ffafbbfu & ~0x0122c9b4u)) {
        static const char mac[] =
            "FPU VME DE PSE TSC MSR PAE MCE CX8 APIC SEP MTRR PGE MCA CMOV PAT PSE36 CLFSH DS "
            "ACPI MMX FXSR SSE SSE2 SS HTT TM PBE SSE3 PCLMULQDQ DTES64 MON DSCPL VMX EST TM2 "
            "SSSE3 FMA CX16 TPR PDCM SSE4.1 SSE4.2 x2APIC MOVBE POPCNT AES PCID XSAVE OSXSAVE "
            "SEGLIM64 TSCTMR AVX1.0 RDRAND F16C";
        if (strcmp(features, mac) != 0) {
            fprintf(stderr, "HOST_SYSCTL_MACHDEP pass=0 error=features '%s' expected the Mac's '%s'\n",
                    features, mac);
            return 1;
        }
    }

    if (compat) {
        // A profile brand from the launcher (tools/system.prop) wins.
        machdep_string("brand_string", brand, sizeof(brand));
        if (strcmp(brand, "Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz") != 0) {
            fprintf(stderr, "HOST_SYSCTL_MACHDEP pass=0 error=brand '%s'\n", brand);
            return 1;
        }
    }

    printf("HOST_SYSCTL_MACHDEP pass=1 mode=%s max_basic=%d family=%d model=%d stepping=%d vendor=%s features=\"%s\"\n",
           label, (int)max_basic, family, model, stepping, got_vendor, features);
    return 0;
}

// The compiled table, child nodes included: two entries of one node must not
// share an OID or a name. This sees the whole file, where the patch tests only
// see the hunks.
static int table_is_unique(const struct known_sysctl* table, const char* node) {
    int unique = 1;

    for (const struct known_sysctl* a = table; a->oid != -1; ++a) {
        for (const struct known_sysctl* b = a + 1; b->oid != -1; ++b) {
            if (a->oid == b->oid || strcmp(a->name, b->name) == 0) {
                fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=duplicate in %s: %s (%d) and %s (%d)\n",
                    node, a->name, a->oid, b->name, b->oid);
                unique = 0;
            }
        }
        if (a->type == CTLTYPE_NODE && !table_is_unique(a->subctls, a->name))
            unique = 0;
    }
    return unique;
}

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "normal";

    if (!table_is_unique(sysctls_hw, "hw"))
        return 1;

    if (strcmp(mode, "machdep") == 0)
        return machdep_mode(0);
    if (strcmp(mode, "machdep-compat") == 0)
        return machdep_mode(1);

    // compat / compat-affinity: RCLOL_PLATFORM_PROFILE=compat is in the
    // environment (the build script sets it); every count is the i7-9750H's.
    int compat = strcmp(mode, "compat") == 0 || strcmp(mode, "compat-affinity") == 0;

    if (strcmp(mode, "affinity") == 0 || strcmp(mode, "compat-affinity") == 0) {
        // Restrict this process to the first four online CPUs.
        unsigned int online = file_count("/sys/devices/system/cpu/online");
        if (online < 4) {
            fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=need four online CPUs (have %u)\n", online);
            return 1;
        }
        cpu_set_t set;
        CPU_ZERO(&set);
        for (unsigned int cpu = 0; cpu < 4; ++cpu)
            CPU_SET(cpu, &set);
        if (sched_setaffinity(0, sizeof(set), &set) != 0) {
            fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=cannot restrict affinity\n");
            return 1;
        }
    } else if (strcmp(mode, "no-detect") == 0) {
        failDetection = 1;
    }

    if (strcmp(mode, "rpc-fail") == 0) {
        // Our own RPC fails: the caller's successful reply keeps its memory,
        // and the next call retries the RPC.
        struct host_basic_info reply;
        sentinel_reply(&reply);
        reply.max_mem = 7ULL << 30;
        reply.memory_size = 0x7fffffffu;
        rpc_fail = 1;
        darling_host_basic_info_present(&reply, HOST_BASIC_INFO_COUNT);
        rpc_fail = 0;
        if (reply.max_mem != (7ULL << 30) || reply.memory_size != 0x7fffffffu) {
            fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=failed RPC overwrote memory max_mem=%llu memory_size=%u\n",
                    (unsigned long long)reply.max_mem, reply.memory_size);
            return 1;
        }
        sentinel_reply(&reply);
        reply.max_mem = 7ULL << 30;
        darling_host_basic_info_present(&reply, HOST_BASIC_INFO_COUNT);
        if (reply.max_mem != SENTINEL_MEMORY) {
            fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=RPC not retried after a failure (max_mem=%llu)\n",
                    (unsigned long long)reply.max_mem);
            return 1;
        }
        printf("HOST_SYSCTL_CPU pass=1 mode=rpc-fail\n");
        return 0;
    }

    // The first use of the cached host info must come through host_info, as
    // dyld's does, with getenv forbidden.
    {
        struct host_basic_info early;
        sentinel_reply(&early);
        forbid_getenv = 1;
        darling_host_basic_info_present(&early, HOST_BASIC_INFO_COUNT);
        forbid_getenv = 0;
    }

    struct expect e;
    if (strcmp(mode, "no-detect") == 0 || strcmp(mode, "optout") == 0)
        e = (struct expect) { 8, 8, 8, 8, 8, 8 };
    else if (compat)
        e = (struct expect) { 12, 12, 12, 12, 6, 6 };
    else
        host_expectations(&e);

    require_value(find_by_name("ncpu"), (int)e.ncpu);
    require_value(find_by_name("activecpu"), (int)e.active);
    require_value(find_by_name("availcpu"), (int)e.active);
    require_value(find_by_name("logicalcpu"), (int)e.logical);
    require_value(find_by_name("logicalcpu_max"), (int)e.logical_max);
    require_value(find_by_name("physicalcpu"), (int)e.physical);
    require_value(find_by_name("physicalcpu_max"), (int)e.physical_max);

    require_byname("hw.activecpu", (int)e.active);
    require_byname("hw.ncpu", (int)e.ncpu);
    require_byname("hw.logicalcpu_max", (int)e.logical_max);

    const struct known_sysctl* model = find_by_name("model");
    char model_buf[64] = "";
    unsigned long model_len = sizeof(model_buf);
    if (model->handler(NULL, 0, model_buf, &model_len, NULL, 0) != 0 ||
        strcmp(model_buf, "MacBookPro16,1") != 0) {
        fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=model changed to '%s'\n", model_buf);
        return 1;
    }

    // host_info(HOST_BASIC_INFO) must present exactly what hw.* reports, and
    // the profile memory size (RCLOL_MEMORY_BYTES) instead of the host's.
    const char* memory_env = getenv("RCLOL_MEMORY_BYTES");
    unsigned long long memory = (memory_env && *memory_env) ? strtoull(memory_env, NULL, 10) : SENTINEL_MEMORY;
    struct host_basic_info reply;
    sentinel_reply(&reply);
    darling_host_basic_info_present(&reply, HOST_BASIC_INFO_COUNT);
    unsigned int memory_size = memory > 0x80000000ULL ? 0x80000000u : (unsigned int)memory;
    if (reply.max_cpus != (int)e.ncpu || reply.avail_cpus != (int)e.active ||
        reply.logical_cpu != (int)e.logical || reply.logical_cpu_max != (int)e.logical_max ||
        reply.physical_cpu != (int)e.physical || reply.physical_cpu_max != (int)e.physical_max ||
        reply.max_mem != memory || reply.memory_size != memory_size || reply.cpu_type != 7) {
        fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=host_info max=%d avail=%d logical=%d/%d physical=%d/%d max_mem=%llu memory_size=%u cpu_type=%d\n",
                reply.max_cpus, reply.avail_cpus, reply.logical_cpu, reply.logical_cpu_max,
                reply.physical_cpu, reply.physical_cpu_max, (unsigned long long)reply.max_mem,
                reply.memory_size, reply.cpu_type);
        return 1;
    }
    // A legacy-sized reply gets only the legacy fields.
    sentinel_reply(&reply);
    darling_host_basic_info_present(&reply, 5);
    if (reply.max_cpus != (int)e.ncpu || reply.physical_cpu != 99 || reply.max_mem != SENTINEL_MEMORY) {
        fprintf(stderr, "HOST_SYSCTL_CPU pass=0 error=legacy host_info reply overwritten past its size\n");
        return 1;
    }

    if (compat) {
        require_value(find_machdep("core_count"), 6);
        require_value(find_machdep("thread_count"), 12);
    }

    printf("HOST_SYSCTL_CPU pass=1 mode=%s ncpu=%d active=%d logicalcpu=%d logicalcpu_max=%d physicalcpu=%d physicalcpu_max=%d model=%s\n",
           mode, (int)e.ncpu, (int)e.active, (int)e.logical, (int)e.logical_max,
           (int)e.physical, (int)e.physical_max, model_buf);
    return 0;
}
