"""proc_pidinfo() compatibility tests.

Covers the proc_info.c fixes for macOS parity:
- PROC_PIDUNIQIDENTIFIERINFO parses Mach-O LC_UUID (thin and universal x86_64
  slices) and returns sizeof(struct proc_uniqidentifierinfo) instead of 1.
- PROC_PIDTHREADINFO returns sizeof(struct proc_threadinfo) instead of 1.
- PROC_PIDREGIONPATHINFO / PROC_PIDREGIONPATH zero their structs, match XNU's
  containing-or-next address-walk contract, unexpand smaps file paths through
  vchroot_unexpand (leaving non-file pseudo mappings empty), and return the
  struct size.
- PROC_PIDTASKINFO converts Linux RSS pages to bytes.
- PROC_PIDLISTFDS is dispatched and classifies open file descriptors.
"""
from pathlib import Path
import shutil
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
                     if line.startswith('+') and not line.startswith('+++'))


def extract_fn(text, start_sig, end_marker='\n}\n'):
    start = text.index(start_sig)
    end = text.index(end_marker, start) + len(end_marker)
    return text[start:end]


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class ProcInfoCompatTests(unittest.TestCase):
    run_c = sources.CompatibilityChecks.run_c

    def test_patch_dispatches_listfds_and_returns_struct_sizes(self):
        text = added()
        self.assertIn('case PROC_PIDLISTFDS:', text)
        self.assertIn('return _proc_pidinfo_listfds(pid, buffer, bufsize);', text)
        self.assertIn('return sizeof(*info);', text)
        self.assertIn('return sizeof(my_rpi);', text)
        self.assertIn('return sizeof(my_rp);', text)
        self.assertIn('unexpand_smaps_path(my_rpi.prp_vip.vip_path, sizeof(my_rpi.prp_vip.vip_path));', text)
        self.assertIn('unexpand_smaps_path(my_rp.prpo_path, sizeof(my_rp.prpo_path));', text)
        self.assertIn('ti->pti_resident_size = rss * 4096ULL;', text)
        self.assertNotIn('static char fakeuuid[16];', text)

    def test_parse_macho_uuid_thin_and_universal(self):
        text = added()
        bswap = extract_fn(text, 'static uint32_t proc_info_bswap32(')
        parser = extract_fn(text, 'static bool parse_macho_uuid_bytes(')
        self.run_c(r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

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
	assert(parse_macho_uuid_bytes(buf, sizeof(buf), uuid, &slice_off));
	assert(slice_off == 0);
	for (int i = 0; i < 16; i++)
		assert(uuid[i] == (uint8_t) (0xa0 + i));

	/* Universal (FAT) header: arm64 first, x86_64 second at offset 0x4000. */
	memset(buf, 0, sizeof(buf));
	put32be(buf + 0, 0xcafebabeu);
	put32be(buf + 4, 2);
	/* arch 0: arm64 */
	put32be(buf + 8, 0x0100000cu);
	put32be(buf + 16, 0x1000u);
	/* arch 1: x86_64 */
	put32be(buf + 28, 0x01000007u);
	put32be(buf + 36, 0x4000u);

	memset(uuid, 0, sizeof(uuid));
	assert(!parse_macho_uuid_bytes(buf, sizeof(buf), uuid, &slice_off));
	assert(slice_off == 0x4000u);

	/* Corrupt / truncated buffer returns false cleanly. */
	assert(!parse_macho_uuid_bytes(buf, 16, uuid, &slice_off));
	assert(slice_off == 0);
	return 0;
}
''')

    def test_unexpand_smaps_path_and_fdtype_classification(self):
        text = added()
        unexpand = extract_fn(text, 'static void unexpand_smaps_path(')
        fdtype = extract_fn(text, 'static uint32_t proc_fdtype_from_link(')
        self.run_c(r'''
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define PROX_FDTYPE_VNODE 1
#define PROX_FDTYPE_SOCKET 2
#define PROX_FDTYPE_KQUEUE 5
#define PROX_FDTYPE_PIPE 6

struct vchroot_unexpand_args {
	char path[4096];
};

static int vchroot_unexpand(struct vchroot_unexpand_args* args)
{
	const char* prefix = "/mnt/kingston/darling-test";
	size_t plen = strlen(prefix);
	if (strncmp(args->path, prefix, plen) == 0 && (args->path[plen] == '/' || args->path[plen] == '\0')) {
		memmove(args->path, args->path + plen, strlen(args->path + plen) + 1);
		if (args->path[0] == '\0')
			strcpy(args->path, "/");
		return 0;
	}
	return 0;
}

''' + unexpand + '\n' + fdtype + r'''

int main(void)
{
	char buf[256];

	/* Prefix-backed host path is unexpanded to guest path. */
	strcpy(buf, "/mnt/kingston/darling-test/Applications/League of Legends.app/Contents/MacOS/League");
	unexpand_smaps_path(buf, sizeof(buf));
	assert(strcmp(buf, "/Applications/League of Legends.app/Contents/MacOS/League") == 0);

	/* Linux pseudo-mappings produce an empty path like macOS. */
	strcpy(buf, "[heap]");
	unexpand_smaps_path(buf, sizeof(buf));
	assert(buf[0] == '\0');

	strcpy(buf, "[vdso]");
	unexpand_smaps_path(buf, sizeof(buf));
	assert(buf[0] == '\0');

	strcpy(buf, "anon_inode:[eventpoll]");
	unexpand_smaps_path(buf, sizeof(buf));
	assert(buf[0] == '\0');

	/* FD type classification from /proc/<pid>/fd/<n> link targets. */
	assert(proc_fdtype_from_link("socket:[12345]") == PROX_FDTYPE_SOCKET);
	assert(proc_fdtype_from_link("pipe:[67890]") == PROX_FDTYPE_PIPE);
	assert(proc_fdtype_from_link("anon_inode:[eventpoll]") == PROX_FDTYPE_KQUEUE);
	assert(proc_fdtype_from_link("anon_inode:[signalfd]") == PROX_FDTYPE_KQUEUE);
	assert(proc_fdtype_from_link("/Applications/LeagueClient.app") == PROX_FDTYPE_VNODE);
	assert(proc_fdtype_from_link("") == PROX_FDTYPE_VNODE);
	return 0;
}
''')


if __name__ == '__main__':
    unittest.main()
