"""The self-view probe stays read-only and prints nothing that names a host or a user.

tests/security/probe/self_view.c runs on public macOS runners and on
contributors' machines, and its output is meant to be shared and diffed.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / 'tests/security/probe/self_view.c'
WORKFLOW = ROOT / '.github/workflows/security-ground-truth.yml'


def code_without_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'//[^\n]*', '', text)


class SelfViewProbeTests(unittest.TestCase):
    def setUp(self):
        self.code = code_without_comments(PROBE.read_text())

    def test_no_identifying_source_is_read(self):
        for name in ('gethostname', 'getlogin', 'nodename', 'pw_name', 'pw_dir', 'gethostuuid',
                     'getifaddrs', 'IOPlatformSerialNumber', 'IOPlatformUUID', 'kern.hostname', 'kern.uuid'):
            self.assertNotIn(name, self.code, name)
        # the gecos field is only tested for being empty, never printed
        self.assertEqual(self.code.count('pw_gecos'), 2)
        self.assertRegex(self.code, r'pw->pw_gecos && pw->pw_gecos\[0\] \? "present" : "empty"')

    def test_environment_values_are_never_printed(self):
        env = self.code[self.code.index('static void sec_env(void)'):]
        env = env[:env.index('\n}\n')]
        # only the characters before the '=' reach a name; the names go out as one sorted array
        self.assertIn('size_t n = strcspn(*e, "=");', env)
        self.assertIn('names[kept] = name_text(*e, n);', env)
        self.assertEqual(env.count('*e'), 6)  # the loop header (twice), strcspn, two name tests, the copy
        self.assertIn('qsort(names, kept, sizeof(*names), name_cmp);', env)
        self.assertIn('kv("names", "[%s]", list);', env)
        self.assertEqual(env.count('kv('), 2)
        self.assertNotIn('getenv', self.code)

    def test_probe_changes_nothing(self):
        for call in ('fopen(', 'open(', 'creat(', 'unlink(', 'kill(', 'ptrace(', 'socket(', 'connect(',
                     'task_for_pid(', 'mach_vm_write(', 'mach_vm_protect(', 'system(', 'popen(', 'fork('):
            self.assertNotRegex(self.code, r'(?<![A-Za-z0-9_])' + re.escape(call), call)

    def test_paths_go_through_the_cleaner(self):
        # only system locations are printed; a home, a temporary directory or any other
        # path is reduced to its kind, on macOS and under the host root Darling shows
        clean = self.code[self.code.index('static const char *clean_path('):]
        clean = clean[:clean.index('\n}\n')]
        for text in ('"/home/"', '"/Users/"', '"<home>"', '"<tmp>"', '"<other>"', '"<relative>"',
                     '"<application>"', '"/Volumes/SystemRoot/"'):
            self.assertIn(text, clean)
        # no prefix under which a user creates folders is in the list printed in full
        listed = clean[clean.index('sys[] = {'):clean.index('NULL};')]
        for prefix in ('"/Applications/"', '"/usr/"', '"/Library/"', '"/opt/"', '"/etc/"', '"/var/"', '"/private/"',
                       '"/System/"', '"/System/Volumes/"'):
            self.assertNotIn(prefix, listed, prefix)
        # nothing of an unknown path survives: no basename, no remainder
        self.assertNotIn('strrchr', clean)
        self.assertEqual(clean.count('snprintf('), 1)
        self.assertIn('snprintf(out, SV_BUFSZ, "%s", kind);', clean)
        # every path the system hands back is printed through it
        for field in ('vip_path', 'dli_fname', 'f_mntfromname', 'pw_shell'):
            for line in self.code.splitlines():
                if field in line and ('kv(' in line or 'snprintf(' in line):
                    self.assertIn('jpath(', line, line.strip())

    def test_other_processes_are_only_named(self):
        # Every per-process call is on the probe itself, except name and path, which are also
        # asked for the parent and for pids 0 and 1.
        self.assertEqual(re.findall(r'proc_pidinfo\(([^,]+),', self.code), ['getpid()'])
        self.assertEqual(re.findall(r'proc_regionfilename\(([^,]+),', self.code), ['getpid()'])
        self.assertEqual(re.findall(r'proc_pid_rusage\(([^,]+),', self.code), ['self'])
        self.assertEqual(re.findall(r'proc_pidpath\(([^,]+),', self.code), ['pid'])
        self.assertEqual(re.findall(r'proc_name\(([^,]+),', self.code), ['pid'])
        self.assertEqual(sorted(re.findall(r'pid_kv\("[a-z_0-9]+", ([^)]+)\);', self.code)),
                         ['0', '1', 'parent', 'self'])
        for call in ('proc_pidfdinfo', 'PROC_PIDLISTFDS', 'task_for_pid', 'processor_set_tasks',
                     'KERN_PROC_ALL', 'proc_listallpids', 'proc_listchildpids'):
            self.assertNotIn(call, self.code, call)
        # the pid list: its length, and whether 0 and 1 are in it; no other entry is used
        self.assertEqual(self.code.count('proc_listpids('), 1)
        listing = self.code[self.code.index('proc_listpids('):]
        listing = listing[:listing.index('kv("listpids_all"')]
        self.assertEqual(re.findall(r'pids\[i\] == (\d+)', listing), ['0', '1'])
        self.assertEqual(listing.count('pids['), 2)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_strings_and_paths_as_printed(self):
        # The output helpers are plain C: compiled here and run on the cases they exist for.
        source_text = PROBE.read_text()
        start = source_text.index('static char *scratch(void)')
        helpers = source_text[start:source_text.index('static const char *jhex(', start)]
        start = source_text.index('static char *name_text(')
        name_text = source_text[start:source_text.index('static void sec_env(void)', start)]
        harness = r"""
