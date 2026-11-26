"""proc_pidinfo() compatibility tests.

Covers the proc_info.c fixes for macOS parity:
- PROC_PIDUNIQIDENTIFIERINFO parses Mach-O LC_UUID (thin, universal x86_64
  slices, and load commands extending beyond the initial 8192-byte buffer),
  handles ppid == 0 cleanly, and returns sizeof(struct proc_uniqidentifierinfo).
- PROC_PIDTHREADINFO returns sizeof(struct proc_threadinfo) instead of 1.
- PROC_PIDREGIONPATHINFO / PROC_PIDREGIONPATH zero their structs, match XNU's
  containing-or-next address-walk contract (including holes), unexpand smaps
  file paths through vchroot_unexpand (leaving non-file pseudo mappings empty),
  and return the struct size.
- PROC_PIDTASKINFO converts Linux RSS pages to bytes.
- PROC_PIDLISTFDS is dispatched, handles buffer == NULL sizing and partial
  buffers, skips the inspection dirfd on self, and classifies FD types.
"""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_runtime_compatibility as sources

ROOT = Path(__file__).resolve().parents[1]
PROC_INFO = 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl/misc/proc_info.c'


def section():
    patch = (ROOT / 'patches/xnu.patch').read_text()
    marker = f'diff --git a/{PROC_INFO} b/{PROC_INFO}\n'
    return patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def added():
    return '\n'.join(line[1:] for line in section().splitlines()
                     if line.startswith('+') and not line.startswith('+++')) + '\n'


def extract_fn(text, start_sig, end_marker='\n}\n'):
    start = text.index(start_sig)
    end = text.index(end_marker, start) + len(end_marker)
    return text[start:end]


