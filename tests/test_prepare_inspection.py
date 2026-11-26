"""Failed Git inspections must stop source preparation."""
import ast
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from itertools import islice
from unittest.mock import patch


def load_prepare(*names, **extra):
    """Execute selected top-level definitions from prepare.py, without its CLI."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'scripts/prepare.py').read_text())
    names = (*names, 'git_environment', 'git_run', 'git_output')
    body = [node for node in tree.body
            if (isinstance(node, ast.FunctionDef) and node.name in names)
            or (isinstance(node, ast.Assign)
                and any(getattr(target, 'id', '') == 'LFS_POINTER' for target in node.targets))]
    namespace = {'subprocess': subprocess, 'Path': Path, 'os': os, **extra}
    exec(compile(ast.Module(body=body, type_ignores=[]), 'prepare.py', 'exec'), namespace)
    return namespace


@unittest.skipUnless(shutil.which('git'), 'Git required')
class PreparationInspectionTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_prepare('submodules', 'pointers', 'repair')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_failed_submodule_inspection_is_not_an_empty_list(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.namespace['submodules'](self.root)

    def test_failed_lfs_inspection_is_not_an_empty_list(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.namespace['pointers'](self.root)

    def test_failed_status_inspection_stops_repair(self):
        child = self.root / 'child'
        child.mkdir()
        self.namespace.update(submodules=lambda _root: [child], pointers=lambda _repo: [])
        with self.assertRaises(subprocess.CalledProcessError):
            self.namespace['repair'](self.root)


@unittest.skipUnless(shutil.which('git') and shutil.which('git-lfs'),
                     'Git and git-lfs required')
class LfsPointerContentTests(unittest.TestCase):
    def setUp(self):
        self.pointers = load_prepare('pointers')['pointers']
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'master')
        self.git('lfs', 'install', '--local')
        self.git('lfs', 'track', '*.bin')

    def git(self, *args, check=True):
        return subprocess.run(['git', '-C', str(self.repo), *args], text=True,
                              capture_output=True, check=check).stdout.strip()

    def commit(self, content):
        (self.repo / 'asset.bin').write_bytes(content)
        self.git('add', '.gitattributes', 'asset.bin')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-m', 'asset')

    def test_pointer_text_is_detected_when_lfs_reports_the_object_present(self):
        # git lfs ls-files trusts the worktree size: when pointer text and object
        # have the same length it prints * even though the worktree is a pointer.
        payload = b'a' * 128
        oid = hashlib.sha256(payload).hexdigest()
        pointer = ('version https://git-lfs.github.com/spec/v1\n'
                   'oid sha256:%s\nsize %d\n' % (oid, len(payload))).encode()
        self.assertEqual(len(pointer), len(payload))
        self.commit(payload)
        (self.repo / 'asset.bin').write_bytes(pointer)
        self.assertIn('*', self.git('lfs', 'ls-files'))
        self.assertEqual(self.pointers(self.repo), ['asset.bin'])

    def test_real_content_is_not_reported_as_a_pointer(self):
        self.commit(b'\x7fELF not a pointer\n' + b'x' * 2000)
        self.assertEqual(self.pointers(self.repo), [])

    def test_missing_tracked_file_is_not_reported_as_a_pointer(self):
        self.assertEqual(self.pointers(self.repo), [])


class PatchJobsTests(unittest.TestCase):
    def setUp(self):
        self.jobs = load_prepare('patch_jobs')['patch_jobs']

    def test_jobs_are_positive_and_bounded(self):
        self.assertEqual(self.jobs(4, cpu=14), 4)
        self.assertEqual(self.jobs(64, cpu=14), 8)
        self.assertEqual(self.jobs(4, cpu=1), 1)

    def test_invalid_and_nonpositive_jobs_fall_back_to_one(self):
        for value in (0, -2, 'bad'):
            self.assertEqual(self.jobs(value, cpu=14), 1)


class CheckOnlyStateTests(unittest.TestCase):
    def test_only_record_patch_creates_the_recording_folder(self):
        # --check-only must work on a read-only build/: the folder is created
        # only when a patch is recorded, never by the top-level flow.
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'scripts/prepare.py').read_text())
        owners = set()
        for node in tree.body:
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == 'mkdir'
                        and getattr(call.func.value, 'id', '') == 'applied'):
                    owners.add(getattr(node, 'name', type(node).__name__))
        self.assertEqual(owners, {'record_patch'})


@unittest.skipUnless(shutil.which('git'), 'Git required')
class HistoricalPatchesTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                             if not k.startswith('GIT_')}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'commit.gpgsign', 'false')
        self.patch = self.repo / 'patches/component.patch'
        self.patch.parent.mkdir()
        self.versions = []
        for body in ('@X@ one\n', '@X@ two\n', '@X@ three\n'):
            self.patch.write_text(body)
            self.git('add', '.')
            self.git('commit', '-qm', body.strip())
            self.versions.append(body)
        self.applied = self.repo / 'build/applied-patches'
        self.namespace = load_prepare(
            'history_texts', 'previous_patches', ROOT=self.repo, applied=self.applied,
            render=lambda text: text.replace('@X@', 'x'))

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def test_versions_are_returned_newest_first(self):
        found = list(self.namespace['history_texts']('patches/component.patch'))
        self.assertEqual(found, list(reversed(self.versions)))

    def test_a_patch_without_history_returns_nothing(self):
        self.assertEqual(list(self.namespace['history_texts']('patches/other.patch')), [])

    def test_the_recorded_copy_comes_first_and_history_is_rendered(self):
        self.applied.mkdir(parents=True)
        (self.applied / 'component.patch').write_text('recorded\n')
        found = list(self.namespace['previous_patches'](
            {'path': 'src/component', 'patch': 'patches/component.patch'}))
        self.assertEqual(found, ['recorded\n', *reversed(
            [body.replace('@X@', 'x') for body in self.versions])])

    def test_stopping_early_closes_the_history_process(self):
        # prepare_patch stops at the first replacement; the open cat-file
        # process must not leak or block the worker.
        found = list(islice(self.namespace['previous_patches'](
            {'path': 'src/component', 'patch': 'patches/component.patch'}), 1))
        self.assertEqual(found, ['x three\n'])


if __name__ == '__main__':
    unittest.main()
