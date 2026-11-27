"""Exercise the runtime overlay stamp that lets an update skip the rebuild."""
import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import git_environment  # noqa: E402

spec = importlib.util.spec_from_file_location(
    'runtime_overlay_stamp', ROOT / 'tools/runtime-overlay-stamp.py')
stamp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stamp)


def git(root, *arguments):
    subprocess.run(['git', '-C', str(root), *arguments], check=True,
                   capture_output=True)


def build_fixture(root):
    """A repository root with a patched checkout, cmake tree and prefix."""
    (root / 'scripts').mkdir(parents=True)
    (root / 'scripts/install-runtime.sh').write_text('#!/bin/sh\n')
    tools = root / 'tools'
    tools.mkdir()
    (tools / 'riot_build_cache.py').write_text('# support module\n')
    (tools / 'build-riot-demo.py').write_text('print("demo")\n')
    config = root / 'build/config.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'prefix': str(root / 'prefix'),
                                  'guest_home': '/Users/tester'}) + '\n')

    base = root / 'build/darling-src'
    base.mkdir()
    git(base, 'init', '-q')
    (base / 'source.c').write_text('int main(void) { return 0; }\n')
    git(base, 'add', 'source.c')
    git(base, '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
        'commit', '-q', '-m', 'fixture')

    build = base / 'build'
    for directory in stamp.TARGET_DIRS:
        folder = build / directory / 'CMakeFiles/Demo.dir'
        folder.mkdir(parents=True)
        (folder / 'flags.make').write_text('C_FLAGS = -O2\n')
        (folder / 'link.txt').write_text('clang -o demo\n')
    for artifact in stamp.ARTIFACTS:
        (build / artifact).parent.mkdir(parents=True, exist_ok=True)
        (build / artifact).write_bytes(b'artifact:' + artifact.encode())

    prefix = root / 'prefix'
    guest = prefix / 'Users/tester'
    (guest / 'RuntimeLibraries').mkdir(parents=True)
    (guest / 'RuntimeLibraries/libsystem_c.dylib').write_bytes(b'installed')
    (guest / 'RuntimeFrameworks/AppKit.framework').mkdir(parents=True)
    (guest / 'RuntimeFrameworks/AppKit.framework/AppKit').write_bytes(b'installed')
    (prefix / 'usr/lib').mkdir(parents=True)
    (prefix / 'usr/lib/libGL.dylib').write_bytes(b'installed')
    (prefix / 'System/Library/Frameworks').mkdir(parents=True)
    return root


