"""A new thread's RPC socket must find a descriptor while the table has room (#216).

mldr hands each thread a slot counted down from RLIMIT_NOFILE - 1 and moves the
socket there with F_DUPFD, which only searches upward. When the slot belonged to
a foreign descriptor and every number above it to our own sockets, F_DUPFD failed
with EMFILE although lower numbers were free; the thread printed "Failed to
create socket" and aborted, taking RiotClientServices and the session down.

A socket placed below its slot has no bit in the bitmap: releasing it at thread
exit cleared a bit past the end of the allocation, or through NULL once the
bitmap had been freed.

The first test compiles `socket_bitmap_place` from the patch against a stub
bitmap and replays the collision under a 1024 limit; it needs no Darling
sources. The release tests compile the real bitmap (pinned mldr.c with the patch
applied) and need the prepared sources: build/darling-src, or DARLING_LOL_SOURCE.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CC = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')


def patched_function(patch, path, signature):
    sections = (ROOT / patch).read_text().split('diff --git ')
    section = next(s for s in sections if s.startswith('a/' + path + ' b/'))
    new_side = '\n'.join(line[1:] for line in section.splitlines()
                         if line[:1] in (' ', '+') and not line.startswith('+++'))
    match = re.search(r'^' + re.escape(signature) + r' \{[\s\S]*?^\}', new_side, re.M)
    if not match:
        raise AssertionError(f'{signature} is not in the {path} patch')
    return match.group()


HARNESS = r'''
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/resource.h>
#include <unistd.h>

typedef struct { int unused; } socket_bitmap_t;
static socket_bitmap_t socket_bitmap;
static int next_slot;
static int returned_slot = -1;
static int socket_bitmap_get(socket_bitmap_t* b) { (void)b; return next_slot; }
static void socket_bitmap_put(socket_bitmap_t* b, int s) { (void)b; returned_slot = s; }

%(place)s

static int fail(const char* what) { fprintf(stderr, "%%s\n", what); return 1; }

int main(void) {
	struct rlimit limit = { 1024, 1024 };
	if (setrlimit(RLIMIT_NOFILE, &limit) != 0) return fail("setrlimit");

	int sock = open("/dev/null", O_RDONLY);
	int foreign = open("/dev/null", O_RDONLY);
	if (sock < 0 || foreign < 0) return fail("open");

	/* Our first thread's socket sits at 1023; something else took 1022, the next slot. */
	if (dup2(sock, 1023) != 1023 || dup2(foreign, 1022) != 1022) return fail("dup2");
	next_slot = 1022;
	int placed = socket_bitmap_place(sock);
	if (placed != 1021) { fprintf(stderr, "collision: placed=%%d errno=%%d\n", placed, errno); return 1; }
	if (returned_slot != 1022) return fail("the foreign slot was not handed back");
	if (fcntl(1022, F_GETFD) < 0 || fcntl(1023, F_GETFD) < 0) return fail("an existing descriptor was closed");

	/* Several taken numbers below the slot: the highest free one is used, nothing is replaced. */
	dup2(foreign, 1020); dup2(foreign, 1019);
	next_slot = 1022; returned_slot = -1;
	placed = socket_bitmap_place(sock);
	if (placed != 1018) { fprintf(stderr, "below: placed=%%d errno=%%d\n", placed, errno); return 1; }

	/* A free slot is still taken exactly. */
	close(placed);
	next_slot = 1018; returned_slot = -1;
	placed = socket_bitmap_place(sock);
	if (placed != 1018 || returned_slot != -1) return fail("free slot not used exactly");

	/* A really full table still fails with EMFILE instead of looping or clobbering. */
	int filler;
	while ((filler = dup(sock)) >= 0) { }
	next_slot = 1022; returned_slot = -1;
	errno = 0;
	placed = socket_bitmap_place(sock);
	if (placed != -1 || errno != EMFILE) { fprintf(stderr, "full: placed=%%d errno=%%d\n", placed, errno); return 1; }
	if (returned_slot != 1022) return fail("slot leaked on a full table");

	puts("ok");
	return 0;
}
'''


MLDR = 'src/startup/mldr/mldr.c'
SOURCE = Path(os.environ.get('DARLING_LOL_SOURCE', ROOT / 'build/darling-src'))


def patched_bitmap():
    '''The bitmap type and socket_bitmap_get/put/place as the build compiles them.'''
    component = next(c for c in json.loads((ROOT / 'upstream/manifest.json').read_text())['components']
                     if c['patch'] == 'patches/darling.patch')
    repo = SOURCE / component['path']
    if not (repo / '.git').exists() or not shutil.which('patch'):
        raise unittest.SkipTest('prepared Darling sources and patch required')
    original = subprocess.check_output(['git', '-C', str(repo), 'show', component['revision'] + ':' + MLDR])
    marker = f'diff --git a/{MLDR} b/{MLDR}\n'
    section = marker + (ROOT / component['patch']).read_text().split(marker, 1)[1].split('\ndiff --git ', 1)[0] + '\n'
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / MLDR
        target.parent.mkdir(parents=True)
        target.write_bytes(original)
        subprocess.run(['patch', '--silent', '-p1', '-d', tmp], input=section, text=True, check=True,
                       capture_output=True)
        source = target.read_text()
    return source[source.index('typedef struct socket_bitmap {'):source.index('int __mldr_create_rpc_socket(void) {')]


# CANARY builds give the bitmap a tail of 0xff bytes, so a bit cleared past the end is seen without a sanitizer.
RELEASE_HARNESS = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>

#ifdef CANARY
#define TAIL 256
static size_t canary_size;
static void* canary_realloc(void* old, size_t size) {
	unsigned char* block = malloc(size + TAIL);
	if (!block) return NULL;
	if (old) memcpy(block, old, size < canary_size ? size : canary_size);
	memset(block + size, 0xff, TAIL);
	free(old);
	canary_size = size;
	return block;
}
static void canary_free(void* old) { free(old); canary_size = 0; }
static int tail_intact(const void* bits) {
	for (size_t i = 0; bits && i < TAIL; ++i) {
		if (((const unsigned char*)bits)[canary_size + i] != 0xff) return 0;
	}
	return 1;
}
#define realloc canary_realloc
#define free canary_free
#else
static int tail_intact(const void* bits) { (void)bits; return 1; }
#endif

%(bitmap)s

static int fail(const char* what) {
	fprintf(stderr, "%%s (bits=%%p bit_length=%%zu)\n", what, (void*)socket_bitmap.bits, socket_bitmap.bit_length);
	return 1;
}

/* __mldr_close_rpc_socket, which a thread calls on its socket when it exits */
static void release(int fd) {
	close(fd);
	socket_bitmap_put(&socket_bitmap, fd);
}

int main(int argc, char** argv) {
	struct rlimit limit = { 1024, 1024 };
	if (argc < 2 || setrlimit(RLIMIT_NOFILE, &limit) != 0) return fail("setrlimit");

	int sock = open("/dev/null", O_RDONLY);
	int foreign = open("/dev/null", O_RDONLY);
	if (sock < 0 || foreign < 0) return fail("open");

	if (argv[1][0] == 'f') {
		/* 1023 is foreign before our first socket: it lands on 1022, and handing slot 1023 back frees the bitmap. */
		if (dup2(foreign, 1023) != 1023) return fail("dup2");
		int placed = socket_bitmap_place(sock);
		if (placed != 1022) return fail("first socket not placed just below the foreign descriptor");
		if (socket_bitmap.bits != NULL || socket_bitmap.bit_length != 0) return fail("slot 1023 was not handed back");
		release(placed);
		if (fcntl(placed, F_GETFD) >= 0) return fail("socket left open");
		if (socket_bitmap.bits != NULL || socket_bitmap.bit_length != 0) return fail("release touched a freed bitmap");
		if (socket_bitmap_place(sock) != 1022) return fail("bitmap unusable after the release");
		puts("ok");
		return 0;
	}

	/* 24 of our sockets on top, foreign descriptors from 999 down to just above `low`, which is free. */
	int low = atoi(argv[1] + 1);
	int ours[25];
	for (int i = 0; i < 24; ++i) {
		ours[i] = socket_bitmap_place(sock);
		if (ours[i] != 1023 - i) return fail("sockets not counted down from the limit");
	}
	for (int n = 999; n > low; --n) {
		if (dup2(foreign, n) != n) return fail("dup2");
	}

	int placed = socket_bitmap_place(sock);
	if (placed != low) { fprintf(stderr, "placed=%%d errno=%%d\n", placed, errno); return 1; }
	if (socket_bitmap.bit_length != 24) return fail("slot 999 was not handed back");

	release(placed);
	if (!tail_intact(socket_bitmap.bits)) return fail("release cleared a bit past the end of the bitmap");
	if (fcntl(placed, F_GETFD) >= 0) return fail("socket left open");
	if (socket_bitmap.bit_length != 24) return fail("release changed the tracked slots");
	for (int i = 0; i < 24; ++i) {
		if (fcntl(ours[i], F_GETFD) < 0) return fail("release closed another socket");
	}

	/* Slot 999 is free again once its foreign owner is gone, and nothing above it was given away. */
	close(999);
	ours[24] = socket_bitmap_place(sock);
	if (ours[24] != 999 || socket_bitmap.bit_length != 25) return fail("slot 999 not reused");

	for (int i = 0; i < 25; ++i) release(ours[i]);
	if (socket_bitmap.bits != NULL || socket_bitmap.bit_length != 0) return fail("bitmap not empty after every release");
	if (!tail_intact(socket_bitmap.bits)) return fail("a release cleared a bit past the end of the bitmap");
	if (socket_bitmap_place(sock) != 1023) return fail("bitmap unusable after every release");

	puts("ok");
	return 0;
}
'''


def address_sanitizer_works(tmp):
    probe = Path(tmp) / 'probe.c'
    probe.write_text('#include <stdlib.h>\nint main(void) { free(malloc(1)); return 0; }\n')
    built = subprocess.run([CC, '-fsanitize=address', str(probe), '-o', str(probe.with_suffix(''))],
                           capture_output=True)
    return built.returncode == 0 and subprocess.run([str(probe.with_suffix(''))], capture_output=True).returncode == 0


@unittest.skipUnless(CC, 'no host C compiler')
class RpcSocketReleaseTests(unittest.TestCase):
    '''Every case runs in a canary build, and under AddressSanitizer when the host compiler has it.'''

    @classmethod
    def setUpClass(cls):
        bitmap = patched_bitmap()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        source = Path(cls.tmp.name) / 'release.c'
        source.write_text(RELEASE_HARNESS % {'bitmap': bitmap})
        variants = {'canary': ['-DCANARY']}
        if address_sanitizer_works(cls.tmp.name):
            variants['asan'] = ['-fsanitize=address', '-fno-omit-frame-pointer']
        cls.binaries = {}
        for name, flags in variants.items():
            binary = Path(cls.tmp.name) / name
            subprocess.run([CC, '-std=gnu11', '-g', '-Wall', '-Werror', *flags, str(source), '-o', str(binary),
                            '-lpthread'], check=True)
            cls.binaries[name] = binary

    def check(self, case):
        for name, binary in self.binaries.items():
            with self.subTest(build=name):
                run = subprocess.run([str(binary), case], capture_output=True, text=True, timeout=60,
                                     env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
                self.assertEqual(run.returncode, 0, run.stderr[-2000:])

    def test_releasing_a_socket_placed_below_the_slots_stays_inside_the_bitmap(self):
        for low in (990, 900, 500):
            self.check(f'b{low}')

    def test_releasing_a_socket_placed_below_a_freed_bitmap(self):
        self.check('f')


@unittest.skipUnless(CC, 'no host C compiler')
class RpcSocketPlacementTests(unittest.TestCase):
    def test_collision_below_our_sockets_uses_a_lower_free_number(self):
        place = patched_function('patches/darling.patch', 'src/startup/mldr/mldr.c',
                                 'static int socket_bitmap_place(int fd)')
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'place.c'
            binary = Path(tmp) / 'place'
            source.write_text(HARNESS % {'place': place})
            subprocess.run([CC, '-std=gnu11', '-Wall', '-Werror', str(source), '-o', str(binary)],
                           check=True)
            run = subprocess.run([str(binary)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_abort_message_reports_errno_and_limit(self):
        sections = (ROOT / 'patches/darling.patch').read_text().split('diff --git ')
        section = next(s for s in sections if s.startswith('a/src/startup/mldr/elfcalls/threads.c '))
        self.assertIn('Failed to create socket: %s (errno %d, RLIMIT_NOFILE %llu)', section)


if __name__ == '__main__':
    unittest.main()
