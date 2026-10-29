"""Failed Git inspections must stop source preparation."""
import ast
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


def load_prepare(*names):
    """Execute selected top-level definitions from prepare.py, without its CLI."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'scripts/prepare.py').read_text())
    body = [node for node in tree.body
            if (isinstance(node, ast.FunctionDef) and node.name in names)
            or (isinstance(node, ast.Assign)
                and any(getattr(target, 'id', '') == 'LFS_POINTER' for target in node.targets))]
    namespace = {'subprocess': subprocess, 'Path': Path}
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


if __name__ == '__main__':
    unittest.main()
