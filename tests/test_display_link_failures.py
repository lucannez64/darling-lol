"""Compile shipped display-link code with descriptor, I/O and pump failures."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_runtime_compatibility as runtime


class DisplayLinkFailureChecks(unittest.TestCase):
    def test_interrupted_lock_and_failed_pump_start(self):
        if not shutil.which('c++'):
            self.skipTest('C++ compiler required')
        source = runtime.CompatibilityChecks().patched(
            'darling', 'src/frameworks/CoreVideo/src/CVDisplayLink.mm')
        guard_start = source.index('static __thread int callbackClockFD')
        guard_end = source.index('\n};', source.index('struct CVStateGuard {', guard_start)) + 3
        bodies = source[guard_start:guard_end]
        for name in ('readHeader', 'slotOffset', 'readState', 'writeState'):
            bodies += runtime.function(source, name)
        bodies += runtime.function(source, 'CVDisplayLinkStop')
        release = runtime.function(source, 'CVDisplayLinkRelease')
        bodies += release.replace('[(NSMutableDictionary*) displayLink release];',
                                  'releaseObject(displayLink);')
        bodies += runtime.function(source, 'InstallDispatchPump')
        bodies += runtime.function(source, 'CVDisplayLinkStart')
        harness = r'''
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <pthread.h>
#include <sys/types.h>
#include <string.h>
using CVDisplayLinkRef = void *;
using CVReturn = int;
constexpr int kCVReturnSuccess = 0, kCVReturnError = -1;
constexpr int kCVReturnInvalidArgument = -2, LOCK_EX = 2, LOCK_UN = 8;
// RECORD_DEFINITION
static CVLinkStateRec records[2];
static CVLinkStateRec &state = records[1];
static const uint32_t kStateMagic = 0xc81dfa10;
static int open_fds, fd_limit=100, releases;
static bool read_failure, write_failure;
static void releaseObject(CVDisplayLinkRef) { ++releases; }

static int lock_calls, unlock_calls, closes, lock_mode, creates;
static int clockOpen() { if(open_fds>=fd_limit)return -1; ++open_fds; return 42; }
static void clockClose(int) { ++closes; --open_fds; }
static long clockRead(int, void *out, size_t size, off_t offset) {
    if (read_failure) return -5;
    assert(offset >= 0 && offset + size <= sizeof(records));
    memcpy(out, (char *)records + offset, size); return size;
}
static long clockWrite(int, const void *in, size_t size, off_t offset) {
    if (write_failure) return -5;
    assert(offset >= 0 && offset + size <= sizeof(records));
    memcpy((char *)records + offset, in, size); return size;
}
static int clockFlock(int, int operation) {
    if (operation == LOCK_UN) { ++unlock_calls; return 0; }
    ++lock_calls;
    if (lock_mode == 1) return lock_calls == 1 ? -4 : 0;
    if (lock_mode == 2) return -9;
    return 0;
}
static int fake_create(pthread_t *thread, const pthread_attr_t *,
                       void *(*)(void *), void *) {
    *thread = (pthread_t)1;
    return ++creates == 1 ? 11 : 0;
}
static int fake_detach(pthread_t) { return 0; }
static void *displayClockThread(void *) { return nullptr; }
static void installFrameSinkBridge() {}
#define pthread_create fake_create
#define pthread_detach fake_detach
'''
        main = r'''
int main() {
    records[0].magic=kStateMagic; records[0].version=2; records[0].refs=1;
    state.link=(void *)1; state.alive=1; state.refs=1;
    lock_mode=1;
    { CVStateGuard guard; assert(guard.fd == 42 && guard.locked); }
    assert(lock_calls == 2 && unlock_calls == 1 && closes == 1);
    lock_mode=2; lock_calls=unlock_calls=closes=0;
    { CVStateGuard guard; assert(guard.fd == -1 && !guard.locked); }
    assert(lock_calls == 1 && unlock_calls == 0 && closes == 1);
    // A failed lock must prevent record access and thread creation.
    assert(CVDisplayLinkStart((void *)1) == kCVReturnError);
    assert(state.running == 0 && creates == 0);
    lock_mode=0; lock_calls=unlock_calls=closes=0;
    fd_limit=1;
    {
        CVStateGuard outer;
        assert(outer.fd == 42 && outer.locked && !outer.borrowed);
        int calls=lock_calls;
        {
            CVCallbackScope scope(outer.fd);
            CVStateGuard inner;
            assert(inner.fd == outer.fd && inner.borrowed && !inner.locked);
            assert(open_fds == 1 && lock_calls == calls);
            assert(CVDisplayLinkStop((void *)1) == kCVReturnSuccess);
            assert(open_fds == 1 && lock_calls == calls);
        }
        assert(callbackClockFD == -1 && open_fds == 1 && unlock_calls == 0);
    }
    assert(open_fds == 0 && closes == 1 && unlock_calls == 1);
    assert(CVDisplayLinkStart((void *)1) == kCVReturnError);
    assert(state.running == 0 && creates == 1);
    assert(CVDisplayLinkStart((void *)1) == kCVReturnSuccess);
    assert(state.running == 1 && creates == 2);
    assert(CVDisplayLinkStart((void *)1) == kCVReturnSuccess);
    assert(creates == 2);
    fd_limit=1; // The guard owns the only available descriptor.
    assert(CVDisplayLinkStop((void *)1) == kCVReturnSuccess);
    assert(state.running == 0 && open_fds == 0);
    state.running=1; read_failure=true;
    assert(CVDisplayLinkStop((void *)1) == kCVReturnError);
    CVDisplayLinkRelease((void *)1);
    assert(releases == 0 && state.refs == 1 && state.alive && state.running);
    read_failure=false; write_failure=true;
    assert(CVDisplayLinkStop((void *)1) == kCVReturnError);
    CVDisplayLinkRelease((void *)1);
    assert(releases == 0 && state.refs == 1 && state.alive && state.running);
    write_failure=false;
    CVDisplayLinkRelease((void *)1);
    assert(releases == 1 && state.refs == 0 && !state.alive && open_fds == 0);
}
'''
        record_start = source.index('struct CVLinkStateRec {')
        record_end = source.index('\n};', record_start) + 3
        harness = harness.replace('// RECORD_DEFINITION', source[record_start:record_end])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'probe.cc').write_text(harness + bodies + main)
            subprocess.run(['c++', '-std=c++11', '-pthread', str(path / 'probe.cc'),
                            '-o', str(path / 'probe')], check=True, capture_output=True)
            subprocess.run([str(path / 'probe')], check=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
