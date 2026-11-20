"""Run the install shutdown hook with process and privilege commands mocked."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('DARLING_SHUTDOWN_SCRIPT',
                             ROOT / 'build/darling-src/tools/shutdown-user.sh'))


@unittest.skipUnless(SCRIPT.is_file(), 'prepare the pinned Darling sources first')
class ShutdownUserTests(unittest.TestCase):
    def run_hook(self, processes, mode='stop', parent=None):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            state = folder / 'state.json'
            state.write_text(json.dumps({'processes': processes, 'calls': 0}))
            driver = folder / 'driver'
            driver.write_text(f'#!{sys.executable}\n' + '''
import json, os, sys
from pathlib import Path
state = Path(os.environ['TEST_STATE'])
data = json.loads(state.read_text())
command = Path(sys.argv[0]).name
if command == 'whoami':
    print('root')
elif command == 'ps':
    if '-eo' in sys.argv:
        for pid, status, args in data['processes']:
            print(pid, status, args)
    elif 'ppid=' in sys.argv:
        print(os.environ.get('TEST_PARENT', '1'))
    elif os.environ['TEST_MODE'] != 'vanished':
        print('test-user')
    else:
        data['processes'] = []
        state.write_text(json.dumps(data))
elif command == 'timeout':
    data['calls'] += 1
    data.setdefault('commands', []).append(sys.argv[1:])
    if os.environ['TEST_MODE'] == 'stop':
        data['processes'] = [p for p in data['processes'] if p[2] != '/sbin/launchd']
    state.write_text(json.dumps(data))
    if os.environ['TEST_MODE'] == 'failed':
        sys.exit(1)
    if os.environ['TEST_MODE'] == 'timed-out':
        sys.exit(124)
elif command != 'sleep':
    raise RuntimeError('Unexpected command: ' + command)
''')
            driver.chmod(0o755)
            for command in ('whoami', 'ps', 'timeout', 'sleep'):
                (folder / command).symlink_to(driver)
            env = dict(os.environ, PATH=str(folder) + os.pathsep + os.environ['PATH'],
                       TEST_STATE=str(state), TEST_MODE=mode)
            if parent is not None:
                env['TEST_PARENT'] = str(parent)
            result = subprocess.run(['sh', str(SCRIPT)], env=env, capture_output=True,
                                    text=True, timeout=10)
            return result, json.loads(state.read_text())

    def test_no_instances(self):
        result, state = self.run_hook([])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state['calls'], 0)

    def test_ignores_mentions_and_zombies(self):
        result, state = self.run_hook([
            [123, 'S', 'editor launchd.txt'], [124, 'Z', '/sbin/launchd'],
            [125, 'S', 'bash -c echo launchd'],
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state['calls'], 0)

    def test_normal_shutdown(self):
        result, state = self.run_hook([[123, 'S', '/sbin/launchd']])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state['calls'], 1)

    def test_bounded_failure(self):
        for mode in ('failed', 'timed-out', 'stuck'):
            with self.subTest(mode=mode):
                result, state = self.run_hook([[123, 'S', '/sbin/launchd']], mode)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('launchd PID 123 remains alive', result.stderr)
                self.assertEqual(state['calls'], 5)

    def test_shutdown_targets_the_server_prefix(self):
        # darling shutdown without DPREFIX only reaches ~/.darling; the hook
        # reads the prefix from the launchd's parent darlingserver.
        if not shutil.which('cc'):
            self.skipTest('C compiler required')
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / 'server.c'
            source.write_text('#include <sys/prctl.h>\n#include <unistd.h>\n'
                              'int main(void) { prctl(PR_SET_NAME, "darlingserver"); pause(); return 0; }\n')
            subprocess.run(['cc', str(source), '-o', str(folder / 'server')], check=True)
            prefix = '/mnt/other disk/darling'
            server = subprocess.Popen([str(folder / 'server'), prefix, '1000', '1000'])
            try:
                for _ in range(100):  # until prctl has renamed it
                    if Path(f'/proc/{server.pid}/comm').read_text().strip() == 'darlingserver':
                        break
                result, state = self.run_hook([[123, 'S', '/sbin/launchd']], parent=server.pid)
            finally:
                server.kill()
                server.wait()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('in ' + prefix, result.stdout)
            [command] = state['commands']
            self.assertEqual(command[:6], ['15s', 'su', '-s', '/bin/sh', '--login', 'test-user'])
            self.assertEqual(command[-1], prefix)
            # The -c text expands the prefix from $1, never from shell text.
            script, name = command[command.index('-c') + 1], command[command.index('-c') + 2]
            darling = folder / 'darling'
            darling.write_text('#!/bin/sh\nprintf "%s|%s" "$DPREFIX" "$1"\n')
            darling.chmod(0o755)
            shown = subprocess.run(['sh', '-c', script, name, prefix], capture_output=True, text=True,
                                   env=dict(os.environ, PATH=str(folder) + os.pathsep + os.environ['PATH']))
            self.assertEqual(shown.stdout, prefix + '|shutdown')

    def test_unknown_parent_falls_back_to_the_default_prefix(self):
        result, state = self.run_hook([[123, 'S', '/sbin/launchd']], parent=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        [command] = state['commands']
        self.assertEqual(command[-2:], ['-c', 'exec darling shutdown'])

    def test_process_exits_before_owner_query(self):
        result, state = self.run_hook([[123, 'S', '/sbin/launchd']], 'vanished')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state['calls'], 0)


if __name__ == '__main__':
    unittest.main()
