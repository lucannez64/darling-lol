/*
 * ground_truth.c - read-only probe of the macOS code-signing / SIP surface.
 *
 * Runs on a real macOS host (developer machine, CI runner, or a volunteer's
 * Mac). Writes JSON to stdout so artifacts from different macOS versions can
 * be diffed. It never mutates signing state: no MARKINVALID/MARKHARD/SET_STATUS
 * calls, no ptrace, no file writes.
 *
 * Build:  clang -O2 -Wall -Wextra -o ground_truth ground_truth.c
 * Run:    ./ground_truth > ground-truth.json
 *
 * The public SDK exposes CS_OPS_* through <sys/codesign.h>, but the CS_* flag
 * values and the CSR bits live in kernel-private headers. They are ABI, so
 * this probe defines them locally with the XNU source as the reference
 * (osfmk/kern/cs_blobs.h, bsd/sys/csr.h). csrctl wrappers are resolved with
 * dlsym so a missing symbol degrades to a reported error instead of a link
 * failure.
 */

#include <sys/types.h>
#include <sys/utsname.h>
#include <sys/wait.h>

#include <dlfcn.h>
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/*
 * csops operations from bsd/sys/codesign.h. The macOS SDK does not ship that
 * header (it lives in the XNU tree), so the ABI constants are defined here.
 */
#define CS_OPS_STATUS 0
#define CS_OPS_MARKINVALID 1
#define CS_OPS_MARKHARD 2
#define CS_OPS_MARKKILL 3
#define CS_OPS_CDHASH 5
#define CS_OPS_PIDOFFSET 6
#define CS_OPS_ENTITLEMENTS_BLOB 7
#define CS_OPS_MARKRESTRICT 8
#define CS_OPS_SET_STATUS 9
#define CS_OPS_BLOB 10
#define CS_OPS_IDENTITY 11
#define CS_OPS_CLEARINSTALLER 12
#define CS_OPS_CLEARPLATFORM 13
#define CS_OPS_TEAMID 14
#define CS_OPS_CLEAR_LV 15

#ifndef CS_CDHASH_LEN
#define CS_CDHASH_LEN 20
#endif
#ifndef CS_MAX_TEAMID_LEN
#define CS_MAX_TEAMID_LEN 64
#endif

/* osfmk/kern/cs_blobs.h */
#ifndef CS_VALID
#define CS_VALID 0x00000001
#define CS_ADHOC 0x00000002
#define CS_GET_TASK_ALLOW 0x00000004
#define CS_INSTALLER 0x00000008
#define CS_HARD 0x00000100
#define CS_KILL 0x00000200
#define CS_CHECK_EXPIRATION 0x00000400
#define CS_RESTRICT 0x00000800
#define CS_ENFORCEMENT 0x00001000
#define CS_REQUIRE_LV 0x00002000
#define CS_ENTITLEMENTS_VALIDATED 0x00004000
#define CS_RUNTIME 0x00010000
#define CS_LINKER_SIGNED 0x00020000
#endif

/* bsd/sys/csr.h */
#ifndef CSR_ALLOW_UNRESTRICTED_FS
#define CSR_ALLOW_UNTRUSTED_KEXTS 0x00000001u
#define CSR_ALLOW_UNRESTRICTED_FS 0x00000002u
#define CSR_ALLOW_TASK_FOR_PID 0x00000004u
#define CSR_ALLOW_KERNEL_DEBUGGER 0x00000008u
#define CSR_ALLOW_APPLE_INTERNAL 0x00000010u
#define CSR_ALLOW_UNRESTRICTED_DTRACE 0x00000020u
#define CSR_ALLOW_UNRESTRICTED_NVRAM 0x00000040u
#define CSR_ALLOW_DEVICE_CONFIGURATION 0x00000080u
#define CSR_ALLOW_ANY_RECOVERY_OS 0x00000100u
#define CSR_ALLOW_UNAPPROVED_KEXTS 0x00000200u
#define CSR_ALLOW_EXECUTABLE_POLICY_OVERRIDE 0x00000400u
#define CSR_ALLOW_UNAUTHENTICATED_ROOT 0x00000800u
#endif

typedef int (*csr_get_config_fn)(uint32_t *config);
typedef int (*csr_check_fn)(uint32_t mask);

static csr_get_config_fn csr_get_config_sym(void)
{
	static csr_get_config_fn fn;

	if (!fn) {
		void *h = dlopen("/usr/lib/libSystem.B.dylib", RTLD_LAZY);
		if (h)
			fn = (csr_get_config_fn)dlsym(h, "csr_get_active_config");
	}
	return fn;
}

static csr_check_fn csr_check_sym(void)
{
	static csr_check_fn fn;

	if (!fn) {
		void *h = dlopen("/usr/lib/libSystem.B.dylib", RTLD_LAZY);
		if (h)
			fn = (csr_check_fn)dlsym(h, "csr_check");
	}
	return fn;
}

