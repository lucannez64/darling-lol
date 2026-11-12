"""riotclient:// links from the host browser reach the running guest.

Google sign-in finishes in the host browser at riotclient://auth/v1/<token>.
darling-url-handler.desktop declares x-scheme-handler/riotclient and runs
scripts/open-guest-url.sh, which hands the URL to `darling shell /usr/bin/open`
only when a Darling session is already running, and never logs the URL.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / 'tools/open-guest-url.sh'
URL = 'riotclient://auth/v1/eyJhbGciOiJ0ZXN0In0.c2VjcmV0LXRva2Vu.c2ln'


class OpenGuestURLTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tools = self.root / 'tools'
        self.tools.mkdir()
        shutil.copy2(HANDLER, self.tools / 'open-guest-url.sh')
        (self.tools / 'runtime-paths.sh').write_text(f'prefix_dir={self.root}/prefix\n')
        # The session probe answers what the test asks for.
        (self.tools / 'session-supervisor.py').write_text(
            'import os, sys\n'
            'assert sys.argv[1:] == ["--prefix", os.environ["EXPECT_PREFIX"], "--probe-runtime"], sys.argv\n'
            'print(os.environ["FAKE_RUNTIME"])\n')
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        self.calls = self.root / 'darling-calls'
        darling = bin_dir / 'darling'
        darling.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done >> "$CALLS"\n'
                           'printf -- "--\\n" >> "$CALLS"\nexit "${FAKE_OPEN_STATUS:-0}"\n')
        darling.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', CALLS=str(self.calls),
                        EXPECT_PREFIX=f'{self.root}/prefix', FAKE_RUNTIME='running')

    def run_handler(self, url, **env):
        return subprocess.run(['sh', str(self.tools / 'open-guest-url.sh'), url], capture_output=True,
                              text=True, env=dict(self.env, **env), check=False)

    def darling_calls(self):
        return self.calls.read_text().split('--\n')[:-1] if self.calls.exists() else []

    def test_forwards_the_url_as_one_argument_to_open_in_the_guest(self):
        result = self.run_handler(URL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.darling_calls(), [f'shell\n/usr/bin/open\n{URL}\n'])
        # The URL carries a sign-in token; the handler's output goes to a log file.
        self.assertNotIn('token', result.stdout + result.stderr)
        self.assertNotIn(URL, result.stdout + result.stderr)

    def test_does_not_start_darling_when_no_session_runs(self):
        result = self.run_handler(URL, FAKE_RUNTIME='stopped')
        self.assertEqual(result.returncode, 3)
        self.assertIn('not running', result.stderr)
        self.assertEqual(self.darling_calls(), [])

    def test_rejects_other_schemes_control_characters_and_long_urls(self):
        for url in ('https://example.com/', 'file:///etc/passwd', 'riotclientx://a',
                    'riotclient://auth/\x01x', 'riotclient://auth/' + 'a' * 4096, ''):
            with self.subTest(url=url[:40]):
                result = self.run_handler(url)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.darling_calls(), [])

    def test_reports_a_guest_that_did_not_take_the_url(self):
        result = self.run_handler(URL, FAKE_OPEN_STATUS='1')
        self.assertEqual(result.returncode, 4)
        self.assertIn('did not accept', result.stderr)
        self.assertNotIn(URL, result.stderr)


class URLHandlerEntryTests(unittest.TestCase):
    def install(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        home, project = root / 'home', root / 'pro ject'
        home.mkdir()
        project.mkdir()
        for name in ('darling-lol.svg', 'darling-lol.png'):
            shutil.copy2(ROOT / name, project / name)
        script = project / 'desktop-entry.py'
        script.write_text((ROOT / 'tools/desktop-entry.py').read_text()
                          .replace('@PROJECT_ROOT@', str(project)).replace('@HOST_HOME@', str(home)))
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                                env=dict(os.environ, HOME=str(home)), check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return home, project

    def test_installs_a_hidden_riotclient_scheme_handler(self):
        home, project = self.install()
        entry = (home / '.local/share/applications/darling-url-handler.desktop').read_text()
        fields = dict(line.split('=', 1) for line in entry.splitlines() if '=' in line)
        self.assertEqual(fields['MimeType'], 'x-scheme-handler/riotclient;')
        self.assertEqual(fields['NoDisplay'], 'true')
        self.assertEqual(fields['Exec'], f'"{project}/scripts/open-guest-url.sh" %u')
        for field in ('Name', 'Comment'):
            for brand in ('League', 'LoL', 'Riot', 'Vanguard'):
                self.assertNotIn(brand.lower(), fields[field].lower())
        # Not a desktop shortcut: it is only a link handler.
        for desktop in (home / 'Desktop', home / 'Escritorio'):
            self.assertFalse((desktop / 'darling-url-handler.desktop').exists())


if __name__ == '__main__':
    unittest.main()
