"""Session supervisor: attribution, idle decisions and scoped cleanup."""
import importlib.util
import io
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import subprocess
import shutil

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'session_supervisor', ROOT / 'tools/session-supervisor.py')
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


def fake_process(root, pid, ppid=1, state='S', comm='mldr', environ=b'', ns=None,
                 uid=None):
    proc = root / str(pid)
    (proc / 'ns').mkdir(parents=True)
    fields = [state, str(ppid)] + ['0'] * 17 + ['100']
    (proc / 'stat').write_text('%d (%s) %s\n' % (pid, comm, ' '.join(fields)))
    (proc / 'comm').write_text(comm + '\n')
    (proc / 'cmdline').write_bytes(comm.encode() + b'\0')
    if environ is not None:
        (proc / 'environ').write_bytes(environ)
    if ns is not None:
        os.symlink(ns, proc / 'ns/pid')
    return proc


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / '.darling'
        self.socket = self.prefix / '.darlingserver.sock'
        self.ns = 'pid:[4026532000]'

    def process(self, pid, **kwargs):
        return fake_process(self.root, pid, ns=kwargs.pop('ns', self.ns), **kwargs)

    def test_socket_names_the_owner_prefix(self):
        proc = self.process(10, comm='RiotClientServices',
                            environ=b'__mldr_sockpath=%s\0' % os.fsencode(self.socket))
        self.assertEqual(supervisor.socket_prefix(proc), self.prefix.resolve())
        self.assertEqual(supervisor.prefix_namespace(self.prefix, self.root), self.ns)

    def test_another_prefix_is_not_owned(self):
        proc = self.process(10, comm='RiotClientServices',
                            environ=b'__mldr_sockpath=/elsewhere/.darlingserver.sock\0')
        self.assertFalse(supervisor.owned_by_prefix(proc, self.prefix.resolve()))

    def test_own_namespace_is_refused(self):
        proc = self.process(10)
        self.assertEqual(
            supervisor.namespace_members(self.ns, self.root, own_namespace=self.ns), [])
        self.assertEqual(
            [pid for pid, _proc in supervisor.namespace_members(self.ns, self.root,
                                                                'pid:[1]')], [10])

    def test_unreadable_environ_is_not_owned(self):
        proc = self.process(10, environ=None)
        self.assertFalse(supervisor.owned_by_prefix(proc, self.prefix.resolve()))

    def test_game_is_matched_through_the_prefix_socket(self):
        proc = self.process(10, comm='LeagueofLegends',
                            environ=b'__mldr_sockpath=%s\0' % os.fsencode(self.socket))
        (proc / 'cmdline').write_bytes(b'/Applications/LeagueofLegends\0')
        self.assertTrue(supervisor.game_running(self.prefix, self.root))
        other = self.root / 'other'
        other.mkdir()
        self.assertFalse(supervisor.game_running(other, self.root))


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / '.darling'
        self.ns = 'pid:[4026532000]'
        self.own = 'pid:[4026531000]'
        self.output = io.StringIO()

    def process(self, pid, ppid=1):
        return fake_process(self.root, pid, ppid=ppid, ns=self.ns)

    def test_cleanup_shuts_down_then_terminates_namespace_members(self):
        self.process(10)
        self.process(11, ppid=10)
        calls = []
        killed = []

        def run(command, **_kwargs):
            calls.append(command)

        def kill(pid, signum):
            killed.append((pid, signum))
            if signum == supervisor.signal.SIGTERM:
                import shutil
                shutil.rmtree(self.root / str(pid))

        remaining = supervisor.cleanup(
            self.prefix, '/usr/bin/darling', self.ns, proc_root=self.root,
            run=run, kill=kill, sleep=lambda _seconds: None, out=self.output,
            own_namespace=self.own, term_grace=0, kill_grace=0)
        self.assertEqual(remaining, [])
        self.assertEqual(calls, [['/usr/bin/darling', 'shutdown']])
        # Deepest process first: the child (11) before its parent (10).
        self.assertEqual(killed, [(11, supervisor.signal.SIGTERM),
                                  (10, supervisor.signal.SIGTERM)])
        self.assertIn('guest session stopped', self.output.getvalue())

    def test_pid_reused_during_shutdown_is_never_signalled(self):
        proc = self.process(10)
        def shutdown(*_args, **_kwargs):
            shutil.rmtree(proc)
            fake_process(self.root, 10, ns=self.own, comm='host-editor')
        killed = []
        remaining = supervisor.cleanup(
            self.prefix, 'darling', self.ns, proc_root=self.root,
            run=shutdown, kill=lambda *args: killed.append(args),
            sleep=lambda _: None, out=self.output, own_namespace=self.own,
            term_grace=0, kill_grace=0)
        self.assertEqual(killed, [])
        self.assertEqual(remaining, [])

    def test_pid_reused_after_term_is_not_killed(self):
        proc = self.process(10)
        killed = []
        def kill(pid, signum):
            killed.append(signum)
            fields = (proc / 'stat').read_text().split()
            fields[-1] = '200'
            (proc / 'stat').write_text(' '.join(fields))
        remaining = supervisor.stop_processes(
            [(10, proc)], kill=kill, proc_root=self.root,
            term_grace=0, kill_grace=0)
        self.assertEqual(killed, [signal.SIGTERM])
        self.assertEqual(remaining, [])

    def test_pidfd_is_checked_after_open_and_closed_on_identity_change(self):
        proc = self.process(10)
        identity = supervisor.process_identity(proc)
        def opened(_pid):
            (proc / 'ns/pid').unlink()
            os.symlink(self.own, proc / 'ns/pid')
            return 123
        with mock.patch.object(supervisor.os, 'pidfd_open', side_effect=opened), \
             mock.patch.object(supervisor.signal, 'pidfd_send_signal') as send, \
             mock.patch.object(supervisor.os, 'close') as close:
            supervisor.signal_process(10, proc, identity, signal.SIGTERM)
        send.assert_not_called()
        close.assert_called_once_with(123)

    def test_unavailable_pidfd_never_falls_back_to_numeric_pid(self):
        proc = self.process(10)
        with mock.patch.object(supervisor.os, 'pidfd_open', side_effect=OSError), \
             mock.patch.object(supervisor.signal, 'pidfd_send_signal') as send:
            supervisor.signal_process(10, proc, supervisor.process_identity(proc), signal.SIGTERM)
        send.assert_not_called()

    def test_real_pidfd_stops_only_the_selected_child(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(child.wait)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        proc = Path('/proc') / str(child.pid)
        supervisor.signal_process(child.pid, proc, supervisor.process_identity(proc), signal.SIGTERM)
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)

    def test_cleanup_does_nothing_without_attribution(self):
        output = io.StringIO()
        remaining = supervisor.cleanup(
            self.prefix, '/usr/bin/darling', None, proc_root=self.root,
            run=lambda *_a, **_k: self.fail('must not run darling shutdown'),
            kill=lambda *_a: self.fail('must not kill'), sleep=lambda _s: None,
            out=output, own_namespace=self.own, term_grace=0, kill_grace=0)
        self.assertEqual(remaining, [])
        self.assertIn('gone', output.getvalue())

    def test_live_survivor_is_reported(self):
        self.process(10)
        output = io.StringIO()
        remaining = supervisor.cleanup(
            self.prefix, None, self.ns, proc_root=self.root,
            run=lambda *_a, **_k: None, kill=lambda *_a: None,
            sleep=lambda _s: None, out=output, own_namespace=self.own, term_grace=0, kill_grace=0)
        self.assertEqual([pid for pid, _proc in remaining], [10])
        self.assertIn('still present: 10', output.getvalue())


class RuntimeMarkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.prefix = self.root / '.darling'
        self.prefix.mkdir()

    def server(self, pid=10, argv=None, state='S'):
        proc = fake_process(self.proc, pid, state=state, comm='darlingserver')
        (proc / 'cmdline').write_bytes(
            b'\0'.join(os.fsencode(part) for part in
                       (argv or ['/usr/libexec/darlingserver', str(self.prefix)])) + b'\0')
        (self.prefix / '.init.pid').write_text('%d\n' % pid)
        return proc

    def test_live_server_of_this_prefix_is_recognised(self):
        self.server()
        self.assertTrue(supervisor.runtime_alive(self.prefix, self.proc))

    def test_missing_marker_is_not_a_runtime(self):
        self.assertFalse(supervisor.runtime_alive(self.prefix, self.proc))

    def test_reused_pid_is_not_a_runtime(self):
        self.server()
        proc = self.proc / '10'
        (proc / 'cmdline').write_bytes(b'/usr/bin/other\0\0')
        self.assertFalse(supervisor.runtime_alive(self.prefix, self.proc))

    def test_other_prefix_is_not_ours(self):
        self.server(argv=['/usr/libexec/darlingserver', '/elsewhere/.darling'])
        self.assertFalse(supervisor.runtime_alive(self.prefix, self.proc))

    def test_zombie_server_is_not_a_runtime(self):
        self.server(state='Z')
        self.assertFalse(supervisor.runtime_alive(self.prefix, self.proc))


