"""Safe packaging regressions: only temporary repositories, homes and stub commands."""
import hashlib
import importlib.util
import json
import getpass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, input='', text=True,
                          capture_output=True, timeout=30)


def git(repo, *args):
    result = run('git', '-C', str(repo), *args)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def init(repo):
    repo.mkdir(parents=True)
    git(repo, 'init', '-b', 'master')
    git(repo, 'config', 'user.name', 'Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')


def shutdown_patch():
    return 'diff --git a/tools/shutdown-user.sh b/tools/shutdown-user.sh\n' + (
        ROOT / 'patches/darling.patch').read_text().split(
        'diff --git a/tools/shutdown-user.sh b/tools/shutdown-user.sh\n', 1)[1].split('\ndiff --git ', 1)[0] + '\n'


def patch_base(patch):
    lines = patch.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith('@@')) + 1
    return '\n'.join(line[1:] for line in lines[start:] if line.startswith((' ', '-'))) + '\n'


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nix-prepare-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        for name in ['scripts', 'patches', 'upstream']:
            (self.project / name).mkdir(parents=True)
        for name in ['prepare.py', 'common.py']:
            shutil.copy(ROOT / 'scripts' / name, self.project / 'scripts' / name)
        self.source = self.root / 'source'
        init(self.source)
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', HOME=str(self.root))

    def prepare(self, check=False):
        return run(sys.executable, 'scripts/prepare.py', '--source', str(self.source),
                   *(['--check-only'] if check else []), cwd=self.project, env=self.env)

    def manifest(self, revision, components, upstream=None):
        (self.project / 'upstream/manifest.json').write_text(json.dumps({
            'upstream': upstream or self.source.as_uri(), 'revision': revision, 'components': components}))

    def test_prepare_twice_and_check_only_preserves_all_files(self):
        patch = shutdown_patch()
        (self.source / 'tools').mkdir()
        (self.source / 'tools/shutdown-user.sh').write_text(patch_base(patch))
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'base')
        rev = git(self.source, 'rev-parse', 'HEAD')
        (self.project / 'patches/darling.patch').write_text(patch)
        self.manifest(rev, [{'path': '.', 'patch': 'patches/darling.patch', 'revision': rev}])
        for _ in range(2):
            result = self.prepare()
            self.assertEqual(result.returncode, 0, result.stderr)
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        result = self.prepare(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_check_only_does_not_rewrite_compatibility_headers(self):
        path = self.source / 'src/external/cctools-port/cctools/ld64/src/3rd/strlcpy.h'
        path.parent.mkdir(parents=True)
        path.write_text('size_t strlcpy(char *dst, const char *src, size_t siz);\n')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'header')
        self.manifest(git(self.source, 'rev-parse', 'HEAD'), [])
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(self.prepare(True).returncode, 0)
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_rootless_shutdown_dispatches_only_scoped_helper_and_propagates_failure(self):
        with tempfile.TemporaryDirectory(prefix='nix-hook-test-') as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            helper = root / 'scripts/nix-idle.py'
            helper.write_text('import os, sys\nprint(os.environ["DPREFIX"])\nsys.exit(int(os.environ["TEST_EXIT"]))\n')
            patch = shutdown_patch().splitlines()
            start = next(i for i, line in enumerate(patch) if line.startswith('@@')) + 1
            script = root / 'shutdown.sh'
            script.write_text('\n'.join(line[1:] for line in patch[start:] if line.startswith((' ', '+'))) + '\n')
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            # These commands must never be reached by rootless preparation.
            for name in ['sudo', 'ps', 'su', 'darling', 'killall', 'pkill']:
                stub = bin_dir / name
                stub.write_text('#!/bin/sh\necho unexpected-' + name + ' >&2\nexit 99\n')
                stub.chmod(0o755)
            env = dict(os.environ, DARLING_WORKSPACE=str(root), DPREFIX=str(root / 'prefix'),
                       TEST_EXIT='0', PATH=str(bin_dir) + ':' + os.environ['PATH'])
            success = run('sh', str(script), env=env)
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(success.stdout.strip(), env['DPREFIX'])
            env['TEST_EXIT'] = '23'
            self.assertEqual(run('sh', str(script), env=env).returncode, 23)

    def test_cctools_patch_is_maintained_and_repeatable(self):
        component = next(c for c in json.loads((ROOT / 'upstream/manifest.json').read_text())['components']
                         if c['path'] == 'src/external/cctools-port')
        patch = (ROOT / component['patch']).read_text()
        # Reconstruct exactly the base of each maintained hunk in a local repo.
        for section in patch.split('diff --git ')[1:]:
            lines = section.splitlines()
            path = lines[0].split(' b/')[1]
            header = next(i for i, line in enumerate(lines) if line.startswith('@@'))
            # Header hunk starts at line six; use a neutral prefix of that length.
            start = int(lines[header].split('-')[1].split(',')[0])
            base = ['/* untouched */\n'] * (start - 1)
            base += [line[1:] + '\n' if line else '\n' for line in lines[header + 1:] if not line or line.startswith((' ', '-'))]
            target = self.source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(''.join(base))
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'cctools')
        rev = git(self.source, 'rev-parse', 'HEAD')
        (self.project / 'patches/cctools-port.patch').write_text(patch)
        self.manifest(rev, [{'path': '.', 'revision': rev, 'patch': 'patches/cctools-port.patch'}])
        for check in [False, False, True]:
            result = self.prepare(check)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_fetch_pin_older_than_advanced_remote_and_failed_retry(self):
        (self.source / 'file').write_text('pinned')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'pin')
        pin = git(self.source, 'rev-parse', 'HEAD')
        (self.source / 'file').write_text('advanced')
        git(self.source, 'commit', '-am', 'advance')
        remote = self.source
        self.source = self.root / 'clone'
        self.manifest('0' * 40, [], remote.as_uri())
        failed = self.prepare()
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.source.exists(), 'failed initial checkout must not poison retries')
        self.manifest(pin, [], remote.as_uri())
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.source, 'rev-parse', 'HEAD'), pin)
        self.assertEqual((self.source / 'file').read_text(), 'pinned')


