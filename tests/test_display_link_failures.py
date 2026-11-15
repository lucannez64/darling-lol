"""Compile shipped clock guard/start code with lock and thread failure injection."""
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
        guard_start = source.index('struct CVStateGuard {')
        guard_end = source.index('\n};', guard_start) + 3
        bodies = source[guard_start:guard_end]
        bodies += runtime.function(source, 'InstallDispatchPump')
        bodies += runtime.function(source, 'CVDisplayLinkStart')
        harness = r'''
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <pthread.h>
using CVDisplayLinkRef = void *;
using CVReturn = int;
constexpr int kCVReturnSuccess = 0, kCVReturnError = -1;
constexpr int kCVReturnInvalidArgument = -2, LOCK_EX = 2, LOCK_UN = 8;
struct CVLinkStateRec { uint64_t reserved; int running; };
static CVLinkStateRec state = {};
static int lock_calls, unlock_calls, closes, lock_mode, creates;
static uint64_t owner;
static int clockOpen() { return 42; }
static void clockClose(int) { ++closes; }
static long clockRead(int, void *out, size_t, size_t) {
    *(uint64_t *)out = owner; return sizeof(owner);
}
static int clockFlock(int, int operation) {
    if (operation == LOCK_UN) { ++unlock_calls; return 0; }
    ++lock_calls;
    if (lock_mode == 1) return lock_calls == 1 ? -4 : 0;
    if (lock_mode == 2) return -9;
    return 0;
}
static pthread_t fake_self() { return (pthread_t)1; }
static int fake_create(pthread_t *thread, const pthread_attr_t *,
                       void *(*)(void *), void *) {
    *thread = (pthread_t)1;
    return ++creates == 1 ? 11 : 0;
}
static int fake_detach(pthread_t) { return 0; }
static void *displayClockThread(void *) { return nullptr; }
static void installFrameSinkBridge() {}
static bool readState(CVLinkStateRec *out, CVDisplayLinkRef) { *out=state; return true; }
static bool writeState(const CVLinkStateRec *rec) { state=*rec; return true; }
#define pthread_self fake_self
#define pthread_create fake_create
#define pthread_detach fake_detach
'''
        main = r'''
int main() {
    lock_mode=1;
    { CVStateGuard guard; assert(guard.fd == 42 && guard.locked); }
    assert(lock_calls == 2 && unlock_calls == 1 && closes == 1);
    lock_mode=2; lock_calls=unlock_calls=closes=0;
    { CVStateGuard guard; assert(guard.fd == -1 && !guard.locked); }
    assert(lock_calls == 1 && unlock_calls == 0 && closes == 1);
    // A failed lock must prevent record access and thread creation.
    assert(CVDisplayLinkStart((void *)1) == kCVReturnError);
    assert(state.running == 0 && creates == 0);
    owner=1; lock_calls=unlock_calls=0;
    { CVStateGuard guard; assert(guard.fd == 42 && !guard.locked); }
    assert(lock_calls == 0 && unlock_calls == 0);
    owner=0; lock_mode=0;
    assert(CVDisplayLinkStart((void *)1) == kCVReturnError);
    assert(state.running == 0 && creates == 1);
    assert(CVDisplayLinkStart((void *)1) == kCVReturnSuccess);
    assert(state.running == 1 && creates == 2);
    assert(CVDisplayLinkStart((void *)1) == kCVReturnSuccess);
    assert(creates == 2);
}
'''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'probe.cc').write_text(harness + bodies + main)
            subprocess.run(['c++', '-std=c++11', '-pthread', str(path / 'probe.cc'),
                            '-o', str(path / 'probe')], check=True, capture_output=True)
            subprocess.run([str(path / 'probe')], check=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