class OverlayStampTests(unittest.TestCase):
    def setUp(self):
        # A hook can point Git at its own repository; keep the fixture here.
        git_environment.isolate(self)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        build_fixture(self.root)
        self.state = self.root / 'build/runtime/overlay-stamp.json'
        # The installers resolve DPREFIX/RIOT_GUEST_HOME from the environment too.
        self.env = patch.dict(os.environ, {'DPREFIX': '', 'RIOT_GUEST_HOME': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def record(self):
        self.assertEqual(0, stamp.record(self.state, stamp.collect(self.root)))

    def check(self):
        return stamp.check(self.state, stamp.collect(self.root))

    def test_recorded_state_allows_the_fast_path(self):
        self.record()
        self.assertEqual(0, self.check())
        self.assertEqual(stamp.VERSION, json.loads(self.state.read_text())['version'])

    def test_missing_or_corrupt_state_forces_a_rebuild(self):
        self.assertEqual(1, self.check())
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text('{not json')
        self.assertEqual(1, self.check())
        self.state.write_text(json.dumps({'version': stamp.VERSION - 1, 'sections': {}}))
        self.assertEqual(1, self.check())

    def test_changed_overlay_inputs_force_a_rebuild(self):
        changes = (
            ('tools', self.root / 'tools/build-riot-demo.py'),
            ('flags', self.root / 'build/darling-src/build/src/external/libplatform/'
                                  'CMakeFiles/Demo.dir/flags.make'),
            ('artifacts', self.root / 'build/darling-src/build/src/external/libplatform/'
                                     'libsystem_platform.dylib'),
            ('config', self.root / 'build/config.json'),
            ('tree', self.root / 'build/darling-src/source.c'),
            ('overlays', self.root / 'prefix/usr/lib/libGL.dylib'),
        )
        for section, path in changes:
            with self.subTest(section=section):
                self.record()
                self.assertEqual(0, self.check())
                original = path.read_bytes()
                path.write_bytes(original + b'\nchanged')
                self.assertEqual(1, self.check())
                path.write_bytes(original)
                self.record()

    def test_deleted_overlay_forces_a_rebuild(self):
        self.record()
        (self.root / 'prefix/Users/tester/RuntimeLibraries/libsystem_c.dylib').unlink()
        self.assertEqual(1, self.check())

    def test_patch_added_untracked_files_are_part_of_the_tree(self):
        # Patches create files with `new file mode`, so `git diff HEAD` alone
        # does not see them; editing one must still invalidate the stamp.
        self.record()
        added = self.root / 'build/darling-src/src/added-header.h'
        added.parent.mkdir(parents=True)
        added.write_text('#define VALUE 1\n')
        self.assertEqual(1, self.check())
        self.record()
        self.assertEqual(0, self.check())
        added.write_text('#define VALUE 2\n')
        self.assertEqual(1, self.check())

    def test_missing_artifact_cannot_be_recorded(self):
        (self.root / 'build/darling-src/build/src/native/libGL.dylib').unlink()
        self.assertEqual(1, stamp.record(self.state, stamp.collect(self.root)))
        self.assertFalse(self.state.exists())

    def test_environment_prefix_is_part_of_the_stamp(self):
        self.record()
        with patch.dict(os.environ, {'DPREFIX': str(self.root / 'other-prefix')}):
            self.assertEqual(1, self.check())

    def test_capabilities_are_hashed(self):
        self.assertRegex(stamp.capabilities_digest(), '^[0-9a-f]{64}$')
        with patch.object(stamp, 'CAPABILITIES',
                          (('missing', ('definitely-not-a-command',)),)):
            self.assertRegex(stamp.capabilities_digest(), '^[0-9a-f]{64}$')

    def test_cli_checks_the_same_state(self):
        self.assertEqual(0, subprocess.run(
            [sys.executable, str(ROOT / 'tools/runtime-overlay-stamp.py'),
             '--root', str(self.root), '--state', str(self.state), 'record'],
            capture_output=True).returncode)
        self.assertEqual(0, subprocess.run(
            [sys.executable, str(ROOT / 'tools/runtime-overlay-stamp.py'),
             '--root', str(self.root), '--state', str(self.state), 'check'],
            capture_output=True).returncode)

    def test_non_builder_tools_do_not_invalidate_the_stamp(self):
        non_builders = (
            'tools/run-riot-client.sh',
            'tools/session-supervisor.py',
            'tools/doctor.py',
            'tools/runtime-sign-stamp.py',
            'tools/darling-build-stamp.py',
            'tools/build-riot-air-check.py',
            'tools/check-appkit-idle-wait.py',
        )
        for rel in non_builders:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# initial\n')
        self.record()
        for rel in non_builders:
            (self.root / rel).write_text('# edited\n')
            self.assertEqual(0, self.check(), rel)

    def test_validation_and_sync_tools_invalidate_the_stamp(self):
        for rel in ('tools/validate-riot-runtime.py',
                    'tools/check-darwin-tlv-calls.py',
                    'tools/sync-canonical-runtime-overlays.py'):
            with self.subTest(tool=rel):
                path = self.root / rel
                path.write_text('# initial\n')
                self.record()
                path.write_text('# edited\n')
                self.assertEqual(1, self.check())

    def test_every_overlay_validation_and_sync_dependency_is_covered(self):
        # Trace all tools/* files reachable from install_overlay_batch,
        # validate-riot-runtime.py, and sync-canonical-runtime-overlays.py.
        tools_dir = ROOT / 'tools'
        seeds = [
            'install-riot-presentation-runtime.sh',
            'build-darling-wayland.sh',
            'install-riot-gpu-runtime.sh',
            'install-riot-carboncore-runtime.sh',
            'build-riot-libs-runtime.sh',
            'install-riot-libs-runtime.py',
            'install-darling-system-profiler.py',
            'build-riot-electron43-frameworks.py',
            'run-parallel.sh',
            'clang-cache.py',
            'validate-riot-runtime.py',
            'check-darwin-tlv-calls.py',
            'sync-canonical-runtime-overlays.py',
        ]
        seen = set()
        queue = list(seeds)

        def enqueue(rel):
            rel = rel.strip('/')
            if rel and rel not in ('.', '..') and rel not in seen:
                queue.append(rel)

        while queue:
            name = queue.pop(0)
            if name in seen:
                continue
            path = tools_dir / name
            if not path.exists():
                continue
            if path.is_dir():
                seen.add(name)
                for sub in sorted(path.rglob('*')):
                    if sub.is_file():
                        enqueue(sub.relative_to(tools_dir).as_posix())
                continue
            seen.add(name)
            text = path.read_text(errors='replace')
            if path.suffix == '.sh':
                code = '\n'.join(
                    line for line in text.splitlines()
                    if not line.lstrip().startswith('#')
                )
                for ref in re.findall(r'(?:tools/)([A-Za-z0-9_./-]+)', code):
                    ref = ref.rstrip(".,;:)'\"")
                    if (tools_dir / ref).exists():
                        enqueue(ref)
            elif path.suffix == '.py':
                tree = ast.parse(text)
                docstrings = set()
                for scope in ast.walk(tree):
                    body = getattr(scope, 'body', None)
                    if isinstance(body, list) and body:
                        first = body[0]
                        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                            docstrings.add(first.value)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if (tools_dir / (alias.name + '.py')).exists():
                                enqueue(alias.name + '.py')
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        if (tools_dir / (node.module + '.py')).exists():
                            enqueue(node.module + '.py')
                    elif (isinstance(node, ast.Constant)
                          and isinstance(node.value, str)
                          and node not in docstrings):
                        val = node.value
                        for ref in re.findall(
                            r'(?:tools/)?([A-Za-z0-9_./-]+\.(?:c|cpp|mm|m|h|s|sh|py))',
                            val,
                        ):
                            if (tools_dir / ref).exists():
                                enqueue(ref)
                        if val.startswith('tools/') and (tools_dir / val[6:]).exists():
                            enqueue(val[6:])
                        elif val and '/' not in val and val not in ('.', '..') and (tools_dir / val).exists():
                            enqueue(val)

        for rel in sorted(seen):
            candidate = tools_dir / rel
            if candidate.is_file():
                self.assertFalse(
                    stamp._tool_output(candidate, ROOT),
                    f'{rel} is reachable from overlay batch/validation/sync but excluded from tools_digest',
                )


if __name__ == '__main__':
    unittest.main()
