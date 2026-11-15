"""Unit and CLI coverage for scripts/install-native.sh."""
import os
import pty
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/install-native.sh'
REGION_CODES = ('EUW', 'EUNE', 'NA', 'BR', 'KR', 'JP', 'OCE', 'LAN', 'LAS', 'TR', 'RU')


def installer_env(**extra):
    env = dict(os.environ, INSTALLER=str(SCRIPT), NO_COLOR='1')
    env.pop('RCLOL_REGION', None)
    env.pop('RCLOL_LOCALE', None)
    # Keep locale detection deterministic unless a test overrides it.
    env['LC_ALL'] = ''
    env['LC_MESSAGES'] = ''
    env['LANG'] = 'C.UTF-8'
    env.update(extra)
    return env


def run_cli(*args, **extra_env):
    return subprocess.run(
        ['bash', str(SCRIPT), *args],
        capture_output=True, text=True, check=False,
        env=installer_env(**extra_env),
    )


def run_sourced(snippet, **extra_env):
    """Run a bash snippet that sources the installer, without starting main()."""
    return subprocess.run(
        ['bash', '-c', snippet],
        capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
        env=installer_env(**extra_env),
    )


def run_sourced_pty(snippet, reply, **extra_env):
    """Run a sourcing snippet with a pseudo-terminal so prompts are interactive."""
    master, slave = pty.openpty()
    process = subprocess.Popen(
        ['bash', '-c', snippet],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        env=installer_env(**extra_env),
    )
    os.close(slave)
    os.write(master, reply)
    chunks = []
    while True:
        try:
            data = os.read(master, 1024)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    os.close(master)
    return process.wait(), b''.join(chunks).decode(errors='replace')


