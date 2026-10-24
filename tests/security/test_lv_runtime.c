/*
 * Guest-side M4 test: dlopen a path, optionally enabling CS_REQUIRE_LV after
 * the process has started (so the test does not depend on how libSystem itself
 * was signed).
 *
 * Usage: test_lv_runtime [--lv] PATH
 */

#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/codesign.h>

int main(int argc, char** argv) {
	void* handle;

	if (argc > 2 && strcmp(argv[1], "--lv") == 0) {
		uint32_t flags = CS_REQUIRE_LV;
		if (csops(0, CS_OPS_SET_STATUS, &flags, sizeof(flags)) != 0) {
			printf("SET_LV_FAIL\n");
			return 3;
		}
		printf("SET_LV_OK\n");
		argv++;
	} else if (argc > 2 && strcmp(argv[1], "--clear-lv") == 0) {
		if (csops(0, CS_OPS_CLEAR_LV, NULL, 0) != 0) {
			printf("CLEAR_LV_FAIL\n");
			return 3;
		}
		printf("CLEAR_LV_OK\n");
		argv++;
	}

	if (argc < 2)
		return 2;

	handle = dlopen(argv[1], RTLD_NOW);
	if (handle == 0) {
		printf("DLOPEN_FAIL %s\n", dlerror());
		return 1;
	}
	printf("DLOPEN_OK\n");
	return 0;
}
