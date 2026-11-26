"""Regressions for launchd's filesystem event provider (UserEventAgent).

The expected behaviour was measured on macOS 15.5 with user LaunchAgents
(build/parity is local and not committed; the numbers are restated here):

- a WatchPaths job runs once when its label is first registered, and not
  again when the same label is unloaded and loaded later;
- every change at or below a watched path runs the job: a new file, an
  append, touching the directory, a file two levels down, creating a missing
  watched path, writing inside it, deleting it, renaming it away and back;
- a watched symlink follows its target;
- two jobs watching one path both run;
- changes 1 s apart run twice (the second deferred by ThrottleInterval) while a
  burst of touches runs once, and a change while the job is still running is
  dropped;
- QueueDirectories and KeepAlive PathState are level-triggered.

Most checks compile the distributed UserEventAgent functions on the host
against real Linux inotify, with launchd and XPC replaced by small fixtures.
They never start Darling or a client.
"""
from pathlib import Path
import json
import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'src/UserEventAgent/main.c'
DIRECTORY = 'src/UserEventAgent/'
CORE = 'src/launchd/src/core.c'


def additions(path):
    """Return added lines of a patch section, or empty text if absent."""
    for section in (ROOT / 'patches/darling.patch').read_text().split('diff --git '):
        if section.startswith('a/' + path + ' b/'):
            return '\n'.join(line[1:] for line in section.splitlines()
                             if line.startswith('+') and not line.startswith('+++'))
    return ''


def function_body(code, name):
    """Extract a C function body without relying on its line formatting."""
    match = re.search(r'\b' + re.escape(name) + r'\s*\([^;{}]*\)\s*\{', code)
    if match is None:
        return ''
    start = match.end()
    depth = 1
    for index in range(start, len(code)):
        depth += (code[index] == '{') - (code[index] == '}')
        if depth == 0:
            return code[start:index]
    return ''


def find_compiler():
    return next((path for name in ('clang', 'clang-22', 'clang-20', 'clang-19',
                                   'clang-18', 'clang-17', 'clang-16',
                                   'clang-15', 'clang-14')
                 if (path := shutil.which(name))), None)


