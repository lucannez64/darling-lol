"""Exercise scripts/create-shortcuts.sh against a disposable HOME and checkout.

The script resolves its repository from its own path and runs
scripts/configure.py inside it, so every test builds a minimal fake checkout
instead of touching the real build directory or the real HOME.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/create-shortcuts.sh'
HELPER_SOURCE = ROOT / 'tools/desktop-entry.py'

# A stand-in for scripts/configure.py that renders just the desktop-entry
# helper, the only runtime tool the shortcut manager needs.
CONFIGURE_STUB = '''\
"""Fake configure.py: renders tools/desktop-entry.py with the local paths."""
import os
from pathlib import Path

root = Path(__file__).resolve().parents[1]
build = root / "build"
(build / "runtime/tools").mkdir(parents=True, exist_ok=True)
target = build / "runtime/tools/desktop-entry.py"
target.write_text((root / "tools/desktop-entry.py").read_text()
                  .replace("@PROJECT_ROOT@", str(root))
                  .replace("@HOST_HOME@", os.environ["HOME"]))
target.chmod(0o755)
print("configured")
'''


def parse_entry(text):
    entry = {}
    for line in text.splitlines():
        if '=' in line and not line.startswith(('#', '[')):
            key, value = line.split('=', 1)
            entry[key] = value
    return entry


class CreateShortcutsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='create shortcuts ', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.repo = self.root / 'repo'
        self.repo.mkdir()

    def fake_repo(self):
        """A minimal checkout: the script, its helper and a stub configure.py."""
        (self.repo / 'scripts').mkdir()
        (self.repo / 'tools').mkdir()
        (self.repo / 'build').mkdir()
        shutil.copy2(SCRIPT, self.repo / 'scripts/create-shortcuts.sh')
        shutil.copy2(HELPER_SOURCE, self.repo / 'tools/desktop-entry.py')
        for icon in ('darling-lol.svg', 'darling-lol.png'):
            shutil.copy2(ROOT / icon, self.repo / icon)
        (self.repo / 'scripts/configure.py').write_text(CONFIGURE_STUB)

    def fake_toolbox(self, container='my-box'):
        """A stub toolbox that only reports the given container as usable."""
        bin_dir = self.root / 'bin'
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / 'toolbox'
        stub.write_text(f'''\
#!/bin/sh
# run -c CONTAINER ...: succeed only for {container}
if [ "$1" = run ] && [ "$2" = -c ] && [ "$3" = {container} ]; then exit 0; fi
exit 1
''')
        stub.chmod(0o755)
        return str(bin_dir)

    def run_script(self, *args, path=None, input=None, desktop='X-Unknown', lang='C'):
        env = {**os.environ,
               'HOME': str(self.home),
               # Desktop discovery (xdg-user-dir) must not read the caller's
               # configuration and write shortcuts into their real folders.
               'XDG_CONFIG_HOME': str(self.home / '.config'),
               # Deterministic locale: 'C' is not a locale the Riot Client
               # ships, so the region/locale question is skipped unless a test
               # asks for a supported one.
               'LC_ALL': lang, 'LC_MESSAGES': lang, 'LANG': lang}
        # Deterministic session: only XDG_CURRENT_DESKTOP=GNOME may create the
        # window entries by default or ask the window-entries question.
        env['XDG_CURRENT_DESKTOP'] = desktop
        if path is not None:
            env['PATH'] = path + os.pathsep + env.get('PATH', '')
        if input is None:
            return subprocess.run(
                ['/bin/bash', str(self.repo / 'scripts/create-shortcuts.sh'), *args],
                capture_output=True, text=True, env=env, check=False,
                cwd=str(self.repo))
        return self.run_pty(args, env, input)

    def run_sourced(self, snippet, **overrides):
        """Source the script in a subshell and run a snippet of it."""
        env = {**os.environ,
               'HOME': str(self.home),
               'XDG_CONFIG_HOME': str(self.home / '.config'),
               'DARLING_LOL_SHORTCUT_SCRIPT': str(self.repo / 'scripts/create-shortcuts.sh'),
               'LC_ALL': 'C', 'LC_MESSAGES': 'C', 'LANG': 'C',
               **overrides}
        return subprocess.run(
            ['/bin/bash', '-c',
             f'source "$DARLING_LOL_SHORTCUT_SCRIPT"\n{snippet}'],
            capture_output=True, text=True, env=env, check=False,
            cwd=str(self.repo))

    def run_pty(self, args, env, input):
        """Run the script on a pty and feed the answers line by line.

        Each answer goes out after the output goes quiet for a moment, so the
        runner never has to guess where a prompt ends.
        """
        import pty
        import select
        import time

        master, slave = pty.openpty()
        process = subprocess.Popen(
            ['/bin/bash', str(self.repo / 'scripts/create-shortcuts.sh'), *args],
            stdin=slave, stdout=slave, stderr=slave, env=env,
            cwd=str(self.repo), close_fds=True)
        os.close(slave)
        out = []

        def drain(timeout=0.4):
            end = time.time() + timeout
            while time.time() < end:
                r, _, _ = select.select([master], [], [], 0.1)
                if r:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        return False
                    if not chunk:
                        return False
                    out.append(chunk.decode(errors='replace'))
                    end = time.time() + timeout
            return True

        try:
            for answer in input.splitlines():
                if not drain(1.5):
                    break
                os.write(master, (answer + '\n').encode())
            # Let the last prompt be answered and the script finish.
            end = time.time() + 60
            while time.time() < end:
                if not drain(0.5):
                    break
                if process.poll() is not None:
                    break
        finally:
            os.close(master)
            if process.poll() is None:
                process.kill()
            process.wait()
        text = ''.join(out)
        return subprocess.CompletedProcess(args, process.returncode, text, text)

    def test_detected_toolbox_is_suggested_by_default(self):
        self.fake_repo()
        (self.repo / 'build/toolbox-container').write_text('my-box\n')
        result = self.run_script('--yes', '--create', path=self.fake_toolbox('my-box'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Shortcut target: Toolbox 'my-box' (detected installation)", result.stdout)
        entry = parse_entry(self.entry().read_text())
        self.assertTrue(entry['Exec'].startswith('toolbox run -c my-box '), entry['Exec'])
        # The helper records the re-entry for every entry it writes.
        window = parse_entry(self.window_entry('darling-league-client.desktop').read_text()) \
            if self.window_entry('darling-league-client.desktop').is_file() else None
        if window:
            self.assertTrue(window['Exec'].startswith('toolbox run -c my-box '), window['Exec'])

    def test_detected_toolbox_defaults_to_yes_interactively(self):
        self.fake_repo()
        (self.repo / 'build/toolbox-container').write_text('my-box\n')
        result = self.run_script(
            path=self.fake_toolbox('my-box'),
            # Outside GNOME the window-entries question is skipped:
            # 1) menu, 2) name, 3) flags, 4) toolbox [Y/n] (Enter accepts the
            # suggested container), 5) summary.
            input='1\n\nn\n\ny\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Toolbox 'my-box' (detected installation)", result.stdout)
        entry = parse_entry(self.entry().read_text())
        self.assertTrue(entry['Exec'].startswith('toolbox run -c my-box '), entry['Exec'])
        self.assertFalse(self.window_entry('darling-league-client.desktop').exists())

    def test_gnome_session_asks_for_window_entries_with_yes_default(self):
        self.fake_repo()
        (self.repo / 'build/toolbox-container').write_text('my-box\n')
        result = self.run_script(
            path=self.fake_toolbox('my-box'), desktop='GNOME',
            # On GNOME the window-entries question appears with [Y/n]:
            # 1) menu, 2) name, 3) window entries (Enter accepts),
            # 4) flags, 5) toolbox (Enter accepts), 6) summary.
            input='1\n\n\nn\n\ny\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('GNOME overview? [Y/n]', result.stdout)
        self.assertTrue(self.window_entry('darling-league-client.desktop').is_file())
        self.assertTrue(self.window_entry('darling-league-game.desktop').is_file())
        self.assertTrue(self.window_entry('darling-riot-client.desktop').is_file())

    def test_declining_the_suggestion_runs_on_the_host(self):
        self.fake_repo()
        (self.repo / 'build/toolbox-container').write_text('my-box\n')
        result = self.run_script(
            path=self.fake_toolbox('my-box'), desktop='GNOME',
            # Same GNOME flow, but the window entries and the toolbox
            # suggestion are declined with 'n'.
            input='1\n\nn\nn\nn\ny\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Shortcut target: the host.', result.stdout)
        entry = parse_entry(self.entry().read_text())
        self.assertNotIn('toolbox', entry['Exec'])
        self.assertFalse(self.window_entry('darling-league-client.desktop').exists())

    def test_a_dead_container_is_ignored(self):
        self.fake_repo()
        (self.repo / 'build/toolbox-container').write_text('dead-box\n')
        result = self.run_script('--yes', '--create', path=self.fake_toolbox('my-box'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('was detected but is not usable', result.stderr)
        self.assertIn('Shortcut target: the host', result.stdout)
        entry = parse_entry(self.entry().read_text())
        self.assertNotIn('toolbox', entry['Exec'])

    def entry(self):
        return self.home / '.local/share/applications/darling-launcher.desktop'

    def window_entry(self, name):
        return self.home / '.local/share/applications' / name

    def handler(self):
        return self.home / '.local/share/applications/darling-url-handler.desktop'

    def desktop_shortcut(self):
        return self.home / 'Desktop/darling-launcher.desktop'

    def test_help_exits_zero(self):
        self.fake_repo()
        result = self.run_script('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--create', result.stdout)

    def test_no_action_without_a_terminal_fails(self):
        self.fake_repo()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--create', result.stderr)

    def test_create_with_defaults(self):
        self.fake_repo()
        result = self.run_script('--yes', '--create')
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = parse_entry(self.entry().read_text())
        self.assertEqual(entry['Name'], 'Darling Launcher')
        self.assertIn('scripts/launch-shortcut.sh', entry['Exec'])
        self.assertNotIn('env ', entry['Exec'])
        # Outside GNOME the question is skipped and no window entries appear.
        self.assertFalse(self.window_entry('darling-league-client.desktop').exists())
        self.assertIn('Window entries only help GNOME', result.stdout)
        self.assertTrue(self.handler().is_file())
        self.assertIn('Shortcut target: the host', result.stdout)

    def test_gnome_session_creates_window_entries_by_default(self):
        self.fake_repo()
        result = self.run_script('--yes', '--create', desktop='GNOME')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.window_entry('darling-league-client.desktop').is_file())
        self.assertTrue(self.window_entry('darling-league-game.desktop').is_file())
        self.assertTrue(self.window_entry('darling-riot-client.desktop').is_file())

    def test_create_with_every_option(self):
        self.fake_repo()
        (self.home / 'Desktop').mkdir()
        # --window-entries is honoured even outside GNOME: it is an explicit
        # request that wins over the skipped question.
        result = self.run_script(
            '--yes', '--create', '--name', 'My Game Launcher', '--window-entries',
            '--env', 'RCLOL_HIDE_LEAGUE_SPLASH=1', '--env', 'RCLOL_CLEAN_ON_EXIT=0')
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = parse_entry(self.entry().read_text())
        self.assertEqual(entry['Name'], 'My Game Launcher')
        self.assertIn('env RCLOL_HIDE_LEAGUE_SPLASH=1 RCLOL_CLEAN_ON_EXIT=0', entry['Exec'])
        self.assertEqual(parse_entry(self.desktop_shortcut().read_text())['Name'],
                         'My Game Launcher')
        for name, wm_class in (('darling-league-client.desktop', 'LeagueClientUx'),
                               ('darling-league-game.desktop', 'LeagueofLegends'),
                               ('darling-riot-client.desktop', 'Riot Client')):
            window = parse_entry(self.window_entry(name).read_text())
            self.assertEqual(window['StartupWMClass'], wm_class)
            self.assertEqual(window['Exec'], entry['Exec'])
        self.assertIn('Launch flags: RCLOL_HIDE_LEAGUE_SPLASH=1 RCLOL_CLEAN_ON_EXIT=0',
                      result.stdout)

    def test_create_rejects_an_invalid_env_flag(self):
        self.fake_repo()
        result = self.run_script('--yes', '--create', '--env', 'RCLOL value')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('NAME=VALUE', result.stderr)
        self.assertFalse(self.entry().exists())

    def test_create_with_an_unavailable_toolbox_fails(self):
        self.fake_repo()
        result = self.run_script('--yes', '--create', '--toolbox', 'no-such-box')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Toolbox container 'no-such-box' is not usable", result.stderr)

    def test_update_prefills_and_rewrites(self):
        self.fake_repo()
        self.assertEqual(self.run_script('--yes', '--create', '--name', 'Old Name').returncode, 0)
        result = self.run_script('--yes', '--update')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(parse_entry(self.entry().read_text())['Name'], 'Old Name')
        renamed = self.run_script('--yes', '--update', '--name', 'New Name')
        self.assertEqual(renamed.returncode, 0, renamed.stderr)
        self.assertEqual(parse_entry(self.entry().read_text())['Name'], 'New Name')

    def test_update_without_a_previous_install_creates(self):
        self.fake_repo()
        result = self.run_script('--yes', '--update')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('creating new shortcuts', result.stdout)
        self.assertTrue(self.entry().is_file())

    def test_update_keeps_window_entries_and_flags_on_gnome(self):
        self.fake_repo()
        self.assertEqual(self.run_script(
            '--yes', '--create', '--window-entries', '--env', 'RCLOL_CLEAN_ON_EXIT=0',
            desktop='GNOME').returncode, 0)
        result = self.run_script('--yes', '--update', desktop='GNOME')
        self.assertEqual(result.returncode, 0, result.stderr)
        # An unattended update keeps the installed flags (Enter keeps them).
        self.assertIn('Launch flags: RCLOL_CLEAN_ON_EXIT=0', result.stdout)
        entry = parse_entry(self.entry().read_text())
        self.assertIn('env RCLOL_CLEAN_ON_EXIT=0', entry['Exec'])
        for name in ('darling-league-client.desktop', 'darling-league-game.desktop',
                     'darling-riot-client.desktop'):
            self.assertTrue(self.window_entry(name).is_file(), name)

    def test_update_keeps_installed_flags_when_the_defaults_are_accepted(self):
        self.fake_repo()
        self.assertEqual(self.run_script(
            '--yes', '--create', '--env', 'RCLOL_CLEAN_ON_EXIT=0').returncode, 0)
        result = self.run_script(
            # 1) menu, 2) name (Enter), 3) change the flags (Enter keeps
            # them), 4) Toolbox (Enter runs on the host), 5) summary.
            input='2\n\n\n\n\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Current flags: RCLOL_CLEAN_ON_EXIT=0', result.stdout)
        self.assertIn('Launch flags: RCLOL_CLEAN_ON_EXIT=0', result.stdout)
        entry = parse_entry(self.entry().read_text())
        self.assertIn('env RCLOL_CLEAN_ON_EXIT=0', entry['Exec'])

    def test_update_clears_flags_only_when_asked(self):
        self.fake_repo()
        self.assertEqual(self.run_script(
            '--yes', '--create', '--env', 'RCLOL_CLEAN_ON_EXIT=0').returncode, 0)
        result = self.run_script(
            # 1) menu, 2) name, 3) change the flags, 4) 'none' clears them,
            # 5) Toolbox (host), 6) summary.
            input='2\n\ny\nnone\n\n\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Launch flags: none', result.stdout)
        entry = parse_entry(self.entry().read_text())
        self.assertNotIn('env ', entry['Exec'])

    def test_update_keeps_a_flag_value_with_spaces_on_enter(self):
        self.fake_repo()
        self.assertEqual(self.run_script(
            '--yes', '--create', '--env', 'RCLOL_KEEP_HOST_ENV=A B').returncode, 0)
        result = self.run_script(
            # 1) menu, 2) name, 3) change the flags, 4) Enter keeps the
            # current array (the value has a space and cannot be retyped),
            # 5) Toolbox (host), 6) summary.
            input='2\n\ny\n\n\n\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = parse_entry(self.entry().read_text())
        self.assertIn('"RCLOL_KEEP_HOST_ENV=A B"', entry['Exec'])

    def test_update_removes_window_entries_outside_gnome(self):
        self.fake_repo()
        self.assertEqual(self.run_script(
            '--yes', '--create', '--window-entries', desktop='GNOME').returncode, 0)
        result = self.run_script('--yes', '--update')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Window entries only help GNOME', result.stdout)
        self.assertIn('they will be removed', result.stdout)
        self.assertFalse(self.window_entry('darling-league-client.desktop').exists())
        self.assertFalse(self.window_entry('darling-league-game.desktop').exists())
        self.assertFalse(self.window_entry('darling-riot-client.desktop').exists())

    def test_remove_keeps_the_handler_and_foreign_files(self):
        self.fake_repo()
        self.assertEqual(self.run_script('--yes', '--create', '--window-entries').returncode, 0)
        foreign = self.window_entry('darling-riot-client.desktop')
        foreign.write_text('[Desktop Entry]\nName=Mine\nExec=/usr/bin/other\n')
        result = self.run_script('--yes', '--remove')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.entry().exists())
        self.assertFalse(self.window_entry('darling-league-client.desktop').exists())
        self.assertFalse(self.window_entry('darling-league-game.desktop').exists())
        self.assertTrue(foreign.is_file())
        self.assertTrue(self.handler().is_file())
        # A second run finds nothing left to do.
        again = self.run_script('--yes', '--remove')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn('No shortcuts created by this project were found.', again.stdout)

    def test_remove_with_the_handler(self):
        self.fake_repo()
        self.assertEqual(self.run_script('--yes', '--create').returncode, 0)
        result = self.run_script('--yes', '--remove', '--url-handler')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.entry().exists())
        self.assertFalse(self.handler().exists())

    def test_custom_icon_flag(self):
        self.fake_repo()
        icon = self.repo / 'custom-icon.png'
        icon.write_bytes(b'\x89PNG\r\n\x1a\nstub')
        result = self.run_script('--yes', '--create', '--icon')
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = parse_entry(self.entry().read_text())
        self.assertEqual(entry['Icon'], str(icon))
        self.assertIn(f'Icon: {icon}', result.stdout)
        # The icon survives an update without --icon while the file exists.
        updated = self.run_script('--yes', '--update')
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertIn('Keeping the custom icon', updated.stdout)
        self.assertEqual(parse_entry(self.entry().read_text())['Icon'], str(icon))

    def test_custom_icon_requires_exactly_one_file(self):
        self.fake_repo()
        result = self.run_script('--yes', '--create', '--icon')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('custom-icon.*', result.stderr)
        self.assertFalse(self.entry().exists())
        # Two candidates are ambiguous.
        (self.repo / 'custom-icon.png').write_bytes(b'x')
        (self.repo / 'custom-icon.svg').write_text('<svg/>')
        result = self.run_script('--yes', '--create', '--icon')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('exactly one', result.stderr)

    def test_update_reverts_the_icon_when_the_file_is_gone(self):
        self.fake_repo()
        icon = self.repo / 'custom-icon.png'
        icon.write_bytes(b'x')
        self.assertEqual(self.run_script('--yes', '--create', '--icon').returncode, 0)
        icon.unlink()
        result = self.run_script('--yes', '--update')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('falling back to the project icon', result.stderr)
        self.assertEqual(parse_entry(self.entry().read_text())['Icon'], 'darling-launcher')

    def test_icon_flag_is_rejected_with_remove(self):
        self.fake_repo()
        result = self.run_script('--yes', '--remove', '--icon')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--icon is only valid when creating or updating', result.stderr)

    def test_gitignore_never_versions_the_custom_icon(self):
        gitignore = (ROOT / '.gitignore').read_text()
        self.assertIn('/custom-icon.*', gitignore)

    def test_unknown_option_fails(self):
        self.fake_repo()
        result = self.run_script('--bogus')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unknown option', result.stderr)

    def test_conflicting_actions_fail(self):
        self.fake_repo()
        result = self.run_script('--yes', '--create', '--remove')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Only one action', result.stderr)

    def test_create_adds_the_system_region_and_locale_by_default(self):
        self.fake_repo()
        result = self.run_script(
            '--create', lang='pt_BR.UTF-8',
            # name, flags (no), region/locale (Enter accepts the default),
            # toolbox (host), summary (yes).
            input='\n\n\n\ny\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'Use your system region and language (BR, pt_BR) for the launch? [Y/n]',
            result.stdout)
        entry = parse_entry(self.entry().read_text())
        self.assertIn('env RCLOL_REGION=BR RCLOL_LOCALE=pt_BR', entry['Exec'])
        self.assertIn('Launch flags: RCLOL_REGION=BR RCLOL_LOCALE=pt_BR',
                      result.stdout)

    def test_create_can_decline_the_system_region_and_locale(self):
        self.fake_repo()
        result = self.run_script(
            '--create', lang='pt_BR.UTF-8',
            # name, flags (no), region/locale (n), toolbox (host), summary (yes).
            input='\n\nn\n\ny\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = parse_entry(self.entry().read_text())
        self.assertNotIn('env ', entry['Exec'])
        self.assertIn('Launch flags: none', result.stdout)

    def test_create_with_yes_uses_the_system_region_and_locale(self):
        self.fake_repo()
        result = self.run_script('--yes', '--create', lang='pt_BR.UTF-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Using the system region and language: BR / pt_BR.', result.stdout)
        entry = parse_entry(self.entry().read_text())
        self.assertIn('env RCLOL_REGION=BR RCLOL_LOCALE=pt_BR', entry['Exec'])

    def test_an_unsupported_system_locale_adds_no_flags(self):
        self.fake_repo()
        result = self.run_script('--yes', '--create', lang='C')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('not one the Riot Client ships', result.stdout)
        entry = parse_entry(self.entry().read_text())
        self.assertNotIn('env ', entry['Exec'])

    def test_supported_language_without_a_region_mapping_keeps_the_region(self):
        self.fake_repo()
        result = self.run_script('--yes', '--create', lang='zh_TW.UTF-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        command = parse_entry(self.entry().read_text())['Exec']
        self.assertIn('RCLOL_LOCALE=zh_TW', command)
        self.assertNotIn('RCLOL_REGION=', command)

    def test_explicit_env_flags_replace_the_system_region_and_locale(self):
        self.fake_repo()
        result = self.run_script(
            '--yes', '--create', '--env', 'RCLOL_CLEAN_ON_EXIT=0',
            lang='pt_BR.UTF-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = parse_entry(self.entry().read_text())
        self.assertIn('env RCLOL_CLEAN_ON_EXIT=0', entry['Exec'])
        self.assertNotIn('RCLOL_REGION', entry['Exec'])
        self.assertNotIn('RCLOL_LOCALE', entry['Exec'])

    def test_update_keeps_installed_region_and_locale_flags(self):
        self.fake_repo()
        self.assertEqual(self.run_script(
            '--yes', '--create', '--env', 'RCLOL_REGION=NA',
            '--env', 'RCLOL_LOCALE=en_US', lang='pt_BR.UTF-8').returncode, 0)
        result = self.run_script('--yes', '--update', lang='pt_BR.UTF-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = parse_entry(self.entry().read_text())
        self.assertIn('env RCLOL_REGION=NA RCLOL_LOCALE=en_US', entry['Exec'])
        self.assertEqual(entry['Exec'].count('RCLOL_REGION='), 1)
        self.assertNotIn('RCLOL_LOCALE=pt_BR', entry['Exec'])

    def test_an_unattended_update_does_not_add_region_and_locale_flags(self):
        # The installed shortcut had no automatic flags; an unattended update
        # must keep it that way instead of baking the system locale in.
        self.fake_repo()
        self.assertEqual(self.run_script('--yes', '--create', lang='C').returncode, 0)
        result = self.run_script('--yes', '--update', lang='pt_BR.UTF-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = parse_entry(self.entry().read_text())
        self.assertNotIn('RCLOL_REGION', entry['Exec'])
        self.assertNotIn('RCLOL_LOCALE', entry['Exec'])
        self.assertIn('Launch flags: none', result.stdout)

    def test_flag_reference_lists_the_idle_stop_timeout(self):
        self.fake_repo()
        result = self.run_sourced('show_flag_reference')
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(line for line in result.stdout.splitlines()
                    if 'RCLOL_STOP_AFTER_CLIENT_SECONDS=N' in line)
        self.assertIn('default: 60', line)

    def test_flag_reference_recommends_pcore_affinity_on_a_hybrid_cpu(self):
        self.fake_repo()
        cpus = self.root / 'cpu_core_cpus'
        cpus.write_text('0-3\n')
        result = self.run_sourced('show_flag_reference',
                                  DARLING_LOL_PCORE_CPUS=str(cpus))
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(line for line in result.stdout.splitlines()
                    if 'DSERVER_PCORE_AFFINITY=1' in line)
        self.assertIn('(recommended)', line)

    def test_flag_reference_omits_the_pcore_recommendation_elsewhere(self):
        self.fake_repo()
        cpus = self.root / 'cpu_core_cpus'
        cpus.write_text('')
        for path in (cpus, self.root / 'no-such-file'):
            with self.subTest(path=path):
                result = self.run_sourced('show_flag_reference',
                                          DARLING_LOL_PCORE_CPUS=str(path))
                self.assertEqual(result.returncode, 0, result.stderr)
                line = next(line for line in result.stdout.splitlines()
                            if 'DSERVER_PCORE_AFFINITY=1' in line)
                self.assertNotIn('(recommended)', line)

    def test_the_locale_list_matches_the_one_configure_uses(self):
        self.fake_repo()
        import importlib.util
        spec = importlib.util.spec_from_file_location('rclol_common', ROOT / 'scripts/common.py')
        common = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(common)
        result = self.run_sourced('printf "%s\\n" "${LOCALES[*]}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), list(common.LOCALES))


if __name__ == '__main__':
    unittest.main()
