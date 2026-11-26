"""tools/run-parallel.sh runs independent commands concurrently and reports failures."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/run-parallel.sh'

# Each job waits for the other's marker. Both only pass when two workers run.
INTERLOCK = [
    'touch a.started; i=0; while [ ! -e b.started ] && [ $i -lt 100 ]; do sleep 0.01; i=$((i+1)); done; [ -e b.started ]',
    'touch b.started; i=0; while [ ! -e a.started ] && [ $i -lt 100 ]; do sleep 0.01; i=$((i+1)); done; [ -e a.started ]',
]


class RunParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_pool(self, lines, jobs=None):
        env = dict(os.environ)
        if jobs is not None:
            env['RCLOL_PARALLEL_JOBS'] = str(jobs)
        return subprocess.run(['sh', str(SCRIPT)], input='\n'.join(lines) + '\n',
                              text=True, cwd=self.root, env=env,
                              capture_output=True, timeout=20)

    def test_commands_run_concurrently(self):
        result = self.run_pool(INTERLOCK, jobs=2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_job_cap_is_enforced(self):
        result = self.run_pool(INTERLOCK, jobs=1)
        self.assertNotEqual(result.returncode, 0)

    def test_a_failed_job_fails_the_run_after_the_others_finish(self):
        result = self.run_pool(['true', 'exit 3', 'touch after'], jobs=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / 'after').is_file())
        self.assertIn('exit 3: exit 3', result.stderr)

    def test_a_cancelled_pool_stops_the_job_trees(self):
        # The job's own child records its pid and would write `survived` later.
        job = 'sh -c "echo \\$\\$ > child.pid; sleep 3; touch survived"; true'
        env = dict(os.environ, RCLOL_PARALLEL_JOBS='2')
        pool = subprocess.Popen(['sh', str(SCRIPT)], stdin=subprocess.PIPE,
                                cwd=self.root, env=env, text=True)
        pool.stdin.write(job + '\n')
        pool.stdin.close()
        pid_file = self.root / 'child.pid'
        deadline = time.monotonic() + 10
        while not pid_file.is_file() or not pid_file.read_text().strip():
            self.assertLess(time.monotonic(), deadline, 'job never started')
            time.sleep(0.01)
        pool.terminate()
        self.assertEqual(pool.wait(timeout=10), 143)
        child = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while True:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            self.assertLess(time.monotonic(), deadline, 'grandchild still running')
            time.sleep(0.01)
        self.assertFalse((self.root / 'survived').exists())

    def test_invalid_job_count_falls_back(self):
        for jobs in ('0', 'nope', ''):
            result = self.run_pool(['true'], jobs=jobs)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_blank_lines_and_comments_are_skipped(self):
        result = self.run_pool(['', '# comment', 'touch ran'], jobs=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'ran').is_file())


if __name__ == '__main__':
    unittest.main()