class FSEventsMonitorSourceTests(unittest.TestCase):
    def test_daemon_source_is_distributed(self):
        self.assertTrue(additions(SOURCE), 'the patch must contain UserEventAgent')

    def test_event_provider_checks_in_on_filesystem_stream(self):
        code = additions(SOURCE)
        self.assertIn('com.apple.fsevents.matching', code)
        self.assertIn('XPC_EVENT_PROVIDER_CHECK_IN', code)
        self.assertIn('XPC_EVENT_ROUTINE_KEY_EVENTS', code)

    def test_provider_talks_to_launchd_on_the_bootstrap_port_like_macos(self):
        main = function_body(additions(SOURCE), 'main')
        # Apple's idiom; libxpc selects the direct mode by destination.
        self.assertRegex(main, r'xpc_pipe_create_from_port\(bootstrap_port,\s*0\)')
        self.assertNotIn('XPC_PIPE_USE_SYNC_IPC_OVERRIDE', main)

    def test_sigusr1_and_a_timer_both_recheck_registrations(self):
        code = additions(SOURCE)
        main = function_body(code, 'main')
        self.assertIn('EVFILT_SIGNAL', main)
        self.assertRegex(main, r'(?s)ident == SIGUSR1\).*?recheck = true;')
        self.assertRegex(main, r'(?s)if \(recheck\) \{\s*provider_check_in\(\);')
        # A lost SIGUSR1 must not leave new jobs unwatched forever.
        self.assertRegex(main, r'time - last_check_in >= RECHECK_SECONDS')

    def test_one_inotify_instance_serves_every_watch(self):
        code = additions(SOURCE)
        self.assertEqual(code.count('inotify_init1('), 1)
        self.assertNotIn('EVFILT_VNODE', code)
        self.assertIn('inotify_init1(', function_body(code, 'main'))

    def test_watched_paths_are_never_opened(self):
        # O_EVTONLY is not translated by Darling and a FIFO would block open().
        code = additions(SOURCE)
        self.assertNotIn('O_EVTONLY', code)
        rebuild = re.sub(r'/\*.*?\*/', '', function_body(code, 'watch_rebuild'), flags=re.S)
        self.assertNotRegex(rebuild, r'\bopen\(')

    def test_pulse_returns_to_false(self):
        pulse = function_body(additions(SOURCE), 'pulse_watchpaths')
        self.assertRegex(pulse, r'(?s)set_state\([^;]*,\s*true\s*\).*?set_state\([^;]*,\s*false\s*\)')

    def test_manifest_records_new_monitor_files(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        main = next(component for component in manifest['components']
                    if component['path'] == '.')
        self.assertIn(SOURCE, main['new_files'])
        self.assertIn(DIRECTORY + 'CMakeLists.txt', main['new_files'])
        self.assertTrue(any('UserEventAgent' in path and path.endswith('.plist')
                            for path in main['new_files']))

    def test_system_launchdaemon_has_eventmonitor_and_macos_program_path(self):
        patch = (ROOT / 'patches/darling.patch').read_text()
        paths = re.findall(r'^diff --git a/(\S*UserEventAgent\S*\.plist) b/', patch, re.M)
        self.assertTrue(paths, 'a UserEventAgent LaunchDaemon must be distributed')
        plist = plistlib.loads(additions(paths[0]).encode())
        self.assertIs(plist.get('EventMonitor'), True)
        self.assertIs(plist.get('KeepAlive'), True)
        self.assertEqual(plist['ProgramArguments'][0], '/usr/libexec/UserEventAgent')
        cmake = additions(DIRECTORY + 'CMakeLists.txt')
        self.assertIn('add_darling_executable(UserEventAgent', cmake)
        self.assertIn('LaunchDaemons', cmake)
        self.assertIn('add_subdirectory(UserEventAgent)', additions('src/CMakeLists.txt'))


class LaunchdWatchPathsTests(unittest.TestCase):
    def test_first_registration_of_a_label_runs_the_job_once(self):
        code = additions(CORE)
        record = function_body(code, 'externalevent_first_watchpaths_registration')
        self.assertTrue(record, 'launchd must record first WatchPaths registrations')
        # Persisted in the launchd database so it survives runtime restarts.
        self.assertIn('LAUNCHD_PERSISTENT_STORE_DB', record)
        self.assertIn('O_CREAT | O_EXCL', record)
        self.assertRegex(code, r'(?s)"com\.apple\.launchd\.WatchPaths"\) == 0 &&\s*'
                               r'externalevent_first_watchpaths_registration\(j\)\) \{.*?'
                               r'j->start_pending = true;')

    def test_a_change_while_the_job_runs_is_dropped_like_macos(self):
        # Measured on macOS: a touch during an 8 s run starts nothing more.
        set_state = function_body(additions(CORE), 'xpc_event_provider_set_state')
        self.assertNotIn('start_pending', set_state)


class FSEventsHostTests(unittest.TestCase):
    """Run the distributed watcher code against real inotify on the host."""

    def test_host_filesystem_behaviour_matches_macos(self):
        compiler = find_compiler()
        if not compiler:
            self.skipTest('clang with C blocks support required')
        code = additions(SOURCE)
        self.assertTrue(code, 'monitor source must be distributed')
        pieces = []
        for name in ('COALESCE_SECONDS', 'MAX_TREE_DIRECTORIES'):
            match = re.search(r'^#define ' + name + r' .*$', code, re.M)
            self.assertIsNotNone(match, name + ' must be defined')
            pieces.append(match.group(0))
        match = re.search(r'^#define TREE_MASK .*?\n(?:\t.*\n)*', code, re.M)
        self.assertIsNotNone(match, 'TREE_MASK must be defined')
        pieces.append(match.group(0))
        for name in ('descriptor', 'path_watch', 'event'):
            match = re.search(r'struct ' + name + r' \{.*?\n\};', code, re.S)
            self.assertIsNotNone(match, name + ' declaration must be present')
            pieces.append(match.group(0))
        pieces.append('static struct descriptor *descriptors;\nstatic struct event *events;')
        functions = []
        for name in ('allocate', 'copy_string', 'pulse_watchpaths',
                     'descriptor_acquire', 'descriptor_release', 'tree_add',
                     'tree_contains', 'watch_release', 'watch_tree', 'parent_path',
                     'component_below', 'watch_rebuild', 'same_snapshot',
                     'refresh_path', 'handle_notification', 'drain_notifications',
                     'directory_nonempty', 'pathstate_matches', 'add_path',
                     'new_event', 'free_event', 'reconcile_events', 'evaluate_events'):
            signature = re.search(r'^static [^\n]+\b' + name + r'\s*\([^;{}]*\)\s*\{',
                                  code, re.M)
            self.assertIsNotNone(signature, name + ' definition must be present')
            functions.append(signature.group(0) + function_body(code, name) + '}')
        harness = (HOST_HARNESS
                   .replace('/* MONITOR_DECLARATIONS */', '\n'.join(pieces))
                   .replace('/* MONITOR_FUNCTIONS */', '\n\n'.join(functions)))
        with tempfile.TemporaryDirectory(prefix='fsevents-host-') as folder:
            directory = Path(folder)
            source, binary = directory / 'check.c', directory / 'check'
            source.write_text(harness)
            build = subprocess.run([compiler, '-std=gnu11', '-fblocks', '-Wall', '-Werror',
                                    '-Wno-unused-parameter', '-Wno-unused-function',
                                    str(source), '-o', str(binary)],
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], cwd=directory, capture_output=True,
                                    text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('FSEVENTS_HOST_PASS', result.stdout)


HOST_HARNESS = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/inotify.h>
#include <time.h>
#include <unistd.h>
#define st_mtimespec st_mtim
#define st_ctimespec st_ctim

static int notify = -1;

/* XPC fixture objects are stack-owned; apply callbacks are synchronous. */
void *_NSConcreteStackBlock[32];
void _Block_object_assign(void *a, const void *b, int c) { abort(); }
void _Block_object_dispose(const void *a, int b) { }
struct object;
typedef struct object *xpc_object_t;
struct entry { const char *key; xpc_object_t value; };
struct object {
    int type, refs;
    bool boolean;
    uint64_t number;
    const char *string;
    size_t count;
    xpc_object_t *items;
    struct entry *entries;
};
enum { XPC_TYPE_BOOL=1, XPC_TYPE_ARRAY, XPC_TYPE_DICTIONARY, STRING, NUMBER };
static int xpc_get_type(xpc_object_t o) { return o ? o->type : 0; }
static bool xpc_bool_get_value(xpc_object_t o) { assert(o->type == XPC_TYPE_BOOL); return o->boolean; }
static xpc_object_t xpc_dictionary_get_value(xpc_object_t o, const char *key) {
    if (!o) return NULL;
    for (size_t i=0; i<o->count; ++i) if (!strcmp(o->entries[i].key,key)) return o->entries[i].value;
    return NULL;
}
static size_t xpc_array_get_count(xpc_object_t o) { return o->count; }
static xpc_object_t xpc_array_get_value(xpc_object_t o, size_t i) { assert(i<o->count); return o->items[i]; }
static uint64_t xpc_array_get_uint64(xpc_object_t o, size_t i) { return xpc_array_get_value(o,i)->number; }
static const char *xpc_array_get_string(xpc_object_t o, size_t i) { return xpc_array_get_value(o,i)->string; }
static xpc_object_t xpc_retain(xpc_object_t o) { ++o->refs; return o; }
static void xpc_release(xpc_object_t o) { --o->refs; assert(o->refs>=0); }
static bool xpc_equal(xpc_object_t a, xpc_object_t b) { return a == b; }
static bool xpc_dictionary_apply(xpc_object_t o, bool (^apply)(const char *, xpc_object_t)) {
    for(size_t i=0; i<o->count; ++i) if(!apply(o->entries[i].key,o->entries[i].value)) return false;
    return true;
}

/* launchd fixture: records every SET_STATE. */
static struct { uint64_t token; bool state; } updates[256];
static size_t update_count;
static bool set_state(uint64_t token, bool state) {
    assert(update_count<256); updates[update_count].token=token; updates[update_count++].state=state;
    return true;
}
/* MONITOR_DECLARATIONS */
/* MONITOR_FUNCTIONS */

static double clock_time = 100;

/* Deliver queued inotify events, then let the coalescing window elapse. */
static void settle(void) {
    drain_notifications();
    evaluate_events(clock_time, false);
    clock_time += COALESCE_SECONDS + 0.01;
    evaluate_events(clock_time, false);
    clock_time += 0.01;
}
static size_t pulses(uint64_t token) {
    size_t n = 0;
    for (size_t i = 0; i < update_count; ++i) {
        if (updates[i].token != token || !updates[i].state) continue;
        assert(i + 1 < update_count && updates[i+1].token == token && !updates[i+1].state);
        ++n;
    }
    return n;
}
static void write_file(const char *path) {
    int fd=open(path,O_CREAT|O_WRONLY|O_APPEND,0600);
    assert(fd>=0); assert(write(fd,"x",1)==1); close(fd);
}
static void touch(const char *path) {
    int fd=open(path,O_CREAT|O_WRONLY,0600); assert(fd>=0); close(fd);
    assert(utimensat(AT_FDCWD, path, NULL, 0)==0);
}
struct desc { struct object path_items[2], array; xpc_object_t items[2]; struct entry entry; struct object dict; };
static xpc_object_t watchpaths(struct desc *d, const char *a, const char *b) {
    memset(d, 0, sizeof(*d));
    d->path_items[0] = (struct object){.type=STRING,.string=a};
    d->path_items[1] = (struct object){.type=STRING,.string=b};
    d->items[0] = &d->path_items[0]; d->items[1] = &d->path_items[1];
    d->array = (struct object){.type=XPC_TYPE_ARRAY,.count=b?2:1,.items=d->items};
    d->entry = (struct entry){"WatchPaths",&d->array};
    d->dict = (struct object){.type=XPC_TYPE_DICTIONARY,.count=1,.entries=&d->entry};
    return &d->dict;
}
static void register_events(size_t count, const uint64_t *tokens, xpc_object_t *descriptions) {
    struct object numbers[8]; xpc_object_t items[16];
    assert(count <= 8);
    for (size_t i = 0; i < count; ++i) {
        numbers[i] = (struct object){.type=NUMBER,.number=tokens[i]};
        items[2*i] = &numbers[i]; items[2*i+1] = descriptions[i];
    }
    struct object registrations={.type=XPC_TYPE_ARRAY,.count=2*count,.items=items};
    reconcile_events(&registrations);
}
static size_t descriptor_count(void) {
    size_t n = 0; for (struct descriptor *d = descriptors; d; d = d->next) ++n; return n;
}

int main(void) {
    notify = inotify_init1(IN_NONBLOCK);
    assert(notify >= 0);

    /* --- A watched directory tree --------------------------------------- */
    assert(mkdir("w",0700)==0 && mkdir("w/sub",0700)==0 && mkdir("w/sub/inner",0700)==0);
    struct desc tree; xpc_object_t d1 = watchpaths(&tree, "w", NULL);
    uint64_t t1 = 1; register_events(1, &t1, &d1);
    settle();
    /* The provider only clears state at load; launchd runs a first registration. */
    assert(pulses(1) == 0);

    touch("w/a"); settle();                       assert(pulses(1) == 1); /* new file */
    write_file("w/a"); settle();                  assert(pulses(1) == 2); /* append */
    assert(utimensat(AT_FDCWD, "w", NULL, 0)==0); settle(); assert(pulses(1) == 3); /* dir mtime */
    touch("w/sub/inner/deep"); settle();          assert(pulses(1) == 4); /* two levels down */
    for (int i = 0; i < 5; ++i) { char p[32]; snprintf(p, sizeof p, "w/b%d", i); touch(p); }
    settle();                                     assert(pulses(1) == 5); /* burst -> once */
    touch("w/c"); settle(); touch("w/d"); settle(); assert(pulses(1) == 7); /* separate changes */
    /* A directory created later is watched too. */
    assert(mkdir("w/sub/new",0700)==0); settle(); assert(pulses(1) == 8);
    touch("w/sub/new/file"); settle();            assert(pulses(1) == 9);
    /* Rename the whole watched directory away and back. */
    assert(rename("w","w-away")==0); settle();    assert(pulses(1) == 10);
    touch("w-away/ignored"); settle();            assert(pulses(1) == 10);
    assert(rename("w-away","w")==0); settle();    assert(pulses(1) == 11);
    touch("w/sub/inner/again"); settle();         assert(pulses(1) == 12);
    settle(); settle();                           assert(pulses(1) == 12); /* idle */

    /* --- A watched path that does not exist yet ------------------------- */
    struct desc missing; xpc_object_t d2 = watchpaths(&missing, "m/later", NULL);
    uint64_t two[2] = {1, 2}; xpc_object_t both[2] = {d1, d2};
    register_events(2, two, both);
    assert(tree.dict.refs == 1);                  /* unchanged token kept */
    settle(); assert(pulses(2) == 0);
    assert(mkdir("m",0700)==0); settle();         assert(pulses(2) == 0); /* sibling only */
    assert(mkdir("m/later",0700)==0); settle();   assert(pulses(2) == 1); /* created */
    touch("m/later/x"); settle();                 assert(pulses(2) == 2); /* inside it */
    touch("m/later/x"); settle();                 assert(pulses(2) == 3);
    assert(unlink("m/later/x")==0 && rmdir("m/later")==0); settle(); assert(pulses(2) == 4); /* deleted */
    assert(mkdir("m/later",0700)==0); settle();   assert(pulses(2) == 5); /* recreated */
    touch("m/later/y"); settle();                 assert(pulses(2) == 6);
    touch("m/unrelated"); settle();               assert(pulses(2) == 6);
    assert(pulses(1) == 12);

    /* --- Symlink, file target, and two jobs on one path ------------------ */
    assert(mkdir("real",0700)==0 && symlink("real","link")==0);
    touch("file");
    struct desc link, file, twin_a, twin_b;
    xpc_object_t descs[4] = { watchpaths(&link, "link", NULL), watchpaths(&file, "file", NULL),
                              watchpaths(&twin_a, "w/sub", NULL), watchpaths(&twin_b, "w/sub", NULL) };
    uint64_t tokens[4] = {3, 4, 5, 6};
    register_events(4, tokens, descs);
    assert(tree.dict.refs == 0 && missing.dict.refs == 0); /* removed tokens released */
    settle();
    touch("real/t"); settle();                    assert(pulses(3) == 1); /* follows the link */
    write_file("file"); settle();                 assert(pulses(4) == 1); /* single file */
    touch("w/sub/t"); settle();                   assert(pulses(5) == 1 && pulses(6) == 1);
    /* Unloading one twin must not remove the shared inotify descriptor. */
    xpc_object_t remaining[3] = { descs[0], descs[1], descs[3] };
    uint64_t remaining_tokens[3] = {3, 4, 6};
    register_events(3, remaining_tokens, remaining);
    touch("w/sub/u"); settle();                   assert(pulses(6) == 2 && pulses(5) == 1);

    /* --- A FIFO must not block the provider ------------------------------ */
    assert(mkfifo("fifo", 0600) == 0);
    struct desc fifo; xpc_object_t d7 = watchpaths(&fifo, "fifo", NULL);
    uint64_t t7 = 7; register_events(1, &t7, &d7);   /* would hang if opened */
    settle(); assert(pulses(7) == 0);

    /* --- QueueDirectories and PathState are levels ----------------------- */
    struct object yes={.type=XPC_TYPE_BOOL,.boolean=true}, no={.type=XPC_TYPE_BOOL,.boolean=false};
    assert(mkdir("queue",0700)==0);
    struct object qpath={.type=STRING,.string="queue"}; xpc_object_t qitems[]={&qpath};
    struct object qarray={.type=XPC_TYPE_ARRAY,.count=1,.items=qitems};
    struct entry qentry={"QueueDirectories",&qarray};
    struct object qdesc={.type=XPC_TYPE_DICTIONARY,.count=1,.entries=&qentry};
    struct entry pentries[]={{"flag",&yes},{"absent",&no}};
    struct object pdict={.type=XPC_TYPE_DICTIONARY,.count=1,.entries=pentries};
    struct entry pentry={"PathState",&pdict};
    struct object pdesc={.type=XPC_TYPE_DICTIONARY,.count=1,.entries=&pentry};
    xpc_object_t levels[2]={&qdesc,&pdesc}; uint64_t ltokens[2]={8,9};
    register_events(2, ltokens, levels);
    update_count = 0; settle();
    bool q = true, p = true;
    for (size_t i = 0; i < update_count; ++i) {
        if (updates[i].token == 8) q = updates[i].state;
        if (updates[i].token == 9) p = updates[i].state;
    }
    assert(!q && !p);
    touch("queue/1"); touch("flag"); settle();
    for (size_t i = 0; i < update_count; ++i) {
        if (updates[i].token == 8) q = updates[i].state;
        if (updates[i].token == 9) p = updates[i].state;
    }
    assert(q && p);
    size_t before = update_count; settle(); assert(update_count == before); /* no repeats */
    assert(unlink("queue/1")==0 && unlink("flag")==0); settle();
    for (size_t i = before; i < update_count; ++i) {
        if (updates[i].token == 8) q = updates[i].state;
        if (updates[i].token == 9) p = updates[i].state;
    }
    assert(!q && !p);
    assert(pathstate_matches("missing/child", &no));   /* ENOENT is absence */

    /* --- Everything released on unload ----------------------------------- */
    register_events(0, NULL, NULL);
    assert(events == NULL && descriptor_count() == 0);
    assert(qdesc.refs == 0 && pdesc.refs == 0 && fifo.dict.refs == 0);
    puts("FSEVENTS_HOST_PASS");
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
