/*
 * self_view.c - read-only record of what a process sees of itself on macOS.
 *
 * Companion of ground_truth.c. It runs on a real macOS host and, unchanged,
 * inside a compatibility layer built against an old SDK, so that the two JSON
 * outputs can be diffed. It describes its own process. Of other processes it
 * reads only the name and path of its parent and of pids 0 and 1, and how many
 * pids the system lists. No signal, no ptrace, no network, no file write,
 * nothing but stdout.
 *
 * Build:  clang -O2 -Wall -Wextra -o self_view self_view.c
 *         (add -arch x86_64 for the Intel slice, which also runs under Rosetta)
 * Run:    ./self_view > self-view.json
 *
 * Output: one JSON object, one field per line, keys sorted inside each
 * section. Nothing that names a machine or a user is printed: no host name,
 * no user name, no hardware identifier. clean_path() prints system paths in
 * full and anything else by kind only.
 *
 * Portability: anything newer than the 10.13 SDK, or absent from the public
 * headers, is resolved with dlsym(RTLD_DEFAULT) and reported as missing when
 * it is not there. Private or newer structures and constants are defined
 * here with the XNU or dyld header named as the reference.
 */

#include <sys/types.h>
#include <sys/mount.h>
#include <sys/param.h>
#include <sys/proc_info.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <sys/utsname.h>

#include <Availability.h>
#include <dlfcn.h>
#include <errno.h>
#include <libproc.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <mach-o/dyld.h>
#include <mach-o/loader.h>
#include <pthread.h>
#include <pwd.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

_Static_assert(sizeof(void *) == 8, "the probe is 64-bit only");

/* osfmk/mach/vm_region.h: struct vm_region_submap_info_64, #pragma pack(4).
 * The first 12 words are not vm_region_submap_short_info_64: the short form
 * has no pages_* fields. */
#pragma pack(push, 4)
struct sv_submap_info {
	int32_t protection;
	int32_t max_protection;
	uint32_t inheritance;
	uint64_t offset;
	uint32_t user_tag;
	uint32_t pages_resident;
	uint32_t pages_shared_now_private;
	uint32_t pages_swapped_out;
	uint32_t pages_dirtied;
	uint32_t ref_count;
	uint16_t shadow_depth;
	uint8_t external_pager;
	uint8_t share_mode;
	uint32_t is_submap;
	int32_t behavior;
	uint32_t object_id;
	uint16_t user_wired_count;
	uint32_t pages_reusable;
	uint64_t object_id_full;
};
#pragma pack(pop)
_Static_assert(sizeof(struct sv_submap_info) == 76, "vm_region_submap_info_64 is 19 words");

#define SV_COUNT_SHORT 12 /* VM_REGION_SUBMAP_SHORT_INFO_COUNT_64 */
#define SV_COUNT_V0 16    /* VM_REGION_SUBMAP_INFO_V0_COUNT_64 */
#define SV_COUNT_V1 17    /* VM_REGION_SUBMAP_INFO_V1_COUNT_64 */
#define SV_COUNT_V2 19    /* VM_REGION_SUBMAP_INFO_V2_COUNT_64 */

/* dyld include/mach-o/dyld_images.h: struct dyld_all_image_infos, LP64. */
struct sv_all_image_infos {
	uint32_t version;
	uint32_t infoArrayCount;
	uint64_t infoArray;
	uint64_t notification;
	uint8_t processDetachedFromSharedRegion;
	uint8_t libSystemInitialized;
	uint8_t pad[6];
	uint64_t dyldImageLoadAddress;
	uint64_t jitInfo;
	uint64_t dyldVersion;
	uint64_t errorMessage;
	uint64_t terminationFlags;
	uint64_t coreSymbolicationShmPage;
	uint64_t systemOrderFlag;
	uint64_t uuidArrayCount;
	uint64_t uuidArray;
	uint64_t dyldAllImageInfosAddress;
	uint64_t initialImageCount;
	uint64_t errorKind;
	uint64_t errorClientOfDylibPath;
	uint64_t errorTargetDylibPath;
	uint64_t errorSymbol;
	uint64_t sharedCacheSlide;        /* version 12 */
	uint8_t sharedCacheUUID[16];      /* version 13 */
	uint64_t sharedCacheBaseAddress;  /* version 15 */
	uint64_t infoArrayChangeTimestamp;
	uint64_t dyldPath;
	uint32_t notifyPorts[8];
	uint64_t reserved[9];
	uint64_t compact_dyld_image_info_addr; /* version 16 */
	uint64_t compact_dyld_image_info_size;
	uint32_t platform;
};
_Static_assert(offsetof(struct sv_all_image_infos, sharedCacheUUID) == 160, "dyld_all_image_infos layout");
_Static_assert(offsetof(struct sv_all_image_infos, platform) == 320, "dyld_all_image_infos layout");

/* bsd/sys/proc_info.h, private flavors */
#ifndef PROC_PIDREGIONPATHINFO2
#define PROC_PIDREGIONPATHINFO2 22
#endif
#ifndef PROC_PIDREGIONPATHINFO3
#define PROC_PIDREGIONPATHINFO3 23
#endif

#define SV_MAX_REGIONS 16384
#define SV_MAX_SEGS 8192
#define SV_NBUF 16
#define SV_BUFSZ 2400
#define B(x) ((x) ? "true" : "false")
#define MISSING "{\"present\": false}"

int main(void);
extern char **environ;

static int sv_errno;
#define CALL(r, expr) do { errno = 0; (r) = (expr); sv_errno = errno; } while (0)
#define ERRNO_IF(failed) ((failed) ? sv_errno : 0)

/* ---- output helpers ---------------------------------------------------- */

/* Rotating scratch strings, so several can be used in one kv() call. */
static char *scratch(void)
{
	static char pool[SV_NBUF][SV_BUFSZ];
	static unsigned next;
	char *b = pool[next++ % SV_NBUF];

	b[0] = '\0';
	return b;
}

/* Length of the well-formed UTF-8 sequence at p (2 to 4 bytes), or 0. */
static size_t utf8_len(const unsigned char *p)
{
	size_t n = p[0] >= 0xf0 ? 4 : p[0] >= 0xe0 ? 3 : p[0] >= 0xc2 ? 2 : 0;

	if (!n || p[0] > 0xf4)
		return 0;
	for (size_t i = 1; i < n; i++)
		if ((p[i] & 0xc0) != 0x80)
			return 0;
	/* overlong forms, surrogates, beyond U+10FFFF */
	if ((p[0] == 0xe0 && p[1] < 0xa0) || (p[0] == 0xed && p[1] > 0x9f) ||
	    (p[0] == 0xf0 && p[1] < 0x90) || (p[0] == 0xf4 && p[1] > 0x8f))
		return 0;
	return n;
}

/* A JSON string: UTF-8 text is kept, a byte that is not UTF-8 becomes U+FFFD. */
static const char *jstr(const char *s)
{
	char *out = scratch();
	size_t n = 0;

	out[n++] = '"';
	for (const unsigned char *p = (const unsigned char *)(s ? s : ""); *p && n < SV_BUFSZ - 8; p++) {
		size_t u;

		if (*p == '"' || *p == '\\') {
			out[n++] = '\\';
			out[n++] = (char)*p;
		} else if (*p < 0x20 || *p == 0x7f) {
			n += (size_t)snprintf(out + n, 7, "\\u%04x", *p);
		} else if (*p < 0x80) {
			out[n++] = (char)*p;
		} else if ((u = utf8_len(p)) != 0) {
			memcpy(out + n, p, u);
			n += u;
			p += u - 1;
		} else {
			n += (size_t)snprintf(out + n, 7, "\\ufffd");
		}
	}
	out[n++] = '"';
	out[n] = '\0';
	return out;
}

