"""getdirentries must never drop or repeat directory entries.

Both calls read Linux records, convert them into the caller's buffer and stop
at the first converted record that does not fit. The records read past that
point used to be lost because the directory offset had already moved on: a
getdirentries64 record can be 8 bytes longer than its Linux record (24-byte
Linux records become 32-byte BSD ones), and the 32-bit call reads at least one
longest Linux record even for a smaller caller buffer. The calls now seek the
directory back to the first record they did not return.

The tests compile the patched functions from patches/xnu.patch against a fake
directory behind fake getdents64/lseek system calls; Darling is not started.
Backport of upstream darling-xnu 5cc9425, 42e995a and 35d3284 plus that fix.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/xnu.patch'
EMU = 'darling/src/libsystem_kernel/emulation/'
GETDIRENTRIES = EMU + 'src/xnu_syscall/bsd/impl/dirent/getdirentries.c'


def new_side(path):
    """Context and added lines of `path`'s section in the xnu patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


def getdirentries_source():
    """Both converters, from the ALIGN macro to the end of the file.

    The file's section is spliced with whole-file context
    (`patchkit.py resplice --context 200`), so its new side is the complete
    file; the Darwin-only dirent64 declaration is dropped.
    """
    if f'+++ b/{GETDIRENTRIES}\n@@ -1,' not in PATCH.read_text():
        raise AssertionError(f'{GETDIRENTRIES} must be one whole-file hunk; resplice it with --context 200')
    source = new_side(GETDIRENTRIES)
    source = source[source.index('#define ALIGN('):]
    return source.replace('struct dirent64 __DARWIN_STRUCT_DIRENTRY;', '')


HARNESS = r'''
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct linux_dirent64 { unsigned long long d_ino; unsigned long long d_off; unsigned short d_reclen;
	unsigned char d_type; char d_name[]; };
struct bsd_dirent { unsigned int d_ino; unsigned short d_reclen; unsigned char d_type; unsigned char d_namlen;
	char d_name[]; };
struct bsd_dirent64 { unsigned long long d_ino; unsigned long long d_seekoff; unsigned short d_reclen;
	unsigned short d_namlen; unsigned char d_type; char d_name[]; };

#define LINUX_SEEK_SET 0
#define LINUX_SEEK_CUR 1
#define LINUX_EINVAL 22
#define EINVAL 22
enum { __NR_getdents64 = 1000, __NR_lseek };

#define MAX_NAMES 64
static char names[MAX_NAMES][256];
static int name_count, dir_pos;

static long fake_getdents64(char* buf, unsigned long count)
{
	unsigned long off = 0;
	while (dir_pos < name_count) {
		size_t namelen = strlen(names[dir_pos]);
		unsigned short reclen = (19 + namelen + 1 + 7) & ~7;
		if (off + reclen > count)
			break;
		struct linux_dirent64* d = (struct linux_dirent64*) (buf + off);
		d->d_ino = 1000 + dir_pos;
		d->d_off = dir_pos + 1;
		d->d_reclen = reclen;
		d->d_type = 8;
		memcpy(d->d_name, names[dir_pos], namelen + 1);
		off += reclen;
		dir_pos++;
	}
	if (off == 0 && dir_pos < name_count)
		return -LINUX_EINVAL;
	return off;
}

static long fake_syscall(long nr, ...)
{
	va_list ap;
	va_start(ap, nr);
	long fd = va_arg(ap, long), a = va_arg(ap, long), b = va_arg(ap, long);
	va_end(ap);
	if (fd != 3)
		return -9;
	if (nr == __NR_getdents64)
		return fake_getdents64((char*) a, (unsigned long) b);
	if (nr == __NR_lseek && b == LINUX_SEEK_CUR && a == 0)
		return dir_pos;
	if (nr == __NR_lseek && b == LINUX_SEEK_SET && a >= 0 && a <= name_count)
		return dir_pos = a;
	return -LINUX_EINVAL;
}
#define LINUX_SYSCALL(...) fake_syscall(__VA_ARGS__)
static long errno_linux_to_bsd(long err) { return err; }

@SOURCE@

#define GUARD 64
static unsigned char out[4096 + GUARD];
static int seen[MAX_NAMES];

// Enumerates the whole directory with `len`-byte calls. When a call reports
// that one record does not fit, the next call uses a large buffer, as a caller
// would. Returns a description of the first problem, or NULL.
static const char* enumerate(int bits, unsigned int len, int verbose)
{
	static char why[256];
	memset(seen, 0, sizeof(seen));
	dir_pos = 0;
	for (int calls = 0; calls < 4 * MAX_NAMES + 4; ++calls) {
		unsigned int use = len;
		memset(out, 0xAA, sizeof(out));
		long base = 0;
		long ret = bits == 64 ? sys_getdirentries64(3, (char*) out, use, &base)
			: sys_getdirentries(3, (char*) out, use, &base);
		if (ret == -EINVAL) {
			use = 4096;
			memset(out, 0xAA, sizeof(out));
			ret = bits == 64 ? sys_getdirentries64(3, (char*) out, use, &base)
				: sys_getdirentries(3, (char*) out, use, &base);
		}
		if (ret < 0) { snprintf(why, sizeof(why), "error %ld", ret); return why; }
		if (ret > use) { snprintf(why, sizeof(why), "returned %ld > %u", ret, use); return why; }
		for (unsigned int i = use; i < use + GUARD; ++i)
			if (out[i] != 0xAA) { snprintf(why, sizeof(why), "wrote past %u", use); return why; }
		if (ret == 0) {
			for (int i = 0; i < name_count; ++i)
				if (seen[i] != 1) { snprintf(why, sizeof(why), "entry %d seen %d times", i, seen[i]); return why; }
			return NULL;
		}
		long pos = 0;
		while (pos < ret) {
			unsigned short reclen;
			unsigned int ino;
			const char* name;
			size_t namlen;
			if (bits == 64) {
				struct bsd_dirent64* d = (struct bsd_dirent64*) (out + pos);
				reclen = d->d_reclen; ino = d->d_ino; name = d->d_name; namlen = d->d_namlen;
				if (reclen % 8) { snprintf(why, sizeof(why), "reclen %u not 8-aligned", reclen); return why; }
			} else {
				struct bsd_dirent* d = (struct bsd_dirent*) (out + pos);
				reclen = d->d_reclen; ino = d->d_ino; name = d->d_name; namlen = d->d_namlen;
				if (reclen % 4) { snprintf(why, sizeof(why), "reclen %u not 4-aligned", reclen); return why; }
			}
			if (reclen == 0 || pos + reclen > ret) { snprintf(why, sizeof(why), "bad reclen %u", reclen); return why; }
			int index = (int) ino - 1000;
			if (index < 0 || index >= name_count || strcmp(name, names[index]) || namlen != strlen(name)) {
				snprintf(why, sizeof(why), "bad record at %ld", pos); return why;
			}
			if (verbose)
				printf("%s\n", name);
			seen[index]++;
			pos += reclen;
		}
	}
	snprintf(why, sizeof(why), "no end of directory");
	return why;
}

static void set_names(int count, const int* lengths)
{
	name_count = count;
	for (int i = 0; i < count; ++i) {
		memset(names[i], 'a' + i % 26, lengths[i]);
		names[i][lengths[i]] = 0;
		names[i][0] = 'A' + i % 26;
	}
}

int main(int argc, char** argv)
{
	if (argc == 4 && !strcmp(argv[1], "list")) {
		// list BITS LEN: four 3-letter names
		int lengths[4] = { 3, 3, 3, 3 };
		set_names(4, lengths);
		const char* why = enumerate(atoi(argv[2]), atoi(argv[3]), 1);
		if (why) printf("FAIL %s\n", why);
		return 0;
	}
	// sweep: every pattern, both calls, many buffer sizes
	static const int patterns[][3] = {
		{ 1, 1, 0 }, { 3, 3, 0 }, { 4, 4, 0 }, { 5, 5, 0 }, { 11, 12, 0 }, { 12, 12, 0 }, { 13, 13, 0 },
		{ 1, 30, 0 }, { 3, 60, 1 }, { 100, 100, 0 }, { 1, 255, 1 }, { 200, 255, 0 }, { 255, 255, 0 },
	};
	int cases = 0, failures = 0;
	for (size_t p = 0; p < sizeof(patterns) / sizeof(patterns[0]); ++p) {
		for (int count = 1; count <= 40; count += 13) {
			int lengths[MAX_NAMES];
			for (int i = 0; i < count; ++i) {
				int lo = patterns[p][0], hi = patterns[p][1];
				lengths[i] = patterns[p][2] ? (i % 2 ? hi : lo) : lo + (i * 7) % (hi - lo + 1);
			}
			set_names(count, lengths);
			for (int bits = 32; bits <= 64; bits += 32)
				for (unsigned int len = 16; len <= 2304; len += len < 400 ? 4 : 52) {
					const char* why = enumerate(bits, len, 0);
					cases++;
					if (why && failures++ < 10)
						printf("FAIL bits=%d len=%u pattern=%zu count=%d: %s\n", bits, len, p, count, why);
				}
		}
	}
	printf("cases=%d failures=%d\n", cases, failures);
	return 0;
}
'''


