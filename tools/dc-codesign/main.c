/*
 * dc-codesign - inspect and verify Mach-O code signatures.
 *
 * Usage:
 *   dc-codesign info <file>
 *   dc-codesign verify <file>
 *   dc-codesign cdhash <file>
 *   dc-codesign entitlements <file>
 *   dc-codesign blobs <file>
 *
 * Exit status: 0 success, 1 verification/parse failure, 2 usage/IO error.
 */

#include "dc_codesign.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void print_hex(const uint8_t *p, size_t n)
{
	for (size_t i = 0; i < n; i++)
		printf("%02x", p[i]);
}

static int read_file(const char *path, uint8_t **out, size_t *out_len)
{
	FILE *f = fopen(path, "rb");
	long end;
	uint8_t *buf;

	if (!f) {
		fprintf(stderr, "dc-codesign: %s: %s\n", path, strerror(errno));
		return -1;
	}
	if (fseek(f, 0, SEEK_END) != 0 || (end = ftell(f)) < 0 ||
	    fseek(f, 0, SEEK_SET) != 0) {
		fprintf(stderr, "dc-codesign: %s: seek failed\n", path);
		fclose(f);
		return -1;
	}
	buf = malloc((size_t)end ? (size_t)end : 1);
	if (!buf) {
		fprintf(stderr, "dc-codesign: %s: out of memory\n", path);
		fclose(f);
		return -1;
	}
	if (fread(buf, 1, (size_t)end, f) != (size_t)end) {
		fprintf(stderr, "dc-codesign: %s: short read\n", path);
		free(buf);
		fclose(f);
		return -1;
	}
	fclose(f);
	*out = buf;
	*out_len = (size_t)end;
	return 0;
}

static int cmd_info(const char *path, struct dc_image *img)
{
	char flags[256];

	dc_flags_to_string(img->cd_flags, flags, sizeof(flags));

	printf("file=%s\n", path);
	printf("size=%zu\n", img->size);
	printf("macho.bits=%d\n", img->is_64 ? 64 : 32);
	printf("macho.endian=%s\n", img->big_endian ? "big" : "little");
	printf("macho.cputype=0x%08x\n", img->cputype);
	printf("signature.offset=%llu\n", (unsigned long long)img->sig_offset);
	printf("signature.size=%llu\n", (unsigned long long)img->sig_size);
	printf("superblob.length=%u\n", img->superblob_length);
	printf("blob.count=%u\n", img->blob_count);

	for (uint32_t i = 0; i < img->blob_count; i++) {
		const struct dc_blob *b = &img->blobs[i];
		printf("blob.%u.type=%u\n", i, b->type);
		printf("blob.%u.name=%s\n", i, dc_blob_type_name(b->type));
		printf("blob.%u.magic=0x%08x\n", i, b->magic);
		printf("blob.%u.offset=%u\n", i, b->offset);
		printf("blob.%u.length=%u\n", i, b->length);
	}

	printf("cd.version=0x%x\n", img->cd_version);
	printf("cd.flags=0x%08x\n", img->cd_flags);
	printf("cd.flags.names=%s\n", flags);
	printf("cd.hash_type=%u\n", img->hash_type);
	printf("cd.hash_type.name=%s\n", dc_hash_name(img->hash_type));
	printf("cd.hash_size=%u\n", img->hash_size);
	printf("cd.page_size=%u\n", 1u << img->page_size_log2);
	printf("cd.code_limit=%llu\n", (unsigned long long)img->code_limit);
	printf("cd.identifier=%s\n", img->identifier);
	printf("cd.teamid=%s\n", img->teamid);
	printf("cd.n_code_slots=%u\n", img->n_code_slots);
	printf("cd.n_special_slots=%u\n", img->n_special_slots);
	printf("cd.cdhash=");
	print_hex(img->cdhash, sizeof(img->cdhash));
	printf("\ncd.sha1=");
	print_hex(img->cd_sha1, sizeof(img->cd_sha1));
	printf("\ncd.sha256=");
	print_hex(img->cd_sha256, sizeof(img->cd_sha256));
	printf("\n");

	printf("entitlements.present=%d\n", img->entitlements != NULL);
	if (img->entitlements)
		printf("entitlements.length=%zu\n", img->entitlements_len);
	printf("der_entitlements.present=%d\n", img->der_entitlements != NULL);
	if (img->der_entitlements)
		printf("der_entitlements.length=%zu\n", img->der_entitlements_len);

	if (img->pages_verified) {
		printf("pages.verified=%s\n", img->pages_failed ? "no" : "yes");
		printf("pages.total=%u\n", img->pages_total);
		printf("pages.failed=%u\n", img->pages_failed);
	}
	return 0;
}

int main(int argc, char **argv)
{
	const char *cmd, *path;
	uint8_t *data = NULL;
	size_t size = 0;
	struct dc_image img;
	char err[512];
	int r;

	if (argc != 3) {
		fprintf(stderr,
			"usage: %s {info|verify|cdhash|entitlements|blobs} <file>\n",
			argv[0]);
		return 2;
	}
	cmd = argv[1];
	path = argv[2];

	if (read_file(path, &data, &size) != 0)
		return 2;

	err[0] = '\0';
	r = dc_parse(data, size, &img, err, sizeof(err));
	if (r != 0) {
		fprintf(stderr, "dc-codesign: %s: %s\n", path, err);
		free(data);
		return 1;
	}

	if (strcmp(cmd, "info") == 0) {
		r = cmd_info(path, &img);
	} else if (strcmp(cmd, "verify") == 0) {
		if (dc_verify_pages(&img, err, sizeof(err)) != 0) {
			fprintf(stderr, "dc-codesign: %s: %s\n", path, err);
			r = 1;
		} else {
			printf("status=ok cdhash=");
			print_hex(img.cdhash, sizeof(img.cdhash));
			printf(" pages=%u\n", img.pages_total);
			r = 0;
		}
	} else if (strcmp(cmd, "cdhash") == 0) {
		print_hex(img.cdhash, sizeof(img.cdhash));
		printf("\n");
		r = 0;
	} else if (strcmp(cmd, "entitlements") == 0) {
		if (!img.entitlements) {
			fprintf(stderr, "dc-codesign: %s: no entitlements blob\n", path);
			r = 1;
		} else {
			fwrite(img.entitlements, 1, img.entitlements_len, stdout);
			if (img.entitlements_len &&
			    img.entitlements[img.entitlements_len - 1] != '\0')
				printf("\n");
			r = 0;
		}
	} else if (strcmp(cmd, "blobs") == 0) {
		for (uint32_t i = 0; i < img.blob_count; i++)
			printf("%u %s type=%u magic=0x%08x offset=%u length=%u\n",
			       i, dc_blob_type_name(img.blobs[i].type),
			       img.blobs[i].type, img.blobs[i].magic,
			       img.blobs[i].offset, img.blobs[i].length);
		r = 0;
	} else {
		fprintf(stderr, "dc-codesign: unknown command '%s'\n", cmd);
		r = 2;
	}

	free(data);
	return r;
}