class CliSmokeTests(unittest.TestCase):
    def test_help_lists_install_and_desktop_options(self):
        result = run_cli('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ('--toolbox', '--region', '--locale', 'DARLING_LOL_TOOLBOX_NAME',
                         'DARLING_LOL_DESKTOP_ENTRY', 'RCLOL_REGION', 'RCLOL_LOCALE'):
            self.assertIn(expected, result.stdout)

    def test_unknown_option_fails(self):
        result = run_cli('--unknown-option')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unknown option', result.stderr)

    def test_region_requires_a_value(self):
        result = run_cli('--region')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--region requires a value', result.stderr)

    def test_invalid_region_fails_before_installing(self):
        result = run_cli('--region', 'XX')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid region', result.stderr)

    def test_script_has_valid_bash_syntax(self):
        result = subprocess.run(['bash', '-n', str(SCRIPT)], capture_output=True, text=True,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


class RegionAndArgumentTests(unittest.TestCase):
    def test_every_documented_region_is_valid(self):
        snippet = '\n'.join([
            'source "$INSTALLER"',
            *[f'valid_region {code} || exit 9' for code in REGION_CODES],
        ])
        result = run_sourced(snippet)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_region_codes_match_the_documented_list(self):
        result = run_sourced('source "$INSTALLER"\nregion_codes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ' '.join(REGION_CODES))

    def test_parse_args_normalizes_region_and_locale_and_sets_flags(self):
        result = run_sourced(
            'source "$INSTALLER"\n'
            'parse_args --region euw --locale PT_br --yes --toolbox --verbose --no-confirm\n'
            'printf "%s|%s|%s|%s|%s|%s\\n" "$REGION" "$LOCALE" "$YES" "$USE_TOOLBOX" "$VERBOSE" "$NO_CONFIRM"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'EUW|pt_BR|1|1|1|1')

    def test_parse_args_rejects_an_unknown_region(self):
        result = run_sourced('source "$INSTALLER"\nparse_args --region XX')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid region', result.stderr)

    def test_locale_requires_a_value_and_rejects_invalid_values(self):
        result = run_sourced('source "$INSTALLER"\nparse_args --locale')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--locale requires a value', result.stderr)

        result = run_sourced('source "$INSTALLER"\nparse_args --locale portuguese')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid locale', result.stderr)


class LocaleDetectionTests(unittest.TestCase):
    def test_detect_system_locale_uses_lc_all_then_messages_then_lang(self):
        cases = (
            ({'LC_ALL': 'pt_BR.UTF-8', 'LC_MESSAGES': 'fr_FR.UTF-8', 'LANG': 'en_US.UTF-8'}, 'pt_BR'),
            ({'LC_ALL': '', 'LC_MESSAGES': 'pt_BR.UTF-8', 'LANG': 'en_US.UTF-8'}, 'pt_BR'),
            ({'LC_ALL': '', 'LC_MESSAGES': '', 'LANG': 'pt_BR.UTF-8'}, 'pt_BR'),
        )
        for environment, expected in cases:
            with self.subTest(environment=environment):
                result = run_sourced('source "$INSTALLER"\ndetect_system_locale', **environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_detect_system_locale_strips_encoding_and_modifier(self):
        result = run_sourced(
            'source "$INSTALLER"\ndetect_system_locale',
            LC_ALL='', LC_MESSAGES='', LANG='pt_BR.UTF-8@euro',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'pt_BR')

    def test_detect_system_locale_ignores_c_and_posix(self):
        for value in ('C', 'C.UTF-8', 'POSIX'):
            with self.subTest(value=value):
                result = run_sourced(
                    'source "$INSTALLER"\ndetect_system_locale',
                    LC_ALL='', LC_MESSAGES='', LANG=value,
                )
                self.assertNotEqual(result.returncode, 0)

    def test_normalize_and_validate_locale(self):
        result = run_sourced(
            'source "$INSTALLER"\n'
            'normalize_locale PT-br\n'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'pt_BR')

        result = run_sourced('source "$INSTALLER"\nvalid_locale pt_BR && valid_locale en_US')
        self.assertEqual(result.returncode, 0, result.stderr)

        result = run_sourced('source "$INSTALLER"\nvalid_locale pt-BR')
        self.assertNotEqual(result.returncode, 0)

    def test_region_for_locale_maps_known_territories(self):
        cases = {
            'pt_BR': 'BR', 'en_US': 'NA', 'en_CA': 'NA', 'en_GB': 'EUW',
            'es_MX': 'LAN', 'es_AR': 'LAS', 'ko_KR': 'KR', 'ja_JP': 'JP',
            'tr_TR': 'TR', 'ru_RU': 'RU', 'en_AU': 'OCE', 'pl_PL': 'EUNE',
            'de_DE': 'EUW', 'xx_ZZ': 'EUW',
        }
        snippet = 'source "$INSTALLER"\n' + '\n'.join(
            f'[[ $(region_for_locale {locale}) == {expected} ]] || exit 9'
            for locale, expected in cases.items()
        )
        result = run_sourced(snippet)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_resolve_locale_prefers_explicit_then_environment_then_system(self):
        result = run_sourced(
            'source "$INSTALLER"\n'
            'LOCALE=fr_FR\n'
            'resolve_locale\n'
            'printf "%s\\n" "$LOCALE"',
            RCLOL_LOCALE='pt_BR', LANG='en_US.UTF-8',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'fr_FR')

        result = run_sourced(
            'source "$INSTALLER"\nresolve_locale\nprintf "%s\\n" "$LOCALE"',
            RCLOL_LOCALE='PT-br', LANG='en_US.UTF-8',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'pt_BR')

        result = run_sourced(
            'source "$INSTALLER"\nresolve_locale\nprintf "%s\\n" "$LOCALE"',
            RCLOL_LOCALE='', LC_ALL='', LC_MESSAGES='', LANG='pt_BR.UTF-8',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'pt_BR')

        result = run_sourced(
            'source "$INSTALLER"\nresolve_locale\nprintf "%s\\n" "$LOCALE"',
            RCLOL_LOCALE='', LC_ALL='C', LC_MESSAGES='', LANG='pt_BR.UTF-8',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'en_GB')

    def test_resolve_region_prefers_explicit_then_environment_then_locale(self):
        result = run_sourced(
            'source "$INSTALLER"\n'
            'REGION=EUW\n'
            'LOCALE=pt_BR\n'
            'YES=1\n'
            'resolve_region\n'
            'printf "%s\\n" "$REGION"',
            RCLOL_REGION='NA',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'EUW')

        result = run_sourced(
            'source "$INSTALLER"\n'
            'LOCALE=pt_BR\n'
            'YES=1\n'
            'resolve_region\n'
            'printf "%s\\n" "$REGION"',
            RCLOL_REGION='na',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'NA')

        result = run_sourced(
            'source "$INSTALLER"\n'
            'LOCALE=pt_BR\n'
            'YES=1\n'
            'resolve_region\n'
            'printf "%s\\n" "$REGION"',
            RCLOL_REGION='',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'BR')

    def test_resolve_region_rejects_an_invalid_environment_value(self):
        result = run_sourced(
            'source "$INSTALLER"\n'
            'LOCALE=pt_BR\n'
            'resolve_region',
            RCLOL_REGION='XX',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid RCLOL_REGION', result.stderr)


class DesktopEntryTests(unittest.TestCase):
    def create_entry(self, *, inside_toolbox=0, name='darling-lol', helper=True,
                     helper_fails=False):
        with tempfile.TemporaryDirectory(prefix='install-native-desktop ') as folder:
            repo = Path(folder) / 'repo'
            tools = repo / 'build/runtime/tools'
            tools.mkdir(parents=True)
            if helper:
                (tools / 'desktop-entry.py').write_text('')
            args_file = Path(folder) / 'args'
            fake_python = (f'python3() {{ printf "%s\\n" "$@" > "{args_file}"; }}'
                           if not helper_fails else 'python3() { return 7; }')
            snippet = f'''
source "$INSTALLER"
TARGET_DIR="{repo}"
TOOLBOX_NAME="{name}"
INSIDE_TOOLBOX={inside_toolbox}
{fake_python}
create_desktop_entry
'''
            result = run_sourced(snippet)
            arguments = args_file.read_text().splitlines() if args_file.exists() else []
            return result, arguments

    def test_native_install_calls_helper_without_toolbox(self):
        result, arguments = self.create_entry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(arguments), 1)
        self.assertTrue(arguments[0].endswith('build/runtime/tools/desktop-entry.py'))

    def test_toolbox_install_passes_the_container_name(self):
        result, arguments = self.create_entry(inside_toolbox=1, name='my-box')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(arguments), 3)
        self.assertEqual(arguments[1:], ['--toolbox', 'my-box'])
        self.assertTrue(arguments[0].endswith('build/runtime/tools/desktop-entry.py'))

    def test_enabled_environment_calls_helper(self):
        result = run_sourced(
            'source "$INSTALLER"\n'
            'create_desktop_entry() { printf "CALLED\\n"; }\n'
            'YES=0\n'
            'offer_desktop_entry',
            DARLING_LOL_DESKTOP_ENTRY='1',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CALLED', result.stdout)

    def test_disabled_environment_skips_helper(self):
        result = run_sourced(
            'source "$INSTALLER"\n'
            'create_desktop_entry() { printf "CALLED\\n"; }\n'
            'offer_desktop_entry',
            DARLING_LOL_DESKTOP_ENTRY='0',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('CALLED', result.stdout)

    def test_assume_yes_and_non_interactive_runs_skip_the_prompt(self):
        snippet = (
            'source "$INSTALLER"\n'
            'create_desktop_entry() { printf "CALLED\\n"; }\n'
            'YES=1\n'
            'offer_desktop_entry'
        )
        result = run_sourced(snippet)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('CALLED', result.stdout)

        result = run_sourced(snippet.replace('YES=1', 'YES=0'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('CALLED', result.stdout)

    def test_affirmative_and_negative_prompt_answers(self):
        snippet = (
            'source "$INSTALLER"\n'
            'create_desktop_entry() { printf "CALLED\\n"; }\n'
            'YES=0\n'
            'offer_desktop_entry'
        )
        code, output = run_sourced_pty(snippet, b'YES\n')
        self.assertEqual(code, 0, output)
        self.assertIn('Create a desktop entry', output)
        self.assertIn('CALLED', output)

        code, output = run_sourced_pty(snippet, b'n\n')
        self.assertEqual(code, 0, output)
        self.assertNotIn('CALLED', output)

    def test_missing_or_failing_helper_only_warns(self):
        result, arguments = self.create_entry(helper=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(arguments, [])
        self.assertIn('Desktop entry helper not found', result.stderr)

        result, _ = self.create_entry(helper_fails=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Could not create the desktop entry', result.stderr)


class ToolboxInvocationTests(unittest.TestCase):
    def run_install_toolbox(self, *, yes=0, verbose=0, name='darling-lol', exists=False,
                            locale='pt_BR'):
        with tempfile.TemporaryDirectory(prefix='install-native-toolbox ') as folder:
            log = Path(folder) / 'log'
            snippet = f'''
source "$INSTALLER"
run() {{ printf "run:%s\\n" "$*" >> "{log}"; }}
info() {{ :; }}
exec() {{ printf "exec:%s\\n" "$*" >> "{log}"; }}
podman() {{ return "$PODMAN_RC"; }}
USE_TOOLBOX=1
INSIDE_TOOLBOX=0
YES={yes}
VERBOSE={verbose}
REGION=EUW
LOCALE="{locale}"
TOOLBOX_NAME="{name}"
install_toolbox
'''
            result = run_sourced(snippet, PODMAN_RC='0' if exists else '1')
            lines = log.read_text().splitlines() if log.exists() else []
            return result, lines

    def test_interactive_install_does_not_forward_yes(self):
        result, lines = self.run_install_toolbox(yes=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        command = next(line for line in lines if line.startswith('exec:'))
        self.assertIn('--no-confirm', command)
        self.assertIn('--region EUW', command)
        self.assertIn('--locale pt_BR', command)
        self.assertNotIn(' --yes ', f' {command} ')

    def test_yes_and_verbose_are_forwarded_when_requested(self):
        result, lines = self.run_install_toolbox(yes=1, verbose=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        command = next(line for line in lines if line.startswith('exec:'))
        self.assertIn(' --yes ', f' {command} ')
        self.assertIn(' --verbose ', f' {command} ')

    def test_container_is_created_only_when_missing(self):
        result, lines = self.run_install_toolbox(exists=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('toolbox create' in line for line in lines), lines)

        result, lines = self.run_install_toolbox(exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any('toolbox create' in line for line in lines), lines)

    def test_custom_container_name_reaches_create_and_run(self):
        result, lines = self.run_install_toolbox(name='my-box')
        self.assertEqual(result.returncode, 0, result.stderr)
        create = next(line for line in lines if 'toolbox create' in line)
        command = next(line for line in lines if line.startswith('exec:'))
        self.assertIn('my-box', create)
        self.assertIn('--container my-box', command)
        self.assertIn('DARLING_LOL_TOOLBOX_NAME=my-box', command)


class FinishTests(unittest.TestCase):
    def test_desktop_offer_precedes_launch_instructions(self):
        with tempfile.TemporaryDirectory(prefix='install-native-finish ') as folder:
            target = Path(folder) / 'repo'
            log = Path(folder) / 'install.log'
            snippet = f'''
source "$INSTALLER"
git() {{
  case "$*" in
    *branch*) printf "main\\n" ;;
    *rev-parse*) printf "abc1234\\n" ;;
  esac
}}
offer_desktop_entry() {{ printf "DESKTOP_OFFER\\n"; }}
TARGET_DIR="{target}"
LOG="{log}"
REGION=EUW
LOCALE=pt_BR
INSIDE_TOOLBOX=0
SECONDS=0
finish
'''
            result = run_sourced(snippet)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f'Repository: {target}', result.stdout)
            self.assertIn('Branch: main', result.stdout)
            self.assertIn('Commit: abc1234', result.stdout)
            self.assertIn('Region/locale: EUW / pt_BR', result.stdout)
            self.assertIn('RCLOL_REGION=EUW RCLOL_LOCALE=pt_BR sh scripts/run.sh',
                          result.stdout)
            self.assertIn(f'Log: {log}', result.stdout)
            self.assertLess(result.stdout.index('DESKTOP_OFFER'),
                            result.stdout.index('To start the runtime'))


class ConfigureProjectTests(unittest.TestCase):
    def test_configure_project_passes_the_selected_region_and_locale(self):
        with tempfile.TemporaryDirectory(prefix='install-native-configure ') as folder:
            target = Path(folder) / 'repo'
            target.mkdir()
            args_file = Path(folder) / 'args'
            snippet = f'''
source "$INSTALLER"
TARGET_DIR="{target}"
REGION=BR
LOCALE=pt_BR
python3() {{ printf "%s\\n" "$@" > "{args_file}"; }}
configure_project
'''
            result = run_sourced(snippet)
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = args_file.read_text().splitlines()
            self.assertEqual(arguments[0], 'scripts/configure.py')
            self.assertEqual(arguments[1], '--guest-user')
            self.assertIn('--region', arguments)
            self.assertEqual(arguments[arguments.index('--region') + 1], 'BR')
            self.assertIn('--locale', arguments)
            self.assertEqual(arguments[arguments.index('--locale') + 1], 'pt_BR')


if __name__ == '__main__':
    unittest.main()
