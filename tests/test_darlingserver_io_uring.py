"""darlingserver's single-threaded io_uring message loop, and the idle run-loop wait in AppKit.

The server receives every call, runs the microthreads and sends every reply on one thread, with one io_uring_enter
per pass. It is only built when the target's kernel headers have what it uses, and falls back to the epoll loop at
runtime (no io_uring, or DSERVER_IO_URING=0). Replies keep their order: a batch goes out as one linked chain, and a
full socket fails the chain instead of letting a later reply overtake an earlier one.

AppKit's port wait is covered separately by test_appkit_port_wait.py.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def patched(patch, path):
    """The hunks of `path` as the patch leaves them (context and added lines)."""
    sections = (ROOT / 'patches' / patch).read_text().split('diff --git ')
    section = next(s for s in sections if s.startswith('a/' + path + ' b/'))
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class BuildTests(unittest.TestCase):
    def test_enabled_only_when_the_target_headers_support_it(self):
        cmake = patched('darlingserver.patch', 'CMakeLists.txt')
        self.assertIn('check_c_source_compiles(', cmake)
        self.assertIn('#include <linux/io_uring.h>', cmake)
        self.assertIn('if (DSERVER_IO_URING AND DSERVER_TARGET_HAS_IO_URING)', cmake)
        self.assertIn('DSERVER_IO_URING=0', cmake)
        self.assertIn('src/uring.cpp', cmake)

    def test_no_liburing_dependency(self):
        cmake = patched('darlingserver.patch', 'CMakeLists.txt')
        self.assertNotIn('uring REQUIRED', cmake)
        self.assertNotIn('-luring', cmake)


class RingTests(unittest.TestCase):
    def setUp(self):
        self.ring = patched('darlingserver.patch', 'src/uring.cpp')

    def test_prefers_deferred_task_running_and_falls_back(self):
        self.assertIn('IORING_SETUP_SINGLE_ISSUER | IORING_SETUP_DEFER_TASKRUN', self.ring)
        self.assertLess(self.ring.index('IORING_SETUP_DEFER_TASKRUN,'), self.ring.index('IORING_SETUP_COOP_TASKRUN,'))
        self.assertIn('errno != EINVAL', self.ring)

    def test_requires_fast_poll(self):
        # without it a waiting receive would occupy a kernel worker thread
        self.assertIn('IORING_FEAT_FAST_POLL', self.ring)

    def test_completion_released_before_the_handler_runs(self):
        body = self.ring[self.ring.index('Ring::nextCompletion'):]
        self.assertLess(body.index('__atomic_store_n(_cqHead, head + 1'), body.index('return true;'))

    def test_real_ring_retry_timeout_and_nonblocking_completion(self):
        compiler = shutil.which('c++')
        if not compiler:
            self.skipTest('C++ compiler unavailable')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            include = root / 'darlingserver'
            include.mkdir()
            (include / 'uring.hpp').write_text(patched('darlingserver.patch',
                                                      'internal-include/darlingserver/uring.hpp'))
            source = root / 'uring.cpp'
            source.write_text(self.ring)
            binary = root / 'retry-check'
            subprocess.run([compiler, '-std=c++17', '-DDSERVER_IO_URING=1',
                            '-I' + str(root), str(source),
                            str(ROOT / 'tests/fixtures/io-uring-retry.cpp'),
                            '-o', str(binary)], check=True, capture_output=True, text=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
            if result.returncode == 77:
                self.skipTest(result.stderr.strip())
            self.assertEqual(result.returncode, 0, result.stderr)


class ServerLoopTests(unittest.TestCase):
    def setUp(self):
        self.server = patched('darlingserver.patch', 'src/server.cpp')

    def test_runtime_opt_out_and_fallback(self):
        self.assertIn('std::getenv("DSERVER_IO_URING")', self.server)
        self.assertIn('using the epoll message loop', self.server)
        self.assertIn('if (!_ring) {\n\t\t_workQueue = std::make_unique', self.server)

    def test_listener_stays_out_of_epoll_on_the_ring(self):
        registration = self.server.index('EPOLL_CTL_ADD, _listenerSocket')
        self.assertIn('if (!_ring) {', self.server[registration - 300:registration])

    def test_sends_are_one_ordered_chain(self):
        flush = self.server[self.server.index('void DarlingServer::Server::_ringFlushSends()'):]
        flush = flush[:flush.index('\n};')]
        self.assertIn('if (_ringSendsPending > 0 || _ringSendBlocked)', flush)
        self.assertIn('IOSQE_IO_LINK', flush)
        self.assertIn('sqe->msg_flags = MSG_DONTWAIT;', flush)

    def test_failed_and_cancelled_sends_go_back_to_the_front(self):
        finish = self.server[self.server.index('void DarlingServer::Server::_ringFinishSends()'):]
        finish = finish[:finish.index('\n};')]
        self.assertIn('result == -ECANCELED', finish)
        self.assertIn('_outbox.requeueFront(std::move(retry));', finish)
        self.assertIn('sqe->opcode = IORING_OP_TIMEOUT;', finish)
        self.assertNotIn('sqe->poll32_events = POLLOUT;', finish)

    def test_scheduling_never_runs_a_microthread_on_the_spot(self):
        schedule = self.server[self.server.index('void DarlingServer::Server::scheduleThread('):]
        schedule = schedule[:schedule.index('\n};')]
        self.assertIn('_runQueue.push_back(std::move(thread));', schedule)
        self.assertNotIn('doWork', schedule)
        self.assertIn('eventfd_write(_wakeupFD, 1);', schedule)

    def test_run_queue_drains_only_what_was_queued(self):
        drain = self.server[self.server.index('bool DarlingServer::Server::_runQueued()'):]
        self.assertIn('batch.swap(_runQueue);', drain[:drain.index('\n};')])

    def test_one_enter_per_pass(self):
        ring_pass = self.server[self.server.index('void DarlingServer::Server::_ringPass('):]
        ring_pass = ring_pass[:ring_pass.index('\n};')]
        self.assertEqual(ring_pass.count('submitAndWait('), 1)
        self.assertIn('submitAndWait(moreWork || (completed && *completed) ? 0 : 1)', ring_pass)

    def test_receives_start_after_initialization(self):
        start = self.server.index('Thread::kernelSync(dtape_init_in_thread);')
        self.assertLess(start, self.server.index('_ringArmReceive(i);'))

    def test_kernel_sync_runs_loop_passes_on_the_ring(self):
        thread = patched('darlingserver.patch', 'src/thread.cpp')
        body = thread[thread.index('void DarlingServer::Thread::kernelSync('):]
        self.assertLess(body.index('runsWorkOnThisThread()'), body.index('std::condition_variable condvar;'))
        self.assertIn('Server::sharedInstance().runOnePass(&finished);', body)


if __name__ == '__main__':
    unittest.main()