def reconstruct_proc_info():
    """Apply the proc_info.c section of patches/xnu.patch onto upstream base when available."""
    xnu_repo = ROOT / 'build/darling-src/src/external/xnu'
    if not (xnu_repo / '.git').exists():
        return None
    manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
    comp = next(c for c in manifest['components'] if c['path'] == 'src/external/xnu')
    rev = comp['revision']
    base = subprocess.run(
        ['git', '-C', str(xnu_repo), 'show', f'{rev}:{PROC_INFO}'],
        capture_output=True, text=True, check=True,
    ).stdout
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'proc_info.c'
        diff = Path(tmp) / 'proc_info.patch'
        src.write_text(base)
        diff.write_text(f'diff --git a/{PROC_INFO} b/{PROC_INFO}\n' + section() + '\n')
        subprocess.run(
            ['patch', '--quiet', '-f', str(src), str(diff)],
            check=True, capture_output=True, text=True,
        )
        return src.read_text()


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class ProcInfoCompatTests(unittest.TestCase):
    run_c = sources.CompatibilityChecks.run_c

    def test_each_handler_returns_its_own_struct_size_and_dispatches_listfds(self):
        sec = section()
        text = added()
        self.assertIn('case PROC_PIDLISTFDS:', text)
        self.assertIn('return _proc_pidinfo_listfds(pid, buffer, bufsize);', text)
        self.assertIn('ti->pti_resident_size = rss * 4096ULL;', text)
        self.assertNotIn('static char fakeuuid[16];', text)

        # Verify each modified handler's own hunk returns its struct size (not 1).
        hunks_body = sec.split('@@ ', 1)[1]
        uniq_sec = hunks_body.split('static long _proc_pidonfo_uniqinfo(', 1)[1].split('_proc_pidinfo_pidthreadinfo', 1)[0]
        uniq_added = '\n'.join(l[1:] for l in uniq_sec.splitlines() if l.startswith('+'))
        self.assertIn('if (ppid > 0) {', uniq_added)
        self.assertIn('return sizeof(*info);', uniq_added)
        self.assertNotIn('return 1;', uniq_added)

        pidthread_sec = hunks_body.split('_proc_pidinfo_pidthreadinfo', 1)[1].split('static void unexpand_smaps_path(', 1)[0]
        pidthread_added = '\n'.join(l[1:] for l in pidthread_sec.splitlines() if l.startswith('+'))
        self.assertIn('return sizeof(*info);', pidthread_added)
        self.assertNotIn('return 1;', pidthread_added)

        rpi_tail = hunks_body.rsplit('static long _proc_pidinfo_regionpathinfo(', 1)[1]
        rpi_sec, rp_rest = rpi_tail.split('static long _proc_pidinfo_regionpath(', 1)
        rpi_added = '\n'.join(l[1:] for l in rpi_sec.splitlines() if l.startswith('+'))
        self.assertIn('memset(&my_rpi, 0, sizeof(my_rpi));', rpi_added)
        self.assertIn('(my_rpi.prp_prinfo.pri_address + my_rpi.prp_prinfo.pri_size) > arg', rpi_added)
        self.assertIn('unexpand_smaps_path(my_rpi.prp_vip.vip_path, sizeof(my_rpi.prp_vip.vip_path));', rpi_added)
        self.assertIn('return sizeof(my_rpi);', rpi_added)

        rp_sec = rp_rest.split('parse_smaps_firstline', 1)[0]
        rp_added = '\n'.join(l[1:] for l in rp_sec.splitlines() if l.startswith('+'))
        self.assertIn('memset(&my_rp, 0, sizeof(my_rp));', rp_added)
        self.assertIn('(my_rp.prpo_addr + my_rp.prpo_regionlength) > arg', rp_added)
        self.assertIn('unexpand_smaps_path(my_rp.prpo_path, sizeof(my_rp.prpo_path));', rp_added)
        self.assertIn('return sizeof(my_rp);', rp_added)

    def test_parse_macho_uuid_thin_universal_and_beyond_8192(self):
        text = added()
        bswap = extract_fn(text, 'static uint32_t proc_info_bswap32(')
        parser = extract_fn(text, 'static bool parse_macho_uuid_bytes(')
        self.run_c(r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define PROC_INFO_MAX_MACHO_CMDS (128u * 1024u)

''' + bswap + '\n' + parser + r'''

static void put32le(uint8_t* p, uint32_t v)
{
	memcpy(p, &v, 4);
}

static void put32be(uint8_t* p, uint32_t v)
{
	p[0] = (uint8_t) (v >> 24);
	p[1] = (uint8_t) (v >> 16);
	p[2] = (uint8_t) (v >> 8);
	p[3] = (uint8_t) v;
}

int main(void)
{
	uint8_t buf[256];
	uint8_t uuid[16];
	uint64_t slice_off = 999;
	uint32_t needed = 999;

	/* Thin 64-bit Mach-O with one dummy command followed by LC_UUID. */
	memset(buf, 0, sizeof(buf));
	put32le(buf + 0, 0xfeedfacfu); /* MH_MAGIC_64 */
	put32le(buf + 16, 2);          /* ncmds */
	put32le(buf + 20, 40);         /* sizeofcmds */
	put32le(buf + 32, 0x19);       /* LC_SEGMENT_64 */
	put32le(buf + 36, 16);
	put32le(buf + 48, 0x1b);       /* LC_UUID */
	put32le(buf + 52, 24);
	for (int i = 0; i < 16; i++)
		buf[56 + i] = (uint8_t) (0xa0 + i);

	memset(uuid, 0, sizeof(uuid));
	assert(parse_macho_uuid_bytes(buf, sizeof(buf), uuid, &slice_off, &needed));
	assert(slice_off == 0);
	assert(needed == 0);
	for (int i = 0; i < 16; i++)
		assert(uuid[i] == (uint8_t) (0xa0 + i));

	/* LC_UUID placed past offset 8192 (e.g. large LC_SEGMENT_64 first). */
	uint8_t* big = (uint8_t*) calloc(1, 12288);
	assert(big != NULL);
	put32le(big + 0, 0xfeedfacfu);
	put32le(big + 16, 2);
	put32le(big + 20, 8256);       /* 8232 + 24 */
	put32le(big + 32, 0x19);       /* large LC_SEGMENT_64 */
	put32le(big + 36, 8232);
	put32le(big + 8264, 0x1b);     /* LC_UUID at offset 8264 (> 8192) */
	put32le(big + 8268, 24);
	for (int i = 0; i < 16; i++)
		big[8272 + i] = (uint8_t) (0x30 + i);

	/* Truncated 8192-byte initial read reports needed_bytes = 32 + 8256 = 8288. */
	memset(uuid, 0, sizeof(uuid));
	assert(!parse_macho_uuid_bytes(big, 8192, uuid, &slice_off, &needed));
	assert(needed == 8288u);

	/* Full read of needed_bytes extracts the UUID cleanly. */
	assert(parse_macho_uuid_bytes(big, needed, uuid, &slice_off, &needed));
	assert(needed == 0);
	for (int i = 0; i < 16; i++)
		assert(uuid[i] == (uint8_t) (0x30 + i));
	free(big);

	/* Universal (FAT) header: arm64 first, x86_64 second at offset 0x4000. */
	memset(buf, 0, sizeof(buf));
	put32be(buf + 0, 0xcafebabeu);
	put32be(buf + 4, 2);
	put32be(buf + 8, 0x0100000cu);
	put32be(buf + 16, 0x1000u);
	put32be(buf + 28, 0x01000007u);
	put32be(buf + 36, 0x4000u);

	memset(uuid, 0, sizeof(uuid));
	assert(!parse_macho_uuid_bytes(buf, sizeof(buf), uuid, &slice_off, &needed));
	assert(slice_off == 0x4000u);
	assert(needed == 0);

	/* Corrupt / truncated buffer returns false cleanly. */
	assert(!parse_macho_uuid_bytes(buf, 16, uuid, &slice_off, &needed));
	assert(slice_off == 0);
	assert(needed == 0);
	return 0;
}
''')

    def test_unexpand_smaps_path_and_listfds_sizing(self):
        text = added()
        unexpand_fn = extract_fn(text, 'static void unexpand_smaps_path(')
        fdtype_fn = extract_fn(text, 'static uint32_t proc_fdtype_from_link(')
        listfds_fn = extract_fn(text, 'static long _proc_pidinfo_listfds(int32_t pid, void* buffer, int32_t bufsize)\n{')
        self.run_c(r'''
#include <assert.h>
#include <ctype.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define BSD_O_RDONLY 0
#define BSD_O_DIRECTORY 0x100000
#define PROX_FDTYPE_VNODE 1
#define PROX_FDTYPE_SOCKET 2
#define PROX_FDTYPE_KQUEUE 5
#define PROX_FDTYPE_PIPE 6
#define __NR_getpid 39
#define LINUX_SYSCALL(nr) (100)
#define __simple_sprintf sprintf

struct proc_fdinfo { int32_t proc_fd; uint32_t proc_fdtype; };
struct bsd_dirent { uint32_t d_ino; uint16_t d_reclen; uint8_t d_type; uint8_t d_namlen; char d_name[256]; };
struct vchroot_unexpand_args { char path[4096]; };

static long long __simple_atoi(const char* s, const char** end) {
	return strtoll(s, (char**) end, 10);
}

static int vchroot_unexpand(struct vchroot_unexpand_args* args)
{
	const char* prefix = "/mnt/kingston/darling-test";
	size_t plen = strlen(prefix);
	if (strncmp(args->path, prefix, plen) == 0 && (args->path[plen] == '/' || args->path[plen] == '\0')) {
		memmove(args->path, args->path + plen, strlen(args->path + plen) + 1);
		if (args->path[0] == '\0') strcpy(args->path, "/");
	}
	return 0;
}

static int listfds_step = 0;
static int sys_open_nocancel(const char* path, int flags, int mode)
{
	(void) flags; (void) mode;
	if (strcmp(path, "/proc/100/fd") == 0) { listfds_step = 0; return 99; }
	return -ENOENT;
}

static int close_internal(int fd) { (void) fd; return 0; }

static int sys_getdirentries(int fd, char* buf, size_t nbytes, long* basep)
{
	static const char* names[] = { ".", "..", "0", "3", "4", "99" };
	(void) fd; (void) basep;
	if (listfds_step > 0) return 0;
	listfds_step = 1;
	int pos = 0;
	for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
		struct bsd_dirent* d = (struct bsd_dirent*) (buf + pos);
		memset(d, 0, sizeof(*d));
		strcpy(d->d_name, names[i]);
		d->d_namlen = (uint8_t) strlen(names[i]);
		d->d_reclen = (uint16_t) ((offsetof(struct bsd_dirent, d_name) + d->d_namlen + 8) & ~7u);
		assert((size_t) (pos + d->d_reclen) <= nbytes);
		pos += d->d_reclen;
	}
	return pos;
}

