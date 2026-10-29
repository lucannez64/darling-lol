/*
 * dc_codesign.c - bounded Mach-O code signature parser.
 *
 * See dc_codesign.h for scope. Every offset read from the file is validated
 * against the containing blob before use; malformed input returns -1 with a
 * reason instead of reading out of bounds.
 */

#include "dc_codesign.h"

#include <openssl/evp.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#define CD_MIN_SIZE 44u
#define CD_V_SCATTER 0x20100u
#define CD_V_TEAMID 0x20200u
#define CD_V_CODELIMIT64 0x20300u
#define CD_V_EXECSEG 0x20400u
#define CD_V_RUNTIME 0x20500u

static uint32_t be32(const uint8_t *p)
{
	return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
	       ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static uint64_t be64(const uint8_t *p)
{
	return ((uint64_t)be32(p) << 32) | be32(p + 4);
}

static uint32_t le32(const uint8_t *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
	       ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint32_t rd32(const struct dc_image *img, const uint8_t *p)
{
	return img->big_endian ? be32(p) : le32(p);
}

static bool in_range(uint64_t total, uint64_t off, uint64_t len)
{
	return off <= total && len <= total - off;
}

static int fail(char *err, size_t errlen, const char *fmt, ...)
{
	va_list ap;

	if (err && errlen) {
		va_start(ap, fmt);
		vsnprintf(err, errlen, fmt, ap);
		va_end(ap);
	}
	return -1;
}

const char *dc_hash_name(uint32_t hash_type)
{
	switch (hash_type) {
	case DC_HASHTYPE_SHA1: return "sha1";
	case DC_HASHTYPE_SHA256: return "sha256";
	case DC_HASHTYPE_SHA256_TRUNCATED: return "sha256-truncated";
	case DC_HASHTYPE_SHA384: return "sha384";
	default: return "unknown";
	}
}

const char *dc_blob_type_name(uint32_t type)
{
	switch (type) {
	case DC_BLOB_CODEDIRECTORY: return "CodeDirectory";
	case DC_BLOB_REQUIREMENTS: return "Requirements";
	case DC_BLOB_ENTITLEMENTS: return "Entitlements";
	case DC_BLOB_DER_ENTITLEMENTS: return "DEREntitlements";
	case DC_BLOB_CMS: return "CMSSignature";
	default: return "other";
	}
}

void dc_flags_to_string(uint32_t flags, char *out, size_t outlen)
{
	static const struct { uint32_t bit; const char *name; } names[] = {
		{ 0x00000001, "CS_VALID" },
		{ 0x00000002, "CS_ADHOC" },
		{ 0x00000004, "CS_GET_TASK_ALLOW" },
		{ 0x00000008, "CS_INSTALLER" },
		{ 0x00000100, "CS_HARD" },
		{ 0x00000200, "CS_KILL" },
		{ 0x00000400, "CS_CHECK_EXPIRATION" },
		{ 0x00000800, "CS_RESTRICT" },
		{ 0x00001000, "CS_ENFORCEMENT" },
		{ 0x00002000, "CS_REQUIRE_LV" },
		{ 0x00004000, "CS_ENTITLEMENTS_VALIDATED" },
		{ 0x00010000, "CS_RUNTIME" },
		{ 0x00020000, "CS_LINKER_SIGNED" },
	};
	size_t used = 0;

	if (!outlen)
		return;
	out[0] = '\0';
	for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
		if (!(flags & names[i].bit))
			continue;
		int n = snprintf(out + used, outlen - used, "%s%s",
				 used ? "," : "", names[i].name);
		if (n < 0 || (size_t)n >= outlen - used)
			break;
		used += (size_t)n;
	}
	if (!used)
		snprintf(out, outlen, "0x%08x", flags);
}

static int hash_data(uint32_t hash_type, const uint8_t *buf, size_t len,
		     uint8_t *out, size_t outcap, size_t *outlen)
{
	const EVP_MD *md;
	unsigned int mdlen = 0;
	uint8_t tmp[EVP_MAX_MD_SIZE];

	switch (hash_type) {
	case DC_HASHTYPE_SHA1: md = EVP_sha1(); break;
	case DC_HASHTYPE_SHA256:
	case DC_HASHTYPE_SHA256_TRUNCATED: md = EVP_sha256(); break;
	case DC_HASHTYPE_SHA384: md = EVP_sha384(); break;
	default: return -1;
	}

	if (EVP_Digest(buf, len, tmp, &mdlen, md, NULL) != 1)
		return -1;

	size_t want = mdlen;
	if (hash_type == DC_HASHTYPE_SHA256_TRUNCATED)
		want = 20;
	if (want > outcap)
		return -1;
	memcpy(out, tmp, want);
	if (outlen)
		*outlen = want;
	return 0;
}

static int expected_hash_size(uint32_t hash_type, uint32_t *size)
{
	switch (hash_type) {
	case DC_HASHTYPE_SHA1: *size = 20; return 0;
	case DC_HASHTYPE_SHA256: *size = 32; return 0;
	case DC_HASHTYPE_SHA256_TRUNCATED: *size = 20; return 0;
	case DC_HASHTYPE_SHA384: *size = 48; return 0;
	default: return -1;
	}
}

static int find_signature(struct dc_image *img, uint64_t *off, uint64_t *len,
			  char *err, size_t errlen)
{
	uint32_t magic;
	size_t header_size;
	uint32_t ncmds, sizeofcmds;
	uint64_t p;
	unsigned found = 0;

	if (img->size < 4)
		return fail(err, errlen, "file shorter than a Mach-O magic");

	magic = le32(img->data);
	switch (magic) {
	case 0xfeedfacfu:
		img->is_64 = true;
		img->big_endian = false;
		break;
	case 0xcffaedfeu:
		img->is_64 = true;
		img->big_endian = true;
		break;
	case 0xfeedfaceu:
		img->is_64 = false;
		img->big_endian = false;
		break;
	case 0xcefaedfeu:
		img->is_64 = false;
		img->big_endian = true;
		break;
	default:
		return fail(err, errlen, "not a thin Mach-O (magic 0x%08x)", magic);
	}

	header_size = img->is_64 ? 32 : 28;
	if (img->size < header_size)
		return fail(err, errlen, "truncated Mach-O header");

	img->cputype = rd32(img, img->data + 4);
	ncmds = rd32(img, img->data + 16);
	sizeofcmds = rd32(img, img->data + 20);

	if (!in_range(img->size, header_size, sizeofcmds))
		return fail(err, errlen, "sizeofcmds runs past end of file");

	p = header_size;
	for (uint32_t i = 0; i < ncmds; i++) {
		if (!in_range(img->size, p, 8))
			return fail(err, errlen, "load command header out of range");

		uint32_t cmd = rd32(img, img->data + p);
		uint32_t cmdsize = rd32(img, img->data + p + 4);

		if (cmdsize < 8 || !in_range(img->size, p, cmdsize))
			return fail(err, errlen, "bad load command size %u", cmdsize);

		if (cmd == DC_LC_CODE_SIGNATURE) {
			if (cmdsize < 16)
				return fail(err, errlen, "LC_CODE_SIGNATURE too small");
			*off = rd32(img, img->data + p + 8);
			*len = rd32(img, img->data + p + 12);
			found++;
		}
		p += cmdsize;
	}

	if (!found)
		return fail(err, errlen, "no LC_CODE_SIGNATURE (unsigned image)");
	if (found > 1)
		return fail(err, errlen, "multiple LC_CODE_SIGNATURE commands");

	if (!in_range(img->size, *off, *len))
		return fail(err, errlen, "signature range outside the file");
	if (*len < 12)
		return fail(err, errlen, "signature smaller than a SuperBlob header");

	return 0;
}

static int parse_superblob(struct dc_image *img, char *err, size_t errlen)
{
	const uint8_t *sb = img->data + img->sig_offset;
	uint32_t magic = be32(sb);
	uint32_t length = be32(sb + 4);
	uint32_t count = be32(sb + 8);
	uint64_t index_end;

	if (magic != DC_CSMAGIC_EMBEDDED_SIGNATURE)
		return fail(err, errlen, "bad SuperBlob magic 0x%08x", magic);
	if (length < 12 || length > img->sig_size)
		return fail(err, errlen, "SuperBlob length %u outside signature", length);
	if (count == 0 || count > DC_MAX_BLOBS)
		return fail(err, errlen, "unsupported blob count %u", count);

	index_end = 12ull + 8ull * count;
	if (index_end > length)
		return fail(err, errlen, "blob index runs past SuperBlob");

	img->superblob_length = length;
	img->blob_count = count;

	for (uint32_t i = 0; i < count; i++) {
		const uint8_t *e = sb + 12 + 8 * i;
		uint32_t type = be32(e);
		uint32_t off = be32(e + 4);
		uint64_t blen;

		if (off < index_end || !in_range(length, off, 8))
			return fail(err, errlen, "blob %u offset out of range", i);

		blen = be32(sb + off + 4);
		if (blen < 8 || !in_range(length, off, blen))
			return fail(err, errlen, "blob %u length out of range", i);

		img->blobs[i].type = type;
		img->blobs[i].offset = off;
		img->blobs[i].length = (uint32_t)blen;
		img->blobs[i].magic = be32(sb + off);
	}
	return 0;
}

static int verify_special_blob(struct dc_image *img, uint32_t blob_type,
			       uint32_t slot, uint32_t magic,
			       char *err, size_t errlen)
{
	const struct dc_blob *blob = NULL;
	const uint8_t *blob_data;
	const uint8_t *slot_hash;
	uint8_t digest[EVP_MAX_MD_SIZE];
	size_t digest_len = 0;
	uint32_t i;

	if (!img->cd)
		return fail(err, errlen, "CodeDirectory not parsed");

	for (i = 0; i < img->blob_count; i++) {
		if (img->blobs[i].type != blob_type)
			continue;
		if (blob != NULL)
			return fail(err, errlen, "duplicate blob type %u", blob_type);
		blob = &img->blobs[i];
	}

	if (slot > img->n_special_slots) {
		/* The CodeDirectory does not reserve this slot. */
		return blob == NULL ? 0 : fail(err, errlen, "special slot -%u missing", slot);
	}
	if ((uint64_t)slot * img->hash_size > img->hash_off)
		return fail(err, errlen, "special slot -%u outside hash area", slot);

	slot_hash = img->cd + img->hash_off - slot * img->hash_size;
	if (blob == NULL) {
		for (i = 0; i < img->hash_size; i++) {
			if (slot_hash[i] != 0)
				return fail(err, errlen,
					    "special slot -%u is set but the blob is absent", slot);
		}
		return 0;
	}

	if (blob->magic != magic)
		return fail(err, errlen, "special blob type %u has magic 0x%08x",
			    blob_type, blob->magic);
	blob_data = img->data + img->sig_offset + blob->offset;
	if (be32(blob_data + 4) != blob->length)
		return fail(err, errlen, "special blob type %u length mismatch", blob_type);
	if (hash_data(img->hash_type, blob_data, blob->length, digest,
		      sizeof(digest), &digest_len) != 0 ||
	    digest_len != img->hash_size ||
	    memcmp(digest, slot_hash, img->hash_size) != 0)
		return fail(err, errlen, "special slot -%u hash mismatch", slot);
	return 0;
}

static int parse_one_codedirectory(struct dc_image *img, const struct dc_blob *blob,
                                   char *err, size_t errlen)
{
	const uint8_t *cd = img->data + img->sig_offset + blob->offset;
	uint32_t cd_len = blob->length;
	uint64_t need;
	uint32_t hash_off, ident_off, n_special, n_code;
	uint32_t code_limit32 = 0;
	uint64_t code_limit = 0;

	if (cd_len < CD_MIN_SIZE)
		return fail(err, errlen, "CodeDirectory shorter than v0 header");
	if (be32(cd) != DC_CSMAGIC_CODEDIRECTORY)
		return fail(err, errlen, "bad CodeDirectory magic");
	if (be32(cd + 4) != cd_len)
		return fail(err, errlen, "CodeDirectory length field mismatch");

	img->cd = cd;
	img->cd_len = cd_len;
	img->cd_version = be32(cd + 8);
	img->cd_flags = be32(cd + 12);
	hash_off = be32(cd + 16);
	ident_off = be32(cd + 20);
	n_special = be32(cd + 24);
	n_code = be32(cd + 28);
	code_limit32 = be32(cd + 32);
	img->hash_size = cd[36];
	img->hash_type = cd[37];
	img->hash_off = hash_off;
	img->page_size_log2 = cd[39];
	img->n_special_slots = n_special;
	img->n_code_slots = n_code;
	img->code_limit = code_limit32;

	if (be32(cd + 40) != 0)
		return fail(err, errlen, "spare2 is not zero");

	need = CD_MIN_SIZE;
	if (img->cd_version >= CD_V_SCATTER)
		need += 4;
	if (img->cd_version >= CD_V_TEAMID)
		need += 4;
	if (img->cd_version >= CD_V_CODELIMIT64)
		need += 4 + 8;
	if (img->cd_version >= CD_V_EXECSEG)
		need += 8 + 8 + 8;
	if (img->cd_version >= CD_V_RUNTIME)
		need += 4 + 4;
	if (cd_len < need)
		return fail(err, errlen, "CodeDirectory v0x%x truncated (need %llu)",
			    img->cd_version, (unsigned long long)need);

	if (img->hash_type == 0)
		return fail(err, errlen, "unknown hash type %u", img->hash_type);
	{
		uint32_t want = 0;
		if (expected_hash_size(img->hash_type, &want) != 0)
			return fail(err, errlen, "unknown hash type %u", img->hash_type);
		if (img->hash_size != want)
			return fail(err, errlen, "hash size %u does not match type %s",
				    img->hash_size, dc_hash_name(img->hash_type));
	}

	if (img->page_size_log2 < 9 || img->page_size_log2 > 30)
		return fail(err, errlen, "invalid page size 2^%u", img->page_size_log2);

	if (img->cd_version >= CD_V_CODELIMIT64) {
		uint64_t limit64 = be64(cd + 56);
		if (limit64)
			code_limit = limit64;
	}
	if (code_limit == 0)
		code_limit = code_limit32;
	if (code_limit == 0)
		return fail(err, errlen, "code limit is zero (no signed executable content)");
	if (code_limit > img->size)
		return fail(err, errlen, "code limit %llu beyond file size %zu",
			    (unsigned long long)code_limit, img->size);
	img->code_limit = code_limit;

	if (ident_off == 0 || ident_off >= cd_len)
		return fail(err, errlen, "identifier offset out of range");
	{
		size_t n = strnlen((const char *)cd + ident_off, cd_len - ident_off);
		if (n == cd_len - ident_off)
			return fail(err, errlen, "identifier is not NUL-terminated");
		if (n >= sizeof(img->identifier))
			return fail(err, errlen, "identifier too long");
		memcpy(img->identifier, cd + ident_off, n + 1);
	}

	img->teamid[0] = '\0';
	if (img->cd_version >= CD_V_TEAMID) {
		uint32_t team_off = be32(cd + 48);
		if (team_off) {
			if (team_off >= cd_len)
				return fail(err, errlen, "team id offset out of range");
			size_t n = strnlen((const char *)cd + team_off, cd_len - team_off);
			if (n == cd_len - team_off)
				return fail(err, errlen, "team id is not NUL-terminated");
			if (n >= sizeof(img->teamid))
				return fail(err, errlen, "team id too long");
			memcpy(img->teamid, cd + team_off, n + 1);
		}
	}

	if ((uint64_t)n_special * img->hash_size > hash_off)
		return fail(err, errlen, "special slots overlap header");
	if (hash_off > cd_len ||
	    (uint64_t)hash_off + (uint64_t)n_code * img->hash_size > cd_len)
		return fail(err, errlen, "code slots run past CodeDirectory");

	{
		uint64_t page = 1ull << img->page_size_log2;
		uint64_t want_slots = code_limit ? (code_limit + page - 1) / page : 0;
		if (want_slots != n_code)
			return fail(err, errlen,
				    "slot count %u does not match code limit %llu / page %llu",
				    n_code, (unsigned long long)code_limit,
				    (unsigned long long)page);
	}

	{
		uint8_t full[EVP_MAX_MD_SIZE];
		size_t flen = 0;

		if (hash_data(img->hash_type, cd, cd_len, full, sizeof(full), &flen) != 0 ||
		    flen < DC_CDHASH_LEN) {
			return fail(err, errlen, "failed to hash CodeDirectory");
		}
		memcpy(img->cdhash, full, DC_CDHASH_LEN);
	}
	if (hash_data(DC_HASHTYPE_SHA1, cd, cd_len, img->cd_sha1,
		      sizeof(img->cd_sha1), NULL) != 0)
		return fail(err, errlen, "failed to compute SHA-1 of CodeDirectory");
	if (hash_data(DC_HASHTYPE_SHA256, cd, cd_len, img->cd_sha256,
		      sizeof(img->cd_sha256), NULL) != 0)
		return fail(err, errlen, "failed to compute SHA-256 of CodeDirectory");

	/* Special slots bind requirements and entitlement blobs to the
	 * CodeDirectory. Verify the complete blob (including its 8-byte
	 * generic header) and reject a non-zero slot with no blob. */
	if (verify_special_blob(img, DC_BLOB_REQUIREMENTS, 2,
				DC_CSMAGIC_REQUIREMENTS, err, errlen) != 0 ||
	    verify_special_blob(img, DC_BLOB_ENTITLEMENTS, 5,
				DC_CSMAGIC_EMBEDDED_ENTITLEMENTS, err, errlen) != 0 ||
	    verify_special_blob(img, DC_BLOB_DER_ENTITLEMENTS, 7,
				DC_CSMAGIC_EMBEDDED_DER_ENTITLEMENTS, err, errlen) != 0)
		return -1;

	/* Embedded entitlements, both XML (type 5) and DER (type 7). */
	for (uint32_t i = 0; i < img->blob_count; i++) {
		const struct dc_blob *b = &img->blobs[i];
		if (b->type != DC_BLOB_ENTITLEMENTS && b->type != DC_BLOB_DER_ENTITLEMENTS)
			continue;
		if (b->length <= 8)
			return fail(err, errlen, "empty entitlements blob");
		if (b->type == DC_BLOB_ENTITLEMENTS) {
			if (b->magic != DC_CSMAGIC_EMBEDDED_ENTITLEMENTS)
				return fail(err, errlen, "bad entitlements magic 0x%08x", b->magic);
			img->entitlements = img->data + img->sig_offset + b->offset + 8;
			img->entitlements_len = b->length - 8;
		} else {
			if (b->magic != DC_CSMAGIC_EMBEDDED_DER_ENTITLEMENTS)
				return fail(err, errlen, "bad DER entitlements magic 0x%08x", b->magic);
			img->der_entitlements = img->data + img->sig_offset + b->offset + 8;
			img->der_entitlements_len = b->length - 8;
		}
	}

	return 0;
}

/* XNU preference: SHA-384, full SHA-256, truncated SHA-256, SHA-1. */
static unsigned hash_rank(uint32_t type)
{
    switch (type) {
    case DC_HASHTYPE_SHA1: return 1;
    case DC_HASHTYPE_SHA256_TRUNCATED: return 2;
    case DC_HASHTYPE_SHA256: return 3;
    case DC_HASHTYPE_SHA384: return 4;
    default: return 0;
    }
}

static int parse_codedirectory(struct dc_image *img, char *err, size_t errlen)
{
    struct dc_image best;
    unsigned best_rank = 0, seen_slots = 0, seen_ranks = 0;
    for (uint32_t i = 0; i < img->blob_count; i++) {
        const struct dc_blob *blob = &img->blobs[i];
        unsigned slot;
        if (blob->type == DC_BLOB_CODEDIRECTORY) slot = 0;
        else if (blob->type >= 0x1000 && blob->type <= 0x1004) slot = blob->type - 0x1000 + 1;
        else continue;
        if (seen_slots & (1u << slot)) return fail(err, errlen, "duplicate CodeDirectory slot");
        seen_slots |= 1u << slot;
        struct dc_image candidate = *img;
        if (parse_one_codedirectory(&candidate, blob, err, errlen) != 0) return -1;
        unsigned rank = hash_rank(candidate.hash_type);
        if (!rank || (seen_ranks & (1u << rank)))
            return fail(err, errlen, "unsupported or duplicate CodeDirectory hash type");
        seen_ranks |= 1u << rank;
        if (rank > best_rank) { best = candidate; best_rank = rank; }
    }
    if (!best_rank) return fail(err, errlen, "no CodeDirectory blob");
    *img = best;
    return 0;
}

int dc_parse(const uint8_t *data, size_t size, struct dc_image *img,
	     char *err, size_t errlen)
{
	int r;

	if (!data || !img)
		return fail(err, errlen, "null argument");
	memset(img, 0, sizeof(*img));
	img->data = data;
	img->size = size;

	r = find_signature(img, &img->sig_offset, &img->sig_size, err, errlen);
	if (r != 0)
		return r;
	r = parse_superblob(img, err, errlen);
	if (r != 0)
		return r;
	return parse_codedirectory(img, err, errlen);
}

int dc_verify_pages(struct dc_image *img, char *err, size_t errlen)
{
	uint64_t page_size;
	uint32_t hash_off;

	if (!img || !img->cd)
		return fail(err, errlen, "image not parsed");

	/* Re-check the special slots here as well: callers that parse once and
	 * verify later must not use entitlements that are not bound to the
	 * CodeDirectory. */
	if (verify_special_blob(img, DC_BLOB_REQUIREMENTS, 2,
				DC_CSMAGIC_REQUIREMENTS, err, errlen) != 0 ||
	    verify_special_blob(img, DC_BLOB_ENTITLEMENTS, 5,
				DC_CSMAGIC_EMBEDDED_ENTITLEMENTS, err, errlen) != 0 ||
	    verify_special_blob(img, DC_BLOB_DER_ENTITLEMENTS, 7,
				DC_CSMAGIC_EMBEDDED_DER_ENTITLEMENTS, err, errlen) != 0)
		return -1;

	page_size = 1ull << img->page_size_log2;
	hash_off = be32(img->cd + 16);

	img->pages_total = img->n_code_slots;
	img->pages_failed = 0;
	img->first_bad_page = 0;

	for (uint32_t i = 0; i < img->n_code_slots; i++) {
		uint64_t off = (uint64_t)i * page_size;
		uint64_t remain;
		size_t len;
		uint8_t digest[64];
		size_t dlen = 0;
		const uint8_t *want = img->cd + hash_off + (size_t)i * img->hash_size;

		if (off >= img->code_limit) {
			img->pages_failed++;
			if (!img->first_bad_page)
				img->first_bad_page = i + 1;
			continue;
		}
		remain = img->code_limit - off;
		len = remain > page_size ? (size_t)page_size : (size_t)remain;
		if (hash_data(img->hash_type, img->data + off, len,
			      digest, sizeof(digest), &dlen) != 0 ||
		    dlen != img->hash_size ||
		    memcmp(digest, want, img->hash_size) != 0) {
			img->pages_failed++;
			if (!img->first_bad_page)
				img->first_bad_page = i + 1;
		}
	}

	img->pages_verified = true;
	if (img->pages_failed)
		return fail(err, errlen, "%u of %u code pages failed verification (first: %u)",
			    img->pages_failed, img->pages_total, img->first_bad_page);
	return 0;
}
