/*
 * libFuzzer harness for the dc-codesign parser.
 *
 * Build (clang only):
 *   clang -D_GNU_SOURCE -g -O1 -fsanitize=fuzzer,address,undefined \
 *         -fno-omit-frame-pointer -o dc-parse-fuzz \
 *         fuzz/dc_parse_fuzz.c dc_codesign.c -lcrypto
 *
 * Run:
 *   ./dc-parse-fuzz -max_total_time=60 -max_len=131072 corpus/
 *
 * The harness parses arbitrary input; only on a successful parse does it
 * verify page hashes and touch the entitlements pointers, so OOB reads in
 * either path are caught by ASan.
 */

#include "../dc_codesign.h"

#include <stdint.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
	struct dc_image img;
	char err[256];

	if (dc_parse(data, size, &img, err, sizeof(err)) == 0) {
		(void)dc_verify_pages(&img, err, sizeof(err));

		/* Force a read at both ends of the located blobs. */
		if (img.entitlements && img.entitlements_len) {
			volatile uint8_t a = img.entitlements[0];
			volatile uint8_t b = img.entitlements[img.entitlements_len - 1];
			(void)a;
			(void)b;
		}
		if (img.der_entitlements && img.der_entitlements_len) {
			volatile uint8_t a = img.der_entitlements[0];
			volatile uint8_t b = img.der_entitlements[img.der_entitlements_len - 1];
			(void)a;
			(void)b;
		}
		if (img.cd) {
			volatile uint8_t a = img.cd[0];
			volatile uint8_t b = img.cd[img.cd_len - 1];
			(void)a;
			(void)b;
		}
	}
	return 0;
}