static long sys_readlink(const char* path, char* out, size_t cap)
{
	const char* target = "/dev/null";
	if (strcmp(path, "/proc/100/fd/3") == 0) target = "socket:[12345]";
	else if (strcmp(path, "/proc/100/fd/4") == 0) target = "pipe:[67890]";
	size_t n = strlen(target);
	if (n >= cap) n = cap - 1;
	memcpy(out, target, n);
	return (long) n;
}

''' + unexpand_fn + '\n' + fdtype_fn + '\n' + listfds_fn + r'''

int main(void)
{
	char buf[256];
	strcpy(buf, "/mnt/kingston/darling-test/Applications/App");
	unexpand_smaps_path(buf, sizeof(buf));
	assert(strcmp(buf, "/Applications/App") == 0);

	strcpy(buf, "[heap]");
	unexpand_smaps_path(buf, sizeof(buf));
	assert(buf[0] == '\0');

	/* NULL buffer returns (3 + 8) * sizeof(proc_fdinfo), skipping self dirfd 99. */
	assert(_proc_pidinfo_listfds(100, NULL, 0) == (3 + 8) * (long) sizeof(struct proc_fdinfo));

	/* Partial buffer fills 2 entries without overrun. */
	struct proc_fdinfo fds[4];
	memset(fds, 0x7f, sizeof(fds));
	assert(_proc_pidinfo_listfds(100, fds, 2 * (int32_t) sizeof(struct proc_fdinfo)) == 2 * (long) sizeof(struct proc_fdinfo));
	assert(fds[0].proc_fd == 0 && fds[0].proc_fdtype == PROX_FDTYPE_VNODE);
	assert(fds[1].proc_fd == 3 && fds[1].proc_fdtype == PROX_FDTYPE_SOCKET);
	assert(fds[2].proc_fd == 0x7f7f7f7f);

	/* Full buffer fills all 3 real FDs and skips self dirfd 99. */
	assert(_proc_pidinfo_listfds(100, fds, (int32_t) sizeof(fds)) == 3 * (long) sizeof(struct proc_fdinfo));
	assert(fds[2].proc_fd == 4 && fds[2].proc_fdtype == PROX_FDTYPE_PIPE);

	/* Unknown pid returns -ESRCH. */
	assert(_proc_pidinfo_listfds(999, fds, (int32_t) sizeof(fds)) == -ESRCH);
	return 0;
}
''')

    def test_reconstructed_uniqinfo_ppid_zero_and_region_walk_over_hole(self):
        full = reconstruct_proc_info()
        if full is None:
            self.skipTest('build/darling-src/src/external/xnu not present')
        uniq_fn = extract_fn(full, 'static long _proc_pidonfo_uniqinfo(int32_t pid, void* buffer, int32_t bufsize)\n{')
        unexpand_fn = extract_fn(full, 'static void unexpand_smaps_path(')
        rpi_fn = extract_fn(full, 'static long _proc_pidinfo_regionpathinfo(int32_t pid, uint64_t arg, void* buffer, int32_t bufsize)\n{')

        self.run_c(r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAXPATHLEN 1024
#define __simple_sprintf sprintf

struct proc_uniqidentifierinfo {
	uint8_t p_uuid[16];
	uint64_t p_uniqueid;
	uint64_t p_puniqueid;
	int32_t p_idversion;
	uint32_t p_reserve2;
	uint64_t p_reserve3;
	uint64_t p_reserve4;
};

struct proc_regioninfo {
	uint32_t pri_protection;
	uint32_t pri_max_protection;
	uint32_t pri_inheritance;
	uint32_t pri_flags;
	uint64_t pri_offset;
	uint32_t pri_behavior;
	uint32_t pri_user_wired_count;
	uint32_t pri_user_tag;
	uint32_t pri_pages_resident;
	uint32_t pri_pages_shared_now_private;
	uint32_t pri_pages_swapped_out;
	uint32_t pri_pages_dirtied;
	uint32_t pri_ref_count;
	uint32_t pri_shadow_depth;
	uint32_t pri_share_mode;
	uint32_t pri_private_pages_resident;
	uint32_t pri_shared_pages_resident;
	uint32_t pri_obj_id;
	uint32_t pri_depth;
	uint64_t pri_address;
	uint64_t pri_size;
};

struct vinfo_stat { uint32_t vst_dev; uint64_t vst_ino; };
struct vnode_info { struct vinfo_stat vi_stat; };
struct vnode_info_path { struct vnode_info vip_vi; char vip_path[MAXPATHLEN]; };
struct proc_regionwithpathinfo { struct proc_regioninfo prp_prinfo; struct vnode_info_path prp_vip; };
struct rdline_buffer { int idx; };
struct vchroot_unexpand_args { char path[4096]; };

static long long __simple_atoi(const char* s, const char** end) {
	return strtoll(s, (char**) end, 10);
}

static void skip_stat_elems(char** p, int n)
{
	while (n-- > 0 && **p) {
		while (**p && **p != ' ') (*p)++;
		while (**p == ' ') (*p)++;
	}
}

static const char* next_stat_elem(char** p)
{
	if (!**p) return NULL;
	const char* start = *p;
	while (**p && **p != ' ') (*p)++;
	if (**p == ' ') { **p = '\0'; (*p)++; }
	while (**p == ' ') (*p)++;
	return start;
}

static bool read_string(const char* path, char* out, size_t cap)
{
	if (strcmp(path, "/proc/1/stat") == 0) {
		snprintf(out, cap, "1 (launchd) S 0 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 42");
		return true;
	}
	return false;
}

static void read_process_executable_uuid(int32_t pid, uint8_t out_uuid[16])
{
	(void) pid;
	for (int i = 0; i < 16; i++) out_uuid[i] = (uint8_t) (0x50 + i);
}

static int vchroot_unexpand(struct vchroot_unexpand_args* args)
{
	const char* prefix = "/mnt/kingston/darling-test";
	size_t plen = strlen(prefix);
	if (strncmp(args->path, prefix, plen) == 0 && (args->path[plen] == '/' || args->path[plen] == '\0')) {
		memmove(args->path, args->path + plen, strlen(args->path + plen) + 1);
		if (args->path[0] == '\0') strcpy(args->path, "/");
	}
	return 0;
}

static const char* fake_smaps[] = {
	"1000-2000 r-xp 00000000 08:01 10 /mnt/kingston/darling-test/Applications/App",
	"5000-7000 rw-p 00000000 00:00 0 [heap]",
	"9000-b000 r--p 00001000 08:01 11 /mnt/kingston/darling-test/usr/lib/libFoo.dylib",
	NULL
};

static long _proc_pidinfo_regionpath_setup(int32_t pid, struct rdline_buffer* buf)
{
	(void) pid;
	buf->idx = 0;
	return 7;
}

static const char* _readline(int fd, struct rdline_buffer* buf)
{
	(void) fd;
	const char* line = fake_smaps[buf->idx];
	if (line) buf->idx++;
	return line;
}

static bool parse_smaps_firstline(const char* line, uint64_t* addr, uint64_t* sz,
	uint32_t* prot, uint32_t* share, uint64_t* off, uint32_t* dev, uint64_t* ino, char* path)
{
	unsigned long long a = 0, b = 0;
	char pbuf[256] = {0};
	if (sscanf(line, "%llx-%llx %*s %*x %*x:%*x %*u %255[^\n]", &a, &b, pbuf) < 2)
		return false;
	*addr = a; *sz = b - a;
	if (prot) *prot = 1;
	if (share) *share = 1;
	if (off) *off = 0;
	if (dev) *dev = 0;
	if (ino) *ino = 0;
	if (path) { strncpy(path, pbuf, MAXPATHLEN - 1); path[MAXPATHLEN - 1] = '\0'; }
	return true;
}

static int close_internal(int fd) { (void) fd; return 0; }

''' + uniq_fn + '\n' + unexpand_fn + '\n' + rpi_fn + r'''

int main(void)
{
	/* PROC_PIDUNIQIDENTIFIERINFO with ppid == 0 succeeds and returns sizeof(*info). */
	struct proc_uniqidentifierinfo uinfo;
	assert(_proc_pidonfo_uniqinfo(1, &uinfo, (int32_t) sizeof(uinfo)) == (long) sizeof(uinfo));
	assert(uinfo.p_uuid[0] == 0x50 && uinfo.p_uuid[15] == 0x5f);
	assert(uinfo.p_uniqueid == ((42ULL << 16) | 1ULL));
	assert(uinfo.p_puniqueid == 0ULL);

	/* PROC_PIDREGIONPATHINFO: exact hit, hole -> next region, past end -> -ESRCH. */
	struct proc_regionwithpathinfo rpi;
	assert(_proc_pidinfo_regionpathinfo(100, 0x1500, &rpi, (int32_t) sizeof(rpi)) == (long) sizeof(rpi));
	assert(rpi.prp_prinfo.pri_address == 0x1000 && rpi.prp_prinfo.pri_size == 0x1000);
	assert(strcmp(rpi.prp_vip.vip_path, "/Applications/App") == 0);

	assert(_proc_pidinfo_regionpathinfo(100, 0x3000, &rpi, (int32_t) sizeof(rpi)) == (long) sizeof(rpi));
	assert(rpi.prp_prinfo.pri_address == 0x5000 && rpi.prp_prinfo.pri_size == 0x2000);
	assert(rpi.prp_vip.vip_path[0] == '\0');

	assert(_proc_pidinfo_regionpathinfo(100, 0x7000, &rpi, (int32_t) sizeof(rpi)) == (long) sizeof(rpi));
	assert(rpi.prp_prinfo.pri_address == 0x9000);
	assert(strcmp(rpi.prp_vip.vip_path, "/usr/lib/libFoo.dylib") == 0);

	assert(_proc_pidinfo_regionpathinfo(100, 0xb000, &rpi, (int32_t) sizeof(rpi)) == -ESRCH);
	return 0;
}
''')


if __name__ == '__main__':
    unittest.main()