typedef int (*csops_fn)(pid_t, unsigned int, void *, size_t);

static csops_fn csops_symbol(void)
{
	static csops_fn fn;

	if (!fn) {
		void *h = dlopen("/usr/lib/libSystem.B.dylib", RTLD_LAZY);
		if (h)
			fn = (csops_fn)dlsym(h, "csops");
	}
	return fn;
}

static int x_csops(pid_t pid, unsigned int op, void *addr, size_t size)
{
	csops_fn fn = csops_symbol();

	if (!fn) {
		errno = ENOSYS;
		return -1;
	}
	return fn(pid, op, addr, size);
}

static void print_hex(const unsigned char *p, size_t n)
{
	for (size_t i = 0; i < n; i++)
		printf("%02x", p[i]);
}

static void print_json_string(const char *s)
{
	putchar('"');
	for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
		switch (*p) {
		case '"':  fputs("\\\"", stdout); break;
		case '\\': fputs("\\\\", stdout); break;
		case '\n': fputs("\\n", stdout); break;
		case '\r': fputs("\\r", stdout); break;
		case '\t': fputs("\\t", stdout); break;
		default:
			if (*p < 0x20)
				printf("\\u%04x", *p);
			else
				putchar(*p);
		}
	}
	putchar('"');
}

static uint32_t be32(const unsigned char *p)
{
	return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
	       ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

/* Validate the 8-byte CS_GenericBlob header and return its total length.
 * Returns 0 for a malformed or truncated blob. */
static size_t blob_length(const unsigned char *buf, size_t cap)
{
	uint32_t length;

	if (cap < 8)
		return 0;
	length = be32(buf + 4);
	if (length < 8 || length > cap)
		return 0;
	return length;
}

static int read_blob(pid_t pid, unsigned int op, unsigned char **out, size_t *out_len)
{
	size_t cap = 65536;
	unsigned char *buf = malloc(cap);

	if (!buf)
		return -1;
	memset(buf, 0, cap);
	errno = 0;
	if (x_csops(pid, op, buf, cap) != 0) {
		int e = errno;
		free(buf);
		errno = e;
		return -1;
	}
	*out = buf;
	*out_len = cap;
	return 0;
}

static void probe_self(void)
{
	uint32_t flags = 0;
	unsigned char cdhash[CS_CDHASH_LEN];
	char teamid[CS_MAX_TEAMID_LEN];
	unsigned char *ents = NULL;
	size_t ents_len = 0;
	off_t pidoffset = 0;
	int r;

	printf("  \"self\": {\n");
	printf("    \"pid\": %d,\n", (int)getpid());

	errno = 0;
	r = x_csops(getpid(), CS_OPS_STATUS, &flags, sizeof(flags));
	printf("    \"status\": {\"rc\": %d, \"errno\": %d, \"flags\": \"0x%08x\", "
	       "\"valid\": %s, \"adhoc\": %s, \"runtime\": %s, \"require_lv\": %s, "
	       "\"get_task_allow\": %s, \"hard\": %s, \"kill\": %s, \"restrict\": %s},\n",
	       r, r ? errno : 0, flags,
	       (flags & CS_VALID) ? "true" : "false",
	       (flags & CS_ADHOC) ? "true" : "false",
	       (flags & CS_RUNTIME) ? "true" : "false",
	       (flags & CS_REQUIRE_LV) ? "true" : "false",
	       (flags & CS_GET_TASK_ALLOW) ? "true" : "false",
	       (flags & CS_HARD) ? "true" : "false",
	       (flags & CS_KILL) ? "true" : "false",
	       (flags & CS_RESTRICT) ? "true" : "false");

	memset(cdhash, 0, sizeof(cdhash));
	errno = 0;
	r = x_csops(getpid(), CS_OPS_CDHASH, cdhash, sizeof(cdhash));
	printf("    \"cdhash\": {\"rc\": %d, \"errno\": %d, \"value\": \"", r, r ? errno : 0);
	if (r == 0)
		print_hex(cdhash, sizeof(cdhash));
	printf("\"},\n");

	memset(teamid, 0, sizeof(teamid));
	errno = 0;
	r = x_csops(getpid(), CS_OPS_TEAMID, teamid, sizeof(teamid));
	printf("    \"teamid\": {\"rc\": %d, \"errno\": %d, \"value\": ", r, r ? errno : 0);
	if (r == 0 && blob_length((unsigned char *)teamid, sizeof(teamid)) > 8) {
		teamid[sizeof(teamid) - 1] = '\0';
		print_json_string(teamid + 8);
	} else if (r == 0) {
		print_json_string("");
	} else {
		printf("null");
	}
	printf("},\n");

	if (read_blob(getpid(), CS_OPS_ENTITLEMENTS_BLOB, &ents, &ents_len) == 0) {
		size_t total;
		ents[ents_len - 1] = '\0';
		total = blob_length(ents, ents_len);
		size_t payload = total > 8 ? total - 8 : 0;
		printf("    \"entitlements\": {\"rc\": 0, \"length\": %zu, \"xml\": ", payload);
		if (payload > 0)
			print_json_string((const char *)(ents + 8));
		else
			print_json_string("");
		printf("},\n");
		free(ents);
	} else {
		printf("    \"entitlements\": {\"rc\": -1, \"errno\": %d},\n", errno);
	}

	errno = 0;
	r = x_csops(getpid(), CS_OPS_PIDOFFSET, &pidoffset, sizeof(pidoffset));
	printf("    \"pidoffset\": {\"rc\": %d, \"errno\": %d, \"value\": %lld}\n",
	       r, r ? errno : 0, (long long)pidoffset);

	printf("  },\n");
}

static void probe_child(void)
{
	pid_t child = fork();
	if (child < 0) {
		printf("  \"child_query\": {\"fork_errno\": %d},\n", errno);
		return;
	}
	if (child == 0) {
		for (;;)
			pause();
		_exit(0);
	}

	usleep(100000);

	uint32_t flags = 0;
	unsigned char cdhash[CS_CDHASH_LEN];
	char teamid[CS_MAX_TEAMID_LEN];

	errno = 0;
	int rs = x_csops(child, CS_OPS_STATUS, &flags, sizeof(flags));
	int es = errno;
	errno = 0;
	int rc = x_csops(child, CS_OPS_CDHASH, cdhash, sizeof(cdhash));
	int ec = errno;
	errno = 0;
	int rt = x_csops(child, CS_OPS_TEAMID, teamid, sizeof(teamid));
	int et = errno;

	printf("  \"child_query\": {\n");
	printf("    \"pid\": %d,\n", (int)child);
	printf("    \"status\": {\"rc\": %d, \"errno\": %d, \"flags\": \"0x%08x\"},\n",
	       rs, rs ? es : 0, flags);
	printf("    \"cdhash\": {\"rc\": %d, \"errno\": %d, \"value\": \"", rc, rc ? ec : 0);
	if (rc == 0)
		print_hex(cdhash, sizeof(cdhash));
	printf("\"},\n");
	printf("    \"teamid\": {\"rc\": %d, \"errno\": %d, \"value\": ", rt, rt ? et : 0);
	if (rt == 0 && blob_length((unsigned char *)teamid, sizeof(teamid)) > 8) {
		teamid[sizeof(teamid) - 1] = '\0';
		print_json_string(teamid + 8);
	} else if (rt == 0) {
		print_json_string("");
	} else {
		printf("null");
	}
	printf("}\n");
	printf("  },\n");

	kill(child, SIGKILL);
	waitpid(child, NULL, 0);
}

int main(void)
{
	struct utsname u;
	uint32_t config = 0;
	csr_get_config_fn get_config = csr_get_config_sym();
	csr_check_fn check = csr_check_sym();
	int cr = -1, cr_errno = ENOSYS, ck_tfp = -1, ck_dbg = -1;

	if (uname(&u) != 0)
		memset(&u, 0, sizeof(u));

	if (get_config) {
		errno = 0;
		cr = get_config(&config);
		cr_errno = cr ? errno : 0;
	}
	if (check) {
		ck_tfp = check(CSR_ALLOW_TASK_FOR_PID);
		ck_dbg = check(CSR_ALLOW_KERNEL_DEBUGGER);
	}

	printf("{\n");
	printf("  \"uname\": {\"sysname\": ");
	print_json_string(u.sysname);
	printf(", \"release\": ");
	print_json_string(u.release);
	printf(", \"version\": ");
	print_json_string(u.version);
	printf(", \"machine\": ");
	print_json_string(u.machine);
	printf("},\n");
	printf("  \"pid\": %d,\n", (int)getpid());

	printf("  \"csr\": {\"rc\": %d, \"errno\": %d, \"config\": \"0x%08x\", "
	       "\"unrestricted_fs\": %s, \"task_for_pid\": %s, \"debugger\": %s, "
	       "\"unauthenticated_root\": %s},\n",
	       cr, cr_errno, config,
	       (config & CSR_ALLOW_UNRESTRICTED_FS) ? "true" : "false",
	       (config & CSR_ALLOW_TASK_FOR_PID) ? "true" : "false",
	       (config & CSR_ALLOW_KERNEL_DEBUGGER) ? "true" : "false",
	       (config & CSR_ALLOW_UNAUTHENTICATED_ROOT) ? "true" : "false");

	printf("  \"csr_check\": {\"task_for_pid\": %d, \"debugger\": %d},\n",
	       ck_tfp, ck_dbg);

	probe_self();
	probe_child();

	printf("  \"done\": true\n");
	printf("}\n");
	return 0;
}