#define _GNU_SOURCE
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define SV_NBUF 16
#define SV_BUFSZ 2400
""" + helpers + name_text + r"""
#define JSON(in, want) assert(strcmp(jstr(in), want) == 0)
#define PATH(in, want) assert(strcmp(clean_path(in), want) == 0)
int main(void)
{
    /* UTF-8 text is kept: two, three and four byte sequences */
    JSON("caf\xc3\xa9", "\"caf\xc3\xa9\"");
    JSON("\xe2\x82\xac", "\"\xe2\x82\xac\"");
    JSON("\xf0\x9f\x98\x80", "\"\xf0\x9f\x98\x80\"");
    /* quotes, backslashes and control characters are escaped */
    JSON("q\"\\\n\x7f", "\"q\\\"\\\\\\u000a\\u007f\"");
    /* not UTF-8: a lone byte, a truncated sequence, an overlong form, a surrogate, beyond U+10FFFF */
    JSON("a\xff" "b", "\"a\\ufffdb\"");
    JSON("a\xc3", "\"a\\ufffd\"");
    JSON("a\xe2\x82", "\"a\\ufffd\\ufffd\"");
    JSON("\xc0\xaf", "\"\\ufffd\\ufffd\"");
    JSON("\xe0\x80\xaf", "\"\\ufffd\\ufffd\\ufffd\"");
    JSON("\xed\xa0\x80", "\"\\ufffd\\ufffd\\ufffd\"");
    JSON("\xf4\x90\x80\x80", "\"\\ufffd\\ufffd\\ufffd\\ufffd\"");
    JSON(NULL, "\"\"");

    /* fixed system locations are printed in full */
    PATH("/usr/lib/dyld", "/usr/lib/dyld");
    PATH("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",
         "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation");
    PATH("/dev/disk3s1s1", "/dev/disk3s1s1");
    PATH("/bin/bash", "/bin/bash");
    PATH("/Volumes/SystemRoot/usr/lib/libc.so.6", "/Volumes/SystemRoot/usr/lib/libc.so.6");
    /* a name somebody chose is never printed, wherever it sits */
    PATH("/Users/alice/project/tool", "<home>");
    PATH("/home/alice", "<home>");
    PATH("/Volumes/SystemRoot/home/alice/project", "<home>");
    /* the data volume holds the homes: the path below it decides */
    PATH("/System/Volumes/Data/Users/alice", "<home>");
    PATH("/System/Volumes/Data/Users/alice/Library/x", "<home>");
    PATH("/System/Volumes/Data/Applications/alice", "<application>");
    PATH("/System/Volumes/Data/opt/alice", "<other>");
    PATH("/System/Volumes/Data/usr/lib/x", "/System/Volumes/Data/usr/lib/x");
    PATH("/System/Volumes/alice", "<other>");
    PATH("/System/alice", "<other>");
    PATH("/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld/dyld_shared_cache_x86_64",
         "/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld/dyld_shared_cache_x86_64");
    PATH("/System/Volumes/Preboot/alice", "<other>");
    PATH("/Applications/alice", "<application>");
    PATH("/Applications/Some Game.app/Contents/MacOS/Game", "<application>");
    PATH("/opt/alice/bin/tool", "<other>");
    PATH("/usr/local/alice/tool", "<other>");
    PATH("/Library/alice", "<other>");
    PATH("/etc/alice", "<other>");
    PATH("/Volumes/alice-disk/x", "<other>");
    PATH("/usr/lib/../../Users/alice", "<other>");
    PATH("/private/var/folders/ab/cdef/T/alice", "<tmp>");
    PATH("/tmp/alice", "<tmp>");
    PATH("alice/relative", "<relative>");
    PATH("/", "/");
    PATH("", "");
    PATH(NULL, "");

    /* an environment name survives as text that gives its bytes back */
    char *n = name_text("XDG_A-B", 7);
    assert(strcmp(n, "XDG_A%2DB") == 0);
    free(n);
    n = name_text("XDG_A.B", 7);
    assert(strcmp(n, "XDG_A%2EB") == 0);
    free(n);
    n = name_text("XDG_\x80", 5);
    assert(strcmp(n, "XDG_%80") == 0);
    free(n);
    n = name_text("XDG_\x81%", 6);
    assert(strcmp(n, "XDG_%81%25") == 0);
    free(n);
    {
        char longname[400];
        memset(longname, 'A', sizeof(longname));
        n = name_text(longname, sizeof(longname));
        assert(strlen(n) == sizeof(longname));
        free(n);
    }
    puts("ok");
    return 0;
}
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'helpers.c'
            binary = Path(folder) / 'helpers'
            source.write_text(harness)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function',
                            str(source), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), 'ok')

    def test_a_failed_write_fails_the_run(self):
        self.assertIn('return fflush(stdout) != 0 || ferror(stdout) ? 1 : 0;', self.code)

    def test_workflow_runs_both_slices_and_filters_sysctl(self):
        workflow = WORKFLOW.read_text()
        self.assertIn('clang -O2 -Wall -Wextra -arch arm64 -o self_view_arm64 tests/security/probe/self_view.c', workflow)
        self.assertIn('clang -O2 -Wall -Wextra -arch x86_64 -o self_view_x86_64 tests/security/probe/self_view.c', workflow)
        self.assertIn("sysctl -a | sed -E 's/^(kern\\.(hostname|nisdomainname|hostid|[a-z]*uuid)): .*/\\1: <removed>/'",
                      workflow)
        self.assertIn('self-view-*.json', workflow)
        self.assertIn('sysctl-*.txt', workflow)
        # the workflow stays read-only
        self.assertIn('permissions:\n  contents: read', workflow)


if __name__ == '__main__':
    unittest.main()
