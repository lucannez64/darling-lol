"""Verify libkqueue listenregistry post-fork reset and bounds handling.

In multithreaded processes (such as Electron / Riot Client), another thread may
hold listenregistry.c's registry_mtx across fork()/posix_spawn(). Because
sys_dup2() calls kqueue_dup() -> __darling_kqueue_get_listen_status() during
posix_spawn file actions, failing to reset registry_mtx in kqueue_atfork()
deadlocks the child in psynch_mutexwait before execve().
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/libkqueue.patch'


def patch_section(target):
    text = PATCH.read_text()
    marker = f'diff --git a/{target} b/{target}\n'
    return text.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def added_lines(target):
    return '\n'.join(
        line[1:]
        for line in patch_section(target).splitlines()
        if line.startswith('+') and not line.startswith('+++')
    )


class LibkqueueListenRegistryTests(unittest.TestCase):
    def test_kqueue_atfork_resets_kq_mtx_and_listen_registry_unconditionally(self):
        kqueue_sec = patch_section('src/common/kqueue.c')
        self.assertIn('+    pthread_mutex_init(&kq_mtx, NULL);', kqueue_sec)
        self.assertIn('+    __darling_kqueue_listen_atfork();', kqueue_sec)
        # Both resets must happen before the `if (kqmap != NULL)` check.
        atfork_part = kqueue_sec.split('void VISIBLE kqueue_atfork(void)', 1)[1]
        self.assertLess(
            atfork_part.index('__darling_kqueue_listen_atfork()'),
            atfork_part.index('if (kqmap != NULL)'),
        )

    def test_listenregistry_header_declares_atfork(self):
        self.assertIn('void __darling_kqueue_listen_atfork(void);', added_lines('src/darling/listenregistry.h'))

    def test_posix_spawn_resets_libsystem_kernel_kqueue_copy(self):
        xnu_patch = (ROOT / 'patches/xnu.patch').read_text()
        marker = 'diff --git a/darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl/process/posix_spawn.c '
        self.assertIn(marker, xnu_patch)
        sec = xnu_patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]
        self.assertIn('+			if (kqueue_atfork)', sec)
        self.assertIn('+				kqueue_atfork();', sec)

    def test_listenregistry_patch_fixes_alloc_and_unregister(self):
        added = added_lines('src/darling/listenregistry.c')
        self.assertIn('malloc(REGISTRY_VECTOR_INITIAL_CAPACITY * sizeof(int))', added)
        self.assertIn('realloc(registry_vector, registry_vector_capacity * 2 * sizeof(int))', added)
        self.assertIn('void __darling_kqueue_listen_atfork(void)', added)
        self.assertIn('pthread_mutex_init(&registry_mtx, NULL);', added)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_compiled_listenregistry_atfork_and_vector_operations(self):
        # Compile a host C harness reproducing listenregistry.c's exact patched
        # logic and verifying:
        # 1) registering/unregistering > 8 fds without OOB writes/reads;
        # 2) unregistering sole/first/last elements;
        # 3) calling __darling_kqueue_listen_atfork() in a forked child while
        #    another parent thread holds registry_mtx does not deadlock.
        code = r'''
#include <assert.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

static pthread_mutex_t registry_mtx = PTHREAD_MUTEX_INITIALIZER;
static int* registry_vector = NULL;
static size_t registry_vector_capacity = 0;
static size_t registry_vector_size = 0;

#define REGISTRY_VECTOR_INITIAL_CAPACITY (8)

static bool get_listen_status(int fd) {
    bool result = false;
    pthread_mutex_lock(&registry_mtx);
    if (registry_vector != NULL) {
        for (size_t i = 0; i < registry_vector_size; ++i) {
            if (registry_vector[i] == fd)
                result = true;
        }
    }
    pthread_mutex_unlock(&registry_mtx);
    return result;
}

static void register_listen(int fd) {
    pthread_mutex_lock(&registry_mtx);
    if (registry_vector == NULL) {
        registry_vector = malloc(REGISTRY_VECTOR_INITIAL_CAPACITY * sizeof(int));
        if (registry_vector == NULL) goto quit;
        registry_vector_capacity = REGISTRY_VECTOR_INITIAL_CAPACITY;
    } else if (registry_vector_size == registry_vector_capacity) {
        int* newPtr = realloc(registry_vector, registry_vector_capacity * 2 * sizeof(int));
        if (newPtr == NULL) goto quit;
        registry_vector = newPtr;
        registry_vector_capacity *= 2;
    }
    for (size_t i = 0; i < registry_vector_size; ++i) {
        if (registry_vector[i] == fd) goto quit;
    }
    registry_vector[registry_vector_size] = fd;
    ++registry_vector_size;
quit:
    pthread_mutex_unlock(&registry_mtx);
}

static void unregister_listen(int fd) {
    pthread_mutex_lock(&registry_mtx);
    for (size_t i = 0; i < registry_vector_size; ) {
        if (registry_vector[i] == fd) {
            --registry_vector_size;
            registry_vector[i] = registry_vector[registry_vector_size];
        } else {
            ++i;
        }
    }
    pthread_mutex_unlock(&registry_mtx);
}

static void listen_atfork(void) {
    pthread_mutex_init(&registry_mtx, NULL);
    registry_vector = NULL;
    registry_vector_capacity = 0;
    registry_vector_size = 0;
}

static int hold_pipe[2];
static int ready_pipe[2];

static void* holder_thread(void* arg) {
    (void)arg;
    char c = 1;
    pthread_mutex_lock(&registry_mtx);
    assert(write(ready_pipe[1], &c, 1) == 1);
    assert(read(hold_pipe[0], &c, 1) == 1);
    pthread_mutex_unlock(&registry_mtx);
    return NULL;
}

int main(void) {
    // 1. Register > initial capacity (triggers realloc) and check all entries.
    for (int fd = 10; fd < 30; ++fd) {
        register_listen(fd);
    }
    for (int fd = 10; fd < 30; ++fd) {
        assert(get_listen_status(fd));
    }
    // Unregister first, middle, last, and remaining elements.
    unregister_listen(10);
    unregister_listen(20);
    unregister_listen(29);
    assert(!get_listen_status(10));
    assert(!get_listen_status(20));
    assert(!get_listen_status(29));
    for (int fd = 11; fd < 29; ++fd) {
        if (fd != 20) {
            assert(get_listen_status(fd));
            unregister_listen(fd);
            assert(!get_listen_status(fd));
        }
    }
    assert(registry_vector_size == 0);

    // 2. Fork while another thread holds registry_mtx; child resets via atfork.
    assert(pipe(hold_pipe) == 0);
    assert(pipe(ready_pipe) == 0);
    pthread_t th;
    assert(pthread_create(&th, NULL, holder_thread, NULL) == 0);
    char c = 0;
    assert(read(ready_pipe[0], &c, 1) == 1);

    pid_t pid = fork();
    assert(pid >= 0);
    if (pid == 0) {
        listen_atfork();
        assert(!get_listen_status(42));
        register_listen(42);
        assert(get_listen_status(42));
        unregister_listen(42);
        assert(!get_listen_status(42));
        _exit(0);
    }

    int status = 0;
    assert(waitpid(pid, &status, 0) == pid);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);

    assert(write(hold_pipe[1], &c, 1) == 1);
    assert(pthread_join(th, NULL) == 0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='test-listenregistry-') as td:
            src = Path(td) / 'test.c'
            exe = Path(td) / 'test'
            src.write_text(code)
            subprocess.run(
                ['cc', '-Wall', '-Wextra', '-Werror', '-pthread', str(src), '-o', str(exe)],
                check=True,
                capture_output=True,
                text=True,
            )
            res = subprocess.run([str(exe)], timeout=10, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


if __name__ == '__main__':
    unittest.main()
