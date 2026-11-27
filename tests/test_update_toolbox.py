"""Exercise updater re-exec and step handling in a disposable checkout."""
import os
import pty
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATE = ROOT / 'scripts/update.sh'
SH = shutil.which('sh')
REEXEC_ARGV = 'run --container {name} env DARLING_LOL_UPDATE_REEXEC=1 sh {repo}/scripts/update.sh'

FAKE_TOOLBOX = """#!/bin/sh
# Fake toolbox: records argv, then behaves per the FAKE_TOOLBOX_* knobs.
if [ "${1:-}" = --version ]; then
  if [ "${FAKE_TOOLBOX_BAD_VERSION:-0}" = 1 ]; then
    echo "toolbox: unrecognized option '--version'" >&2
    exit 1
  fi
  printf 'toolbox version 0.0.99.5\\n'
  exit 0
fi
printf '%s\\n' "$*" >> "${FAKE_TOOLBOX_LOG:?}"
case "${1:-}" in run) shift ;; *) exit 0 ;; esac
case "${1:-}" in
  -c) container=$2; shift 2 ;;
  --container) container=$2; shift 2 ;;
  *) exit 0 ;;
esac
case "${1:-}" in
  true)
    # FAKE_TOOLBOX_STOPPED=1: the first probe finds the container stopped and
    # the next one succeeds, as `toolbox run` would after starting it.
    # FAKE_TOOLBOX_STOPPED=never: the container never becomes reachable.
    if [ "${FAKE_TOOLBOX_STOPPED:-0}" != 0 ]; then
      count_file="${FAKE_TOOLBOX_LOG}.true-count"
      count=$(cat "$count_file" 2>/dev/null || printf 0)
      count=$((count + 1))
      printf '%s' "$count" > "$count_file"
      if [ "${FAKE_TOOLBOX_STOPPED}" = never ] || [ "$count" -lt 2 ]; then
        exit 1
      fi
      exit 0
    fi
    [ "${FAKE_TOOLBOX_CONTAINER_OK:-0}" = 1 ] || exit 1
    printf 1 > "${FAKE_TOOLBOX_LOG}.started"
    ;;
  env) [ "${FAKE_TOOLBOX_REEXEC_FAIL:-0}" != 1 ] || exit 1 ;;
  sh) [ "${FAKE_TOOLBOX_HAS_DARLING:-0}" = 1 ] || exit 1 ;;
esac
exit 0
"""

FAKE_DISTROBOX = """#!/bin/sh
# Fake distrobox: records argv and DBX_CONTAINER_MANAGER, then behaves per FAKE_TOOLBOX_*.
printf '%s\\n' "${DBX_CONTAINER_MANAGER:-}" >> "${FAKE_DISTROBOX_LOG:?}.manager"
printf '%s\\n' "$*" >> "${FAKE_DISTROBOX_LOG:?}"
case "${1:-}" in enter) shift ;; *) exit 0 ;; esac
while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-tty) shift ;;
    --name) shift 2 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
case "${1:-}" in
  true)
    [ "${FAKE_TOOLBOX_CONTAINER_OK:-0}" = 1 ] || exit 1
    printf 1 > "${FAKE_TOOLBOX_LOG}.started"
    ;;
  env) [ "${FAKE_TOOLBOX_REEXEC_FAIL:-0}" != 1 ] || exit 1 ;;
  sh) [ "${FAKE_TOOLBOX_HAS_DARLING:-0}" = 1 ] || exit 1 ;;
esac
exit 0
"""


FAKE_PODMAN = """#!/bin/sh
# Fake podman: reports existence, labels and running state from FAKE_PODMAN_*.
printf '%s\\n' "$*" >> "${FAKE_PODMAN_LOG:?}"
case "${1:-}" in container) shift ;; esac
case "${1:-}" in
  exists)
    [ "${FAKE_PODMAN_EXISTS:-1}" = 1 ] || exit 1
    ;;
  inspect)
    case "${3:-}" in
      *Labels*)
        [ "${FAKE_PODMAN_EXISTS:-1}" = 1 ] || exit 1
        printf '%s\\n' "${FAKE_PODMAN_MANAGER-distrobox}"
        exit 0
        ;;
    esac
    running="${FAKE_PODMAN_RUNNING:-0}"
    # FAKE_PODMAN_STARTS=1: the container counts as running only after a
    # `toolbox run` started it, so a stop can fail after the update.
    if [ "$running" != 1 ] && [ "${FAKE_PODMAN_STARTS:-0}" = 1 ] &&
       [ -f "${FAKE_TOOLBOX_LOG}.started" ]; then
      running=1
    fi
    [ "$running" = 1 ] || exit 1
    case "${3:-}" in
      *State.Pid*) printf '%s\\n' "${FAKE_PODMAN_PID:-0}" ;;
      *State.ConmonPid*) printf '%s\\n' "${FAKE_PODMAN_CONMON_PID:-0}" ;;
      *.Id*) printf '%s\\n' "${FAKE_PODMAN_ID:-}" ;;
      *) printf 'true\\n' ;;
    esac
    ;;
  stop)
    if [ "${FAKE_PODMAN_STOP_FAIL:-0}" = 1 ]; then exit 1; fi
    ;;
esac
exit 0
"""


