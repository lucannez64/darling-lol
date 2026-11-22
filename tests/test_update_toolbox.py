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
    ;;
  sh) [ "${FAKE_TOOLBOX_HAS_DARLING:-0}" = 1 ] || exit 1 ;;
esac
exit 0
"""


FAKE_PODMAN = """#!/bin/sh
# Fake podman: reports the container state from FAKE_PODMAN_RUNNING, can fail a
# stop, and records every call so a restart can be asserted.
printf '%s\\n' "$*" >> "${FAKE_PODMAN_LOG:?}"
case "${1:-}" in container) shift ;; esac
case "${1:-}" in
  inspect)
    [ "${FAKE_PODMAN_RUNNING:-0}" = 1 ] || exit 1
    case "${3:-}" in
      *State.Pid*) printf '%s\\n' "${FAKE_PODMAN_PID:-0}" ;;
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
        self.repo = folder / 'repo'
        self.home = folder / 'home'
        self.bindir = folder / 'bin'
        self.log = folder / 'toolbox.log'
        self.podman_log = folder / 'podman.log'
        (self.repo / 'scripts').mkdir(parents=True)
        self.home.mkdir()
        self.bindir.mkdir()
        shutil.copy2(UPDATE, self.repo / 'scripts' / 'update.sh')
        (self.bindir / 'toolbox').write_text(FAKE_TOOLBOX)
        (self.bindir / 'toolbox').chmod(0o755)
        (self.bindir / 'podman').write_text(FAKE_PODMAN)
        (self.bindir / 'podman').chmod(0o755)
        # The client probe scans the real /proc, so answer it from a stub: the
        # tests must not depend on a Darling session running on the host. The
        # probe reads a result token from stdout, so an empty "none" scan is a
        # completed scan and a stub that says nothing is a failed probe.
        self.stub(self.bindir / 'python3',
                  'for arg in "$@"; do\n'
                  '  if [ "$arg" = - ]; then echo none; fi\n'
                  'done\nexit 0')
        self.env = dict(os.environ)
        self.env.update(HOME=str(self.home), FAKE_TOOLBOX_LOG=str(self.log),
                        FAKE_PODMAN_LOG=str(self.podman_log))
        self.env['PATH'] = f'{self.bindir}:{self.env["PATH"]}'
        self.env.pop('DARLING_WORKSPACE', None)
        self.env.pop('DARLING_LOL_UPDATE_REEXEC', None)

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
        self.assertNotIn('stop --time 10 gone', self.podman_lines())
        self.assertNotIn('start gone', self.podman_lines())
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

    def test_stop_failure_falls_back_to_signaling_the_recorded_pid(self):
        self.write_marker('stuck')
        # Keep the retry delay out of the test, and give the updater a real
        # process that its SIGTERM fallback can stop.
        self.stub(self.bindir / 'sleep', 'exit 0')
        victim = subprocess.Popen(['sleep', '30'])
        self.addCleanup(victim.wait)
        self.addCleanup(victim.kill)
        result = self.run_update_in_terminal(
            '\n', FAKE_TOOLBOX_STOPPED='1', FAKE_PODMAN_RUNNING='1',
            FAKE_PODMAN_STOP_FAIL='1', FAKE_PODMAN_PID=str(victim.pid))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"signaling PID {victim.pid}", result.stderr)
        self.assertIn('start stuck', self.podman_lines())
        # The warning alone would pass if the production SIGTERM were removed.
        self.assertEqual(victim.wait(timeout=5), -signal.SIGTERM)
        self.assert_reexec_into('stuck')

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


if __name__ == '__main__':
    unittest.main()