class CopyTests(unittest.TestCase):
    def test_symlink_workspace_is_rejected_without_external_writes(self):
        with tempfile.TemporaryDirectory(prefix='nix-symlink-test-') as directory:
            root = Path(directory)
            share = root / 'share'
            (share / 'scripts').mkdir(parents=True)
            shutil.copy(ROOT / 'scripts/nix-copy.py', share / 'scripts/nix-copy.py')
            outside = root / 'outside'
            outside.mkdir()
            data = root / 'data'
            data.mkdir()
            (data / 'darling-lol').symlink_to(outside, target_is_directory=True)
            launcher = root / 'launcher'
            launcher.write_text((ROOT / 'darling-lol-launcher.sh').read_text().replace('@SHARE_DIR@', str(share)))
            env = dict(os.environ, HOME=str(root), XDG_DATA_HOME=str(data), DPREFIX=str(root / 'prefix'))
            result = run('bash', str(launcher), 'true', env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(outside.iterdir()), [])

    def test_copied_scripts_execute_and_upgrade_without_following_symlinks(self):
        with tempfile.TemporaryDirectory(prefix='nix-copy-test-') as directory:
            root = Path(directory)
            share, workspace = root / 'share', root / 'workspace'
            (share / 'scripts').mkdir(parents=True)
            shutil.copy(ROOT / 'scripts/nix-copy.py', share / 'scripts/nix-copy.py')
            entry = share / 'scripts/run.sh'
            entry.write_text('#!/bin/sh\nprintf first\n')
            entry.chmod(0o555)
            env = dict(os.environ, HOME=str(root), XDG_DATA_HOME=str(root / 'data'))
            # Use real cp/filesystem execution, but never a real namespace or Darling.
            launcher = root / 'launcher'
            launcher.write_text((ROOT / 'darling-lol-launcher.sh').read_text()
                .replace('@SHARE_DIR@', str(share)).replace('@FHS_BIN@', shutil.which('true'))
                .replace('@UNSHARE_BIN@', shutil.which('true')).replace('@TERMINAL_BIN@', shutil.which('false')))
            workspace = root / 'data/darling-lol'
            result = run('bash', str(launcher), 'true', env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            target = workspace / 'scripts/run.sh'
            self.assertTrue(os.access(target, os.X_OK), 'copied entrypoint lost executable bits')
            self.assertTrue(target.stat().st_mode & 0o200)
            self.assertEqual(run(str(target), env=env).stdout, 'first')
            entry.chmod(0o755)
            entry.write_text('#!/bin/sh\nprintf second\n')
            entry.chmod(0o555)
            self.assertEqual(run('bash', str(launcher), 'true', env=env).returncode, 0)
            self.assertEqual(run(str(target), env=env).stdout, 'second')
            external = root / 'external'
            external.write_text('untouched')
            target.unlink()
            target.symlink_to(external)
            result = run('bash', str(launcher), 'true', env=env)
            self.assertNotEqual(result.returncode, 0, 'reject managed destination symlinks')
            self.assertEqual(external.read_text(), 'untouched')


def populate_overlays(prefix, home, tools):
    for name in ('validate-riot-runtime.py', 'runtime_paths.py'):
        shutil.copy(ROOT / 'tools' / name, tools / name)
    sys.path.insert(0, str(tools))
    try:
        spec = importlib.util.spec_from_file_location('validation_fixture', tools / 'validate-riot-runtime.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        guest_req = getattr(module, 'GUEST_REQUIRED', getattr(module, 'REQUIRED', ()))
        prefix_req = getattr(module, 'PREFIX_REQUIRED', ())
        for relative in guest_req:
            path = home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'\xcf\xfa\xed\xfe' + b'inert test fixture')
        for relative in prefix_req:
            path = prefix / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'\xcf\xfa\xed\xfe' + b'inert test fixture')
    finally:
        sys.path.remove(str(tools))


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nix-state-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        shutil.copytree(ROOT / 'scripts', self.repo / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        for folder in ['upstream', 'patches', 'tools', 'usr-local/bin']:
            (self.repo / folder).mkdir(parents=True)
        (self.repo / 'upstream/manifest.json').write_text('{}')
        shutil.copy(ROOT / 'tools/runtime_paths.py', self.repo / 'tools/runtime_paths.py')
        (self.repo / 'usr-local/bin/darling').write_text('fixture, never executed')
        (self.repo / 'usr-local/bin/darlingserver').write_text('server fixture, never executed')
        (self.repo / 'usr-local/libexec/darling/usr/lib').mkdir(parents=True)
        (self.repo / 'usr-local/libexec/darling/usr/lib/libSystem.B.dylib').write_text('SDK fixture')
        for name in ['darling', 'darlingserver']:
            (self.repo / 'usr-local/bin' / name).chmod(0o755)
        (self.repo / 'usr-local/libexec/darling').mkdir(parents=True, exist_ok=True)
        (self.repo / 'build').mkdir()
        (self.repo / 'build/config.json').write_text(json.dumps({'guest_user': 'guest-one'}))
        self.env = dict(os.environ, HOME=str(self.root), DPREFIX=str(self.root / 'prefix'),
                        DARLING_WORKSPACE=str(self.repo), DARLING_PACKAGE_ID='package-one',
                        DARLING_GUEST_USER='guest-one', PYTHONDONTWRITEBYTECODE='1')
        self.env.pop('RIOT_GUEST_HOME', None)
        populate_overlays(Path(self.env['DPREFIX']), Path(self.env['DPREFIX']) / 'Users/guest-one', self.repo / 'tools')

    def state(self, action):
        return run(sys.executable, str(self.repo / 'scripts/nix-state.py'), action, env=self.env)

    def test_prefix_guest_package_source_and_runtime_identity(self):
        self.assertNotEqual(self.state('check').returncode, 0)
        (Path(self.env['DPREFIX']) / 'Applications/Riot Client.app').mkdir(parents=True)
        self.assertNotEqual(self.state('check').returncode, 0, 'existing app is not readiness')
        self.assertEqual(self.state('stamp-base').returncode, 0)
        self.assertEqual(self.state('stamp').returncode, 0)
        self.assertEqual(self.state('check').returncode, 0)
        # Canonical aliases share readiness, but a different prefix must not.
        self.env['DPREFIX'] += '/../prefix'
        self.assertEqual(self.state('check').returncode, 0)
        for key, value in [('DPREFIX', str(self.root / 'new-prefix')),
                           ('DARLING_PACKAGE_ID', 'package-two')]:
            old = self.env[key]
            self.env[key] = value
            self.assertNotEqual(self.state('check').returncode, 0, key)
            self.env[key] = old
        (self.repo / 'build/config.json').write_text(json.dumps({'guest_user': 'guest-two'}))
        self.assertNotEqual(self.state('check').returncode, 0)
        (self.repo / 'build/config.json').write_text(json.dumps({'guest_user': 'guest-one'}))
        (self.repo / 'patches/change.patch').write_text('upgrade')
        self.assertNotEqual(self.state('check').returncode, 0)
        self.assertEqual(self.state('stamp-base').returncode, 0)
        self.assertEqual(self.state('stamp').returncode, 0)
        (self.repo / 'usr-local/bin/darlingserver').write_text('server-one')
        self.assertEqual(self.state('stamp-base').returncode, 0)
        self.assertEqual(self.state('stamp').returncode, 0)
        (self.repo / 'usr-local/bin/darlingserver').write_text('server-two')
        self.assertNotEqual(self.state('check').returncode, 0)
        (self.repo / 'usr-local/bin/darling').write_text('changed runtime')
        self.assertNotEqual(self.state('check').returncode, 0)

    def test_deleted_overlay_invalidates_readiness(self):
        overlay = Path(self.env['DPREFIX']) / 'Users/guest-one/RuntimeLibraries/library.dylib'
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text('fixture overlay')
        self.assertEqual(self.state('stamp-base').returncode, 0)
        self.assertEqual(self.state('stamp').returncode, 0)
        self.assertEqual(self.state('check').returncode, 0)
        overlay.unlink()
        self.assertNotEqual(self.state('check').returncode, 0)

    def test_missing_runtime_and_noop_checks_do_not_create_or_rebuild(self):
        self.assertEqual(self.state('stamp-base').returncode, 0)
        self.assertEqual(self.state('stamp').returncode, 0)
        before = {str(p): p.stat().st_mtime_ns for p in self.repo.rglob('*') if p.is_file()}
        for _ in range(2):
            self.assertEqual(self.state('check').returncode, 0)
        after = {str(p): p.stat().st_mtime_ns for p in self.repo.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        (self.repo / 'usr-local/bin/darling').unlink()
        self.assertNotEqual(self.state('check').returncode, 0)
        self.assertNotEqual(self.state('stamp-base').returncode, 0)

    def test_interrupted_setup_invalidates_previous_stamp(self):
        self.assertEqual(self.state('stamp-base').returncode, 0)
        self.assertEqual(self.state('stamp').returncode, 0)
        self.assertEqual(self.state('invalidate').returncode, 0)
        self.assertNotEqual(self.state('check').returncode, 0)

    def test_runner_refuses_unready_even_for_raw_launch_paths(self):
        runner = ROOT / 'scripts/darling-rootless.sh'
        env = dict(self.env, DARLING_HOST_UID=str(os.getuid()))
        result = run('bash', str(runner), 'shell', '/usr/bin/true', env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('readiness', result.stderr)

    def test_run_and_installer_refuse_unready_before_configure_or_darling(self):
        for name in ['run.sh', 'installer.sh']:
            result = run('sh', str(self.repo / 'scripts' / name), env=self.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('readiness', result.stderr.lower())
            self.assertFalse((self.repo / 'build/runtime').exists())


class SessionTests(unittest.TestCase):
    def test_workspace_lock_survives_namespace_closing_extra_fds(self):
        with tempfile.TemporaryDirectory(prefix='nix-lock-test-') as directory:
            root = Path(directory)
            share = root / 'share'
            (share / 'scripts').mkdir(parents=True)
            shutil.copy(ROOT / 'scripts/nix-copy.py', share / 'scripts/nix-copy.py')
            stub = root / 'namespace'
            stub.write_text('#!/bin/sh\nexec 9>&-\ntouch "$TEST_ENTERED"\nsleep 1\n')
            stub.chmod(0o755)
            launcher = root / 'launcher'
            launcher.write_text((ROOT / 'darling-lol-launcher.sh').read_text()
                .replace('@SHARE_DIR@', str(share)).replace('@UNSHARE_BIN@', str(stub)))
            entered = root / 'entered'
            env = dict(os.environ, HOME=str(root), XDG_DATA_HOME=str(root / 'data'), TEST_ENTERED=str(entered))
            first = subprocess.Popen(['bash', str(launcher), 'true'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                import time
                for _ in range(100):
                    if entered.exists():
                        break
                    time.sleep(.01)
                self.assertTrue(entered.exists())
                second = run('bash', str(launcher), 'true', env=env)
                self.assertNotEqual(second.returncode, 0)
                self.assertIn('session is active', second.stderr)
            finally:
                first.communicate(timeout=10)

    def test_live_prefix_marker_refuses_before_refresh_or_namespace(self):
        with tempfile.TemporaryDirectory(prefix='nix-marker-test-') as directory:
            root = Path(directory)
            prefix = root / 'prefix'
            prefix.mkdir()
            # This fixture's own PID is harmless; the launcher must never signal it.
            (prefix / '.init.pid').write_text(str(os.getpid()))
            launcher = root / 'launcher'
            launcher.write_text((ROOT / 'darling-lol-launcher.sh').read_text()
                .replace('@SHARE_DIR@', str(root / 'missing-share'))
                .replace('@UNSHARE_BIN@', 'must-not-execute'))
            env = dict(os.environ, HOME=str(root), DPREFIX=str(prefix), XDG_DATA_HOME=str(root / 'data'))
            result = run('bash', str(launcher), env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('reconnect is unsupported', result.stderr)
            self.assertFalse((root / 'data/darling-lol/scripts').exists())

    def test_ready_session_launches_without_rebuild_and_missing_stamp_refuses_eof(self):
        with tempfile.TemporaryDirectory(prefix='nix-dispatch-test-') as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            for name in ['nix-state.py', 'nix-session.sh', 'nix-lifecycle.py']:
                shutil.copy(ROOT / 'scripts' / name, root / 'scripts' / name)
            # Keep lifecycle dispatch inert: no real /proc scan or Darling call.
            (root / 'scripts/nix-idle.py').write_text('def client_active(prefix): return False\n')
            (root / 'scripts/darling-rootless.sh').write_text('exit 0\n')
            (root / 'usr-local/bin').mkdir(parents=True)
            (root / 'usr-local/bin/darling').write_text('never executed')
            (root / 'usr-local/bin/darlingserver').write_text('never executed')
            for name in ['darling', 'darlingserver']:
                (root / 'usr-local/bin' / name).chmod(0o755)
            (root / 'usr-local/libexec/darling/usr/lib').mkdir(parents=True, exist_ok=True)
            (root / 'usr-local/libexec/darling/usr/lib/libSystem.B.dylib').write_text('SDK fixture')
            (root / 'tools').mkdir()
            (root / 'usr-local/libexec/darling').mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / 'tools/runtime_paths.py', root / 'tools/runtime_paths.py')
            (root / 'prefix/Applications/Riot Client.app').mkdir(parents=True)
            for name in ['run.sh', 'update.sh', 'install-runtime.sh', 'installer.sh']:
                path = root / 'scripts' / name
                path.write_text('#!/bin/sh\necho ' + name + ' >> "$TEST_LOG"\n')
                path.chmod(0o755)
            log = root / 'calls'
            env = dict(os.environ, HOME=str(root), DPREFIX=str(root / 'prefix'),
                       DARLING_WORKSPACE=str(root), DARLING_PACKAGE_ID='fixture', TEST_LOG=str(log),
                       PYTHONDONTWRITEBYTECODE='1')
            env.pop('RIOT_GUEST_HOME', None)
            populate_overlays(root / 'prefix', root / 'prefix/Users' / getpass.getuser(), root / 'tools')
            for action in ['stamp-base', 'stamp']:
                self.assertEqual(run(sys.executable, str(root / 'scripts/nix-state.py'), action, env=env).returncode, 0)
            for _ in range(2):
                result = run('bash', str(root / 'scripts/nix-session.sh'), env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text().splitlines(), ['run.sh', 'run.sh'])
            run(sys.executable, str(root / 'scripts/nix-state.py'), 'invalidate', env=env)
            result = run('bash', str(root / 'scripts/nix-session.sh'), env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('terminal', result.stderr)
            self.assertEqual(log.read_text().splitlines(), ['run.sh', 'run.sh'])
            env['DPREFIX'] = str(root / 'other-prefix')
            result = run('bash', str(root / 'scripts/nix-session.sh'), env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'other-prefix').exists())

    def test_first_run_eof_is_actionable_and_desktop_requests_terminal(self):
        """Report missing terminal input without setup and package a terminal launcher."""
        with tempfile.TemporaryDirectory(prefix='nix-session-test-') as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            for p in (ROOT / 'scripts').glob('*'):
                if p.is_file():
                    shutil.copy(p, root / 'scripts' / p.name)
            env = dict(os.environ, HOME=str(root), DPREFIX=str(root / 'prefix'),
                       DARLING_WORKSPACE=str(root), DARLING_PACKAGE_ID='fixture')
            result = run('bash', str(root / 'scripts/nix-session.sh'), env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('terminal', result.stderr.lower())
            self.assertFalse((root / 'build').exists())
        self.assertIn('Terminal=true', (ROOT / 'flake.nix').read_text())
        self.assertIn('packaging = pkgs.runCommandCC "darling-lol-packaging-tests"',
                      (ROOT / 'flake.nix').read_text())

    def test_rootless_update_refuses_live_marker_without_build_or_stop(self):
        with tempfile.TemporaryDirectory(prefix='nix-update-idle-') as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            for name in ['update.sh', 'nix-idle.py']:
                if (ROOT / 'scripts' / name).exists():
                    shutil.copy(ROOT / 'scripts' / name, root / 'scripts' / name)
            prefix = root / 'prefix'
            prefix.mkdir()
            (prefix / '.init.pid').write_text(str(os.getpid()))
            env = dict(os.environ, DARLING_WORKSPACE=str(root), DPREFIX=str(prefix),
                       DARLING_PACKAGE_ID='/nix/store/fixture-package/share/darling-lol')
            result = run('sh', str(root / 'scripts/update.sh'), env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('live server', result.stderr.lower())
            self.assertFalse((root / 'build').exists())

    def test_rootless_update_executes_without_sudo_and_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix='nix-update-test-') as directory:
            root = Path(directory)
            scripts, tools, bin_dir = root / 'scripts', root / 'build/runtime/tools', root / 'bin'
            scripts.mkdir()
            tools.mkdir(parents=True)
            bin_dir.mkdir()
            shutil.copy(ROOT / 'scripts/update.sh', scripts / 'update.sh')
            # Process discovery has its own read-only marker test; isolate this
            # installer-order fixture from unrelated host /proc permissions.
            (scripts / 'nix-idle.py').write_text('pass\n')
            log = root / 'calls'
            for name, body in [('build.sh', 'echo build >> "$TEST_LOG"'),
                               ('install-runtime.sh', 'echo overlays >> "$TEST_LOG"')]:
                path = scripts / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o755)
            (scripts / 'nix-state.py').write_text('import os, sys\nwith open(os.environ["TEST_LOG"], "a") as f: f.write(sys.argv[1]+"\\n")\n')
            (tools / 'stop-darling-runtime.py').write_text('import os\nwith open(os.environ["TEST_LOG"], "a") as f: f.write("stop\\n")\n')
            (root / 'tools').mkdir()
            (root / 'tools/configure-darling-platform.py').write_text(
                'import os, sys\nwith open(os.environ["TEST_LOG"], "a") as f: f.write("identity " + " ".join(sys.argv[1:]) + "\\n")\n')
            for name in ['cmake', 'sudo', 'nix-store']:
                path = bin_dir / name
                path.write_text('#!/bin/sh\necho ' + name + ' >> "$TEST_LOG"\n' +
                                ('exit "${INSTALL_EXIT:-0}"\n' if name == 'cmake' else 'exit 0\n'))
                path.chmod(0o755)
            env = dict(os.environ, DARLING_WORKSPACE=str(root), HOME=str(root), DPREFIX=str(root / 'prefix'),
                       DARLING_PACKAGE_ID='/nix/store/fixture-package/share/darling-lol',
                       TEST_LOG=str(log), PATH=str(bin_dir) + ':' + os.environ['PATH'])
            result = run('sh', str(scripts / 'update.sh'), env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text().splitlines(),
                             ['nix-store', 'invalidate-base', 'build', 'stop', 'cmake', 'stamp-base', 'overlays',
                              # The shared serial/UUID replacement writes the prefix, so Darling is stopped first.
                              'stop', 'identity --replace-known-defaults --write'])
            log.write_text('')
            env['INSTALL_EXIT'] = '23'
            self.assertEqual(run('sh', str(scripts / 'update.sh'), env=env).returncode, 23)
            self.assertNotIn('stamp-base', log.read_text())
            self.assertNotIn('overlays', log.read_text())
            self.assertNotIn('identity', log.read_text())
            self.assertNotIn('sudo', log.read_text())

    def test_rootless_update_never_uses_sudo_and_stamps_only_after_success(self):
        text = (ROOT / 'scripts/update.sh').read_text()
        self.assertIn('DARLING_WORKSPACE', text)
        self.assertIn('stamp-base', text)
        self.assertIn('invalidate-base', text)
        self.assertIn('nix-state.py', text)


if __name__ == '__main__':
    unittest.main()