def tool_sandbox(folder, *names):
    """Return a PATH dir exposing only the given host tools."""
    bindir = Path(folder) / 'tools'
    bindir.mkdir()
    for name in names:
        resolved = shutil.which(name)
        if resolved is None:
            raise unittest.SkipTest(f'{name} is unavailable')
        os.symlink(resolved, bindir / name)
    return bindir


class UpdateToolboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Path('/run/.toolboxenv').exists():
            raise unittest.SkipTest('test host runs inside a Toolbox container')
        if SH is None:
            raise unittest.SkipTest('sh is unavailable')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='update-toolbox ')
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        self.folder = folder
        self.repo = folder / 'repo'
        self.home = folder / 'home'
        self.bindir = folder / 'bin'
        self.log = folder / 'toolbox.log'
        self.distrobox_log = folder / 'distrobox.log'
        self.podman_log = folder / 'podman.log'
        (self.repo / 'scripts').mkdir(parents=True)
        self.home.mkdir()
        self.bindir.mkdir()
        shutil.copy2(UPDATE, self.repo / 'scripts' / 'update.sh')
        (self.bindir / 'toolbox').write_text(FAKE_TOOLBOX)
        (self.bindir / 'toolbox').chmod(0o755)
        (self.bindir / 'distrobox').write_text(FAKE_DISTROBOX)
        (self.bindir / 'distrobox').chmod(0o755)
        (self.bindir / 'podman').write_text(FAKE_PODMAN)
        (self.bindir / 'podman').chmod(0o755)
        # The client probe scans the real /proc, so answer it from a stub: the
        # tests must not depend on a Darling session running on the host. The
        # probe reads a result token from stdout, so an empty "none" scan is a
        # completed scan and a stub that says nothing is a failed probe.
        self.stub(self.bindir / 'python3',
                  'for arg in "$@"; do\n'
                  '  if [ "$arg" = - ]; then echo none; fi\n'
                  '  case "$arg" in\n'
                  '    *darling-install-stamp.py*)\n'
                  '      case "$*" in\n'
                  '        *check*) [ "${FAKE_INSTALL_STAMP_CURRENT:-0}" = 1 ] || exit 1 ;;\n'
                  '      esac ;;\n'
                  '    *sdk-sign-stamp.py*)\n'
                  '      case "$*" in\n'
                  '        *check*) [ "${FAKE_SDK_STAMP_CURRENT:-0}" = 1 ] || exit 1 ;;\n'
                  '      esac ;;\n'
                  '  esac\n'
                  'done\nexit 0')
        self.env = dict(os.environ)
        self.env.update(HOME=str(self.home), FAKE_TOOLBOX_LOG=str(self.log),
                        FAKE_DISTROBOX_LOG=str(self.distrobox_log),
                        FAKE_PODMAN_LOG=str(self.podman_log),
                        DARLING_TOOLBOX_ENV=str(folder / 'missing-toolboxenv'),
                        DARLING_CONTAINER_ENV=str(folder / 'missing-containerenv'))
        self.env['PATH'] = f'{self.bindir}:{self.env["PATH"]}'
        self.env.pop('DARLING_WORKSPACE', None)
        self.env.pop('DARLING_LOL_UPDATE_REEXEC', None)
        self.env.pop('CONTAINER_ID', None)

    def run_update(self, *args, input_text=None, **overrides):
        env = dict(self.env)
        env.update(overrides)
        return subprocess.run(
            [SH, str(self.repo / 'scripts' / 'update.sh'), *args],
            capture_output=True, text=True, check=False, env=env,
            input=input_text,
        )

    def run_update_in_terminal(self, answer, *args, **overrides):
        """Run the updater with a pseudo-terminal on stdin, as a user would."""
        env = dict(self.env)
        env.update(overrides)
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(
                [SH, str(self.repo / 'scripts' / 'update.sh'), *args],
                stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env,
            )
            os.close(slave)
            slave = -1
            os.write(master, answer.encode())
            try:
                stdout, stderr = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                raise
        finally:
            os.close(master)
            if slave != -1:
                os.close(slave)
        return subprocess.CompletedProcess(proc.args, proc.returncode,
                                           stdout, stderr)

    def argv_lines(self):
        if not self.log.exists():
            return []
        return self.log.read_text().splitlines()

    def distrobox_lines(self):
        if not self.distrobox_log.exists():
            return []
        return self.distrobox_log.read_text().splitlines()

    def podman_lines(self):
        if not self.podman_log.exists():
            return []
        return self.podman_log.read_text().splitlines()

    def write_marker(self, name):
        build = self.repo / 'build'
        build.mkdir(exist_ok=True)
        (build / 'toolbox-container').write_text(name + '\n')

    def write_native_marker(self):
        build = self.repo / 'build'
        build.mkdir(exist_ok=True)
        (build / 'native-install').write_text('')

    def use_real_python(self):
        """Replace the probe stub with the host interpreter for scan tests."""
        real = shutil.which('python3')
        if real is None:
            self.skipTest('python3 is unavailable')
        (self.bindir / 'python3').unlink()
        os.symlink(real, self.bindir / 'python3')

    def proc_entry(self, pid, cmdline=None):
        """Create an UPDATE_PROC_ROOT entry; no cmdline means a vanished process."""
        entry = self.folder / 'proc' / str(pid)
        entry.mkdir(parents=True)
        if cmdline is not None:
            (entry / 'cmdline').write_bytes(
                b'\0'.join(part.encode() for part in cmdline) + b'\0')
        return entry

    def unreadable_proc_entry(self, pid):
        if os.geteuid() == 0:
            self.skipTest('root can read a mode-000 cmdline')
        entry = self.proc_entry(pid, ['/usr/bin/python3', 'player'])
        (entry / 'cmdline').chmod(0)
        return entry

    def write_desktop_entry(self, container, script_repo=None):
        """Write the launcher entry as tools/desktop-entry.py would for script_repo."""
        script_repo = Path(self.repo if script_repo is None else script_repo)
        apps = self.home / '.local/share/applications'
        apps.mkdir(parents=True, exist_ok=True)
        (apps / 'darling-launcher.desktop').write_text(
            '[Desktop Entry]\n'
            f'Exec=toolbox run -c {container} {script_repo}/scripts/desktop-run.sh\n'
        )

    def assert_reexec_into(self, name):
        self.assertIn(REEXEC_ARGV.format(name=name, repo=self.repo), self.argv_lines())

    def assert_no_reexec(self):
        self.assertFalse(any('--container' in line for line in self.argv_lines()),
                         self.argv_lines())

    def test_reexec_from_marker(self):
        self.write_marker('mycont')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('mycont')

    def test_marker_takes_precedence_over_desktop_entry(self):
        self.write_marker('a')
        self.write_desktop_entry('b')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('a')

    def test_reexec_from_desktop_entry_of_this_checkout(self):
        self.write_desktop_entry('deskcont')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('deskcont')

    def test_ignores_desktop_entry_of_another_checkout(self):
        self.write_desktop_entry('other', script_repo=Path('/outro/checkout'))
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn('No existing build', result.stderr)
        self.assert_no_reexec()

    def test_native_install_marker_blocks_all_detection(self):
        self.write_native_marker()
        self.write_marker('mycont')
        self.write_desktop_entry('deskcont')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1', FAKE_TOOLBOX_HAS_DARLING='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('No existing build', result.stderr)
        self.assertEqual(self.argv_lines(), [])

    def test_legacy_checkout_falls_back_to_default_container(self):
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1', FAKE_TOOLBOX_HAS_DARLING='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('darling-lol')

    def test_no_reexec_when_nothing_matches(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn('No existing build', result.stderr)
        self.assert_no_reexec()

    def test_container_unreachable_prompts_and_starts_it_by_default(self):
        self.write_marker('gone')
        result = self.run_update_in_terminal('\n', FAKE_TOOLBOX_STOPPED='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('is not reachable', result.stdout)
        self.assertIn('[Y/n]', result.stdout)
        self.assertIn("Starting Toolbox container 'gone'", result.stdout)
        self.assert_reexec_into('gone')
        # The failed probe and the successful start are both recorded.
        self.assertEqual(
            sum(1 for line in self.argv_lines() if line.endswith(' true')), 2)

    def test_nonterminal_input_keeps_the_default(self):
        self.write_marker('gone')
        # A pipe is not a terminal, so a piped answer is ignored and the
        # default (start the container) applies instead of blocking or
        # declining.
        result = self.run_update(input_text='n\n', FAKE_TOOLBOX_STOPPED='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Starting Toolbox container 'gone'", result.stdout)
        self.assert_reexec_into('gone')

    def test_container_unreachable_declined_exits_1_with_hint(self):
        self.write_marker('gone')
        result = self.run_update_in_terminal('n\n', FAKE_TOOLBOX_STOPPED='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('that container is not', result.stderr)
        self.assertIn('toolbox enter gone', result.stderr)
        self.assert_no_reexec()

    def test_container_that_never_starts_exits_1_with_hint(self):
        self.write_marker('gone')
        # Keep the retry delay out of the test.
        self.stub(self.bindir / 'sleep', 'exit 0')
        result = self.run_update(input_text='\n', FAKE_TOOLBOX_STOPPED='never')
        self.assertEqual(result.returncode, 1)
        self.assertIn("Toolbox container 'gone' could not be started", result.stderr)
        self.assertIn('toolbox enter gone', result.stderr)
        self.assert_no_reexec()

    def test_running_but_unreachable_container_is_restarted_by_default(self):
        self.write_marker('stuck')
        result = self.run_update_in_terminal('\n', FAKE_TOOLBOX_STOPPED='1',
                                             FAKE_PODMAN_RUNNING='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('is running but not reachable', result.stdout)
        self.assertIn('Restart it and continue the update? [Y/n]', result.stdout)
        self.assertIn("Restarting Toolbox container 'stuck'", result.stdout)
        self.assertIn('container inspect --format {{.State.Running}} stuck',
                      self.podman_lines())
        self.assertIn('stop --time 10 stuck', self.podman_lines())
        self.assertIn('start stuck', self.podman_lines())
        self.assert_reexec_into('stuck')

    def test_running_container_restart_declined_exits_1_with_restart_hint(self):
        self.write_marker('stuck')
        result = self.run_update_in_terminal('n\n', FAKE_TOOLBOX_STOPPED='1',
                                             FAKE_PODMAN_RUNNING='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('unreachable', result.stderr)
        self.assertIn('podman restart stuck', result.stderr)
        self.assertNotIn('stop --time 10 stuck', self.podman_lines())
        self.assert_no_reexec()

    def test_nonterminal_running_container_restarts_it_by_default(self):
        self.write_marker('stuck')
        result = self.run_update(input_text='n\n', FAKE_TOOLBOX_STOPPED='1',
                                 FAKE_PODMAN_RUNNING='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Restarting Toolbox container 'stuck'", result.stdout)
        self.assert_reexec_into('stuck')

    def test_stopped_container_is_started_without_a_restart(self):
        self.write_marker('gone')
        result = self.run_update_in_terminal('\n', FAKE_TOOLBOX_STOPPED='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('is not reachable. Start it and continue the update?',
                      result.stdout)
        self.assertIn("Starting Toolbox container 'gone'", result.stdout)
        self.assertNotIn('start gone', self.podman_lines())
        # The update started it, so it stops it again once the work is done.
        self.assertEqual(self.podman_lines().count('stop --time 10 gone'), 1)
        self.assert_reexec_into('gone')

    def test_running_container_that_never_recovers_hints_a_restart(self):
        self.write_marker('stuck')
        self.stub(self.bindir / 'sleep', 'exit 0')
        result = self.run_update(input_text='\n', FAKE_TOOLBOX_STOPPED='never',
                                 FAKE_PODMAN_RUNNING='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn("Toolbox container 'stuck' could not be started", result.stderr)
        self.assertIn('podman restart stuck', result.stderr)
        self.assert_no_reexec()

    CONTAINER_ID = 'c0ffee' * 10 + 'beef'
    CONMON_PID = 4100

    def stop_failure_victim(self, cgroup='0::/user.slice/app.slice/app-browser.scope',
                            comm='sleep', ppid=1, conmon_comm='conmon'):
        """Start a real process standing in for the PID podman recorded.

        Its ownership is answered from a fake proc root: the cgroup it runs
        in, its parent and the parent's command name. Return the victim and
        the overrides that make podman report it with a failing stop.
        """
        # Keep the retry delay out of the test.
        self.stub(self.bindir / 'sleep', 'exit 0')
        victim = subprocess.Popen(['sleep', '30'])
        self.addCleanup(victim.wait)
        self.addCleanup(victim.kill)
        proc = self.folder / 'proc'
        entry = proc / str(victim.pid)
        entry.mkdir(parents=True)
        (entry / 'cgroup').write_text(cgroup + '\n')
        (entry / 'stat').write_text(f'{victim.pid} ({comm}) S {ppid} {ppid} 1 0 -1\n')
        conmon = proc / str(self.CONMON_PID)
        conmon.mkdir()
        (conmon / 'comm').write_text(conmon_comm + '\n')
        return victim, dict(
            FAKE_TOOLBOX_STOPPED='1', FAKE_PODMAN_RUNNING='1',
            FAKE_PODMAN_STOP_FAIL='1', FAKE_PODMAN_PID=str(victim.pid),
            FAKE_PODMAN_ID=self.CONTAINER_ID,
            FAKE_PODMAN_CONMON_PID=str(self.CONMON_PID),
            UPDATE_PROC_ROOT=str(proc))

    def assert_victim_left_alone(self, victim, result):
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'PID {victim.pid} no longer belongs to it', result.stderr)
        self.assertNotIn('signaling PID', result.stderr)
        self.assertIn("Toolbox container 'stuck' could not be stopped", result.stderr)
        self.assertIn('podman stop stuck', result.stderr)
        self.assertNotIn('start stuck', self.podman_lines())
        self.assertIsNone(victim.poll())
        self.assert_no_reexec()

    def test_stop_failure_signals_the_init_pid_of_the_container_conmon(self):
        self.write_marker('stuck')
        # The command name holds spaces and parentheses; the parent PID must
        # still be read from the field after the last ')'.
        victim, overrides = self.stop_failure_victim(
            comm='init) S 1 (x', ppid=self.CONMON_PID)
        result = self.run_update_in_terminal('\n', **overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"signaling PID {victim.pid}", result.stderr)
        self.assertIn('start stuck', self.podman_lines())
        # The warning alone would pass if the production SIGTERM were removed.
        self.assertEqual(victim.wait(timeout=5), -signal.SIGTERM)
        self.assert_reexec_into('stuck')

    def test_nonterminal_stop_failure_signals_a_pid_in_the_container_cgroup(self):
        self.write_marker('stuck')
        # Without a terminal the default answer restarts the container, and
        # the ownership check alone guards the signal.
        victim, overrides = self.stop_failure_victim(
            cgroup=f'0::/user.slice/user@1000.service/user.slice/libpod-{self.CONTAINER_ID}.scope/container')
        result = self.run_update(input_text='n\n', **overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"signaling PID {victim.pid}", result.stderr)
        self.assertEqual(victim.wait(timeout=5), -signal.SIGTERM)
        self.assert_reexec_into('stuck')

    def test_stop_failure_leaves_a_reused_pid_alone(self):
        self.write_marker('stuck')
        # After a suspend the recorded PID now belongs to an unrelated process
        # of this user: another cgroup, another parent.
        victim, overrides = self.stop_failure_victim(comm='Web Content')
        result = self.run_update_in_terminal('\n', **overrides)
        self.assert_victim_left_alone(victim, result)

    def test_nonterminal_stop_failure_leaves_a_reused_pid_alone(self):
        self.write_marker('stuck')
        victim, overrides = self.stop_failure_victim()
        result = self.run_update(input_text='\n', **overrides)
        self.assert_victim_left_alone(victim, result)

    def test_stop_failure_rejects_a_recorded_conmon_pid_that_is_not_conmon(self):
        self.write_marker('stuck')
        # The conmon PID can be stale too: a matching parent only counts while
        # that parent is still conmon.
        victim, overrides = self.stop_failure_victim(
            ppid=self.CONMON_PID, conmon_comm='firefox')
        result = self.run_update_in_terminal('\n', **overrides)
        self.assert_victim_left_alone(victim, result)

    def test_running_container_is_left_alone_when_the_client_probe_fails(self):
        self.write_marker('stuck')
        # An interpreter that fails before the scan prints nothing and exits 1,
        # the status a completed no-client scan used to share.
        self.stub(self.bindir / 'python3', 'exit 1')
        result = self.run_update_in_terminal('\n', FAKE_TOOLBOX_STOPPED='1',
                                             FAKE_PODMAN_RUNNING='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Could not check for a running Riot Client or League process',
                      result.stderr)
        self.assertIn('left untouched', result.stderr)
        self.assertNotIn('stop --time 10 stuck', self.podman_lines())
        self.assert_no_reexec()

    def test_failed_client_probe_stops_the_update(self):
        (self.repo / 'build/darling-src/build').mkdir(parents=True)
        self.write_native_marker()
        # The scan itself raised: nonzero status with no result token.
        self.stub(self.bindir / 'python3', 'exit 2')
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Could not check for a running Riot Client or League process',
                      result.stderr)
        self.assertIn('refusing to update', result.stderr)

    def test_running_container_is_left_alone_while_clients_are_open(self):
        self.write_marker('stuck')
        # Pretend the Riot Client probe found a live session.
        self.stub(self.bindir / 'python3', 'echo running')
        result = self.run_update_in_terminal('\n', FAKE_TOOLBOX_STOPPED='1',
                                             FAKE_PODMAN_RUNNING='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('A Riot Client or League process is running', result.stderr)
        self.assertIn('was left untouched', result.stderr)
        self.assertNotIn('stop --time 10 stuck', self.podman_lines())
        self.assert_no_reexec()

    def test_real_scan_finds_a_running_client_and_refuses_the_restart(self):
        self.write_marker('stuck')
        self.use_real_python()
        self.proc_entry(4242, ['/Applications/Riot Client.app/Contents/MacOS/RiotClientServices'])
        result = self.run_update_in_terminal(
            '\n', FAKE_TOOLBOX_STOPPED='1', FAKE_PODMAN_RUNNING='1',
            UPDATE_PROC_ROOT=str(self.folder / 'proc'))
        self.assertEqual(result.returncode, 1)
        self.assertIn('A Riot Client or League process is running', result.stderr)
        self.assertIn('was left untouched', result.stderr)
        self.assertNotIn('stop --time 10 stuck', self.podman_lines())
        self.assert_no_reexec()

    def test_scan_skips_a_process_that_exited_during_the_scan(self):
        self.write_marker('stuck')
        self.use_real_python()
        # A process gone before its cmdline could be read leaves the empty
        # directory behind; that is not an unreadable live process.
        self.proc_entry(4242)
        result = self.run_update_in_terminal(
            '\n', FAKE_TOOLBOX_STOPPED='1', FAKE_PODMAN_RUNNING='1',
            UPDATE_PROC_ROOT=str(self.folder / 'proc'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Restarting Toolbox container 'stuck'", result.stdout)
        self.assert_reexec_into('stuck')

    def test_unreadable_owned_process_refuses_the_restart(self):
        self.write_marker('stuck')
        self.use_real_python()
        # An unreadable entry of this user could be the client, so it must not
        # be read as "no client".
        self.unreadable_proc_entry(4242)
        result = self.run_update_in_terminal(
            '\n', FAKE_TOOLBOX_STOPPED='1', FAKE_PODMAN_RUNNING='1',
            UPDATE_PROC_ROOT=str(self.folder / 'proc'))
        self.assertEqual(result.returncode, 1)
        self.assertIn('Could not check for a running Riot Client or League process',
                      result.stderr)
        self.assertIn('left untouched', result.stderr)
        self.assertNotIn('stop --time 10 stuck', self.podman_lines())
        self.assert_no_reexec()

    def test_unreadable_owned_process_stops_a_native_update(self):
        (self.repo / 'build/darling-src/build').mkdir(parents=True)
        self.write_native_marker()
        self.use_real_python()
        self.unreadable_proc_entry(4242)
        result = self.run_update(UPDATE_PROC_ROOT=str(self.folder / 'proc'))
        self.assertEqual(result.returncode, 1)
        self.assertIn('Could not check for a running Riot Client or League process',
                      result.stderr)
        self.assertIn('refusing to update', result.stderr)

    def test_toolbox_missing_gives_distinct_message(self):
        self.write_marker('mycont')
        # PATH must not contain the fake toolbox (nor a real one): only the
        # tools update.sh needs before its first toolbox call.
        bindir = tool_sandbox(Path(self.env['FAKE_TOOLBOX_LOG']).parent,
                              'cat', 'sed', 'grep', 'head', 'dirname')
        result = self.run_update(PATH=str(bindir))
        self.assertEqual(result.returncode, 1)
        self.assertIn('is not installed on this host', result.stderr)
        self.assertIn('toolbox enter mycont', result.stderr)
        self.assertEqual(self.argv_lines(), [])

    def test_reexec_guard_prevents_loop(self):
        self.write_marker('mycont')
        result = self.run_update(DARLING_LOL_UPDATE_REEXEC='1', FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('No existing build', result.stderr)
        self.assertEqual(self.argv_lines(), [])

    def test_workspace_branch_unaffected(self):
        result = self.run_update(DARLING_WORKSPACE='/nix/store/abc/share/darling-lol')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.argv_lines(), [])

    def test_verbose_flag_is_forwarded_to_the_container(self):
        self.write_marker('mycont')
        result = self.run_update('--verbose', FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'{REEXEC_ARGV.format(name="mycont", repo=self.repo)} --verbose',
                      self.argv_lines())

    def test_container_started_for_the_update_is_stopped_afterwards(self):
        self.write_marker('mycont')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('mycont')
        self.assertIn("Stopping Toolbox container 'mycont'", result.stdout)
        self.assertIn('Stopped Toolbox container', result.stdout)
        self.assertEqual(self.podman_lines().count('stop --time 10 mycont'), 1)

    def test_container_already_running_is_left_running(self):
        self.write_marker('mycont')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1',
                                 FAKE_PODMAN_RUNNING='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_reexec_into('mycont')
        self.assertNotIn('stop --time 10 mycont', self.podman_lines())
        self.assertNotIn('Stopping Toolbox container', result.stdout)

    def test_failed_update_still_stops_the_container_it_started(self):
        self.write_marker('mycont')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1',
                                 FAKE_TOOLBOX_REEXEC_FAIL='1')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.podman_lines().count('stop --time 10 mycont'), 1)
        self.assertIn('Stopped Toolbox container', result.stdout)

    def test_stop_failure_warns_without_changing_the_update_status(self):
        self.write_marker('mycont')
        result = self.run_update(FAKE_TOOLBOX_CONTAINER_OK='1',
                                 FAKE_PODMAN_STOP_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not stop Toolbox container 'mycont'; it is still running",
                      result.stderr)

    def test_final_stop_signals_the_init_pid_when_podman_refuses(self):
        self.write_marker('mycont')
        # The container is started by the update, so the final stop runs; the
        # `operation not permitted` refusal is recovered the same way as a
        # restart, through the recorded init PID.
        victim, overrides = self.stop_failure_victim(
            cgroup=f'0::/user.slice/user@1000.service/user.slice/libpod-{self.CONTAINER_ID}.scope/container')
        overrides.pop('FAKE_PODMAN_RUNNING')
        overrides.pop('FAKE_TOOLBOX_STOPPED')
        overrides.update(FAKE_PODMAN_STARTS='1', FAKE_TOOLBOX_CONTAINER_OK='1')
        result = self.run_update(**overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'signaling PID {victim.pid}', result.stderr)
        self.assertIn("Stopped Toolbox container 'mycont'", result.stdout)
        self.assertEqual(victim.wait(timeout=5), -signal.SIGTERM)

    def test_help_exits_without_touching_the_container(self):
        result = self.run_update('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--verbose', result.stdout)
        self.assertEqual(self.argv_lines(), [])

    def test_unknown_option_is_rejected(self):
        result = self.run_update('--bogus')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Unknown option: --bogus', result.stderr)
        self.assertEqual(self.argv_lines(), [])

    def stub(self, path, body):
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def prepare_stub_update(self, build_exit=0):
        (self.repo / 'build/darling-src/build').mkdir(parents=True)
        self.write_native_marker()
        self.stub(self.repo / 'scripts/build.sh',
                  f'echo build-diagnostic >&2\nexit {build_exit}')
        self.stub(self.repo / 'scripts/install-runtime.sh', 'echo overlay-ran')
        self.stub(self.bindir / 'sudo', 'echo sudo-foreground')
        # A privileged command must never go through the detachment wrapper.
        self.stub(self.bindir / 'setsid',
                  'if [ "$1" = sudo ]; then exit 97; fi\nexec "$@"')

    def test_quiet_update_preserves_failure_and_its_saved_log(self):
        self.prepare_stub_update(build_exit=23)
        cache = self.home / '.cache'
        result = self.run_update(XDG_CACHE_HOME=str(cache))
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertIn('build-diagnostic', result.stderr)
        self.assertNotIn('sudo-foreground', result.stdout)
        logs = list(cache.glob('darling-lol-update.*.log'))
        self.assertEqual(len(logs), 1)
        self.assertIn(f'Full log: {logs[0]}', result.stderr)

    def test_quiet_update_streams_failure_if_cache_cannot_be_created(self):
        self.prepare_stub_update(build_exit=23)
        result = self.run_update(XDG_CACHE_HOME='/dev/null/no-cache')
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertIn('build-diagnostic', result.stderr)
        self.assertIn('No update log was saved.', result.stderr)
        self.assertNotIn('Full log:', result.stderr)

    def test_quiet_update_logs_sudo_output_after_terminal_auth(self):
        self.prepare_stub_update()
        sudo_log = self.home / 'sudo-argv.log'
        self.stub(self.bindir / 'sudo',
                  f'printf "%s\\n" "$*" >> "{sudo_log}"\n'
                  '[ "$1" = -v ] || echo sudo-install-output')
        cache = self.home / '.cache'
        result = self.run_update(XDG_CACHE_HOME=str(cache))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('sudo-install-output', result.stdout)
        self.assertIn('Log:', result.stdout)
        logs = list(cache.glob('darling-lol-update.*.log'))
        self.assertEqual(len(logs), 1)
        self.assertIn('sudo-install-output', logs[0].read_text())
        calls = sudo_log.read_text().splitlines()
        self.assertEqual(calls[0], '-v')
        self.assertIn('-n cmake --install build/darling-src/build', calls)

    def test_verbose_update_streams_sudo_output(self):
        self.prepare_stub_update()
        result = self.run_update('--verbose')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sudo-foreground', result.stdout)

    def test_current_install_stamp_skips_the_privileged_install(self):
        self.prepare_stub_update()
        sudo_log = self.home / 'sudo-argv.log'
        self.stub(self.bindir / 'sudo', f'printf "%s\\n" "$*" >> "{sudo_log}"\n')
        self.env['FAKE_INSTALL_STAMP_CURRENT'] = '1'
        result = self.run_update('--verbose')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Darling system components are up to date', result.stdout)
        self.assertNotIn('Installing Darling system components', result.stdout)
        self.assertFalse(sudo_log.exists())

    def test_quiet_update_surfaces_overlay_signing_warning(self):
        self.prepare_stub_update()
        self.stub(self.repo / 'scripts/install-runtime.sh',
                  'echo "WARNING: the darling-lol runtime is NOT signed." >&2\nexit 0')
        result = self.run_update(XDG_CACHE_HOME=str(self.home / '.cache'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('user runtime overlays were NOT signed', result.stderr)

    def test_update_signs_non_writable_sdk_under_sudo(self):
        self.prepare_stub_update()
        sdk = self.home / 'readonly-sdk'
        sdk.mkdir()
        sdk.chmod(0o555)
        self.addCleanup(sdk.chmod, 0o755)
        (self.repo / 'tools/dc-codesign').mkdir(parents=True)
        self.stub(self.repo / 'tools/dc-codesign/dc-codesign', 'exit 0')
        self.stub(self.repo / 'tools/sign-darling-runtime.sh', 'exit 0')
        rcodesign = self.home / '.local/bin/rcodesign'
        rcodesign.parent.mkdir(parents=True)
        self.stub(rcodesign, 'exit 0')
        sudo_log = self.home / 'sudo.log'
        self.stub(self.bindir / 'sudo',
                  f'printf "%s\\n" "$*" >> "{sudo_log}"\n'
                  'if [ "${FAKE_SUDO_SIGN_FAIL:-0}" = 1 ] && [ "$1" = env ]; then exit 1; fi\n'
                  'exit 0')
        # Exclude any host rcodesign from PATH so resolve_rcodesign exercises $HOME/.local/bin.
        clean_path = os.pathsep.join(
            entry for entry in self.env['PATH'].split(os.pathsep)
            if not (Path(entry) / 'rcodesign').is_file()
        )

        result = self.run_update(XDG_CACHE_HOME=str(self.home / '.cache'),
                                 DARLING_SDK_DIR=str(sdk), PATH=clean_path, RCODESIGN='')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = sudo_log.read_text().splitlines()
        self.assertTrue(
            any(f'env RCODESIGN={rcodesign} sh {self.repo}/tools/sign-darling-runtime.sh --sdk --quiet' == line
                for line in calls),
            calls,
        )

        # A failing SDK sign warns prominently instead of staying silent.
        result = self.run_update(XDG_CACHE_HOME=str(self.home / '.cache'),
                                 DARLING_SDK_DIR=str(sdk), PATH=clean_path, RCODESIGN='',
                                 FAKE_SUDO_SIGN_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Base Darling SDK signing failed', result.stderr)

    def test_current_signing_stamp_skips_the_privileged_signing(self):
        self.prepare_stub_update()
        sdk = self.home / 'readonly-sdk'
        sdk.mkdir()
        sdk.chmod(0o555)
        self.addCleanup(sdk.chmod, 0o755)
        (self.repo / 'tools').mkdir()
        self.stub(self.repo / 'tools/sign-darling-runtime.sh', 'exit 0')
        sudo_log = self.home / 'sudo.log'
        self.stub(self.bindir / 'sudo', f'printf "%s\\n" "$*" >> "{sudo_log}"\n')
        result = self.run_update(XDG_CACHE_HOME=str(self.home / '.cache'),
                                 DARLING_SDK_DIR=str(sdk),
                                 FAKE_INSTALL_STAMP_CURRENT='1',
                                 FAKE_SDK_STAMP_CURRENT='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Darling system components are signed', result.stdout)
        self.assertNotIn('Signing Darling system components', result.stdout)
        calls = sudo_log.read_text().splitlines() if sudo_log.exists() else []
        self.assertFalse(any('sign-darling-runtime.sh' in line for line in calls), calls)

    def test_distrobox_marker_reenters_and_stops_container(self):
        self.write_marker('my-dbx')
        (self.repo / 'build/toolbox-backend').write_text('distrobox\n')
        result = self.run_update(input_text='', FAKE_TOOLBOX_CONTAINER_OK='1', FAKE_PODMAN_STARTS='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = (
            f'enter --no-tty --name my-dbx -- env DARLING_LOL_UPDATE_REEXEC=1 '
            f'sh {self.repo}/scripts/update.sh'
        )
        self.assertIn(expected, self.distrobox_lines())
        self.assertEqual(self.argv_lines(), [])
        self.assertIn('stop --time 10 my-dbx', self.podman_lines())

    def test_distrobox_desktop_entry_reenters_distrobox(self):
        apps = self.home / '.local/share/applications'
        apps.mkdir(parents=True, exist_ok=True)
        (apps / 'darling-launcher.desktop').write_text(
            '[Desktop Entry]\n'
            f'Exec=env DBX_CONTAINER_MANAGER=podman distrobox enter --no-tty --name deskdbx -- {self.repo}/scripts/desktop-run.sh\n'
        )
        result = self.run_update(input_text='', FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            any('enter --no-tty --name deskdbx -- ' in line for line in self.distrobox_lines()),
            self.distrobox_lines(),
        )

    def test_legacy_checkout_falls_back_to_distrobox_when_toolbox_missing(self):
        (self.bindir / 'toolbox').unlink()
        bindir = tool_sandbox(self.folder, 'cat', 'dirname', 'grep', 'sed', 'head', 'sleep')
        path = f'{self.bindir}:{bindir}'
        result = self.run_update(input_text='', PATH=path, FAKE_TOOLBOX_CONTAINER_OK='1',
                                 FAKE_TOOLBOX_HAS_DARLING='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            any('enter --no-tty --name darling-lol -- ' in line for line in self.distrobox_lines()),
            self.distrobox_lines(),
        )

    def test_legacy_checkout_skips_microos_toolbox_and_uses_distrobox(self):
        result = self.run_update(
            input_text='',
            FAKE_TOOLBOX_BAD_VERSION='1',
            FAKE_TOOLBOX_CONTAINER_OK='1',
            FAKE_TOOLBOX_HAS_DARLING='1',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.argv_lines(), [])
        self.assertTrue(
            any('enter --no-tty --name darling-lol -- ' in line for line in self.distrobox_lines()),
            self.distrobox_lines(),
        )

    def test_distrobox_missing_gives_distinct_message(self):
        self.write_marker('my-dbx')
        (self.repo / 'build/toolbox-backend').write_text('distrobox\n')
        bindir = tool_sandbox(self.folder, 'cat', 'dirname', 'grep', 'sed', 'head', 'sleep')
        result = self.run_update(input_text='', PATH=str(bindir))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Distrobox container 'my-dbx'", result.stderr)
        self.assertIn('is not installed on this host', result.stderr)
        self.assertIn('distrobox enter my-dbx', result.stderr)
        self.assertEqual(self.distrobox_lines(), [])

    def test_distrobox_verbose_forwards_verbose_flag(self):
        self.write_marker('my-dbx')
        (self.repo / 'build/toolbox-backend').write_text('distrobox\n')
        result = self.run_update('--verbose', input_text='', FAKE_TOOLBOX_CONTAINER_OK='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = (
            f'enter --no-tty --name my-dbx -- env DARLING_LOL_UPDATE_REEXEC=1 '
            f'sh {self.repo}/scripts/update.sh --verbose'
        )
        self.assertIn(expected, self.distrobox_lines())

    def test_inside_distrobox_skips_reexec(self):
        self.write_marker('my-dbx')
        (self.repo / 'build/toolbox-backend').write_text('distrobox\n')
        containerenv = self.folder / 'containerenv'
        containerenv.write_text('name="my-dbx"\n')
        result = self.run_update(
            input_text='',
            DARLING_TOOLBOX_ENV=str(self.folder / 'missing-toolboxenv'),
            DARLING_CONTAINER_ENV=str(containerenv),
            CONTAINER_ID='my-dbx',
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('No existing build', result.stderr)
        self.assertEqual(self.distrobox_lines(), [])

    def test_missing_distrobox_container_fails_without_entering_or_prompting(self):
        self.write_marker('my-dbx')
        (self.repo / 'build/toolbox-backend').write_text('distrobox\n')
        result = self.run_update(input_text='', FAKE_PODMAN_EXISTS='0')
        self.assertEqual(result.returncode, 1)
        self.assertIn('does not exist', result.stderr)
        self.assertNotIn('Start it and continue', result.stdout)
        self.assertEqual(self.distrobox_lines(), [])

    def test_stopped_existing_distrobox_container_still_enters_and_stops(self):
        self.write_marker('my-dbx')
        (self.repo / 'build/toolbox-backend').write_text('distrobox\n')
        result = self.run_update(
            input_text='',
            FAKE_PODMAN_EXISTS='1',
            FAKE_PODMAN_RUNNING='0',
            FAKE_PODMAN_STARTS='1',
            FAKE_TOOLBOX_CONTAINER_OK='1',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            any('enter --no-tty --name my-dbx -- ' in line for line in self.distrobox_lines()),
            self.distrobox_lines(),
        )
        self.assertEqual(
            set(Path(f'{self.distrobox_log}.manager').read_text().splitlines()),
            {'podman'},
        )
        self.assertIn('stop --time 10 my-dbx', self.podman_lines())


if __name__ == '__main__':
    unittest.main()
