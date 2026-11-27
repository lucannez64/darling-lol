"""launchctl exits the way macOS does for jobs that are not loaded.

Riot Client's agent runs `launchctl list <label>`, then stop/unload, load and
start for its com.riot.riotclient.checkinstalls LaunchAgent. On macOS 15.5:

- `launchctl list <unknown label>` prints `Could not find service "<label>" in
  domain for port` and exits 113;
- `launchctl start|stop <unknown label>` exits 3 (ESRCH);
- `launchctl load|unload <plist>` prints `Load failed: 5: Input/output error`
  and exits 0 when there is nothing to do.

Darling exited 1 in all of these cases, so the agent logged `Failed stopping
existing service` and gave up. These checks read the patch; CI cannot run
Darling.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = 'patches/darling.patch'
LAUNCHCTL = 'src/launchd/support/launchctl.c'


def section(path):
    sections = (ROOT / PATCH).read_text().split('diff --git ')
    return next(s for s in sections if s.startswith('a/' + path + ' b/'))


def added(path):
    return '\n'.join(line[1:] for line in section(path).splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


def removed(path):
    return '\n'.join(line[1:] for line in section(path).splitlines()
                     if line.startswith('-') and not line.startswith('---'))


class LaunchctlExitCodeTests(unittest.TestCase):
    def test_list_unknown_label_exits_113_with_the_macos_message(self):
        code = added(LAUNCHCTL)
        self.assertIn('launch_data_get_errno(resp) == ESRCH', code)
        self.assertIn('Could not find service \\"%s\\" in domain for port', code)
        self.assertIn('r = 113;', code)

    def test_start_and_stop_exit_with_the_error_number(self):
        self.assertIn('r = e;', added(LAUNCHCTL))

    def test_load_and_unload_with_nothing_found_exit_zero(self):
        code = added(LAUNCHCTL)
        self.assertIn('"%s failed: 5: Input/output error", lus.load ? "Load" : "Unload"', code)
        self.assertIn('return 0;', code)
        self.assertIn('return _launchctl_is_managed ? 0 : 1;', removed(LAUNCHCTL))


if __name__ == '__main__':
    unittest.main()
