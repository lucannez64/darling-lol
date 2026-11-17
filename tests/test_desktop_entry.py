"""Verify the desktop-entry helper against a disposable HOME."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'tools/desktop-entry.py'
FLAKE = ROOT / 'flake.nix'
OLD_SVG = '<svg><linearGradient id="goldGrad"/><path d="M172 108 L242 108"/></svg>'
IDENTITY_KEYS = ('Name', 'GenericName', 'Comment', 'Icon', 'Keywords', 'Categories', 'StartupWMClass')


def parse_entry(text):
    entry = {}
    for line in text.splitlines():
        if '=' in line and not line.startswith(('#', '[')):
            key, value = line.split('=', 1)
            entry[key] = value
    return entry


class DesktopEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='desktop entry ', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()

    def project(self, name='project'):
        project = self.root / name
        project.mkdir(parents=True)
        shutil.copy2(ROOT / 'darling-lol.svg', project / 'darling-lol.svg')
        shutil.copy2(ROOT / 'darling-lol.png', project / 'darling-lol.png')
        return project

    def child_env(self, **extra):
        """Environment for the helper: HOME is the disposable one and an exported
        DARLING_LOL_WINDOW_ENTRIES never leaks in unless a test sets it."""
        env = {key: value for key, value in os.environ.items()
               if key != 'DARLING_LOL_WINDOW_ENTRIES'}
        env.update(HOME=str(self.home), **extra)
        return env

    def install(self, project, *args):
        script = project / 'desktop-entry.py'
        script.write_text(HELPER.read_text()
                          .replace('@PROJECT_ROOT@', str(project))
                          .replace('@HOST_HOME@', str(self.home)))
        return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                              env=self.child_env(), check=False)

    def entry(self):
        return self.home / '.local/share/applications/darling-launcher.desktop'

    def icons(self):
        base = self.home / '.local/share/icons'
        return base / 'hicolor/scalable/apps/darling-launcher.svg', base / 'hicolor/128x128/apps/darling-launcher.png'

    def test_installs_neutral_entry_and_icons(self):
        project = self.project()
        result = self.install(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.entry().read_text()
        self.assertIn('Name=Darling Launcher\n', text)
        self.assertIn('Icon=darling-launcher\n', text)
        self.assertIn(f'Path={project}\n', text)
        exec_line = next(line for line in text.splitlines() if line.startswith('Exec='))
        self.assertIn(str(project / 'scripts/launch-shortcut.sh'), exec_line)
        for field in ('Name', 'Comment', 'GenericName', 'Keywords'):
            line = next(line for line in text.splitlines() if line.startswith(field + '='))
            for brand in ('League', 'LoL', 'Riot', 'Vanguard', 'anti-cheat'):
                self.assertNotIn(brand.lower(), line.lower())
        svg, png = self.icons()
        self.assertTrue(svg.is_file())
        self.assertTrue(png.is_file())
        self.assertEqual(png.read_bytes(), (project / 'darling-lol.png').read_bytes())

    def test_preserves_entries_and_icons_it_did_not_create(self):
        project = self.project()
        other = self.root / 'elsewhere'
        (self.home / '.local/share/applications').mkdir(parents=True)
        legacy_entry = self.home / '.local/share/applications/league-of-legends.desktop'
        legacy_entry.write_text(f'[Desktop Entry]\nExec={other}/scripts/desktop-run.sh\n')
        (self.home / 'Desktop').mkdir()
        desktop_entry = self.home / 'Desktop/league-of-legends.desktop'
        desktop_entry.write_text(f'[Desktop Entry]\nExec={other}/scripts/desktop-run.sh\n')
        icons = self.home / '.local/share/icons'
        icons.mkdir(parents=True)
        (icons / 'league-of-legends.png').write_text('user icon')
        result = self.install(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(legacy_entry.is_file())
        self.assertTrue(desktop_entry.is_file())
        self.assertEqual((icons / 'league-of-legends.png').read_text(), 'user icon')
        self.assertTrue(self.entry().is_file())

    def test_removes_legacy_artifacts_pointing_at_the_current_launcher(self):
        project = self.project()
        applications = self.home / '.local/share/applications'
        applications.mkdir(parents=True)
        owned = f'[Desktop Entry]\nExec="{project}/scripts/launch-shortcut.sh"\n'
        legacy_entry = applications / 'league-of-legends.desktop'
        legacy_entry.write_text(owned)
        (self.home / 'Desktop').mkdir()
        desktop_link = self.home / 'Desktop/league-of-legends.desktop'
        desktop_link.symlink_to(project / 'scripts/launch-shortcut.sh')
        result = self.install(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(legacy_entry.exists())
        self.assertFalse(desktop_link.is_symlink())
        self.assertTrue(self.entry().is_file())

    def test_removes_legacy_artifacts_it_created(self):
        project = self.project()
        applications = self.home / '.local/share/applications'
        applications.mkdir(parents=True)
        owned = f'[Desktop Entry]\nExec="{project}/scripts/desktop-run.sh"\n'
        legacy_entry = applications / 'league-of-legends.desktop'
        legacy_entry.write_text(owned)
        (self.home / 'Desktop').mkdir()
        desktop_link = self.home / 'Desktop/league-of-legends.desktop'
        desktop_link.symlink_to(project / 'scripts/desktop-run.sh')
        icons = self.home / '.local/share/icons'
        scalable = icons / 'hicolor/scalable/apps'
        legacy_png_dir = icons / 'hicolor/512x512/apps'
        scalable.mkdir(parents=True)
        legacy_png_dir.mkdir(parents=True)
        legacy_svg = scalable / 'league-of-legends.svg'
        legacy_svg.write_text(OLD_SVG)
        legacy_png = legacy_png_dir / 'league-of-legends.png'
        legacy_png.write_bytes(b'old client icon')
        result = self.install(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(legacy_entry.exists())
        self.assertFalse(desktop_link.is_symlink())
        self.assertFalse(legacy_svg.exists())
        self.assertFalse(legacy_png.exists())
        self.assertTrue(self.entry().is_file())

    def test_quotes_exec_arguments_with_reserved_characters(self):
        project = self.project('proj $dollar#hash\'quote')
        result = self.install(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.entry().read_text()
        exec_line = next(line for line in text.splitlines() if line.startswith('Exec='))
        self.assertTrue(exec_line.startswith('Exec="'), exec_line)
        self.assertTrue(exec_line.endswith('"'), exec_line)
        self.assertIn('\\$dollar', exec_line)
        if shutil.which('desktop-file-validate'):
            check = subprocess.run(['desktop-file-validate', str(self.entry())],
                                   capture_output=True, text=True, check=False)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_toolbox_entry_launches_through_the_container(self):
        project = self.project()
        result = self.install(project, '--toolbox', 'darling-lol')
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.entry().read_text()
        exec_line = next(line for line in text.splitlines() if line.startswith('Exec='))
        self.assertTrue(exec_line.startswith('Exec=toolbox run -c darling-lol '), exec_line)
        self.assertIn(str(project / 'scripts/launch-shortcut.sh'), exec_line)
        if shutil.which('desktop-file-validate'):
            check = subprocess.run(['desktop-file-validate', str(self.entry())],
                                   capture_output=True, text=True, check=False)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def handler_exec_line(self):
        handler = self.home / '.local/share/applications/darling-url-handler.desktop'
        return handler, next(line for line in handler.read_text().splitlines()
                             if line.startswith('Exec='))

    def test_toolbox_link_handler_opens_inside_the_container(self):
        project = self.project()
        result = self.install(project, '--toolbox', 'my-box')
        self.assertEqual(result.returncode, 0, result.stderr)
        handler, exec_line = self.handler_exec_line()
        self.assertEqual(
            exec_line, f'Exec=toolbox run -c my-box "{project}/scripts/open-guest-url.sh" %u')
        self.assertIn('MimeType=x-scheme-handler/riotclient;', handler.read_text())
        if shutil.which('desktop-file-validate'):
            check = subprocess.run(['desktop-file-validate', str(handler)],
                                   capture_output=True, text=True, check=False)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_native_link_handler_runs_the_script_directly(self):
        project = self.project()
        result = self.install(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        _, exec_line = self.handler_exec_line()
        self.assertEqual(exec_line, f'Exec="{project}/scripts/open-guest-url.sh" %u')

    def test_identity_fields_match_the_packaged_entry(self):
        project = self.project()
        result = self.install(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = parse_entry(self.entry().read_text())
        packaged_here = re.search(
            r"cat << 'DESKTOP_EOF' > \$out/share/applications/darling-launcher\.desktop\n(.*?)\n\s*DESKTOP_EOF",
            FLAKE.read_text(), re.S)
        self.assertIsNotNone(packaged_here, 'packaged desktop entry not found in flake.nix')
        packaged = parse_entry(packaged_here.group(1))
        for key in IDENTITY_KEYS:
            self.assertIn(key, packaged)
            self.assertEqual(rendered.get(key), packaged[key], key)

    def window_entry(self, name):
        return self.home / '.local/share/applications' / name

    def test_window_entries_are_not_created_by_default(self):
        project = self.project()
        result = self.install(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.window_entry('darling-league-client.desktop').exists())
        self.assertFalse(self.window_entry('darling-riot-client.desktop').exists())

    def test_window_entries_name_and_icon_the_client_windows(self):
        project = self.project()
        (self.home / 'Desktop').mkdir()
        result = self.install(project, '--window-entries')
        self.assertEqual(result.returncode, 0, result.stderr)
        launcher = parse_entry(self.entry().read_text())
        expected = {
            'darling-league-client.desktop': ('League of Legends', 'LeagueClientUx'),
            'darling-riot-client.desktop': ('Riot Client', 'Riot Client'),
        }
        for file_name, (name, wm_class) in expected.items():
            path = self.window_entry(file_name)
            entry = parse_entry(path.read_text())
            self.assertEqual(entry['Name'], name)
            self.assertEqual(entry['StartupWMClass'], wm_class)
            self.assertEqual(entry['Icon'], 'darling-launcher')
            self.assertEqual(entry['NoDisplay'], 'true')
            self.assertEqual(entry['Exec'], launcher['Exec'])
            self.assertEqual(entry['Path'], launcher['Path'])
            self.assertTrue(os.access(path, os.X_OK))
            if shutil.which('desktop-file-validate'):
                check = subprocess.run(['desktop-file-validate', str(path)],
                                       capture_output=True, text=True, check=False)
                self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        # Window entries are hidden helpers: never placed on the desktop.
        self.assertEqual(sorted(path.name for path in (self.home / 'Desktop').iterdir()),
                         ['darling-launcher.desktop'])

    def test_window_entries_add_search_aliases_to_the_launcher_only(self):
        project = self.project()
        result = self.install(project, '--window-entries')
        self.assertEqual(result.returncode, 0, result.stderr)
        launcher = parse_entry(self.entry().read_text())
        self.assertEqual(launcher['Keywords'],
                         'Darling;Launcher;Compatibility;LoL;League;League of Legends;Riot;')
        for field in ('Name', 'Comment', 'GenericName'):
            for brand in ('League', 'LoL', 'Riot'):
                self.assertNotIn(brand.lower(), launcher[field].lower())
        self.assertEqual(launcher['StartupWMClass'], 'RiotClientServices')

    def test_window_entries_environment_variable_enables_them(self):
        project = self.project()
        script = project / 'desktop-entry.py'
        script.write_text(HELPER.read_text()
                          .replace('@PROJECT_ROOT@', str(project))
                          .replace('@HOST_HOME@', str(self.home)))
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                                env=self.child_env(DARLING_LOL_WINDOW_ENTRIES='1'), check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.window_entry('darling-league-client.desktop').is_file())
        self.assertTrue(self.window_entry('darling-riot-client.desktop').is_file())

    def test_window_entries_launch_through_the_container(self):
        project = self.project()
        result = self.install(project, '--toolbox', 'my-box', '--window-entries')
        self.assertEqual(result.returncode, 0, result.stderr)
        for file_name in ('darling-league-client.desktop', 'darling-riot-client.desktop'):
            exec_line = next(line for line in self.window_entry(file_name).read_text().splitlines()
                             if line.startswith('Exec='))
            self.assertTrue(exec_line.startswith('Exec=toolbox run -c my-box '), exec_line)
            self.assertIn(str(project / 'scripts/launch-shortcut.sh'), exec_line)

    def test_dropping_the_option_removes_only_our_window_entries(self):
        project = self.project()
        self.assertEqual(self.install(project, '--window-entries').returncode, 0)
        foreign = self.window_entry('darling-riot-client.desktop')
        foreign.write_text('[Desktop Entry]\nName=Mine\nExec=/usr/bin/other\n')
        result = self.install(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.window_entry('darling-league-client.desktop').exists())
        self.assertTrue(foreign.is_file())
        self.assertIn('Name=Mine', foreign.read_text())

    def test_dropping_the_option_removes_entries_of_a_path_with_reserved_characters(self):
        # exec_command backslash-escapes $ ` " \\ and doubles %, so ownership has to be
        # recognised from the generated Exec, not only from the raw script path.
        for args in ((), ('--toolbox', 'my-box')):
            with self.subTest(args=args):
                project = self.project("proj $dollar#hash'quote" + ('-tb' if args else ''))
                enabled = self.install(project, *args, '--window-entries')
                self.assertEqual(enabled.returncode, 0, enabled.stderr)
                names = ('darling-league-client.desktop', 'darling-riot-client.desktop')
                for name in names:
                    self.assertTrue(self.window_entry(name).is_file(), name)
                disabled = self.install(project, *args)
                self.assertEqual(disabled.returncode, 0, disabled.stderr)
                for name in names:
                    self.assertFalse(self.window_entry(name).exists(), name)
                self.assertTrue(self.entry().is_file())

    def test_enabling_the_option_keeps_a_foreign_window_entry(self):
        project = self.project()
        foreign = self.window_entry('darling-league-client.desktop')
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text('[Desktop Entry]\nName=Mine\nExec=/usr/bin/other\n')
        result = self.install(project, '--window-entries')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(foreign.read_text(), '[Desktop Entry]\nName=Mine\nExec=/usr/bin/other\n')
        self.assertIn(str(foreign), result.stderr)
        self.assertNotIn(f'Created window entry: {foreign}', result.stdout)
        # The conflict does not stop the other entry from being written.
        self.assertTrue(self.window_entry('darling-riot-client.desktop').is_file())

    def test_enabling_the_option_refreshes_an_entry_this_project_wrote(self):
        project = self.project()
        self.assertEqual(self.install(project, '--window-entries').returncode, 0)
        result = self.install(project, '--toolbox', 'my-box', '--window-entries')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('already exists', result.stderr)
        exec_line = next(line for line in
                         self.window_entry('darling-league-client.desktop').read_text().splitlines()
                         if line.startswith('Exec='))
        self.assertTrue(exec_line.startswith('Exec=toolbox run -c my-box '), exec_line)

    def foreign_entry_mentioning(self, project):
        """A file that is not ours but names this checkout's launch script outside Exec."""
        return ('[Desktop Entry]\nName=Mine\n'
                f'Comment=wraps {project / "scripts/launch-shortcut.sh"} for later\n'
                'Exec=/usr/bin/other\n')

    def test_ownership_comes_from_exec_not_from_other_fields(self):
        project = self.project()
        foreign_text = self.foreign_entry_mentioning(project)
        names = ('darling-league-client.desktop', 'darling-riot-client.desktop')
        legacy = self.home / '.local/share/applications/league-of-legends.desktop'
        with self.subTest(flow='enable'):
            self.window_entry(names[0]).parent.mkdir(parents=True, exist_ok=True)
            for name in names:
                self.window_entry(name).write_text(foreign_text)
            result = self.install(project, '--window-entries')
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in names:
                self.assertEqual(self.window_entry(name).read_text(), foreign_text, name)
                self.assertIn(str(self.window_entry(name)), result.stderr)
        with self.subTest(flow='disable'):
            result = self.install(project)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in names:
                self.assertEqual(self.window_entry(name).read_text(), foreign_text, name)
        with self.subTest(flow='legacy cleanup'):
            legacy.write_text(foreign_text)
            result = self.install(project)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(legacy.read_text(), foreign_text)

    def test_missing_icon_fails(self):
        project = self.project()
        (project / 'darling-lol.png').unlink()
        result = self.install(project)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('launcher icon is missing', result.stderr)


if __name__ == '__main__':
    unittest.main()
