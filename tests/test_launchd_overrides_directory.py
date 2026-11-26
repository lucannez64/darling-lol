"""Host-only regression for launchd's installed overrides database directory."""
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def launchd_cmake_additions():
    for section in (ROOT / 'patches/darling.patch').read_text().split('diff --git '):
        if section.startswith('a/src/launchd/CMakeLists.txt b/'):
            return '\n'.join(line[1:] for line in section.splitlines()
                             if line.startswith('+') and not line.startswith('+++'))
    return ''


def startup_patch():
    for section in (ROOT / 'patches/darling.patch').read_text().split('diff --git '):
        if section.startswith('a/src/startup/darling.c b/'):
            return section
    return ''


def startup_additions():
    return '\n'.join(line[1:] for line in startup_patch().splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


class LaunchdOverridesDirectoryTests(unittest.TestCase):
    def test_prefix_state_is_prepared_only_at_cold_start(self):
        section = startup_patch()
        context = '\n'.join(line[1:] for line in section.splitlines()
                            if line.startswith((' ', '+')) and not line.startswith('+++'))
        self.assertRegex(context, r'if\s*\(pidInit == 0\)\s*\{\s*setupLaunchdState\(\);')
        self.assertEqual(len(re.findall(r'^\+\s*setupLaunchdState\(\);', section, re.M)), 1)

    def test_prefix_state_creation_preserves_existing_database_and_modes(self):
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('a C compiler is required for prefix state provisioning')
        code = startup_additions()
        helpers = [re.search(r'static void ' + name + r'\([^)]*\)\s*\{.*?^\}', code, re.M | re.S)
                   for name in ('createLaunchdStateDir', 'setupLaunchdState')]
        for helper in helpers:
            self.assertIsNotNone(helper, 'prefix-local launchd state helpers must be distributed')
        # The prefix has no private/var/db yet: missing parents must be created.
        with tempfile.TemporaryDirectory(prefix='launchd-prefix-') as folder:
            root = Path(folder)
            source, binary = root / 'check.c', root / 'check'
            source.write_text(PREFIX_HARNESS.replace(
                '/* HELPER */', '\n\n'.join(helper.group(0) for helper in helpers)))
            built = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    str(source), '-o', str(binary)],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            result = subprocess.run([str(binary), str(root)], capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('LAUNCHD_PREFIX_STATE_PASS', result.stdout)

            # A file where a directory belongs must not stop Darling from
            # starting; launchd runs without persisted state and it warns.
            blocked = root / 'blocked'
            (blocked / 'private/var').mkdir(parents=True)
            (blocked / 'private/var/db').write_text('not a directory')
            result = subprocess.run([str(binary), str(blocked), 'blocked'],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('LAUNCHD_PREFIX_BLOCKED_PASS', result.stdout)
            self.assertIn('is not a directory', result.stderr)

    def test_runtime_installs_overrides_directories_with_mode_755(self):
        cmake = shutil.which('cmake')
        if not cmake:
            self.skipTest('cmake is required to check runtime directory installation')
        code = launchd_cmake_additions()
        with tempfile.TemporaryDirectory(prefix='launchd-overrides-') as folder:
            root = Path(folder)
            for relative in ('private/var/db/launchd.db',
                             'private/var/db/launchd.db/com.apple.launchd'):
                with self.subTest(directory=relative):
                    command = re.search(
                        r'install\(DIRECTORY\s+DESTINATION\s+libexec/darling/' +
                        re.escape(relative) + r'(?=\s|\))[^)]*\)', code)
                    self.assertIsNotNone(command, relative + ' must be installed')
                    # Configure a tiny project using the actual distributed
                    # install command; this never starts Darling.
                    project = root / 'CMakeLists.txt'
                    project.write_text('cmake_minimum_required(VERSION 3.13)\n'
                                       'project(overrides NONE)\n' + command.group(0) + '\n')
                    build = root / 'build'
                    configured = subprocess.run(
                        [cmake, '-S', str(root), '-B', str(build),
                         '-DCMAKE_INSTALL_PREFIX=' + str(root / 'runtime')],
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(configured.returncode, 0,
                                     configured.stdout + configured.stderr)
                    installed = subprocess.run([cmake, '--install', str(build)],
                                               capture_output=True, text=True, timeout=30)
                    self.assertEqual(installed.returncode, 0,
                                     installed.stdout + installed.stderr)
                    directory = root / 'runtime/libexec/darling' / relative
                    self.assertTrue(directory.is_dir())
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)


PREFIX_HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
static char *prefix;
static uid_t g_originalUid = 1000;
static gid_t g_originalGid = 1001;
static uid_t effective_uid;
static gid_t effective_gid;
static unsigned transitions;
static uid_t test_geteuid(void) { return effective_uid; }
static gid_t test_getegid(void) { return effective_gid; }
static int test_seteuid(uid_t uid) {
    assert(effective_gid == g_originalGid);
    effective_uid = uid; ++transitions; return 0;
}
static int test_setegid(gid_t gid) {
    assert(effective_uid == 0);
    effective_gid = gid; ++transitions; return 0;
}
#define geteuid test_geteuid
#define getegid test_getegid
#define seteuid test_seteuid
#define setegid test_setegid
/* HELPER */
int main(int argc, char **argv) {
    assert(argc == 2 || argc == 3); prefix = argv[1]; umask(0077);
    if (argc == 3) {
        setupLaunchdState();
        assert(effective_uid == 0 && effective_gid == 0 && transitions == 4);
        puts("LAUNCHD_PREFIX_BLOCKED_PASS"); return 0;
    }
    setupLaunchdState();
    assert(umask(0077) == 0077);
    assert(effective_uid == 0 && effective_gid == 0 && transitions == 4);
    const char *paths[] = {"/private/var/db/launchd.db",
                          "/private/var/db/launchd.db/com.apple.launchd"};
    char path[4096]; struct stat before[2];
    for (size_t i = 0; i < 2; ++i) {
        snprintf(path, sizeof(path), "%s%s", prefix, paths[i]);
        assert(stat(path, &before[i]) == 0);
        assert((before[i].st_mode & 0777) == 0755);
        assert(chmod(path, 0710) == 0);
    }
    snprintf(path, sizeof(path), "%s%s/overrides.plist", prefix, paths[1]);
    FILE *db = fopen(path, "w"); assert(db);
    assert(fputs("existing overrides", db) >= 0); assert(fclose(db) == 0);
    setupLaunchdState();
    assert(umask(0077) == 0077);
    assert(effective_uid == 0 && effective_gid == 0 && transitions == 8);
    for (size_t i = 0; i < 2; ++i) {
        struct stat after;
        snprintf(path, sizeof(path), "%s%s", prefix, paths[i]);
        assert(stat(path, &after) == 0);
        assert(after.st_ino == before[i].st_ino);
        assert((after.st_mode & 0777) == 0710);
    }
    snprintf(path, sizeof(path), "%s%s/overrides.plist", prefix, paths[1]);
    db = fopen(path, "r"); assert(db); char contents[64];
    assert(fgets(contents, sizeof(contents), db)); assert(fclose(db) == 0);
    assert(strcmp(contents, "existing overrides") == 0);
    puts("LAUNCHD_PREFIX_STATE_PASS"); return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
