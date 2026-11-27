"""The self-view probe stays read-only and prints nothing that names a host or a user.

tests/security/probe/self_view.c runs on public macOS runners and on
contributors' machines, and its output is meant to be shared and diffed.
"""
from pathlib import Path
import re
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
        # only the characters before the '=' are copied; the names go out as one sorted array
        self.assertIn('size_t n = strcspn(*e, "=");', env)
        self.assertIn("memcpy(names[kept], *e, n);", env)
        self.assertEqual(env.count('*e'), 6)  # the loop header (twice), strcspn, two name tests, the copy
        self.assertIn('qsort(names, kept, sizeof(names[0]), name_cmp);', env)
        self.assertIn('kv("names", "[%s]", list);', env)
        self.assertEqual(env.count('kv('), 3)
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
                     '"/Volumes/SystemRoot/"'):
            self.assertIn(text, clean)
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
        # the parent and pids 0 and 1: name and path, nothing else; the pid list: a count
        self.assertEqual(self.code.count('proc_listpids('), 1)
        self.assertNotIn('proc_pidfdinfo', self.code)
        self.assertNotIn('PROC_PIDLISTFDS', self.code)

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
