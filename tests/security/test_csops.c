/*
 * Guest-side acceptance test for the M3 csops implementation.
 *
 * Modes:
 *   signed        ad-hoc signed, no entitlements
 *   unsigned      no LC_CODE_SIGNATURE
 *   entitlements  ad-hoc signed with get-task-allow
 *
 * Prints CSOPS_OK only when every assertion passes.
 */

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/codesign.h>

#ifndef CS_OPS_PIDPATH
#define CS_OPS_PIDPATH 4
#endif

static int failures;

#define CHECK(cond) do { \
	if (!(cond)) { \
		printf("FAIL line %d: %s\n", __LINE__, #cond); \
		failures++; \
	} \
} while (0)

static void busy(unsigned long n) {
	volatile unsigned long i;
	for (i = 0; i < n; i++) {}
}

static int contains(const unsigned char* haystack, size_t haystack_len, const char* needle) {
	size_t needle_len = strlen(needle);
	size_t i;
	if (needle_len == 0 || haystack_len < needle_len)
		return 0;
	for (i = 0; i + needle_len <= haystack_len; i++) {
		if (memcmp(haystack + i, needle, needle_len) == 0)
			return 1;
	}
	return 0;
}

static uint32_t be32(const unsigned char* p) {
	return ((uint32_t) p[0] << 24) | ((uint32_t) p[1] << 16) |
	       ((uint32_t) p[2] << 8) | (uint32_t) p[3];
}

static void print_cdhash(const unsigned char* cdhash) {
	int i;
	for (i = 0; i < 20; i++)
		printf("%02x", cdhash[i]);
}

int main(int argc, char** argv) {
	const char* mode = argc > 1 ? argv[1] : "signed";
	int unsigned_mode = strcmp(mode, "unsigned") == 0;
	int entitlements_mode = strcmp(mode, "entitled") == 0 || strcmp(mode, "entitlements") == 0;
	unsigned char cdhash[20];
	unsigned char blob[65536];
	unsigned char entitlements[4096];
	unsigned char small[8];
	char identity[512];
	char path[512];
	char teamid[256];
	uint64_t offset = 1;
	uint32_t flags = 0;
	uint32_t child_flags = 0;
	pid_t child;
	int r, tries;

	errno = 0;
	r = csops(0, CS_OPS_STATUS, &flags, sizeof(flags));
	CHECK(r == 0);
	if (unsigned_mode) {
		CHECK(flags == 0);
	} else {
		CHECK(flags & CS_VALID);
		CHECK(flags & CS_SIGNED);
		CHECK(flags & CS_ADHOC);
	}
	if (entitlements_mode)
		CHECK(flags & CS_GET_TASK_ALLOW);

	memset(cdhash, 0, sizeof(cdhash));
	errno = 0;
	r = csops(0, CS_OPS_CDHASH, cdhash, sizeof(cdhash));
	if (unsigned_mode) {
		CHECK(r == -1 && errno == EINVAL);
	} else {
		CHECK(r == 0);
		CHECK(cdhash[0] || cdhash[1] || cdhash[19]);
	}

	memset(identity, 0, sizeof(identity));
	errno = 0;
	r = csops(0, CS_OPS_IDENTITY, identity, sizeof(identity));
	if (unsigned_mode) {
		CHECK(r == -1 && errno == EINVAL);
	} else {
		CHECK(r == 0);
		CHECK(be32((unsigned char*) identity + 4) == strlen("test_csops") + 1 + 8);
		CHECK(strcmp(identity + 8, "test_csops") == 0);
	}

	errno = 0;
	r = csops(0, CS_OPS_TEAMID, teamid, sizeof(teamid));
	if (unsigned_mode)
		CHECK(r == -1 && errno == EINVAL);
	else
		CHECK(r == -1 && errno == ENOENT);

	memset(blob, 0, sizeof(blob));
	errno = 0;
	r = csops(0, CS_OPS_BLOB, blob, sizeof(blob));
	if (unsigned_mode) {
		CHECK(r == -1 && errno == EINVAL);
	} else {
		CHECK(r == 0);
		CHECK(blob[0] == 0xfa && blob[1] == 0xde && blob[2] == 0x0c && blob[3] == 0xc0);
	}

	memset(small, 0, sizeof(small));
	errno = 0;
	r = csops(0, CS_OPS_BLOB, small, sizeof(small));
	if (unsigned_mode)
		CHECK(r == -1 && errno == EINVAL);
	else
		CHECK(r == -1 && errno == ERANGE);
	if (r == -1 && errno == ERANGE) {
		CHECK(small[0] == 0 && small[1] == 0 && small[2] == 0 && small[3] == 0);
		CHECK(be32(small + 4) > 8);
	}

	memset(entitlements, 0, sizeof(entitlements));
	errno = 0;
	r = csops(0, CS_OPS_ENTITLEMENTS_BLOB, entitlements, sizeof(entitlements));
	if (unsigned_mode) {
		CHECK(r == -1 && errno == EINVAL);
	} else if (entitlements_mode) {
		CHECK(r == 0);
		CHECK(entitlements[0] == 0xfa && entitlements[1] == 0xde &&
		      entitlements[2] == 0x71 && entitlements[3] == 0x71);
		CHECK(entitlements[8] == '<');
		CHECK(contains(entitlements + 8, sizeof(entitlements) - 8, "get-task-allow"));
	} else {
		CHECK(r == 0);
		CHECK(entitlements[0] == 0 && entitlements[1] == 0 &&
		      entitlements[2] == 0 && entitlements[3] == 0);
	}

	errno = 0;
	r = csops(0, CS_OPS_PIDOFFSET, &offset, sizeof(offset));
	CHECK(r == 0);
	CHECK(offset == 0);

	memset(path, 0, sizeof(path));
	errno = 0;
	r = csops(0, CS_OPS_PIDPATH, path, sizeof(path));
	if (!unsigned_mode) {
		CHECK(r == 0);
		CHECK(strstr(path + 8, "test_csops") != NULL);
	}

	errno = 0;
	r = csops(0, CS_OPS_MARKHARD, NULL, 0);
	if (unsigned_mode) {
		CHECK(r == -1 && errno == EINVAL);
	} else {
		CHECK(r == 0);
		flags = 0;
		CHECK(csops(0, CS_OPS_STATUS, &flags, sizeof(flags)) == 0);
		CHECK(flags & CS_HARD);
	}

	child = fork();
	if (child == 0) {
		busy(400000000UL);
		_exit(0);
	}
	CHECK(child >= 0);
	if (child > 0) {
		busy(30000000UL);
		errno = 0;
		r = csops(child, CS_OPS_STATUS, &child_flags, sizeof(child_flags));
		CHECK(r == 0);
		CHECK(child_flags == flags);

		for (tries = 0; tries < 4000; tries++) {
			errno = 0;
			r = csops(child, CS_OPS_STATUS, &child_flags, sizeof(child_flags));
			if (r == -1 && errno == ESRCH)
				break;
			busy(1000000UL);
		}
		CHECK(r == -1 && errno == ESRCH);
	}

	errno = 0;
	r = csops(getpid() + 100000, CS_OPS_STATUS, &child_flags, sizeof(child_flags));
	CHECK(r == -1 && errno == ESRCH);

	if (failures != 0) {
		printf("CSOPS_FAIL mode=%s failures=%d\n", mode, failures);
		return 1;
	}

	printf("CSOPS_OK mode=%s flags=0x%08x cdhash=", mode, flags);
	print_cdhash(cdhash);
	printf("\n");
	return 0;
}
