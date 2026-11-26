"""Source regressions for launchd's filesystem event provider.

These checks read the distributed patch, so host-only CI never starts Darling
or a client. Runtime delivery still needs a separate guest verification.
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


class FSEventsMonitorTests(unittest.TestCase):
    def test_daemon_source_is_distributed(self):
        self.assertTrue(additions(SOURCE), 'the patch must contain UserEventAgent')

    def test_event_provider_checks_in_on_filesystem_stream(self):
        code = additions(SOURCE)
        self.assertIn('com.apple.fsevents.matching', code)
        self.assertIn('XPC_EVENT_PROVIDER_CHECK_IN', code)
        self.assertIn('XPC_EVENT_ROUTINE_KEY_STREAM', code)
        self.assertIn('XPC_EVENT_ROUTINE_KEY_EVENTS', code)

    def test_state_updates_use_the_event_token_and_boolean_state(self):
        code = additions(SOURCE)
        self.assertIn('XPC_EVENT_PROVIDER_SET_STATE', code)
        self.assertIn('XPC_EVENT_ROUTINE_KEY_TOKEN', code)
        self.assertIn('XPC_EVENT_ROUTINE_KEY_STATE', code)
        self.assertIn('xpc_dictionary_set_uint64', code)
        self.assertIn('xpc_dictionary_set_bool', code)

    def test_signal_rechecks_provider_registration(self):
        code = additions(SOURCE)
        self.assertIn('SIGUSR1', code)
        self.assertIn('EVFILT_SIGNAL', code)
        main = function_body(code, 'main')
        self.assertRegex(main, r'(?s)notification->ident == SIGUSR1\).*?recheck = true;')
        self.assertRegex(main, r'if\s*\(recheck\)\s*provider_check_in\(')

    def test_reconciles_tokens_without_discarding_existing_watch_state(self):
        code = additions(SOURCE)
        self.assertIn('xpc_array_get_uint64', code)
        self.assertIn('xpc_array_get_value', code)
        reconcile = function_body(code, 'reconcile_events')
        self.assertTrue(reconcile, 'registration refresh must reconcile tokens')
        self.assertIn('xpc_equal', reconcile)
        self.assertIn('close(', code)

    def test_watches_paths_and_recovers_missing_or_replaced_paths(self):
        code = additions(SOURCE)
        for flag in ('EVFILT_VNODE', 'NOTE_WRITE', 'NOTE_DELETE', 'NOTE_RENAME'):
            self.assertIn(flag, code)
        self.assertRegex(code, r'(?i)parent|ancestor')
        self.assertIn('ENOENT', code)

    def test_watchpaths_are_coalesced_and_pulsed(self):
        code = additions(SOURCE)
        self.assertIn('WatchPaths', code)
        self.assertRegex(code, r'(?i)coalesc|latency')
        # An event pulse must return false after true; an asserted level would
        # keep relaunching a WatchPaths job after it exits.
        pulse = function_body(code, 'pulse_watchpaths')
        self.assertRegex(pulse, r'(?s)set_state\([^;]*,\s*true\s*\).*?set_state\([^;]*,\s*false\s*\)')

    def test_queuedirectories_and_pathstate_are_levels(self):
        code = additions(SOURCE)
        self.assertIn('QueueDirectories', code)
        self.assertIn('PathState', code)
        directory = function_body(code, 'directory_nonempty')
        self.assertIn('opendir(', directory)
        self.assertIn('readdir(', directory)
        pathstate = function_body(code, 'pathstate_matches')
        self.assertIn('stat(', pathstate)
        self.assertIn('xpc_bool_get_value', pathstate)

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
        self.assertEqual(plist['ProgramArguments'][0], '/usr/libexec/UserEventAgent')
        cmake = additions(DIRECTORY + 'CMakeLists.txt')
        self.assertIn('add_darling_executable(UserEventAgent', cmake)
        self.assertIn('libexec', cmake)
        self.assertIn('LaunchDaemons', cmake)
        self.assertIs(plist.get('KeepAlive'), True)
        self.assertIn('add_subdirectory(UserEventAgent)', additions('src/CMakeLists.txt'))

    def test_watch_filters_are_deleted_before_releasing_their_fd(self):
        code = additions(SOURCE)
        for name, filter_name in (('close_watch', 'EVFILT_VNODE'),
                                  ('close_parent_watch', 'EVFILT_READ')):
            with self.subTest(helper=name):
                close = function_body(code, name)
                self.assertIn(filter_name, close)
                self.assertIn('EV_DELETE', close)
                self.assertIn('close(fd)', close)
                self.assertLess(close.index('EV_DELETE'), close.index('close(fd)'))
                self.assertIn(name + '(', function_body(code, 'free_event'))

    def test_parent_notifications_include_transient_child_creation_and_removal(self):
        code = additions(SOURCE)
        parent = function_body(code, 'watch_parent')
        self.assertTrue(parent, 'parent watcher must receive named child events')
        for mask in ('IN_CREATE', 'IN_DELETE', 'IN_MOVED_FROM', 'IN_MOVED_TO'):
            self.assertIn(mask, parent)
        drain = function_body(code, 'drain_parent')
        self.assertTrue(drain, 'queued parent child events must be drained')
        self.assertIn('name', drain)
        self.assertIn('changed', drain)
        main = function_body(code, 'main')
        self.assertIn('EVFILT_READ', main)
        self.assertIn('drain_parent(', main)

    def test_host_filesystem_pulses_levels_and_token_reconciliation(self):
        compiler = next((path for name in ('clang', 'clang-20', 'clang-19',
                                           'clang-18', 'clang-17', 'clang-16',
                                           'clang-15', 'clang-14')
                         if (path := shutil.which(name))), None)
        if not compiler:
            self.skipTest('clang with C blocks support required')
        code = additions(SOURCE)
        self.assertTrue(code, 'monitor source must be distributed')
        declarations = []
        for name in ('path_watch', 'event'):
            match = re.search(r'struct ' + name + r' \{.*?\n\};', code, re.S)
            self.assertIsNotNone(match, name + ' declaration must be present')
            declarations.append(match.group(0))
        declarations.append('static struct event *events;')
        functions = []
        for name in ('allocate', 'copy_string', 'pulse_watchpaths',
                     'directory_nonempty', 'pathstate_matches', 'watch_parent',
                     'close_parent_watch', 'parent_affects_path', 'drain_parent', 'parent_path',
                     'same_node', 'same_snapshot', 'refresh_path', 'add_path',
                     'new_event', 'free_event', 'reconcile_events', 'evaluate_events'):
            signature = re.search(r'^static [^\n]+\b' + name +
                                  r'\s*\([^;{}]*\)\s*\{', code, re.M)
            self.assertIsNotNone(signature, name + ' definition must be present')
            functions.append(signature.group(0) + function_body(code, name) + '}')
        harness = HOST_HARNESS.replace('/* MONITOR_DECLARATIONS */',
                                       '\n'.join(declarations)).replace(
                                           '/* MONITOR_FUNCTIONS */', '\n\n'.join(functions))
        with tempfile.TemporaryDirectory(prefix='fsevents-host-') as folder:
            directory = Path(folder)
            source, binary = directory / 'check.c', directory / 'check'
            source.write_text(harness)
            build = subprocess.run([compiler, '-std=c11', '-fblocks', '-Wall',
                                    '-Werror', '-Wno-unused-parameter', str(source),
                                    '-o', str(binary)], capture_output=True,
                                   text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], cwd=directory, capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('FSEVENTS_HOST_PASS', result.stdout)


HOST_HARNESS = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
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

/* Registration is the sole kqueue boundary here. Parent notifications still
 * come from real Linux inotify descriptors through the production helpers. */
#define EVFILT_READ -1
#define EV_ADD 1
#define EV_DELETE 2
#define EV_CLEAR 4
struct kevent { uintptr_t ident; int filter, flags; void *udata; };
#define EV_SET(ke, id, fil, fl, ff, data, user) \
    (*(ke) = (struct kevent){(uintptr_t)(id), (fil), (fl), (user)})
static int queue;
static unsigned parent_registrations;
static int kevent(int kq, const struct kevent *changes, int nchanges,
                  struct kevent *out, int nout, const struct timespec *timeout) {
    assert(nchanges==1 && changes->filter==EVFILT_READ);
    if (changes->flags & EV_ADD) ++parent_registrations;
    if (changes->flags & EV_DELETE) {
        assert(parent_registrations>0);
        --parent_registrations;
    }
    return 0;
}

/* XPC fixture objects are stack-owned. Apply callbacks are synchronous, as in
 * libxpc; no block is copied or retained by this fixture. */
void *_NSConcreteStackBlock[32];
void _Block_object_assign(void *a, const void *b, int c) { abort(); }
void _Block_object_dispose(const void *a, int b) { assert(b == 8); }
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
static double clock_time;
static double now(void) { return clock_time; }
static struct { uint64_t token; bool state; } updates[64];
static size_t update_count;
static bool set_state(uint64_t token, bool state) {
    assert(update_count<64); updates[update_count].token=token; updates[update_count++].state=state;
    return true;
}
/* MONITOR_DECLARATIONS */
static unsigned open_watches;
static int watch_path(const char *path, struct path_watch *watch) {
    (void)watch;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd >= 0) ++open_watches;
    return fd;
}
static void close_watch(int fd) {
    assert(fd>=0 && open_watches>0);
    assert(close(fd)==0);
    --open_watches;
}
/* MONITOR_FUNCTIONS */
static void register_one(uint64_t token, xpc_object_t description) {
    struct object number={.type=NUMBER,.number=token};
    xpc_object_t items[]={&number,description};
    struct object registrations={.type=XPC_TYPE_ARRAY,.count=2,.items=items};
    reconcile_events(&registrations);
}
static void clear_events(void) {
    struct object registrations={.type=XPC_TYPE_ARRAY};
    reconcile_events(&registrations);
    assert(events==NULL);
}
static void write_file(const char *path) {
    int fd=open(path,O_CREAT|O_WRONLY|O_APPEND,0600);
    assert(fd>=0); assert(write(fd,"x",1)==1); close(fd);
}
static void assert_update(size_t i, uint64_t token, bool state) {
    assert(i<update_count); assert(updates[i].token==token); assert(updates[i].state==state);
}
int main(void) {
    struct object yes={.type=XPC_TYPE_BOOL,.boolean=true};
    struct object no={.type=XPC_TYPE_BOOL,.boolean=false};
    struct object invalid={.type=STRING,.string="false"};
    assert(!directory_nonempty("missing"));
    assert(mkdir("queue",0700)==0);
    assert(!directory_nonempty("queue"));
    assert(pathstate_matches("missing",&no));
    assert(!pathstate_matches("missing",&yes));
    assert(!pathstate_matches("missing",&invalid));
    write_file("plain");
    assert(pathstate_matches("plain/child",&no)); /* ENOTDIR is absence. */
    assert(pathstate_matches("plain",&yes));
    assert(!directory_nonempty("plain"));
    char *parent=parent_path("/a/b/"); assert(!strcmp(parent,"/a")); free(parent);
    parent=parent_path("/a"); assert(!strcmp(parent,"/")); free(parent);
    parent=parent_path("a"); assert(!strcmp(parent,".")); free(parent);

    struct object watched_path={.type=STRING,.string="tree/missing/item"};
    xpc_object_t watch_items[]={&watched_path};
    struct object watch_array={.type=XPC_TYPE_ARRAY,.count=1,.items=watch_items};
    struct entry watch_entries[]={{"WatchPaths",&watch_array}};
    struct object watch_desc={.type=XPC_TYPE_DICTIONARY,.count=1,.entries=watch_entries};
    register_one(11,&watch_desc);
    assert(watch_desc.refs==1);
    assert(events->paths && !strcmp(events->paths->parent,"."));
    clock_time=100; evaluate_events();
    assert(update_count==1); assert_update(0,11,false); /* No pulse on load. */
    assert(mkdir("tree",0700)==0); assert(mkdir("tree/missing",0700)==0);
    write_file("tree/missing/item");
    evaluate_events();
    assert(events->pending==101); assert(update_count==1);
    struct event *preserved=events;
    register_one(11,&watch_desc);
    assert(events==preserved && events->pending==101 && watch_desc.refs==1);
    clock_time=100.5; write_file("tree/missing/item"); evaluate_events();
    assert(events->pending==101 && update_count==1); /* A burst cannot postpone forever. */
    clock_time=101; evaluate_events();
    assert(update_count==3); assert_update(1,11,true); assert_update(2,11,false);
    assert(events->pending==0);
    clock_time=102; evaluate_events(); assert(update_count==3);
    write_file("tree/missing/unrelated"); evaluate_events(); assert(events->pending==0);
    assert(unlink("tree/missing/item")==0); evaluate_events(); assert(events->pending==103);
    clock_time=103; evaluate_events(); assert(update_count==5);
    assert_update(3,11,true); assert_update(4,11,false);
    write_file("tree/missing/item"); evaluate_events(); assert(events->pending==104);
    clock_time=104; evaluate_events(); assert(update_count==7);
    assert_update(5,11,true); assert_update(6,11,false);

    struct object queue_path={.type=STRING,.string="queue"};
    xpc_object_t queue_items[]={&queue_path};
    struct object queue_array={.type=XPC_TYPE_ARRAY,.count=1,.items=queue_items};
    struct entry queue_entries[]={{"QueueDirectories",&queue_array}};
    struct object queue_desc={.type=XPC_TYPE_DICTIONARY,.count=1,.entries=queue_entries};
    register_one(22,&queue_desc); /* Removed token releases its watches/description. */
    assert(watch_desc.refs==0 && queue_desc.refs==1);
    update_count=0; evaluate_events(); assert_update(0,22,false);
    write_file("queue/item"); evaluate_events(); assert(directory_nonempty("queue"));
    assert(update_count==2); assert_update(1,22,true);
    evaluate_events(); assert(update_count==2); /* Level remains true, no pulse. */
    assert(unlink("queue/item")==0); evaluate_events(); assert(update_count==3);
    assert_update(2,22,false);

    struct entry path_entries[]={{"present",&yes},{"absent",&no}};
    struct object path_dict={.type=XPC_TYPE_DICTIONARY,.count=2,.entries=path_entries};
    struct entry state_entries[]={{"PathState",&path_dict}};
    struct object state_desc={.type=XPC_TYPE_DICTIONARY,.count=1,.entries=state_entries};
    register_one(22,&state_desc); /* Same token, changed definition must be replaced. */
    assert(queue_desc.refs==0 && state_desc.refs==1 && !events->watchpaths);
    update_count=0; evaluate_events(); assert_update(0,22,true); /* Any entry matches. */
    write_file("absent"); evaluate_events(); assert_update(1,22,false);
    write_file("present"); evaluate_events(); assert_update(2,22,true);
    evaluate_events(); assert(update_count==3);
    assert(unlink("present")==0); evaluate_events(); assert_update(3,22,false);
    clear_events(); assert(state_desc.refs==0 && open_watches==0 && parent_registrations==0);

    /* A child can appear and disappear entirely between periodic stat scans.
     * Run the actual parent-inotify reader, not a synthetic changed flag. */
    assert(mkdir("transient",0700)==0);
    watched_path.string="transient/target";
    register_one(33,&watch_desc);
    clock_time=200; update_count=0; evaluate_events();
    assert_update(0,33,false);
    struct path_watch *watch=events->paths;
    int parent_fd=watch->parent_fd;
    assert(parent_fd>=0);
    write_file("transient/target"); assert(unlink("transient/target")==0);
    assert(!pathstate_matches("transient/target",&yes));
    evaluate_events(); /* Unchanged parent watch must retain queued events. */
    assert(watch->parent_fd==parent_fd && events->pending==0);
    drain_parent(watch); assert(watch->changed);
    evaluate_events(); assert(events->pending==201);
    clock_time=201; evaluate_events();
    assert(update_count==3); assert_update(1,33,true); assert_update(2,33,false);
    write_file("transient/unrelated"); assert(unlink("transient/unrelated")==0);
    drain_parent(watch); assert(!watch->changed);
    evaluate_events(); assert(events->pending==0 && update_count==3);
    write_file("transient/other"); drain_parent(watch); assert(!watch->changed);
    assert(rename("transient/other","transient/target")==0);
    assert(rename("transient/target","transient/other")==0);
    drain_parent(watch); assert(watch->changed);
    evaluate_events(); assert(events->pending==202);
    clock_time=202; evaluate_events(); assert(update_count==5);
    assert_update(3,33,true); assert_update(4,33,false);
    clear_events(); assert(open_watches==0 && parent_registrations==0);

    /* The nearest existing ancestor must notice a newly created component;
     * once deeper directories exist, subsequent scans move the parent watch. */
    watched_path.string="transient/missing/deeper/target";
    register_one(44,&watch_desc);
    watch=events->paths;
    assert(!strcmp(watch->parent,"transient"));
    assert(parent_affects_path(watch,"missing"));
    assert(!parent_affects_path(watch,"unrelated"));
    clock_time=300; update_count=0; evaluate_events();
    assert(mkdir("transient/missing",0700)==0);
    assert(mkdir("transient/missing/deeper",0700)==0);
    write_file("transient/missing/deeper/target");
    assert(unlink("transient/missing/deeper/target")==0);
    drain_parent(watch); assert(watch->changed);
    evaluate_events(); assert(events->pending==301);
    assert(!strcmp(watch->parent,"transient/missing/deeper"));
    clock_time=301; evaluate_events(); assert(update_count==3);
    assert(rename("transient/missing/deeper","transient/missing/moved")==0);
    drain_parent(watch); assert(watch->changed); /* IN_MOVE_SELF. */
    evaluate_events(); assert(events->pending==302);
    assert(!strcmp(watch->parent,"transient/missing"));
    clear_events(); assert(watch_desc.refs==0 && open_watches==0 && parent_registrations==0);
    puts("FSEVENTS_HOST_PASS filesystem pulse levels reconciliation");
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
