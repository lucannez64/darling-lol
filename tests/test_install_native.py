"""Unit and CLI coverage for scripts/install-native.sh."""
import os
import pty
import shutil
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
    # Use the default desktop-entry policy unless a test overrides it.
    env.pop('DARLING_LOL_DESKTOP_ENTRY', None)
    # Keep default-release cases hermetic unless a test overrides it.
    env.pop('DARLING_LOL_TOOLBOX_RELEASE', None)
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


def bin_sandbox(folder, *names):
    """Return a PATH-restricted bin dir exposing only the given system tools."""
    bindir = Path(folder) / 'bin'
    bindir.mkdir()
    for name in names:
        resolved = shutil.which(name)
        if resolved is None:
            raise unittest.SkipTest(f'{name} is unavailable')
        os.symlink(resolved, bindir / name)
    return bindir


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

    def test_an_explicit_locale_the_client_does_not_ship_is_refused(self):
        for arguments in ('--locale fr_CA', '--locale=de-at'):
            with self.subTest(arguments=arguments):
                result = run_sourced(f'source "$INSTALLER"\nparse_args {arguments}\necho reached')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Unsupported locale', result.stderr)
                self.assertIn('Supported locales: ', result.stderr)
                self.assertIn('fr_FR', result.stderr)
                self.assertNotIn('reached', result.stdout)

        result = run_sourced('source "$INSTALLER"\nresolve_locale\necho reached',
                             RCLOL_LOCALE='fr_CA')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported RCLOL_LOCALE 'fr_CA'", result.stderr)
        self.assertNotIn('reached', result.stdout)

    def test_the_locale_list_matches_the_one_configure_uses(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('rclol_common', ROOT / 'scripts/common.py')
        common = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(common)
        result = run_sourced('source "$INSTALLER"\nlocale_codes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), list(common.LOCALES))
        self.assertIn(common.DEFAULTS['locale'], common.LOCALES)
        # Every region the installer offers is one configure.py accepts.
        self.assertLessEqual(set(REGION_CODES), set(common.REGIONS))


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

    def test_region_for_country_only_matches_real_servers(self):
        snippet = (
            'source "$INSTALLER"\n'
            '[[ $(region_for_country BR) == BR ]] || exit 9\n'
            '[[ $(region_for_country CA) == NA ]] || exit 9\n'
            '[[ $(region_for_country PL) == EUNE ]] || exit 9\n'
        )
        result = run_sourced(snippet)
        self.assertEqual(result.returncode, 0, result.stderr)

        for country in ('DE', 'GB', ''):
            result = run_sourced(f'source "$INSTALLER"\nregion_for_country {country!r}')
            self.assertNotEqual(result.returncode, 0, country)
            self.assertEqual(result.stdout, '', country)

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

        # Without a terminal the system locale is never used.
        result = run_sourced(
            'source "$INSTALLER"\nresolve_locale\nprintf "%s\\n" "$LOCALE"',
            RCLOL_LOCALE='', LC_ALL='', LC_MESSAGES='', LANG='pt_BR.UTF-8',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'en_GB')

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
            'LOCALE_EXPLICIT=1\n'
            'YES=1\n'
            'resolve_region\n'
            'printf "%s\\n" "$REGION"',
            RCLOL_REGION='',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'BR')

    def unattended(self, arguments='--yes', **environment):
        """Region and locale as main() resolves them, without a terminal."""
        return run_sourced(
            'source "$INSTALLER"\n'
            f'parse_args {arguments}\n'
            'resolve_locale\nselect_locale\nresolve_region\n'
            'printf "%s/%s\\n" "$REGION" "$LOCALE"',
            **environment,
        )

    def test_unattended_runs_keep_the_default_region_and_locale(self):
        for system in ('en_US.UTF-8', 'pt_BR.UTF-8', 'fr_CA.UTF-8', 'de_AT.UTF-8', 'C.UTF-8'):
            for arguments in ('--yes', ''):
                with self.subTest(system=system, arguments=arguments):
                    result = self.unattended(arguments, LANG=system)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'EUW/en_GB')

    def test_an_explicit_locale_still_derives_the_region_when_unattended(self):
        result = self.unattended('--yes --locale pt_BR', LANG='en_US.UTF-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'BR/pt_BR')

        result = self.unattended('--yes', LANG='en_US.UTF-8', RCLOL_LOCALE='ko_KR')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'KR/ko_KR')

        result = self.unattended('--yes --locale pt_BR --region na', LANG='en_US.UTF-8')
        self.assertEqual(result.stdout.strip(), 'NA/pt_BR')

    def test_supported_system_locale_drops_locales_the_client_does_not_ship(self):
        result = run_sourced('source "$INSTALLER"\nsupported_system_locale', LANG='en_US.UTF-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'en_US')
        for system in ('fr_CA.UTF-8', 'de_AT.UTF-8', 'C.UTF-8'):
            with self.subTest(system=system):
                result = run_sourced('source "$INSTALLER"\nsupported_system_locale', LANG=system)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')

    def test_resolve_region_rejects_an_invalid_environment_value(self):
        result = run_sourced(
            'source "$INSTALLER"\n'
            'LOCALE=pt_BR\n'
            'resolve_region',
            RCLOL_REGION='XX',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid RCLOL_REGION', result.stderr)


class LocalePromptTests(unittest.TestCase):
    """The system locale is only the suggested answer of the interactive picker."""

    def run_prompt(self, reply, *, system, yes=0):
        snippet = (
            'source "$INSTALLER"\n'
            f'YES={yes}\n'
            'resolve_locale\n'
            'printf "BEFORE=%s\\n" "$LOCALE"\n'
            'select_locale\n'
            'printf "LOCALE=%s\\n" "$LOCALE"\n'
        )
        return run_sourced_pty(snippet, reply, LANG=system)

    def test_the_system_locale_is_offered_and_confirmed_with_enter(self):
        code, output = self.run_prompt(b'\n', system='en_US.UTF-8')
        self.assertEqual(code, 0, output)
        self.assertIn('BEFORE=\r\n', output)
        self.assertIn('▶ Locale [en_US]: ', output)
        self.assertIn('LOCALE=en_US', output)

    def test_another_listed_locale_can_be_typed(self):
        code, output = self.run_prompt(b'FR-fr\n', system='en_US.UTF-8')
        self.assertEqual(code, 0, output)
        self.assertIn('LOCALE=fr_FR', output)

    def test_an_unshipped_system_locale_suggests_the_default(self):
        for system in ('fr_CA.UTF-8', 'de_AT.UTF-8'):
            with self.subTest(system=system):
                code, output = self.run_prompt(b'\n', system=system)
                self.assertEqual(code, 0, output)
                self.assertIn('▶ Locale [en_GB]: ', output)
                self.assertIn('LOCALE=en_GB', output)

    def test_an_unshipped_answer_asks_again(self):
        code, output = self.run_prompt(b'fr_CA\nes_MX\n', system='en_US.UTF-8')
        self.assertEqual(code, 0, output)
        self.assertIn('Unsupported locale. Choose one of the listed locales.', output)
        self.assertIn('LOCALE=es_MX', output)

    def test_yes_never_asks(self):
        code, output = self.run_prompt(b'', system='en_US.UTF-8', yes=1)
        self.assertEqual(code, 0, output)
        self.assertNotIn('Locale [', output)
        self.assertIn('BEFORE=en_GB', output)
        self.assertIn('LOCALE=en_GB', output)


class RegionPromptTests(unittest.TestCase):
    """Interactive shortcut when the chosen locale maps to a server."""

    def run_prompt(self, locale, reply, *, yes=0):
        snippet = (
            'source "$INSTALLER"\n'
            f'LOCALE={locale}\n'
            f'YES={yes}\n'
            'resolve_region\n'
            'printf "REGION=%s\\n" "$REGION"\n'
        )
        return run_sourced_pty(snippet, reply)

    def test_accepting_the_shortcut_skips_the_region_list(self):
        code, output = self.run_prompt('pt_BR', b'y\n')
        self.assertEqual(code, 0, output)
        # The arrow marks an input prompt, and no region code leaks from
        # region_for_country's stdout before it.
        self.assertIn('▶ Use the region server for your locale (BR)? [Y/n]', output)
        self.assertNotIn('BRUse', output)
        self.assertNotIn('Region number or code', output)
        self.assertIn('REGION=BR', output)

    def test_an_empty_answer_takes_the_default_yes(self):
        code, output = self.run_prompt('pt_BR', b'\n')
        self.assertEqual(code, 0, output)
        self.assertNotIn('Region number or code', output)
        self.assertIn('REGION=BR', output)

    def test_declining_the_shortcut_falls_back_to_the_region_list(self):
        code, output = self.run_prompt('pt_BR', b'n\n4\n')
        self.assertEqual(code, 0, output)
        self.assertIn('▶ Use the region server for your locale (BR)? [Y/n]', output)
        self.assertIn('▶ Region number or code [BR]', output)
        self.assertIn('REGION=BR', output)

    def test_an_invalid_answer_asks_again(self):
        code, output = self.run_prompt('pt_BR', b'x\ny\n')
        self.assertEqual(code, 0, output)
        self.assertIn('Invalid answer. Enter y or n.', output)
        self.assertIn('REGION=BR', output)

    def test_an_unmapped_locale_goes_straight_to_the_region_list(self):
        code, output = self.run_prompt('de_DE', b'3\n')
        self.assertEqual(code, 0, output)
        self.assertNotIn('Use the region server', output)
        self.assertIn('▶ Region number or code [EUW]', output)
        self.assertIn('REGION=NA', output)

    def test_non_interactive_runs_derive_the_region_only_from_an_explicit_locale(self):
        for explicit, expected in ((1, 'REGION=BR'), (0, 'REGION=EUW')):
            with self.subTest(explicit=explicit):
                snippet = (
                    'source "$INSTALLER"\n'
                    'LOCALE=pt_BR\n'
                    f'LOCALE_EXPLICIT={explicit}\n'
                    'YES=0\n'
                    'resolve_region\n'
                    'printf "REGION=%s\\n" "$REGION"\n'
                )
                result = run_sourced(snippet)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)


class DetectDistributionTests(unittest.TestCase):
    FEDORA_40 = 'ID=fedora\nID_LIKE="rhel fedora"\nVERSION_ID="40"\n'
    FEDORA_44 = 'ID=fedora\nID_LIKE="rhel fedora"\nVERSION_ID="44"\n'
    FEDORA_47 = 'ID=fedora\nID_LIKE="rhel fedora"\nVERSION_ID="47"\n'
    UBUNTU_2404 = 'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n'
    ZORIN_17 = 'ID=zorin\nID_LIKE="ubuntu debian"\nVERSION_ID="17"\n'
    OPENSUSE_LEAP = 'ID=opensuse-leap\nID_LIKE="suse opensuse"\nVERSION_ID="16.0"\n'

    def detect(self, os_release, *, container=0, release=''):
        with tempfile.TemporaryDirectory(prefix='install-native-distro ') as folder:
            release_file = Path(folder) / 'os-release'
            release_file.write_text(os_release)
            snippet = (
                'source "$INSTALLER"\n'
                f'CONTAINER_INSTALL={container}\n'
                'detect_distribution\n'
                'printf "FAMILY=%s\\n" "$FAMILY"\n'
                'printf "TOOLBOX_RELEASE=%s\\n" "$TOOLBOX_RELEASE"\n'
            )
            env = {'DARLING_OS_RELEASE': str(release_file)}
            if release:
                env['DARLING_LOL_TOOLBOX_RELEASE'] = release
            return run_sourced(snippet, **env)

    def test_container_mode_skips_host_version_gates(self):
        result = self.detect(self.UBUNTU_2404, container=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('26.04', result.stderr)
        self.assertNotIn('only launches the container', result.stdout)
        self.assertIn('FAMILY=debian', result.stdout)

    def test_native_mode_still_requires_new_ubuntu(self):
        result = self.detect(self.UBUNTU_2404)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Ubuntu 26.04', result.stderr)

    def test_container_mode_accepts_an_old_fedora_host(self):
        result = self.detect(self.FEDORA_40, container=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Fedora 44', result.stderr)
        self.assertIn('FAMILY=fedora', result.stdout)
        # An old host is bumped to the minimum release, keeping the banner and
        # the created container consistent.
        self.assertIn('TOOLBOX_RELEASE=44', result.stdout)

        result = self.detect(self.FEDORA_40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Fedora 44 or newer', result.stderr)

    def test_toolbox_release_resolution_matches_the_container(self):
        result = self.detect(self.FEDORA_47, container=1)
        self.assertIn('TOOLBOX_RELEASE=47', result.stdout)

        result = self.detect(self.UBUNTU_2404, container=1)
        self.assertIn('TOOLBOX_RELEASE=44', result.stdout)

        result = self.detect(self.FEDORA_47, container=1, release='45')
        self.assertIn('TOOLBOX_RELEASE=45', result.stdout)

    def test_a_current_fedora_host_still_passes_natively(self):
        result = self.detect(self.FEDORA_44)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('FAMILY=fedora', result.stdout)

    def test_an_unknown_host_distro_never_fails_in_container_mode(self):
        result = self.detect(self.OPENSUSE_LEAP, container=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('FAMILY=other', result.stdout)

        result = self.detect(self.OPENSUSE_LEAP)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Supported distributions', result.stderr)

    def test_a_derivative_keeps_its_debian_family(self):
        result = self.detect(self.ZORIN_17, container=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('FAMILY=debian', result.stdout)

        result = self.detect(self.ZORIN_17)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Derivative 'zorin' detected", result.stderr)
        self.assertIn('FAMILY=debian', result.stdout)


class ToolboxSupportTests(unittest.TestCase):
    """check_toolbox_support must not depend on the host distribution."""

    PLENTY_KIB = 200 * 1024 * 1024  # 200 GiB

    def run_support(self, *, toolbox=True, podman=True, podman_info='ok',
                    free_kib=PLENTY_KIB, family=''):
        with tempfile.TemporaryDirectory(prefix='install-native-toolbox-support ') as folder:
            # Restrict PATH so the real toolbox/podman binaries cannot leak in.
            bindir = bin_sandbox(folder, 'bash', 'awk', 'dirname', 'basename')
            stubs = []
            if toolbox:
                stubs.append('toolbox() { :; }')
            if podman:
                if podman_info == 'ok':
                    stubs.append('podman() { printf "%s\\n" "/mock-toolbox-test/containers"; }')
                elif podman_info == 'fail':
                    stubs.append('podman() { return 1; }')
            stubs.append(
                'df() { printf "Filesystem 1024-blocks Used Available Capacity Mounted\\n'
                'mock 1 1 %s 1%% /\\n" "$DF_FREE"; }'
            )
            snippet = '\n'.join([
                'source "$INSTALLER"',
                *stubs,
                f'FAMILY={family}',
                f'DF_FREE={free_kib}',
                'check_toolbox_support',
            ])
            return run_sourced(snippet, PATH=str(bindir))

    def test_toolbox_and_podman_with_plenty_of_storage_pass_on_any_host(self):
        result = self.run_support()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_toolbox_fails_without_a_fedora_requirement(self):
        result = self.run_support(toolbox=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Toolbox is not installed', result.stderr)
        self.assertNotIn('Fedora hosts', result.stderr)

    def test_missing_podman_fails(self):
        result = self.run_support(podman=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Podman is not installed', result.stderr)

    def test_missing_packages_hint_the_host_package_manager(self):
        result = self.run_support(toolbox=False, family='debian')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Install it with 'sudo apt install podman-toolbox'", result.stderr)

        result = self.run_support(podman=False, family='fedora')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Install it with 'sudo dnf install toolbox podman'", result.stderr)

    def test_an_unknown_host_falls_back_to_a_generic_hint(self):
        result = self.run_support(podman=False, family='other')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('packages from your distribution', result.stderr)

    def test_little_space_on_the_podman_storage_partition_fails(self):
        result = self.run_support(free_kib=1024)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('GiB free', result.stderr)
        self.assertIn('/mock-toolbox-test/containers', result.stderr)

    def test_a_podman_info_failure_falls_back_to_the_default_storage(self):
        result = self.run_support(podman_info='fail')
        self.assertEqual(result.returncode, 0, result.stderr)


class PreflightTests(unittest.TestCase):
    """preflight relaxes host-only requirements in container mode."""

    def run_preflight(self, *, container=0):
        with tempfile.TemporaryDirectory(prefix='install-native-preflight ') as folder:
            # Restrict PATH: sudo, git, uname and df must not resolve, so their
            # behaviour has to come from the stubs (git is stubbed in both
            # modes; sudo absence is exactly what the test exercises).
            bindir = bin_sandbox(folder, 'bash', 'awk', 'dirname', 'basename')
            stubs = (
                'uname() { case $1 in -r) printf "6.6.0-generic" ;; *) printf "x86_64" ;; esac; }\n'
                'df() { printf "Filesystem 1024-blocks Used Available Capacity Mounted\\n'
                'mock 1 1 62914560 1%% /\\n"; }\n'
                'git() { :; }\n'
            )
            snippet = (
                'source "$INSTALLER"\n'
                f'{stubs}\n'
                f'CONTAINER_INSTALL={container}\n'
                'preflight\n'
            )
            return run_sourced(snippet, PATH=str(bindir))

    def test_container_mode_does_not_require_sudo_on_the_host(self):
        result = self.run_preflight(container=1)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_mode_still_requires_sudo(self):
        result = self.run_preflight(container=0)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sudo is required', result.stderr)


class ShowBannerTests(unittest.TestCase):
    def run_banner(self, *, container=0, family='debian', region='EUW', locale='pt_BR',
                   toolbox_release='', explicit=1, yes=0):
        release_line = f'\nTOOLBOX_RELEASE="{toolbox_release}"' if toolbox_release else ''
        snippet = (
            'source "$INSTALLER"\n'
            f'CONTAINER_INSTALL={container}\n'
            f'FAMILY={family}\n'
            f'REGION={region}\n'
            f'LOCALE={locale}\n'
            f'LOCALE_EXPLICIT={explicit}\n'
            f'YES={yes}\n'
            'TOOLBOX_NAME="my-box"\n'
            f'{release_line}\n'
            'show_banner\n'
        )
        return run_sourced(snippet)

    def test_container_mode_shows_the_toolbox_fedora_release(self):
        result = self.run_banner(container=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Toolbox Distribution: Fedora 44', result.stdout)
        self.assertNotIn('Toolbox container', result.stdout)

        result = self.run_banner(container=1, toolbox_release='45')
        self.assertIn('Toolbox Distribution: Fedora 45', result.stdout)

    def test_native_mode_shows_only_the_family(self):
        result = self.run_banner(container=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Distribution: debian', result.stdout)
        self.assertNotIn('Toolbox Distribution', result.stdout)

    def test_locale_derived_region_has_no_source_suffix(self):
        result = self.run_banner(container=0, region='', locale='pt_BR')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Riot Client region: BR', result.stdout)
        self.assertNotIn('(from the system locale)', result.stdout)


    def test_an_unattended_banner_shows_the_defaults(self):
        result = self.run_banner(region='', locale='en_GB', explicit=0, yes=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Riot Client region: EUW (default)', result.stdout)
        self.assertIn('Riot Client locale: en_GB (default)', result.stdout)


class ArchUpgradeTests(unittest.TestCase):
    PROMPT_SNIPPET = (
        'source "$INSTALLER"\n'
        'CONTAINER_INSTALL={container}\n'
        'FAMILY=arch\n'
        'confirm_arch_upgrade\n'
        'printf "DONE\\n"\n'
    )

    def test_container_mode_skips_the_upgrade_warning(self):
        result = run_sourced(self.PROMPT_SNIPPET.format(container=1))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('DONE', result.stdout)
        self.assertNotIn('full system upgrade', result.stdout)

    def test_native_mode_still_asks_on_arch(self):
        result = run_sourced(self.PROMPT_SNIPPET.format(container=0))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pacman -Syu', result.stderr)
        self.assertIn('Installation cancelled', result.stderr)


class DesktopEntryTests(unittest.TestCase):
    def create_entry(self, *, inside_toolbox=0, name='darling-lol', helper=True,
                     helper_fails=False, entry=None):
        with tempfile.TemporaryDirectory(prefix='install-native-desktop-') as folder:
            repo = Path(folder) / 'repo'
            home = Path(folder) / 'home'
            home.mkdir()
            tools = repo / 'build/runtime/tools'
            tools.mkdir(parents=True)
            if helper:
                (tools / 'desktop-entry.py').write_text('')
            entry_file = home / '.local/share/applications/darling-launcher.desktop'
            if entry is not None:
                entry_file.parent.mkdir(parents=True)
                script = ('launch-shortcut.sh' if entry == 'current'
                          else 'desktop-run.sh')
                entry_file.write_text(
                    f'[Desktop Entry]\nExec="{repo}/scripts/{script}"\n')
            args_file = Path(folder) / 'args'
            # The stub helper records its arguments and, like the real one,
            # rewrites an existing entry to the current shortcut script.
            if helper_fails:
                fake_python = 'python3() { return 7; }'
            else:
                refresh = ''
                if entry is not None:
                    refresh = (
                        f'  printf "[Desktop Entry]\\nExec=\\"{repo}/scripts/launch-shortcut.sh\\"\\n"'
                        f' > "{entry_file}"\n'
                    )
                fake_python = (
                    'python3() {\n'
                    f'  printf "%s\\n" "$@" > "{args_file}"\n'
                    f'{refresh}'
                    '}\n'
                )
            snippet = f'''
source "$INSTALLER"
TARGET_DIR="{repo}"
TOOLBOX_NAME="{name}"
INSIDE_TOOLBOX={inside_toolbox}
HOME="{home}"
{fake_python}
create_desktop_entry
'''
            result = run_sourced(snippet)
            arguments = args_file.read_text().splitlines() if args_file.exists() else []
            # The disposable tree dies with this helper; hand back content.
            entry_text = entry_file.read_text() if entry_file.exists() else ''
            return result, arguments, entry_text

    def test_native_install_calls_helper_without_toolbox(self):
        result, arguments, _ = self.create_entry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(arguments), 1)
        self.assertTrue(arguments[0].endswith('build/runtime/tools/desktop-entry.py'))

    def test_toolbox_install_passes_the_container_name(self):
        result, arguments, _ = self.create_entry(inside_toolbox=1, name='my-box')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(arguments), 3)
        self.assertEqual(arguments[1:], ['--toolbox', 'my-box'])
        self.assertTrue(arguments[0].endswith('build/runtime/tools/desktop-entry.py'))

    def test_an_outdated_shortcut_is_flagged_and_refreshed(self):
        result, _, entry_text = self.create_entry(entry='old')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Updating the Darling Launcher shortcut', result.stdout)
        self.assertNotIn('still outdated', result.stderr)
        self.assertIn('scripts/launch-shortcut.sh', entry_text)

    def test_a_current_shortcut_is_not_flagged(self):
        result, _, entry_text = self.create_entry(entry='current')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Updating the Darling Launcher shortcut', result.stdout)
        self.assertNotIn('still outdated', result.stderr)
        self.assertIn('scripts/launch-shortcut.sh', entry_text)

    def test_a_failed_helper_leaves_the_outdated_shortcut_with_a_warning(self):
        result, _, entry_text = self.create_entry(entry='old', helper_fails=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('Updating the Darling Launcher shortcut', result.stdout)
        self.assertIn('Could not create the desktop entry', result.stderr)
        self.assertIn('scripts/desktop-run.sh', entry_text)

    def offer_home(self, *, ready=False):
        """A disposable HOME, optionally holding a current launcher entry."""
        tmp = tempfile.TemporaryDirectory(prefix='install-native-offer-')
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        if ready:
            entry = home / '.local/share/applications/darling-launcher.desktop'
            entry.parent.mkdir(parents=True)
            entry.write_text('[Desktop Entry]\nExec="/repo/scripts/launch-shortcut.sh"\n')
        return home

    def offer_snippet(self, home, body='YES=0\noffer_desktop_entry'):
        return (
            'source "$INSTALLER"\n'
            'create_desktop_entry() { printf "CALLED\\n"; }\n'
            f'HOME="{home}"\n'
            f'{body}\n'
        )

    def test_enabled_environment_calls_helper(self):
        result = run_sourced(
            self.offer_snippet(self.offer_home(ready=True)),
            DARLING_LOL_DESKTOP_ENTRY='1',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CALLED', result.stdout)

    def test_disabled_environment_skips_helper_and_counts_as_declined(self):
        result = run_sourced(
            self.offer_snippet(self.offer_home()),
            DARLING_LOL_DESKTOP_ENTRY='0',
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn('CALLED', result.stdout)

    def test_assume_yes_and_non_interactive_runs_decline_the_offer(self):
        result = run_sourced(self.offer_snippet(self.offer_home(), body='YES=1\noffer_desktop_entry'))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn('CALLED', result.stdout)

        result = run_sourced(self.offer_snippet(self.offer_home()))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn('CALLED', result.stdout)

    def test_affirmative_and_negative_prompt_answers(self):
        code, output = run_sourced_pty(
            self.offer_snippet(self.offer_home(ready=True)), b'YES\n')
        self.assertEqual(code, 0, output)
        self.assertIn('Create a desktop entry', output)
        self.assertIn('CALLED', output)

        code, output = run_sourced_pty(
            self.offer_snippet(self.offer_home()), b'n\n')
        self.assertEqual(code, 1, output)
        self.assertNotIn('CALLED', output)

    def test_an_empty_answer_defaults_to_yes(self):
        code, output = run_sourced_pty(
            self.offer_snippet(self.offer_home(ready=True)), b'\n')
        self.assertEqual(code, 0, output)
        self.assertIn('Create a desktop entry automatically? [Y/n]', output)
        self.assertIn('CALLED', output)

    def test_an_accepted_offer_without_a_ready_shortcut_counts_as_declined(self):
        # The stub create_desktop_entry prints but never installs the entry,
        # mirroring a failed helper: the summary keeps the manual blocks.
        code, output = run_sourced_pty(self.offer_snippet(self.offer_home()), b'\n')
        self.assertEqual(code, 1, output)
        self.assertIn('CALLED', output)

    def test_an_invalid_answer_asks_again(self):
        code, output = run_sourced_pty(
            self.offer_snippet(self.offer_home()), b'maybe\nn\n')
        self.assertEqual(code, 1, output)
        self.assertIn('Invalid answer. Enter y or n.', output)
        self.assertNotIn('CALLED', output)

    def test_missing_or_failing_helper_only_warns(self):
        result, arguments, _ = self.create_entry(helper=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(arguments, [])
        self.assertIn('Desktop entry helper not found', result.stderr)

        result, _, _ = self.create_entry(helper_fails=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Could not create the desktop entry', result.stderr)


class ToolboxInvocationTests(unittest.TestCase):
    def run_install_toolbox(self, *, yes=0, verbose=0, name='darling-lol', exists=False,
                            locale='pt_BR', family='', release='', toolbox_release=''):
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
FAMILY="{family}"
TOOLBOX_RELEASE="{toolbox_release}"
TOOLBOX_NAME="{name}"
install_toolbox
'''
            env = {'PODMAN_RC': '0' if exists else '1'}
            if release:
                env['DARLING_LOL_TOOLBOX_RELEASE'] = release
            result = run_sourced(snippet, **env)
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

    def test_a_fedora_host_reuses_its_supported_release(self):
        result, lines = self.run_install_toolbox(family='fedora', toolbox_release='47')
        self.assertEqual(result.returncode, 0, result.stderr)
        create = next(line for line in lines if 'toolbox create' in line)
        self.assertIn('--distro fedora', create)
        self.assertIn('--release 47', create)

    def test_non_fedora_hosts_pin_the_minimum_fedora_release(self):
        result, lines = self.run_install_toolbox(family='debian')
        self.assertEqual(result.returncode, 0, result.stderr)
        create = next(line for line in lines if 'toolbox create' in line)
        self.assertIn('--distro fedora', create)
        self.assertIn('--release 44', create)

        result, lines = self.run_install_toolbox(family='other')
        create = next(line for line in lines if 'toolbox create' in line)
        self.assertIn('--release 44', create)

    def test_release_override_is_forwarded_on_any_host(self):
        result, lines = self.run_install_toolbox(family='debian', release='45')
        self.assertEqual(result.returncode, 0, result.stderr)
        create = next(line for line in lines if 'toolbox create' in line)
        self.assertIn('--release 45', create)

        result, lines = self.run_install_toolbox(family='fedora', release='45')
        create = next(line for line in lines if 'toolbox create' in line)
        self.assertIn('--release 45', create)


class RiotInstallerOfferTests(unittest.TestCase):
    def run_offer(self, reply, *, region='BR', yes=0, installer=True, installer_rc=0):
        with tempfile.TemporaryDirectory(prefix='install-native-riot ') as folder:
            target = Path(folder) / 'repo'
            (target / 'scripts').mkdir(parents=True)
            if installer:
                helper = target / 'scripts' / 'installer.sh'
                helper.write_text(
                    '#!/bin/sh\n'
                    'printf "RIOT %s %s\\n" "$1" "${RCLOL_LOCALE:-unset}" > riot.log\n'
                    f'exit {installer_rc}\n'
                )
                helper.chmod(0o755)
            snippet = (
                'source "$INSTALLER"\n'
                f'TARGET_DIR="{target}"\n'
                f'REGION={region}\n'
                'LOCALE=pt_BR\n'
                f'YES={yes}\n'
                'offer_riot_client_installer\n'
                'printf "OFFER_RC=%s\\n" "$?"\n'
            )
            code, output = run_sourced_pty(snippet, reply)
            log = ''
            log_file = target / 'riot.log'
            if log_file.exists():
                log = log_file.read_text()
            return code, output, log

    def test_accepting_launches_the_installer_with_the_region_and_locale(self):
        code, output, log = self.run_offer(b'y\n')
        self.assertEqual(code, 0)
        self.assertIn('▶ Launch the Riot Client installer now? [y/N]', output)
        self.assertNotIn('(br)', output)
        self.assertIn('OFFER_RC=0', output)
        self.assertEqual(log.strip(), 'RIOT br pt_BR')

    def test_an_empty_answer_takes_the_default_no(self):
        code, output, log = self.run_offer(b'\n')
        self.assertIn('Launch the Riot Client installer now? [y/N]', output)
        self.assertIn('OFFER_RC=1', output)
        self.assertEqual(log, '')

    def test_declining_skips_the_installer(self):
        code, output, log = self.run_offer(b'n\n')
        self.assertIn('Launch the Riot Client installer', output)
        self.assertIn('OFFER_RC=1', output)
        self.assertEqual(log, '')

    def test_the_installer_exit_code_propagates(self):
        code, output, log = self.run_offer(b'y\n', installer_rc=3)
        self.assertIn('OFFER_RC=3', output)
        self.assertEqual(log.strip(), 'RIOT br pt_BR')

    def test_yes_runs_skip_the_prompt(self):
        code, output, log = self.run_offer(b'', yes=1)
        self.assertNotIn('Launch the Riot Client installer', output)
        self.assertIn('OFFER_RC=1', output)
        self.assertEqual(log, '')

    def test_a_run_without_a_terminal_skips_the_prompt(self):
        with tempfile.TemporaryDirectory(prefix='install-native-riot ') as folder:
            target = Path(folder) / 'repo'
            (target / 'scripts').mkdir(parents=True)
            helper = target / 'scripts' / 'installer.sh'
            helper.write_text('#!/bin/sh\ntouch riot.log\n')
            helper.chmod(0o755)
            result = run_sourced(
                'source "$INSTALLER"\n'
                f'TARGET_DIR="{target}"\n'
                'REGION=BR\nYES=0\n'
                'offer_riot_client_installer\n'
                'printf "OFFER_RC=%s\\n" "$?"\n'
            )
            self.assertNotIn('Launch the Riot Client installer', result.stdout + result.stderr)
            self.assertIn('OFFER_RC=1', result.stdout)
            self.assertFalse((target / 'riot.log').exists())

    def test_a_missing_installer_script_is_not_offered(self):
        code, output, log = self.run_offer(b'\n', installer=False)
        self.assertNotIn('Launch the Riot Client installer', output)
        self.assertIn('OFFER_RC=1', output)
        self.assertEqual(log, '')

    def test_the_region_comes_from_the_selected_region(self):
        code, output, log = self.run_offer(b'y\n', region='EUW')
        self.assertIn('Launch the Riot Client installer now? [y/N]', output)
        self.assertNotIn('(euw)', output)
        self.assertIn('OFFER_RC=0', output)
        self.assertEqual(log.strip(), 'RIOT euw pt_BR')


class FinishTests(unittest.TestCase):
    def finish_snippet(self, *, riot_offer_rc, desktop_offer_rc=1):
        with tempfile.TemporaryDirectory(prefix='install-native-finish-') as folder:
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
offer_desktop_entry() {{ printf "DESKTOP_OFFER\\n"; return {desktop_offer_rc}; }}
offer_riot_client_installer() {{ printf "RIOT_OFFER\\n"; return {riot_offer_rc}; }}
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
            return result

    def test_desktop_offer_precedes_launch_instructions(self):
        result = self.finish_snippet(riot_offer_rc=1)
        self.assertIn('Repository: ', result.stdout)
        self.assertIn('Branch: main', result.stdout)
        self.assertIn('Commit: abc1234', result.stdout)
        self.assertIn('Region/locale: EUW / pt_BR', result.stdout)
        self.assertIn('Log: ', result.stdout)
        self.assertIn('RCLOL_REGION=EUW RCLOL_LOCALE=pt_BR sh scripts/run.sh',
                      result.stdout)
        self.assertLess(result.stdout.index('DESKTOP_OFFER'),
                        result.stdout.index('To start the runtime'))

    def test_a_declined_shortcut_keeps_both_manual_blocks(self):
        result = self.finish_snippet(riot_offer_rc=1, desktop_offer_rc=1)
        self.assertIn('To start the runtime', result.stdout)
        self.assertIn('To install Riot Client later (EUW):', result.stdout)
        self.assertIn('./scripts/installer.sh euw', result.stdout)

    def test_a_launched_riot_installer_hides_only_the_later_hint(self):
        result = self.finish_snippet(riot_offer_rc=0, desktop_offer_rc=1)
        self.assertIn('To start the runtime', result.stdout)
        self.assertNotIn('To install Riot Client later', result.stdout)

    def test_a_ready_shortcut_hides_both_manual_blocks(self):
        result = self.finish_snippet(riot_offer_rc=1, desktop_offer_rc=0)
        self.assertNotIn('To start the runtime', result.stdout)
        self.assertNotIn('To install Riot Client later', result.stdout)

    def test_a_ready_shortcut_hides_the_manual_blocks_even_after_the_riot_offer(self):
        result = self.finish_snippet(riot_offer_rc=0, desktop_offer_rc=0)
        self.assertLess(result.stdout.index('DESKTOP_OFFER'),
                        result.stdout.index('RIOT_OFFER'))
        self.assertNotIn('To start the runtime', result.stdout)
        self.assertNotIn('To install Riot Client later', result.stdout)

    def test_riot_offer_sits_after_the_desktop_entry_and_hides_the_later_hint(self):
        result = self.finish_snippet(riot_offer_rc=0, desktop_offer_rc=1)
        self.assertLess(result.stdout.index('DESKTOP_OFFER'),
                        result.stdout.index('RIOT_OFFER'))
        self.assertLess(result.stdout.index('RIOT_OFFER'),
                        result.stdout.index('To start the runtime'))
        self.assertNotIn('To install Riot Client later', result.stdout)

    def test_the_safety_notice_precedes_the_riot_offer(self):
        with tempfile.TemporaryDirectory(prefix='install-native-finish-') as folder:
            snippet = f'''
source "$INSTALLER"
exec 2>&1
git() {{ :; }}
offer_desktop_entry() {{ return 1; }}
offer_riot_client_installer() {{ printf "RIOT_OFFER\\n"; return 0; }}
TARGET_DIR="{folder}"
LOG="{folder}/install.log"
REGION=EUW
LOCALE=pt_BR
SECONDS=0
finish
'''
            result = run_sourced(snippet)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.count('safety notice'), 1, result.stdout)
        self.assertLess(result.stdout.index('safety notice'),
                        result.stdout.index('RIOT_OFFER'))

    def test_the_summary_does_not_promise_a_locale_change_on_existing_installs(self):
        result = self.finish_snippet(riot_offer_rc=1)
        self.assertIn('Region/locale: EUW / pt_BR (used for new installs and the first launch)',
                      result.stdout)

    def test_a_skipped_riot_offer_keeps_the_manual_hint(self):
        result = self.finish_snippet(riot_offer_rc=1)
        self.assertIn('RIOT_OFFER', result.stdout)
        self.assertIn('To install Riot Client later (EUW):', result.stdout)
        self.assertIn('./scripts/installer.sh euw', result.stdout)


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