/* System locations are printed in full. Any other path is reduced to its
 * kind: a name a user chose (home, project folder, mount point, temporary
 * directory) is never printed. */
static const char *clean_path(const char *p)
{
	static const char *const sys[] = {"/System/", "/usr/", "/Library/", "/bin/", "/sbin/", "/dev/",
		"/Applications/", "/etc/", "/private/etc/", "/private/var/db/", NULL};
	static const char *const homes[] = {"/Users/", "/home/", "/root/", "/var/root/", "/private/var/root/", NULL};
	static const char *const tmps[] = {"/tmp/", "/private/tmp/", "/var/tmp/", "/private/var/tmp/",
		"/var/folders/", "/private/var/folders/", NULL};
	static const char host[] = "/Volumes/SystemRoot/";
	char *out = scratch();
	const char *q, *kind = "<other>";

	if (!p || !*p)
		return out;
	if (p[0] != '/')
		kind = "<relative>";
	else if (!p[1])
		kind = "/";
	/* the host's root as Darling shows it: judged by the path below it */
	q = strncmp(p, host, sizeof(host) - 1) ? p : p + sizeof(host) - 2;
	for (int i = 0; homes[i]; i++)
		if (!strncmp(q, homes[i], strlen(homes[i])))
			kind = "<home>";
	for (int i = 0; tmps[i]; i++)
		if (!strncmp(q, tmps[i], strlen(tmps[i])))
			kind = "<tmp>";
	for (int i = 0; sys[i]; i++)
		if (!strncmp(q, sys[i], strlen(sys[i])))
			kind = p;
	snprintf(out, SV_BUFSZ, "%s", kind);
	return out;
}

static const char *jpath(const char *p)
{
	return jstr(clean_path(p));
}

static const char *jhex(const uint8_t *p, size_t n)
{
	char *out = scratch();

	out[0] = '"';
	for (size_t i = 0; i < n && i < 64; i++)
		snprintf(out + 1 + 2 * i, 3, "%02x", p[i]);
	strlcat(out, "\"", SV_BUFSZ);
	return out;
}

static const char *num(unsigned long long v)
{
	char *out = scratch();

	snprintf(out, SV_BUFSZ, "%llu", v);
	return out;
}

static const char *words_hex(const uint32_t *w, unsigned n)
{
	char *out = scratch();

	for (unsigned i = 0; i < n && i < 64; i++)
		snprintf(out + 9 * i, 10, "%08x%s", w[i], i + 1 < n ? " " : "");
	return out;
}

/* One character per 32-bit word: which words a call filled, not their values. */
static const char *nonzero(const uint32_t *w, unsigned n)
{
	char *out = scratch();
	unsigned i;

	for (i = 0; i < n && i < 128; i++)
		out[i] = w[i] ? '1' : '0';
	out[i] = '\0';
	return out;
}

static uint64_t u64_at(const uint32_t *w, unsigned word)
{
	uint64_t v;

	memcpy(&v, w + word, sizeof(v));
	return v;
}

struct sv_flag {
	uint32_t bit;
	const char *name;
};

static const char *decode(uint32_t v, const struct sv_flag *t)
{
	char *out = scratch();

	for (; t->name; t++) {
		if ((v & t->bit) != t->bit)
			continue;
		if (*out)
			strlcat(out, "|", SV_BUFSZ);
		strlcat(out, t->name, SV_BUFSZ);
	}
	return out;
}

static const char *key_n(const char *prefix, unsigned n)
{
	char *out = scratch();

	snprintf(out, SV_BUFSZ, "%s_%05u", prefix, n);
	return out;
}

static struct sv_kv {
	char *key, *val;
} *kvs;
static size_t nkv, capkv;