@unittest.skipUnless(shutil.which('cc'), 'needs a C compiler')
class GetdirentriesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        src = Path(cls.tmp.name) / 'getdirentries.c'
        src.write_text(HARNESS.replace('@SOURCE@', getdirentries_source()))
        cls.binary = Path(cls.tmp.name) / 'getdirentries'
        subprocess.run(['cc', '-std=gnu11', '-w', '-o', str(cls.binary), str(src)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_binary(self, *args):
        return subprocess.run([str(self.binary), *args], check=True, capture_output=True, text=True,
                              timeout=120).stdout

    def test_64_bit_112_byte_buffer_returns_all_four_short_names(self):
        # Four 24-byte Linux records become four 32-byte BSD records; only
        # three fit in 112 bytes, so the fourth must come on the next call.
        self.assertEqual(self.run_binary('list', '64', '112').split(), ['Aaa', 'Bbb', 'Ccc', 'Ddd'])

    def test_32_bit_small_buffer_returns_all_four_short_names(self):
        for length in ('16', '24', '40', '112'):
            with self.subTest(len=length):
                self.assertEqual(self.run_binary('list', '32', length).split(), ['Aaa', 'Bbb', 'Ccc', 'Ddd'])

    def test_no_entry_is_dropped_or_repeated_for_any_name_or_buffer_size(self):
        out = self.run_binary('sweep')
        self.assertIn('failures=0', out, out)

    def test_proc_enumeration_buffer_holds_a_longest_record(self):
        text = new_side(EMU + 'src/xnu_syscall/bsd/helper/misc/sysctl_proc.c')
        self.assertIn('char dents[1024];', text)


if __name__ == '__main__':
    unittest.main()