class SupervisorModeTests(unittest.TestCase):
    """run_supervised must not end a runtime that another session started."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / '.darling'
        self.prefix.mkdir()
        self.proc = self.root / 'proc'
        self.proc.mkdir()

    def run_supervisor(self):
        output = io.StringIO()
        code = supervisor.run_supervised(
            ['true'], self.prefix, None, proc_root=self.proc, poll=0,
            run=lambda *_a, **_k: None, kill=lambda *_a: None,
            sleep=lambda _s: None, out=output)
        self.assertEqual(code, 0)
        return output.getvalue()

    def test_own_session_is_cleaned_up(self):
        self.assertIn('gone', self.run_supervisor())

    def test_attached_session_is_left_running(self):
        proc = fake_process(self.proc, 10, comm='darlingserver')
        (proc / 'cmdline').write_bytes(
            b'/usr/libexec/darlingserver\0' + os.fsencode(self.prefix) + b'\0')
        (self.prefix / '.init.pid').write_text('10\n')
        output = self.run_supervisor()
        self.assertIn('another session owns this runtime', output)
        self.assertNotIn('guest session stopped', output)

    def test_runtime_started_by_startup_helper_is_owned(self):
        proc = fake_process(self.proc, 10, comm='darlingserver')
        (proc / 'cmdline').write_bytes(b'darlingserver\0' + os.fsencode(self.prefix) + b'\0')
        (self.prefix / '.init.pid').write_text('10')
        with mock.patch.object(supervisor, 'cleanup', return_value=[]) as cleanup:
            code = supervisor.run_supervised(
                ['true'], self.prefix, None, proc_root=self.proc, poll=0,
                sleep=lambda _: None, runtime_at_start='stopped')
        self.assertEqual(code, 0)
        cleanup.assert_called_once()

    def test_runtime_existing_before_helpers_is_left_running(self):
        with mock.patch.object(supervisor, 'cleanup') as cleanup:
            supervisor.run_supervised(
                ['true'], self.prefix, None, proc_root=self.proc, poll=0,
                sleep=lambda _: None, out=io.StringIO(), runtime_at_start='running')
        cleanup.assert_not_called()


class ExitStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / '.darling'
        self.prefix.mkdir()
        self.proc = self.root / 'proc'
        self.proc.mkdir()

    def run_supervisor(self, command, remaining):
        previous = supervisor.cleanup
        supervisor.cleanup = lambda *_a, **_k: remaining
        self.addCleanup(setattr, supervisor, 'cleanup', previous)
        return supervisor.run_supervised(
            command, self.prefix, None, proc_root=self.proc, poll=0,
            run=lambda *_a, **_k: None, kill=lambda *_a: None,
            sleep=lambda _s: None, out=io.StringIO())

    def test_child_status_is_kept_when_cleanup_is_clean(self):
        self.assertEqual(self.run_supervisor(['sh', '-c', 'exit 3'], []), 3)

    def test_surviving_processes_force_a_failure_status(self):
        self.assertEqual(self.run_supervisor(['true'], [1234]), 1)

    def test_a_failed_child_stays_failed_with_survivors(self):
        self.assertEqual(self.run_supervisor(['sh', '-c', 'exit 3'], [1234]), 3)


class SignalEscalationTests(unittest.TestCase):
    """A signal must start the TERM grace period, then kill the whole group."""

    CHILD = (
        "import os, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "worker = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(3)'])\n"
        "with open(sys.argv[1], 'w') as handle:\n"
        "    handle.write('%d %d' % (os.getpid(), worker.pid))\n"
        "time.sleep(3)\n"
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / '.darling'
        self.prefix.mkdir()
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.pids = self.root / 'pids'
        self.script = self.root / 'ignore-term.py'
        self.script.write_text(self.CHILD)
        previous = supervisor.TERM_GRACE
        supervisor.TERM_GRACE = 0.2
        self.addCleanup(setattr, supervisor, 'TERM_GRACE', previous)

    def wait_dead(self, pid, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()[0]
            except (OSError, IndexError):
                return True
            if state == 'Z':
                return True
            time.sleep(0.02)
        return False

    def interrupt_when_ready(self):
        deadline = time.monotonic() + 20
        while not self.pids.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGTERM)

    def test_sigterm_reaches_the_grace_period_and_the_group_is_killed(self):
        thread = threading.Thread(target=self.interrupt_when_ready, daemon=True)
        thread.start()
        output = io.StringIO()
        try:
            code = supervisor.run_supervised(
                [sys.executable, str(self.script), str(self.pids)], self.prefix, None,
                proc_root=self.proc, poll=0, run=lambda *_a, **_k: None,
                kill=lambda *_a: None, sleep=lambda _s: time.sleep(0.01), out=output)
        finally:
            thread.join(timeout=5)
        parent, worker = (int(part) for part in self.pids.read_text().split())
        self.assertEqual(code, 137)
        self.assertTrue(self.wait_dead(parent), 'the loader survived the escalation')
        self.assertTrue(self.wait_dead(worker), 'the descendant survived the escalation')
        self.assertIn('interrupted by signal 15', output.getvalue())


class IdleEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.previous = os.environ.get('RCLOL_STOP_AFTER_CLIENT_SECONDS')
        self.addCleanup(self.restore)

    def restore(self):
        if self.previous is None:
            os.environ.pop('RCLOL_STOP_AFTER_CLIENT_SECONDS', None)
        else:
            os.environ['RCLOL_STOP_AFTER_CLIENT_SECONDS'] = self.previous

    def test_absent_value_disables_the_idle_stop(self):
        os.environ.pop('RCLOL_STOP_AFTER_CLIENT_SECONDS', None)
        self.assertEqual(supervisor.env_seconds('RCLOL_STOP_AFTER_CLIENT_SECONDS'), 0)

    def test_valid_value_is_used(self):
        os.environ['RCLOL_STOP_AFTER_CLIENT_SECONDS'] = ' 120 '
        self.assertEqual(supervisor.env_seconds('RCLOL_STOP_AFTER_CLIENT_SECONDS'), 120)

    def test_invalid_value_warns_and_disables_the_idle_stop(self):
        os.environ['RCLOL_STOP_AFTER_CLIENT_SECONDS'] = '5m'
        output = io.StringIO()
        self.assertEqual(supervisor.env_seconds('RCLOL_STOP_AFTER_CLIENT_SECONDS', output), 0)
        self.assertIn("ignoring invalid RCLOL_STOP_AFTER_CLIENT_SECONDS='5m'", output.getvalue())

    def test_empty_value_disables_the_idle_stop(self):
        os.environ['RCLOL_STOP_AFTER_CLIENT_SECONDS'] = ''
        self.assertEqual(supervisor.env_seconds('RCLOL_STOP_AFTER_CLIENT_SECONDS'), 0)


class IdleDecisionTests(unittest.TestCase):
    def test_window_present_resets_the_timer(self):
        self.assertEqual(supervisor.idle_decision(True, 100.0, 200.0, True, False, 60),
                         (True, None, False))

    def test_window_gone_starts_and_then_fires(self):
        seen, since, stop = supervisor.idle_decision(True, None, 100.0, False, False, 60)
        self.assertEqual((seen, since, stop), (True, 100.0, False))
        self.assertEqual(supervisor.idle_decision(True, since, 150.0, False, False, 60),
                         (True, 100.0, False))
        self.assertEqual(supervisor.idle_decision(True, since, 161.0, False, False, 60),
                         (True, 100.0, True))

    def test_game_resets_the_timer(self):
        self.assertEqual(supervisor.idle_decision(True, 100.0, 200.0, False, True, 60),
                         (True, None, False))

    def test_unknown_window_state_never_stops(self):
        self.assertEqual(supervisor.idle_decision(True, 100.0, 200.0, None, False, 60),
                         (True, None, False))

    def test_client_that_was_never_seen_never_stops(self):
        self.assertEqual(supervisor.idle_decision(False, None, 200.0, False, False, 60),
                         (False, None, False))

    def test_disabled_never_stops(self):
        self.assertEqual(supervisor.idle_decision(True, 100.0, 999.0, False, False, 0),
                         (True, None, False))


class ManagedWindowTests(unittest.TestCase):
    def test_client_class_is_detected(self):
        def run(command, **_kwargs):
            output = ('_NET_CLIENT_LIST(WINDOW): window id # 0x1' if '_NET_CLIENT_LIST' in command
                      else 'WM_CLASS(STRING) = "riot client", "Riot Client"')

            class Result:
                stdout = output
            return Result()

        self.assertTrue(supervisor.managed_client_window(run))

    def test_missing_property_is_unknown(self):
        def run(_command, **_kwargs):
            class Result:
                stdout = 'not found'
            return Result()

        self.assertIsNone(supervisor.managed_client_window(run))


if __name__ == '__main__':
    unittest.main()