static void kv(const char *key, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static void kv(const char *key, const char *fmt, ...)
{
	va_list ap;
	char *val = NULL;

	if (nkv == capkv) {
		capkv = capkv ? capkv * 2 : 256;
		kvs = realloc(kvs, capkv * sizeof(*kvs));
		if (!kvs)
			exit(2);
	}
	va_start(ap, fmt);
	if (vasprintf(&val, fmt, ap) < 0)
		val = NULL;
	va_end(ap);
	kvs[nkv].key = strdup(key);
	kvs[nkv].val = val;
	if (!kvs[nkv].key || !val)
		exit(2);
	nkv++;
}

static int kv_cmp(const void *a, const void *b)
{
	return strcmp(((const struct sv_kv *)a)->key, ((const struct sv_kv *)b)->key);
}

/* Prints the fields collected since the previous section, sorted by key. The
 * flush keeps the finished sections if a later one crashes. */
static void section(const char *name, int last)
{
	qsort(kvs, nkv, sizeof(*kvs), kv_cmp);
	printf("  \"%s\": {\n", name);
	for (size_t i = 0; i < nkv; i++) {
		printf("    \"%s\": %s%s\n", kvs[i].key, kvs[i].val, i + 1 < nkv ? "," : "");
		free(kvs[i].key);
		free(kvs[i].val);
	}
	printf("  }%s\n", last ? "" : ",");
	fflush(stdout);
	nkv = 0;
}

static const char *ftype(mode_t m)
{
	return S_ISDIR(m) ? "dir" : S_ISLNK(m) ? "link" : S_ISREG(m) ? "file" : S_ISCHR(m) ? "chr" :
	       S_ISBLK(m) ? "blk" : S_ISFIFO(m) ? "fifo" : S_ISSOCK(m) ? "sock" : "other";
}

static void stat_kv(const char *call, const char *path)
{
	struct stat st;
	char key[200];
	int r;

	memset(&st, 0, sizeof(st));
	if (call[0] == 'l')
		CALL(r, lstat(path, &st));
	else
		CALL(r, stat(path, &st));
	snprintf(key, sizeof(key), "%s %s", call, path);
	kv(key, "{\"rc\": %d, \"errno\": %d, \"type\": \"%s\"}", r, ERRNO_IF(r), r ? "" : ftype(st.st_mode));
}

/* ---- dyld images ------------------------------------------------------- */

struct sv_seg {
	uint64_t lo, hi;
	const char *image;
};
static struct sv_seg segs[SV_MAX_SEGS];
static size_t nsegs;
static uint64_t cache_lo, cache_len;
static int cache_range_present;

static const struct sv_all_image_infos *aii;
static struct task_dyld_info aii_task;
static kern_return_t aii_kr;

static void add_image(const struct mach_header_64 *mh, const char *name)
{
	uint64_t slide = 0;

	if (!mh || mh->magic != MH_MAGIC_64)
		return;
	/* slide = header address - __TEXT vmaddr; this also holds for dyld,
	 * which _dyld_get_image_vmaddr_slide() does not list. */
	for (int pass = 0; pass < 2; pass++) {
		const struct load_command *lc = (const struct load_command *)(mh + 1);

		for (uint32_t i = 0; i < mh->ncmds; i++, lc = (const void *)((const char *)lc + lc->cmdsize)) {
			const struct segment_command_64 *sg = (const void *)lc;

			if (lc->cmd != LC_SEGMENT_64)
				continue;
			if (pass == 0 && !strncmp(sg->segname, "__TEXT", 16))
				slide = (uint64_t)(uintptr_t)mh - sg->vmaddr;
			if (pass == 1 && sg->vmsize && strncmp(sg->segname, "__PAGEZERO", 16) && nsegs < SV_MAX_SEGS)
				segs[nsegs++] = (struct sv_seg){sg->vmaddr + slide, sg->vmaddr + slide + sg->vmsize, name};
		}
	}
}

static void load_images(void)
{
	mach_msg_type_number_t count = TASK_DYLD_INFO_COUNT;
	void *fn;

	aii_kr = task_info(mach_task_self(), TASK_DYLD_INFO, (task_info_t)&aii_task, &count);
	if (aii_kr == KERN_SUCCESS && aii_task.all_image_info_addr)
		aii = (const struct sv_all_image_infos *)(uintptr_t)aii_task.all_image_info_addr;
	for (uint32_t i = 0; i < _dyld_image_count(); i++)
		add_image((const struct mach_header_64 *)_dyld_get_image_header(i), _dyld_get_image_name(i));
	if (aii && aii->version >= 2)
		add_image((const struct mach_header_64 *)(uintptr_t)aii->dyldImageLoadAddress, "/usr/lib/dyld");
	fn = dlsym(RTLD_DEFAULT, "_dyld_get_shared_cache_range");
	if (fn) {
		size_t len = 0;

		cache_range_present = 1;
		cache_lo = (uint64_t)(uintptr_t)((const void *(*)(size_t *))fn)(&len);
		cache_len = len;
	}
}

static const char *image_at(uint64_t lo, uint64_t hi)
{
	for (size_t i = 0; i < nsegs; i++)
		if (lo < segs[i].hi && segs[i].lo < hi)
			return segs[i].image;
	return NULL;
}

/* ---- regions ----------------------------------------------------------- */

struct sv_region {
	uint64_t start, size;
	uint32_t depth, count;
	struct sv_submap_info info;
};
static struct sv_region regs[SV_MAX_REGIONS];

static kern_return_t recurse(uint64_t *addr, uint64_t *size, uint32_t *depth, void *info, uint32_t *count)
{
	return mach_vm_region_recurse(mach_task_self(), addr, size, depth, (vm_region_recurse_info_t)info, count);
}

/* What a memory scanner does: ask for the deepest nesting. XNU then never
 * returns a submap entry; if one comes back anyway, go one level down. */
static unsigned walk_scanner(kern_return_t *end_kr, unsigned *descents)
{
	uint64_t addr = 0;
	unsigned n = 0;

	*descents = 0;
	while (n < SV_MAX_REGIONS) {
		struct sv_region *r = &regs[n];
		uint32_t ask = 1024;

		for (int tries = 0;; tries++) {
			r->start = addr;
			r->size = 0;
			r->depth = ask;
			r->count = SV_COUNT_V2;
			memset(&r->info, 0, sizeof(r->info));
			*end_kr = recurse(&r->start, &r->size, &r->depth, &r->info, &r->count);
			if (*end_kr != KERN_SUCCESS || !r->info.is_submap || tries == 8)
				break;
			ask = r->depth + 1;
			(*descents)++;
		}
		if (*end_kr != KERN_SUCCESS)
			break;
		n++;
		if (r->start + r->size <= r->start)
			break;
		addr = r->start + r->size;
	}
	return n;
}

static void walk_flat(void)
{
	struct sv_submap_info info;
	uint64_t addr = 0, size = 0;
	uint32_t depth, count, last_count = 0;
	unsigned n = 0, submaps = 0;
	kern_return_t kr = KERN_SUCCESS;

	while (n < SV_MAX_REGIONS) {
		depth = 0;
		count = SV_COUNT_V2;
		memset(&info, 0, sizeof(info));
		kr = recurse(&addr, &size, &depth, &info, &count);
		if (kr != KERN_SUCCESS)
			break;
		n++;
		submaps += info.is_submap != 0;
		last_count = count;
		if (addr + size <= addr)
			break;
		addr += size;
	}
	kv("walk_flat", "{\"regions\": %u, \"submap_entries\": %u, \"end_kr\": %d, \"count_out\": %u}",
	   n, submaps, kr, last_count);
}

/* mach_vm_region and the vm_map subsystem's vm_region_64 on the text of main. */
static void region_flavor(const char *key, int narrow, int flavor)
{
	uint32_t w[32];
	mach_msg_type_number_t count = 10; /* vm_region_info_t is array[*:10] in mach_types.defs */
	mach_port_t object = MACH_PORT_NULL;
	uint64_t size = 0;
	kern_return_t kr;
	char extra[160] = "";

	memset(w, 0x5a, sizeof(w));
	if (narrow) {
		vm_address_t a = (vm_address_t)(uintptr_t)&main;
		vm_size_t s = 0;

		kr = vm_region_64(mach_task_self(), &a, &s, flavor, (vm_region_info_t)w, &count, &object);
		size = s;
	} else {
		mach_vm_address_t a = (mach_vm_address_t)(uintptr_t)&main;
		mach_vm_size_t s = 0;

		kr = mach_vm_region(mach_task_self(), &a, &s, flavor, (vm_region_info_t)w, &count, &object);
		size = s;
	}
	if (kr == KERN_SUCCESS && flavor == VM_REGION_BASIC_INFO_64)
		snprintf(extra, sizeof(extra), ", \"protection\": %u, \"max_protection\": %u, \"shared\": %u", w[0], w[1], w[3]);
	if (kr == KERN_SUCCESS && flavor == VM_REGION_EXTENDED_INFO)
		snprintf(extra, sizeof(extra), ", \"protection\": %u, \"user_tag\": %u, \"external_pager\": %u, \"share_mode\": %u",
			 w[0], w[1], (w[7] >> 16) & 0xff, w[7] >> 24);
	if (kr == KERN_SUCCESS && flavor == VM_REGION_TOP_INFO)
		snprintf(extra, sizeof(extra), ", \"ref_count\": %u, \"share_mode\": %u", w[1], w[4] & 0xff);
	kv(key, "{\"kr\": %d, \"count_out\": %u, \"size\": \"0x%llx\", \"object_name_null\": %s, \"words\": \"%s\"%s}",
	   kr, count, (unsigned long long)size, B(object == MACH_PORT_NULL), words_hex(w, 12), extra);
}

static void sec_regions(void)
{
	static const uint32_t counts[] = {SV_COUNT_SHORT, SV_COUNT_V0, SV_COUNT_V1, SV_COUNT_V2, 20, 0};
	static struct sv_hist {
		struct sv_submap_info k;
		unsigned n;
	} hist[1024];
	unsigned nhist = 0, descents = 0, exec = 0, exec_image = 0, exec_cache = 0, exec_none = 0;
	kern_return_t end_kr = KERN_SUCCESS;
	unsigned n = walk_scanner(&end_kr, &descents);

	kv("walk_scanner", "{\"regions\": %u, \"submap_descents\": %u, \"end_kr\": %d, \"truncated\": %s}",
	   n, descents, end_kr, B(n == SV_MAX_REGIONS));
	walk_flat();

	for (unsigned i = 0; i < n; i++) {
		const struct sv_region *r = &regs[i];
		const struct sv_submap_info *f = &r->info;
		const char *image = image_at(r->start, r->start + r->size);
		int in_cache = cache_len && r->start >= cache_lo && r->start < cache_lo + cache_len;
		char file[MAXPATHLEN + 1]; /* proc_regionfilename copies up to MAXPATHLEN whatever the size given */
		struct sv_submap_info k;
		unsigned h;
		int len;

		memset(file, 0, sizeof(file));
		len = proc_regionfilename(getpid(), r->start, file, MAXPATHLEN);
		if (len <= 0)
			file[0] = '\0';
		kv(key_n("region", i),
		   "{\"start\": \"0x%llx\", \"size\": \"0x%llx\", \"depth\": %u, \"count_out\": %u, "
		   "\"protection\": %d, \"max_protection\": %d, \"inheritance\": %u, \"offset\": \"0x%llx\", "
		   "\"user_tag\": %u, \"pages_resident\": %u, \"pages_shared_now_private\": %u, "
		   "\"pages_swapped_out\": %u, \"pages_dirtied\": %u, \"ref_count\": %u, \"shadow_depth\": %u, "
		   "\"external_pager\": %u, \"share_mode\": %u, \"is_submap\": %u, \"behavior\": %d, "
		   "\"object_id\": \"0x%x\", \"user_wired_count\": %u, \"pages_reusable\": %u, "
		   "\"object_id_full\": \"0x%llx\", \"file\": %s, \"image\": %s, \"in_shared_cache\": %s}",
		   (unsigned long long)r->start, (unsigned long long)r->size, r->depth, r->count,
		   f->protection, f->max_protection, f->inheritance, (unsigned long long)f->offset,
		   f->user_tag, f->pages_resident, f->pages_shared_now_private,
		   f->pages_swapped_out, f->pages_dirtied, f->ref_count, f->shadow_depth,
		   f->external_pager, f->share_mode, f->is_submap, f->behavior,
		   f->object_id, f->user_wired_count, f->pages_reusable,
		   (unsigned long long)f->object_id_full, jpath(file), jpath(image), B(in_cache));

		if (f->protection & VM_PROT_EXECUTE) {
			exec++;
			exec_image += image != NULL;
			exec_cache += in_cache;
			exec_none += !image && !in_cache;
		}

		memset(&k, 0, sizeof(k));
		k.protection = f->protection;
		k.max_protection = f->max_protection;
		k.share_mode = f->share_mode;
		k.user_tag = f->user_tag;
		k.is_submap = f->is_submap;
		k.external_pager = f->external_pager;
		k.inheritance = f->inheritance;
		k.behavior = f->behavior;
		for (h = 0; h < nhist && memcmp(&hist[h].k, &k, sizeof(k)); h++)
			;
		if (h == nhist && nhist < 1024)
			hist[nhist++].k = k;
		if (h < nhist)
			hist[h].n++;
	}
	for (unsigned h = 0; h < nhist; h++) {
		char key[160];

		snprintf(key, sizeof(key), "hist prot=%d max=%d share=%u tag=%03u submap=%u pager=%u inherit=%u behavior=%d",
			 hist[h].k.protection, hist[h].k.max_protection, hist[h].k.share_mode, hist[h].k.user_tag,
			 hist[h].k.is_submap, hist[h].k.external_pager, hist[h].k.inheritance, hist[h].k.behavior);
		kv(key, "%u", hist[h].n);
	}
	/* in_dyld_image and in_shared_cache overlap: a cache dylib counts in both. */
	kv("exec_regions", "{\"total\": %u, \"in_dyld_image\": %u, \"in_shared_cache\": %u, \"in_neither\": %u}",
	   exec, exec_image, exec_cache, exec_none);

	/* How the kernel treats each count: the sentinel shows the words it left alone. */
	for (unsigned i = 0; n && i < sizeof(counts) / sizeof(counts[0]); i++) {
		uint32_t w[24], depth = 1024, count = counts[i];
		uint64_t addr = regs[0].start, size = 0;
		kern_return_t kr;
		char key[32];

		memset(w, 0x5a, sizeof(w));
		kr = recurse(&addr, &size, &depth, w, &count);
		snprintf(key, sizeof(key), "count_probe_%02u", counts[i]);
		kv(key, "{\"kr\": %d, \"count_out\": %u, \"depth\": %u, \"size\": \"0x%llx\", \"words\": \"%s\"}",
		   kr, count, depth, (unsigned long long)size, words_hex(w, 24));
	}

	region_flavor("mach_vm_region_basic_info_64", 0, VM_REGION_BASIC_INFO_64);
	region_flavor("mach_vm_region_extended_info", 0, VM_REGION_EXTENDED_INFO);
	region_flavor("mach_vm_region_top_info", 0, VM_REGION_TOP_INFO);
	region_flavor("vm_region_64_basic_info_64", 1, VM_REGION_BASIC_INFO_64);
	{
		struct sv_submap_info info;
		vm_address_t a = (vm_address_t)(uintptr_t)&main;
		vm_size_t s = 0;
		natural_t depth = 1024;
		mach_msg_type_number_t count = SV_COUNT_V2;
		kern_return_t kr;

		memset(&info, 0, sizeof(info));
		kr = vm_region_recurse_64(mach_task_self(), &a, &s, &depth, (vm_region_recurse_info_t)&info, &count);
		kv("vm_region_recurse_64", "{\"kr\": %d, \"count_out\": %u, \"depth\": %u, \"size\": \"0x%llx\", "
		   "\"protection\": %d, \"max_protection\": %d, \"share_mode\": %u, \"user_tag\": %u}",
		   kr, count, depth, (unsigned long long)s, info.protection, info.max_protection,
		   info.share_mode, info.user_tag);
	}
}

/* ---- dyld -------------------------------------------------------------- */

static void dladdr_kv(const char *key, const void *addr)
{
	Dl_info dl;
	int r;

	memset(&dl, 0, sizeof(dl));
	r = dladdr(addr, &dl);
	kv(key, "{\"rc\": %d, \"fname\": %s, \"sname\": %s, \"fbase_nonzero\": %s}",
	   r, jpath(dl.dli_fname), jstr(dl.dli_sname), B(dl.dli_fbase));
}

static void sec_dyld(void)
{
	/* dyld include/mach-o/dyld_priv.h; all take no argument and return bool */
	static const char *const flags[] = {"dyld_shared_cache_some_image_overridden",
		"_dyld_shared_cache_optimized", "_dyld_shared_cache_is_locally_built", NULL};
	static const char *const paths[] = {"/usr/lib/libSystem.B.dylib",
		"/usr/lib/system/libsystem_kernel.dylib", "/usr/lib/dyld", NULL};
	static const char *const libs[] = {"System", "CoreFoundation", NULL};
	uint32_t n = _dyld_image_count();
	char key[160];
	void *fn;

	kv("image_count", "%u", n);
	for (uint32_t i = 0; i < n; i++)
		kv(key_n("image", i), "%s", jpath(_dyld_get_image_name(i)));

	kv("task_dyld_info", "{\"kr\": %d, \"addr_nonzero\": %s, \"all_image_info_size\": %llu, \"all_image_info_format\": %d}",
	   aii_kr, B(aii_task.all_image_info_addr), (unsigned long long)aii_task.all_image_info_size,
	   (int)aii_task.all_image_info_format);
	if (aii) {
		uint32_t v = aii->version;
		uint64_t size = aii_task.all_image_info_size;

		kv("infos_version", "%u", v);
		kv("infos_infoArrayCount", "%u", aii->infoArrayCount);
		kv("infos_processDetachedFromSharedRegion", "%s", B(aii->processDetachedFromSharedRegion));
		kv("infos_libSystemInitialized", "%s", v >= 2 ? B(aii->libSystemInitialized) : "null");
		kv("infos_dyldVersion", "%s", v >= 5 && aii->dyldVersion ? jstr((const char *)(uintptr_t)aii->dyldVersion) : "null");
		kv("infos_sharedCacheSlide_nonzero", "%s", v >= 12 ? B(aii->sharedCacheSlide) : "null");
		/* The cache UUID names an OS build, not a machine. */
		kv("infos_sharedCacheUUID", "%s", v >= 13 ? jhex(aii->sharedCacheUUID, 16) : "null");
		kv("infos_sharedCacheBaseAddress_nonzero", "%s", v >= 15 ? B(aii->sharedCacheBaseAddress) : "null");
		kv("infos_dyldPath", "%s", v >= 15 && aii->dyldPath ? jpath((const char *)(uintptr_t)aii->dyldPath) : "null");
		kv("infos_platform", "%s", v >= 16 && size >= offsetof(struct sv_all_image_infos, platform) + 4 ? num(aii->platform) : "null");
	}

	fn = dlsym(RTLD_DEFAULT, "_dyld_get_shared_cache_uuid");
	if (fn) {
		uint8_t uuid[16] = {0};
		bool ok = ((bool (*)(uint8_t *))fn)(uuid);

		kv("fn _dyld_get_shared_cache_uuid", "{\"present\": true, \"result\": %s, \"uuid\": %s}", B(ok), jhex(uuid, 16));
	} else {
		kv("fn _dyld_get_shared_cache_uuid", MISSING);
	}
	if (cache_range_present)
		kv("fn _dyld_get_shared_cache_range", "{\"present\": true, \"base_nonzero\": %s, \"length\": %llu}",
		   B(cache_lo), (unsigned long long)cache_len);
	else
		kv("fn _dyld_get_shared_cache_range", MISSING);
	fn = dlsym(RTLD_DEFAULT, "dyld_shared_cache_file_path");
	if (fn) {
		const char *path = ((const char *(*)(void))fn)();

		kv("fn dyld_shared_cache_file_path", "{\"present\": true, \"path\": %s}", path ? jpath(path) : "null");
	} else {
		kv("fn dyld_shared_cache_file_path", MISSING);
	}
	fn = dlsym(RTLD_DEFAULT, "_dyld_shared_cache_contains_path");
	for (int i = 0; i < 2; i++) {
		snprintf(key, sizeof(key), "fn _dyld_shared_cache_contains_path %s", paths[i]);
		if (fn)
			kv(key, "{\"present\": true, \"result\": %s}", B(((bool (*)(const char *))fn)(paths[i])));
		else
			kv(key, MISSING);
	}
	for (int i = 0; flags[i]; i++) {
		snprintf(key, sizeof(key), "fn %s", flags[i]);
		fn = dlsym(RTLD_DEFAULT, flags[i]);
		if (fn)
			kv(key, "{\"present\": true, \"result\": %s}", B(((bool (*)(void))fn)()));
		else
			kv(key, MISSING);
	}
	fn = dlsym(RTLD_DEFAULT, "_dyld_launch_mode");
	if (fn)
		kv("fn _dyld_launch_mode", "{\"present\": true, \"result\": \"0x%x\"}", ((uint32_t (*)(void))fn)());
	else
		kv("fn _dyld_launch_mode", MISSING);
	fn = dlsym(RTLD_DEFAULT, "NSVersionOfRunTimeLibrary");
	for (int i = 0; libs[i]; i++) {
		snprintf(key, sizeof(key), "fn NSVersionOfRunTimeLibrary %s", libs[i]);
		if (fn)
			kv(key, "{\"present\": true, \"result\": \"0x%x\"}", (uint32_t)((int32_t (*)(const char *))fn)(libs[i]));
		else
			kv(key, MISSING);
	}

	for (int i = 0; paths[i]; i++)
		stat_kv("stat", paths[i]);
	dladdr_kv("dladdr getpid", (const void *)(uintptr_t)&getpid);
	dladdr_kv("dladdr main", (const void *)(uintptr_t)&main);
}

/* ---- proc -------------------------------------------------------------- */

/* bsd/sys/proc_info.h: PROC_FLAG_* */
static const struct sv_flag bsd_flags[] = {{0x1, "SYSTEM"}, {0x2, "TRACED"}, {0x4, "INEXIT"}, {0x8, "PPWAIT"},
	{0x10, "LP64"}, {0x20, "SLEADER"}, {0x40, "CTTY"}, {0x80, "CONTROLT"}, {0x100, "THCWD"},
	{0x2000, "PSUGID"}, {0x4000, "EXEC"}, {0, NULL}};
/* bsd/sys/proc.h: P_* */
static const struct sv_flag p_flags[] = {{0x2, "P_CONTROLT"}, {0x4, "P_LP64"}, {0x100, "P_SUGID"},
	{0x200, "P_SYSTEM"}, {0x800, "P_TRACED"}, {0x4000, "P_EXEC"}, {0, NULL}};

static int pidinfo(int flavor, uint64_t arg, void *buf, size_t size)
{
	int r;

	memset(buf, 0, size);
	CALL(r, proc_pidinfo(getpid(), flavor, arg, buf, (int)size));
	return r;
}

static const char *pri_str(const struct proc_regioninfo *p)
{
	char *out = scratch();

	snprintf(out, SV_BUFSZ, "\"protection\": %u, \"max_protection\": %u, \"share_mode\": %u, \"user_tag\": %u, "
		 "\"flags\": \"0x%x\", \"depth\": %u, \"size\": \"0x%llx\"", p->pri_protection, p->pri_max_protection,
		 p->pri_share_mode, p->pri_user_tag, p->pri_flags, p->pri_depth, (unsigned long long)p->pri_size);
	return out;
}

static void pid_kv(const char *key, pid_t pid)
{
	char path[PROC_PIDPATHINFO_MAXSIZE], name[2 * MAXCOMLEN + 1];
	int rp, ep, rn;

	memset(path, 0, sizeof(path));
	memset(name, 0, sizeof(name));
	CALL(rp, proc_pidpath(pid, path, sizeof(path)));
	ep = sv_errno;
	CALL(rn, proc_name(pid, name, sizeof(name)));
	kv(key, "{\"pidpath_rc\": %d, \"pidpath_errno\": %d, \"path\": %s, \"name_rc\": %d, \"name_errno\": %d, \"name\": %s}",
	   rp, rp <= 0 ? ep : 0, jpath(rp > 0 ? path : ""), rn, ERRNO_IF(rn <= 0), jstr(rn > 0 ? name : ""));
}

static void sec_proc(void)
{
	static int pids[65536];
	static uint64_t tids[1024];
	pid_t self = getpid(), parent = getppid();
	uint64_t text = (uint64_t)(uintptr_t)&main;
	struct proc_bsdinfo bi;
	struct proc_bsdshortinfo si;
	struct proc_taskinfo ti;
	struct proc_taskallinfo tai;
	struct proc_regioninfo ri;
	struct proc_regionwithpathinfo rp;
	struct proc_vnodepathinfo vp;
	struct rusage_info_v2 ru;
	struct kinfo_proc kp;
	struct stat st;
	int mib[4] = {CTL_KERN, KERN_PROC, KERN_PROC_PID, self};
	size_t len;
	long long age;
	int r, has0 = 0, has1 = 0, argmax = 0;
	unsigned threadnum;

	r = pidinfo(PROC_PIDTBSDINFO, 0, &bi, sizeof(bi));
	bi.pbi_comm[sizeof(bi.pbi_comm) - 1] = bi.pbi_name[sizeof(bi.pbi_name) - 1] = '\0';
	age = (long long)time(NULL) - (long long)bi.pbi_start_tvsec;
	kv("pidinfo_tbsdinfo", "{\"rc\": %d, \"errno\": %d, \"size\": %zu, \"flags\": \"0x%x\", \"flags_decoded\": \"%s\", "
	   "\"status\": %u, \"xstatus\": %u, \"start_nonzero\": %s, \"start_within_day\": %s, \"uid_eq_ruid\": %s, "
	   "\"nice\": %d, \"comm\": %s, \"name\": %s}", r, ERRNO_IF(r <= 0), sizeof(bi), bi.pbi_flags,
	   decode(bi.pbi_flags, bsd_flags), bi.pbi_status, bi.pbi_xstatus, B(bi.pbi_start_tvsec),
	   B(age > -86400 && age < 86400), B(bi.pbi_uid == bi.pbi_ruid), (int)bi.pbi_nice,
	   jstr(bi.pbi_comm), jstr(bi.pbi_name));

	r = pidinfo(PROC_PIDT_SHORTBSDINFO, 0, &si, sizeof(si));
	si.pbsi_comm[sizeof(si.pbsi_comm) - 1] = '\0';
	kv("pidinfo_t_shortbsdinfo", "{\"rc\": %d, \"errno\": %d, \"size\": %zu, \"flags\": \"0x%x\", \"flags_decoded\": \"%s\", "
	   "\"status\": %u, \"comm\": %s}", r, ERRNO_IF(r <= 0), sizeof(si), si.pbsi_flags,
	   decode(si.pbsi_flags, bsd_flags), si.pbsi_status, jstr(si.pbsi_comm));

	r = pidinfo(PROC_PIDTASKINFO, 0, &ti, sizeof(ti));
	threadnum = (unsigned)ti.pti_threadnum;
	kv("pidinfo_taskinfo", "{\"rc\": %d, \"errno\": %d, \"size\": %zu, \"threadnum\": %d, \"numrunning\": %d, "
	   "\"virtual_size_nonzero\": %s, \"resident_size_nonzero\": %s, \"total_user_nonzero\": %s, "
	   "\"total_system_nonzero\": %s, \"faults_nonzero\": %s, \"policy\": %d, \"priority\": %d}",
	   r, ERRNO_IF(r <= 0), sizeof(ti), ti.pti_threadnum, ti.pti_numrunning, B(ti.pti_virtual_size),
	   B(ti.pti_resident_size), B(ti.pti_total_user), B(ti.pti_total_system), B(ti.pti_faults),
	   ti.pti_policy, ti.pti_priority);

	r = pidinfo(PROC_PIDTASKALLINFO, 0, &tai, sizeof(tai));
	kv("pidinfo_taskallinfo", "{\"rc\": %d, \"errno\": %d, \"size\": %zu, \"threadnum\": %d, \"flags\": \"0x%x\"}",
	   r, ERRNO_IF(r <= 0), sizeof(tai), tai.ptinfo.pti_threadnum, tai.pbsd.pbi_flags);

	r = pidinfo(PROC_PIDREGIONINFO, text, &ri, sizeof(ri));
	kv("pidinfo_regioninfo", "{\"rc\": %d, \"errno\": %d, \"size\": %zu, %s}", r, ERRNO_IF(r <= 0), sizeof(ri), pri_str(&ri));
	r = pidinfo(PROC_PIDREGIONPATHINFO, text, &rp, sizeof(rp));
	kv("pidinfo_regionpathinfo", "{\"rc\": %d, \"errno\": %d, \"size\": %zu, %s, \"path\": %s}",
	   r, ERRNO_IF(r <= 0), sizeof(rp), pri_str(&rp.prp_prinfo), jpath(rp.prp_vip.vip_path));
	r = pidinfo(PROC_PIDREGIONPATHINFO2, text, &rp, sizeof(rp));
	kv("pidinfo_regionpathinfo2", "{\"rc\": %d, \"errno\": %d, \"size\": %zu, %s, \"path\": %s}",
	   r, ERRNO_IF(r <= 0), sizeof(rp), pri_str(&rp.prp_prinfo), jpath(rp.prp_vip.vip_path));
	/* Flavor 3 takes a device: the first region backed by a file on it. */
	memset(&st, 0, sizeof(st));
	CALL(r, stat("/usr/lib/dyld", &st));
	if (r != 0) {
		kv("pidinfo_regionpathinfo3", "{\"skipped\": \"no device to ask for\", \"stat_errno\": %d}", sv_errno);
	} else {
		r = pidinfo(PROC_PIDREGIONPATHINFO3, (uint64_t)st.st_dev, &rp, sizeof(rp));
		kv("pidinfo_regionpathinfo3", "{\"rc\": %d, \"errno\": %d, \"size\": %zu, %s, \"path\": %s}",
		   r, ERRNO_IF(r <= 0), sizeof(rp), pri_str(&rp.prp_prinfo), jpath(rp.prp_vip.vip_path));
	}

	r = pidinfo(PROC_PIDVNODEPATHINFO, 0, &vp, sizeof(vp));
	kv("pidinfo_vnodepathinfo", "{\"rc\": %d, \"errno\": %d, \"size\": %zu, \"cdir\": %s, \"rdir\": %s}",
	   r, ERRNO_IF(r <= 0), sizeof(vp), jpath(vp.pvi_cdir.vip_path), jpath(vp.pvi_rdir.vip_path));

	r = pidinfo(PROC_PIDLISTTHREADS, 0, tids, sizeof(tids));
	kv("pidinfo_listthreads", "{\"rc\": %d, \"errno\": %d, \"count\": %d}", r, ERRNO_IF(r <= 0), r > 0 ? r / 8 : 0);
	for (int i = 0; i < r / 8 && i < 64; i++) {
		struct proc_threadinfo pt;
		int rt = pidinfo(PROC_PIDTHREADINFO, tids[i], &pt, sizeof(pt));

		pt.pth_name[sizeof(pt.pth_name) - 1] = '\0';
		kv(key_n("pidinfo_threadinfo", (unsigned)i), "{\"rc\": %d, \"errno\": %d, \"size\": %zu, \"name\": %s}",
		   rt, ERRNO_IF(rt <= 0), sizeof(pt), jstr(pt.pth_name));
	}

	memset(&ru, 0, sizeof(ru));
	CALL(r, proc_pid_rusage(self, RUSAGE_INFO_V2, (rusage_info_t *)&ru));
	kv("pid_rusage_v2", "{\"rc\": %d, \"errno\": %d, \"phys_footprint_nonzero\": %s, \"resident_size_nonzero\": %s}",
	   r, ERRNO_IF(r), B(ru.ri_phys_footprint), B(ru.ri_resident_size));

	pid_kv("pid_self", self);
	pid_kv("pid_parent", parent);
	pid_kv("pid_1", 1);
	pid_kv("pid_0", 0);

	CALL(r, proc_listpids(PROC_ALL_PIDS, 0, pids, sizeof(pids)));
	for (int i = 0; i < r / (int)sizeof(int); i++) {
		has0 |= pids[i] == 0;
		has1 |= pids[i] == 1;
	}
	kv("listpids_all", "{\"rc\": %d, \"errno\": %d, \"count\": %d, \"has_pid_0\": %s, \"has_pid_1\": %s}",
	   r, ERRNO_IF(r <= 0), r > 0 ? r / (int)sizeof(int) : 0, B(has0), B(has1));

	memset(&kp, 0, sizeof(kp));
	len = sizeof(kp);
	CALL(r, sysctl(mib, 4, &kp, &len, NULL, 0));
	kv("sysctl_kern_proc_pid", "{\"rc\": %d, \"errno\": %d, \"len\": %zu, \"size\": %zu, \"p_flag\": \"0x%x\", "
	   "\"p_flag_decoded\": \"%s\", \"p_stat\": %d, \"p_starttime_nonzero\": %s, \"e_ppid_is_getppid\": %s, "
	   "\"cr_uid\": %u}", r, ERRNO_IF(r), len, sizeof(kp), (unsigned)kp.kp_proc.p_flag,
	   decode((uint32_t)kp.kp_proc.p_flag, p_flags), (int)kp.kp_proc.p_stat,
	   B(kp.kp_proc.p_starttime.tv_sec), B(kp.kp_eproc.e_ppid == parent), (unsigned)kp.kp_eproc.e_ucred.cr_uid);

	/* kern.procargs2: argc, executable path, padding, argv strings, then the
	 * environment and the kernel's own launch strings. Only lengths and counts
	 * are reported, never the strings; the environment is recognised by its
	 * first entry. */
	mib[1] = KERN_ARGMAX;
	len = sizeof(argmax);
	if (sysctl(mib, 2, &argmax, &len, NULL, 0) != 0 || argmax <= 0)
		argmax = 1 << 20;
	{
		char *buf = calloc(1, (size_t)argmax), *p, *end;
		int argc = 0, strings = 0, env_seen = 0;
		const char *first = environ ? environ[0] : NULL;

		mib[1] = KERN_PROCARGS2;
		mib[2] = self;
		len = (size_t)argmax;
		r = -1;
		if (buf)
			CALL(r, sysctl(mib, 3, buf, &len, NULL, 0));
		if (r == 0 && len > sizeof(argc)) {
			memcpy(&argc, buf, sizeof(argc));
			end = buf + len;
			p = buf + sizeof(argc);
			p += strnlen(p, (size_t)(end - p));
			while (p < end && !*p)
				p++;
			for (int i = 0; i < argc && p < end; i++) {
				p += strnlen(p, (size_t)(end - p));
				p += p < end;
			}
			while (p < end && *p) {
				size_t l = strnlen(p, (size_t)(end - p));

				strings++;
				env_seen |= first && strlen(first) == l && !memcmp(p, first, l);
				p += l;
				p += p < end;
			}
		}
		kv("sysctl_kern_procargs2", "{\"rc\": %d, \"errno\": %d, \"argmax\": %d, \"length\": %zu, \"argc\": %d, "
		   "\"environ_found_after_argv\": %s, \"strings_after_argv\": %d}",
		   r, ERRNO_IF(r), argmax, r == 0 ? len : 0, argc, B(env_seen), strings);
		free(buf);
	}

	{
		thread_act_array_t threads = NULL;
		mach_msg_type_number_t count = 0;
		kern_return_t kr = task_threads(mach_task_self(), &threads, &count);

		kv("task_threads", "{\"kr\": %d, \"count\": %u, \"equals_pti_threadnum\": %s}",
		   kr, count, B(kr == KERN_SUCCESS && count == threadnum));
		/* Give the rights back so the port count in the task section is not inflated. */
		for (mach_msg_type_number_t i = 0; kr == KERN_SUCCESS && i < count; i++)
			mach_port_deallocate(mach_task_self(), threads[i]);
		if (kr == KERN_SUCCESS && threads)
			vm_deallocate(mach_task_self(), (vm_address_t)threads, count * sizeof(threads[0]));
	}
}

/* ---- task -------------------------------------------------------------- */

static void sec_task(void)
{
	/* osfmk/mach/task_info.h flavor numbers; 5 is not served on arm64, 18 is its replacement there */
	static const struct {
		const char *key;
		int flavor;
	} tf[] = {{"task_info_01_absolutetime", 1}, {"task_info_02_events", 2}, {"task_info_05_basic_64", 5},
		{"task_info_13_security_token", 13}, {"task_info_15_audit_token", 15}, {"task_info_17_dyld", 17},
		{"task_info_18_basic_64_2", 18}, {"task_info_19_extmod", 19}, {"task_info_20_mach_basic", 20},
		{"task_info_21_power", 21}, {"task_info_22_vm", 22}, {"task_info_28_flags", 28}},
	  thf[] = {{"thread_info_3_basic", 3}, {"thread_info_4_identifier", 4}, {"thread_info_5_extended", 5}};
	static uint32_t w[1024]; /* TASK_INFO_MAX words */
	exception_mask_t masks[32];
	mach_port_t ports[32];
	exception_behavior_t behaviors[32];
	thread_state_flavor_t flavors[32];
	mach_port_name_array_t names = NULL;
	mach_port_type_array_t types = NULL;
	mach_msg_type_number_t count, ncount = 0, tcount = 0;
	thread_t thread = pthread_mach_thread_np(pthread_self());
	kern_return_t kr;
	char extra[420]; /* a 63-byte name with every byte escaped is 378 characters */
	int pid = -1, nonnull = 0;

	kr = mach_port_names(mach_task_self(), &names, &ncount, &types, &tcount);
	kv("mach_port_names", "{\"kr\": %d, \"count\": %u}", kr, ncount);
	if (kr == KERN_SUCCESS) {
		vm_deallocate(mach_task_self(), (vm_address_t)names, ncount * sizeof(names[0]));
		vm_deallocate(mach_task_self(), (vm_address_t)types, tcount * sizeof(types[0]));
	}

	for (unsigned i = 0; i < sizeof(tf) / sizeof(tf[0]); i++) {
		memset(w, 0, sizeof(w));
		count = 1024;
		extra[0] = '\0';
		kr = task_info(mach_task_self(), (task_flavor_t)tf[i].flavor, (task_info_t)w, &count);
		if (kr == KERN_SUCCESS && tf[i].flavor == 15) /* bsm/audit.h: word 5 of audit_token_t is the pid */
			snprintf(extra, sizeof(extra), ", \"pid_word_is_getpid\": %s", B(count >= 8 && (pid_t)w[5] == getpid()));
		if (kr == KERN_SUCCESS && tf[i].flavor == 17)
			snprintf(extra, sizeof(extra), ", \"all_image_info_format\": %u", w[4]);
		if (kr == KERN_SUCCESS && tf[i].flavor == 20)
			snprintf(extra, sizeof(extra), ", \"virtual_size_nonzero\": %s, \"resident_size_nonzero\": %s, "
				 "\"suspend_count\": %u", B(u64_at(w, 0)), B(u64_at(w, 2)), w[11]);
		if (kr == KERN_SUCCESS && tf[i].flavor == 22) /* struct task_vm_info, pack(4): 64-bit fields at these words */
			snprintf(extra, sizeof(extra), ", \"page_size\": %u, \"internal_nonzero\": %s, "
				 "\"phys_footprint_nonzero\": %s, \"min_address\": \"0x%llx\", \"max_address\": \"0x%llx\"",
				 w[3], B(u64_at(w, 12)), count >= 38 ? B(u64_at(w, 36)) : "null",
				 count >= 42 ? (unsigned long long)u64_at(w, 38) : 0ULL,
				 count >= 42 ? (unsigned long long)u64_at(w, 40) : 0ULL);
		if (kr == KERN_SUCCESS && tf[i].flavor == 28)
			snprintf(extra, sizeof(extra), ", \"flags\": \"0x%x\"", w[0]);
		kv(tf[i].key, "{\"kr\": %d, \"count_out\": %u, \"nonzero\": \"%s\"%s}",
		   kr, count, nonzero(w, kr == KERN_SUCCESS ? count : 0), extra);
	}

	for (unsigned i = 0; i < sizeof(thf) / sizeof(thf[0]); i++) {
		memset(w, 0, sizeof(w));
		count = 32; /* THREAD_INFO_MAX */
		extra[0] = '\0';
		kr = thread_info(thread, (thread_flavor_t)thf[i].flavor, (thread_info_t)w, &count);
		if (kr == KERN_SUCCESS && thf[i].flavor == 5) {
			/* struct thread_extended_info: pth_name[64] starts at byte 48 */
			char name[65];

			memcpy(name, (const char *)w + 48, 64);
			name[64] = '\0';
			snprintf(extra, sizeof(extra), ", \"pth_name\": %s", jstr(name));
		}
		kv(thf[i].key, "{\"kr\": %d, \"count_out\": %u, \"nonzero\": \"%s\"%s}",
		   kr, count, nonzero(w, kr == KERN_SUCCESS ? count : 0), extra);
	}

	memset(ports, 0, sizeof(ports));
	count = 32;
	kr = task_get_exception_ports(mach_task_self(), EXC_MASK_ALL, masks, &count, ports, behaviors, flavors);
	for (mach_msg_type_number_t i = 0; kr == KERN_SUCCESS && i < count && i < 32; i++)
		nonnull += ports[i] != MACH_PORT_NULL;
	/* EXC_MASK_ALL comes from the SDK the probe was built with, so it is printed. */
	kv("task_get_exception_ports", "{\"kr\": %d, \"mask\": \"0x%x\", \"count\": %u, \"non_null_ports\": %d}",
	   kr, (unsigned)EXC_MASK_ALL, kr == KERN_SUCCESS ? count : 0, nonnull);

	kr = pid_for_task(mach_task_self(), &pid);
	kv("pid_for_task", "{\"kr\": %d, \"is_getpid\": %s}", kr, B(kr == KERN_SUCCESS && pid == getpid()));
	kv("ppid", "%d", (int)getppid());
	kv("parent_is_pid_1", "%s", B(getppid() == 1));
}

/* ---- fs ---------------------------------------------------------------- */

static void sec_fs(void)
{
	static const char *const paths[] = {"/System/Library/CoreServices/SystemVersion.plist",
		"/System/Library/Extensions", "/System/Library/Kernels", "/System/Library/dyld",
		"/System/Volumes/Preboot", "/System/Volumes/Data", "/System/Cryptexes", "/System/Applications",
		"/usr/lib/dyld", "/usr/libexec/amfid", "/private/var/db/dyld", "/proc", "/proc/version",
		"/proc/self/maps", "/sys", "/Volumes/SystemRoot", "/dev/null", "/dev/urandom", "/dev/shm", "/run",
		"/usr/libexec/darling", "/usr/lib/darling", "/usr/lib/native", NULL};
	struct statfs sf;
	struct passwd *pw;
	int r;

	for (int i = 0; paths[i]; i++)
		stat_kv("lstat", paths[i]);

	memset(&sf, 0, sizeof(sf));
	CALL(r, statfs("/", &sf));
	kv("statfs /", "{\"rc\": %d, \"errno\": %d, \"fstypename\": %s, \"mntfromname\": %s}",
	   r, ERRNO_IF(r), jstr(sf.f_fstypename), jpath(sf.f_mntfromname));

	/* Only the numeric ids and the shell: the account name and the full
	 * name stay out of the output. */
	CALL(pw, getpwuid(getuid()));
	if (pw)
		kv("getpwuid", "{\"found\": true, \"uid\": %u, \"gid\": %u, \"shell\": %s, \"gecos\": \"%s\"}",
		   (unsigned)pw->pw_uid, (unsigned)pw->pw_gid, jpath(pw->pw_shell),
		   pw->pw_gecos && pw->pw_gecos[0] ? "present" : "empty");
	else
		kv("getpwuid", "{\"found\": false, \"errno\": %d}", sv_errno);
	kv("uid_gid", "{\"uid\": %u, \"euid\": %u, \"gid\": %u, \"egid\": %u}",
	   (unsigned)getuid(), (unsigned)geteuid(), (unsigned)getgid(), (unsigned)getegid());
}

/* ---- raw --------------------------------------------------------------- */

/* getpid through the trap instruction itself, not through libsystem_kernel. */
static long raw_getpid(void)
{
#if defined(__x86_64__)
	long ret = 0x2000014; /* SYSCALL_CLASS_UNIX << 24 | SYS_getpid, osfmk/mach/i386/syscall_sw.h */

	__asm__ volatile("syscall" : "+a"(ret) : : "rcx", "rdx", "r11", "memory", "cc");
	return ret;
#elif defined(__arm64__) || defined(__aarch64__)
	register long x16 __asm__("x16") = 20; /* SYS_getpid, bsd/kern/syscalls.master */
	register long x0 __asm__("x0") = 0;

	__asm__ volatile("svc #0x80" : "+r"(x0), "+r"(x16) : : "x1", "x17", "memory", "cc");
	return x0;
#else
	return -1;
#endif
}

static void sec_raw(void)
{
	struct utsname u;
	long raw = raw_getpid();
	int translated = -1, r;
	size_t len = sizeof(translated);

	if (raw == (long)getpid())
		kv("syscall_getpid", "{\"matches_getpid\": true}");
	else
		kv("syscall_getpid", "{\"matches_getpid\": false, \"raw\": %ld}", raw);

	CALL(r, sysctlbyname("sysctl.proc_translated", &translated, &len, NULL, 0));
	kv("sysctl_proc_translated", "{\"rc\": %d, \"errno\": %d, \"value\": %d}", r, ERRNO_IF(r), r ? -1 : translated);

	memset(&u, 0, sizeof(u));
	CALL(r, uname(&u));
	kv("uname", "{\"rc\": %d, \"errno\": %d, \"sysname\": %s, \"release\": %s, \"machine\": %s}",
	   r, ERRNO_IF(r), jstr(u.sysname), jstr(u.release), jstr(u.machine));
}

/* ---- env --------------------------------------------------------------- */

static int name_cmp(const void *a, const void *b)
{
	return strcmp((const char *)a, (const char *)b);
}

static void sec_env(void)
{
	static const char *const prefixes[] = {"DYLD_", "DARLING", "RCLOL", "LD_", "XDG_", "DBUS_", NULL};
	static const char *const exact[] = {"DISPLAY", "WAYLAND_DISPLAY", "EGL_PLATFORM", NULL};
	static char names[64][128], list[64 * 140];
	unsigned total = 0, kept = 0, dropped = 0;
	size_t at = 0;

	/* Names only, never values: nothing after the '=' is copied. */
	for (char **e = environ; e && *e; e++, total++) {
		size_t n = strcspn(*e, "=");
		int wanted = 0;

		for (int i = 0; prefixes[i]; i++)
			wanted |= !strncmp(*e, prefixes[i], strlen(prefixes[i]));
		for (int i = 0; exact[i]; i++)
			wanted |= n == strlen(exact[i]) && !strncmp(*e, exact[i], n);
		if (!wanted)
			continue;
		if (kept == 64 || n >= sizeof(names[0])) {
			dropped++;
			continue;
		}
		memcpy(names[kept], *e, n);
		names[kept++][n] = '\0';
	}
	qsort(names, kept, sizeof(names[0]), name_cmp);
	list[0] = '\0';
	for (unsigned i = 0; i < kept; i++)
		if (at < sizeof(list) - 800)
			at += (size_t)snprintf(list + at, sizeof(list) - at, "%s%s", i ? ", " : "", jstr(names[i]));
		else
			dropped++;
	kv("names", "[%s]", list);
	kv("names_dropped", "%u", dropped);
	kv("total", "%u", total);
}

/* ---- meta -------------------------------------------------------------- */

static void sec_meta(void)
{
#if defined(__x86_64__)
	const char *arch = "x86_64";
#elif defined(__arm64__) || defined(__aarch64__)
	const char *arch = "arm64";
#else
	const char *arch = "unknown";
#endif
	long sdk = 0, target = 0;

#ifdef __MAC_OS_X_VERSION_MAX_ALLOWED
	sdk = __MAC_OS_X_VERSION_MAX_ALLOWED;
#endif
#ifdef __MAC_OS_X_VERSION_MIN_REQUIRED
	target = __MAC_OS_X_VERSION_MIN_REQUIRED;
#endif
	kv("probe", "\"self_view\"");
	kv("schema", "1");
	kv("arch", "\"%s\"", arch);
	kv("built_with_sdk", "%ld", sdk);
	kv("built_for_target", "%ld", target);
	kv("page_size", "%d", getpagesize());
}

int main(void)
{
	load_images();

	printf("{\n");
	sec_dyld();
	section("dyld", 0);
	sec_env();
	section("env", 0);
	sec_fs();
	section("fs", 0);
	sec_meta();
	section("meta", 0);
	sec_proc();
	section("proc", 0);
	sec_raw();
	section("raw", 0);
	sec_regions();
	section("regions", 0);
	sec_task();
	section("task", 1);
	printf("}\n");
	/* a short capture must not pass for a complete one */
	return fflush(stdout) != 0 || ferror(stdout) ? 1 : 0;
}
