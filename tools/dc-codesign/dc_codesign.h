/*
 * dc_codesign.h - bounded parser for Mach-O code signatures.
 *
 * Part of the phase-M1 work in docs/security/SIP_AMFI_IMPLEMENTATION_PLAN.md.
 * The parser is deliberately read-only and allocation-free: it points into the
 * caller's file buffer and validates every offset before use.
 *
 * Hashing uses OpenSSL here. When this code moves into libsystem_kernel the
 * hash backend must be swapped for CommonCrypto/corecrypto; the parser and the
 * validation rules stay the same.
 */

#ifndef DC_CODESIGN_H
#define DC_CODESIGN_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DC_CDHASH_LEN 20
#define DC_MAX_BLOBS 64
#define DC_IDENT_MAX 256
#define DC_TEAMID_MAX 64

/* CS_SuperBlob magics (big-endian on disk). */
#define DC_CSMAGIC_EMBEDDED_SIGNATURE 0xfade0cc0u
#define DC_CSMAGIC_CODEDIRECTORY 0xfade0c02u
#define DC_CSMAGIC_REQUIREMENTS 0xfade0c01u
#define DC_CSMAGIC_EMBEDDED_ENTITLEMENTS 0xfade7171u
#define DC_CSMAGIC_EMBEDDED_DER_ENTITLEMENTS 0xfade7172u
#define DC_CSMAGIC_BLOBWRAPPER 0xfade0b01u

/* SuperBlob slot types. */
#define DC_BLOB_CODEDIRECTORY 0u
#define DC_BLOB_REQUIREMENTS 2u
#define DC_BLOB_ENTITLEMENTS 5u
#define DC_BLOB_DER_ENTITLEMENTS 7u
#define DC_BLOB_CMS 0x10000u

/* Mach-O. */
#define DC_LC_CODE_SIGNATURE 0x1du

/* CodeDirectory hash types. */
#define DC_HASHTYPE_SHA1 1u
#define DC_HASHTYPE_SHA256 2u
#define DC_HASHTYPE_SHA256_TRUNCATED 3u
#define DC_HASHTYPE_SHA384 4u

struct dc_blob {
	uint32_t type;
	uint32_t offset;
	uint32_t length;
	uint32_t magic;
};

struct dc_image {
	const uint8_t *data;
	size_t size;

	bool is_64;
	bool big_endian;
	uint32_t cputype;

	uint64_t sig_offset;
	uint64_t sig_size;
	uint32_t superblob_length;
	uint32_t blob_count;
	struct dc_blob blobs[DC_MAX_BLOBS];

	/* CodeDirectory, pointing into the file buffer. */
	const uint8_t *cd;
	size_t cd_len;
	uint32_t cd_version;
	uint32_t cd_flags;
	uint32_t hash_type;
	uint32_t hash_size;
	uint32_t hash_off;
	uint32_t page_size_log2;
	uint32_t n_special_slots;
	uint32_t n_code_slots;
	uint64_t code_limit;
	char identifier[DC_IDENT_MAX];
	char teamid[DC_TEAMID_MAX];

	/* Digests of the CodeDirectory blob. */
	uint8_t cdhash[DC_CDHASH_LEN];
	uint8_t cd_sha1[20];
	uint8_t cd_sha256[32];

	/* Embedded entitlements (type 5) and DER entitlements (type 7). */
	const uint8_t *entitlements;
	size_t entitlements_len;
	const uint8_t *der_entitlements;
	size_t der_entitlements_len;

	/* Page-hash verification (filled by dc_verify_pages). */
	bool pages_verified;
	uint32_t pages_total;
	uint32_t pages_failed;
	uint32_t first_bad_page;
};

/*
 * Parse a Mach-O image and its embedded signature. Returns 0 on success,
 * -1 on error (err is always NUL-terminated).
 */
int dc_parse(const uint8_t *data, size_t size, struct dc_image *img,
	     char *err, size_t errlen);

/*
 * Verify every code page hash against the CodeDirectory. Requires a parsed
 * image. Returns 0 when all pages match, -1 otherwise.
 */
int dc_verify_pages(struct dc_image *img, char *err, size_t errlen);

const char *dc_hash_name(uint32_t hash_type);
const char *dc_blob_type_name(uint32_t type);
void dc_flags_to_string(uint32_t flags, char *out, size_t outlen);

#endif /* DC_CODESIGN_H */
