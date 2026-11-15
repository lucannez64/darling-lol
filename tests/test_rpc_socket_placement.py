"""A new thread's RPC socket must find a descriptor while the table has room (#216).

mldr hands each thread a slot counted down from RLIMIT_NOFILE - 1 and moves the
socket there with F_DUPFD, which only searches upward. When the slot belonged to
a foreign descriptor and every number above it to our own sockets, F_DUPFD failed
with EMFILE although lower numbers were free; the thread printed "Failed to
create socket" and aborted, taking RiotClientServices and the session down.

The test compiles `socket_bitmap_place` from the patch with the host compiler
against a stub bitmap and replays that collision under a 1024 limit.
"""
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
