"""darlingserver can be kept on the performance cores of a hybrid CPU.

On Intel hybrid CPUs the scheduler tends to place the server's short, frequent
bursts on efficiency cores. DSERVER_PCORE_AFFINITY=1 restricts the server's
threads to /sys/devices/cpu_core/cpus. It must run after launchd is forked, so
guest processes keep their own affinity, and before the Server constructor
starts the work-queue threads, which inherit the mask.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def patched(path):
    """The hunks of `path` as the patch leaves them (context and added lines)."""
    sections = (ROOT / 'patches/darlingserver.patch').read_text().split('diff --git ')
    section = next(s for s in sections if s.startswith('a/' + path + ' b/'))
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class PerformanceCoreAffinityTests(unittest.TestCase):
    def setUp(self):
        self.main = patched('src/darlingserver.cpp')

    def test_switch_is_opt_in(self):
        self.assertIn('getenv("DSERVER_PCORE_AFFINITY")', self.main)
        self.assertIn('strcmp(value, "1") != 0', self.main)

    def test_reads_the_kernel_performance_core_list(self):
        self.assertIn('"/sys/devices/cpu_core/cpus"', self.main)
        self.assertIn('sched_setaffinity(0, sizeof(set), &set)', self.main)

    def test_pins_after_launchd_fork_and_before_server_threads(self):
        call = self.main.index('\tpinToPerformanceCoresIfRequested();')
        # main() forks launchd before it snapshots the server's own credentials
        self.assertLess(self.main.index('DarlingServer::Message::initializeServerCredentials();'), call)
        self.assertLess(call, self.main.index('auto server = new DarlingServer::Server(prefix);'))

    def test_bad_topology_leaves_affinity_alone(self):
        body = self.main[self.main.index('static void pinToPerformanceCoresIfRequested'):]
        body = body[:body.index('\n};')]
        self.assertLess(body.index('if (!ok || count == 0)'), body.index('sched_setaffinity'))


if __name__ == '__main__':
    unittest.main()
