"""Prebuilt runtime archives: deterministic export, hashed and contained install."""
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'runtime_package', ROOT / 'tools/runtime-package.py')
package_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_tool)


def make_home(root):
    libraries = root / 'RuntimeLibraries'
    libraries.mkdir(parents=True)
    (libraries / 'libsystem_kernel.dylib').write_bytes(b'kernel-bytes')
    (libraries / 'libGL.dylib').write_bytes(b'gl-bytes')
    os.chmod(libraries / 'libGL.dylib', 0o755)
    framework = root / 'RuntimeFrameworks/Foo.framework/Versions/A'
    framework.mkdir(parents=True)
    (framework / 'Foo').write_bytes(b'framework-bytes')
    os.symlink('A', root / 'RuntimeFrameworks/Foo.framework/Versions/Current')
    os.symlink('Versions/Current/Foo',
               root / 'RuntimeFrameworks/Foo.framework/Foo')
    return root


def write_package(path, files, links, manifest=None):
    """A hand-built archive, so a test can craft a hostile or corrupt package."""
    if manifest is None:
        manifest = {
            'schema': package_tool.SCHEMA,
            'label': 'test',
            'roots': list(package_tool.ROOTS),
            'files': [{'path': name, 'size': len(data),
                       'sha256': hashlib.sha256(data).hexdigest(), 'mode': 0o644}
                      for name, data in files.items()],
            'symlinks': [{'path': name, 'target': target}
                         for name, target in links.items()],
        }
    with open(path, 'wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode='w', format=tarfile.PAX_FORMAT) as archive:
                package_tool.add_bytes(
                    archive, package_tool.MANIFEST,
                    (json.dumps(manifest, sort_keys=True) + '\n').encode())
                for name, data in files.items():
                    package_tool.add_bytes(archive, name, data)
                for name, target in links.items():
                    info = tarfile.TarInfo(name)
                    info.type = tarfile.SYMTYPE
                    info.linkname = target
                    info.uid = info.gid = 0
                    archive.addfile(info)
    return path


class RuntimePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = make_home(self.root / 'home')
        self.archive = self.root / 'runtime.tar.gz'

    def test_export_and_install_round_trip(self):
        manifest = package_tool.export(self.home, self.archive, 'r142')
        self.assertEqual(manifest['label'], 'r142')
        target = self.root / 'target'
        package_tool.install(self.archive, target, out=lambda _line: None)
        for relative in ('RuntimeLibraries/libsystem_kernel.dylib',
                         'RuntimeLibraries/libGL.dylib',
                         'RuntimeFrameworks/Foo.framework/Versions/A/Foo'):
            self.assertEqual((target / relative).read_bytes(),
                             (self.home / relative).read_bytes())
        self.assertEqual(stat.S_IMODE((target / 'RuntimeLibraries/libGL.dylib').stat().st_mode),
                         0o755)
        self.assertEqual(os.readlink(target / 'RuntimeFrameworks/Foo.framework/Foo'),
                         'Versions/Current/Foo')

    def test_export_is_deterministic(self):
        first = self.root / 'first.tar.gz'
        second = self.root / 'second.tar.gz'
        package_tool.export(self.home, first, 'r142')
        package_tool.export(self.home, second, 'r142')
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_install_replaces_old_overlay_bytes(self):
        target = self.root / 'target'
        (target / 'RuntimeLibraries').mkdir(parents=True)
        (target / 'RuntimeLibraries/libGL.dylib').write_bytes(b'old')
        package_tool.export(self.home, self.archive, 'r142')
        package_tool.install(self.archive, target, out=lambda _line: None)
        self.assertEqual((target / 'RuntimeLibraries/libGL.dylib').read_bytes(),
                         b'gl-bytes')

    def test_checksum_mismatch_installs_nothing(self):
        files = {'RuntimeLibraries/libGL.dylib': b'tampered'}
        manifest = {
            'schema': package_tool.SCHEMA,
            'files': [{'path': 'RuntimeLibraries/libGL.dylib', 'size': 4,
                       'sha256': hashlib.sha256(b'expected').hexdigest(), 'mode': 0o644}],
            'symlinks': [],
        }
        write_package(self.archive, files, {}, manifest)
        target = self.root / 'target'
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, target, out=lambda _line: None)
        self.assertFalse((target / 'RuntimeLibraries/libGL.dylib').exists())
        self.assertEqual(list(target.glob('.runtime-package-*')), [])

    def test_later_checksum_failure_preserves_every_old_file(self):
        files = {'RuntimeLibraries/a': b'new-a', 'RuntimeLibraries/b': b'bad-b'}
        manifest = {'schema': package_tool.SCHEMA, 'files': [
            {'path': name, 'size': len(data), 'mode': 0o644,
             'sha256': hashlib.sha256(data if name.endswith('/a') else b'expected').hexdigest()}
            for name, data in files.items()], 'symlinks': []}
        target = self.root / 'target'
        (target / 'RuntimeLibraries').mkdir(parents=True)
        for name in ('a', 'b'):
            (target / 'RuntimeLibraries' / name).write_bytes(b'old-' + name.encode())
        write_package(self.archive, files, {}, manifest)
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, target)
        for name in ('a', 'b'):
            self.assertEqual((target / 'RuntimeLibraries' / name).read_bytes(), b'old-' + name.encode())

    def test_later_directory_collision_preserves_earlier_file(self):
        target = self.root / 'target'
        (target / 'RuntimeLibraries/b').mkdir(parents=True)
        (target / 'RuntimeLibraries/a').write_bytes(b'old')
        write_package(self.archive, {'RuntimeLibraries/a': b'new', 'RuntimeLibraries/b': b'bad'}, {})
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, target)
        self.assertEqual((target / 'RuntimeLibraries/a').read_bytes(), b'old')

    def test_composed_symlink_escape_is_refused(self):
        # Both targets stay in the root lexically, but a -> b/.. and b -> .
        # resolves a outside RuntimeLibraries after b is followed.
        write_package(self.archive, {}, {'RuntimeLibraries/a': 'b/..', 'RuntimeLibraries/b': '.'})
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, self.root / 'target')

    def test_dry_run_does_not_create_destination(self):
        package_tool.export(self.home, self.archive, 'test')
        target = self.root / 'absent'
        package_tool.install(self.archive, target, dry_run=True, out=lambda _: None)
        self.assertFalse(target.exists())

    def test_package_file_cannot_be_another_entries_parent(self):
        target = self.root / 'target'
        (target / 'RuntimeLibraries').mkdir(parents=True)
        write_package(self.archive, {'RuntimeLibraries/a': b'file',
                                     'RuntimeLibraries/a/b': b'child'}, {})
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, target)
        self.assertFalse((target / 'RuntimeLibraries/a').exists())

    def test_archive_directory_cannot_be_under_a_manifest_file(self):
        target = self.root / 'target'
        (target / 'RuntimeLibraries').mkdir(parents=True)
        good = target / 'RuntimeLibraries/good'
        good.write_bytes(b'old')
        write_package(self.archive, {'RuntimeLibraries/good': b'new',
                                     'RuntimeLibraries/conflict': b'file'}, {})
        # Rewrite the archive with a directory not represented in the manifest.
        with tarfile.open(self.archive, 'r:gz') as source:
            entries = [(member, source.extractfile(member).read() if member.isreg() else None)
                       for member in source.getmembers()]
        with tarfile.open(self.archive, 'w:gz') as archive:
            for member, data in entries:
                archive.addfile(member, io.BytesIO(data) if data is not None else None)
            directory = tarfile.TarInfo('RuntimeLibraries/conflict/child')
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, target)
        self.assertEqual(good.read_bytes(), b'old')
        self.assertFalse((target / 'RuntimeLibraries/conflict').exists())

    def test_duplicate_manifest_path_is_rejected_before_replacement(self):
        target = self.root / 'target'
        (target / 'RuntimeLibraries').mkdir(parents=True)
        good = target / 'RuntimeLibraries/good'
        good.write_bytes(b'old')
        data = b'new'
        entry = {'path': 'RuntimeLibraries/good', 'size': len(data), 'mode': 0o644,
                 'sha256': hashlib.sha256(data).hexdigest()}
        manifest = {'schema': package_tool.SCHEMA, 'files': [entry, dict(entry)], 'symlinks': []}
        write_package(self.archive, {entry['path']: data}, {}, manifest)
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, target)
        self.assertEqual(good.read_bytes(), b'old')

    def test_package_link_cannot_escape_through_preserved_destination_link(self):
        target = self.root / 'target'
        (target / 'RuntimeLibraries').mkdir(parents=True)
        (target / 'secret').write_bytes(b'private')
        os.symlink('..', target / 'RuntimeLibraries/existing')
        write_package(self.archive, {}, {'RuntimeLibraries/new': 'existing/secret'})
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, target)
        self.assertFalse((target / 'RuntimeLibraries/new').is_symlink())
        self.assertEqual((target / 'secret').read_bytes(), b'private')

    def test_package_replacement_does_not_follow_an_old_destination_link(self):
        target = self.root / 'target'
        (target / 'RuntimeLibraries').mkdir(parents=True)
        os.symlink('..', target / 'RuntimeLibraries/existing')
        write_package(self.archive, {'RuntimeLibraries/existing': b'replacement'},
                      {'RuntimeLibraries/new': 'existing'})
        package_tool.install(self.archive, target, out=lambda _: None)
        self.assertEqual((target / 'RuntimeLibraries/new').read_bytes(), b'replacement')
        self.assertFalse((target / 'RuntimeLibraries/existing').is_symlink())

    def test_pax_nul_path_is_rejected_before_replacement(self):
        target = self.root / 'target'
        (target / 'RuntimeLibraries').mkdir(parents=True)
        good = target / 'RuntimeLibraries/good'
        good.write_bytes(b'old')
        files = {'RuntimeLibraries/good': b'new', 'RuntimeLibraries/b\0c': b'bad'}
        manifest = {'schema': package_tool.SCHEMA, 'files': [
            {'path': name, 'size': len(data), 'mode': 0o644,
             'sha256': hashlib.sha256(data).hexdigest()} for name, data in files.items()],
            'symlinks': []}
        with tarfile.open(self.archive, 'w:gz', format=tarfile.PAX_FORMAT) as archive:
            package_tool.add_bytes(archive, package_tool.MANIFEST, json.dumps(manifest).encode())
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.pax_headers = {'path': name}  # PAX preserves embedded NULs.
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, target)
        self.assertEqual(good.read_bytes(), b'old')

    def test_overlong_name_below_missing_directory_is_rejected_before_replacement(self):
        target = self.root / 'target'
        (target / 'RuntimeLibraries').mkdir(parents=True)
        good = target / 'RuntimeLibraries/good'
        good.write_bytes(b'old')
        length = os.pathconf(target, 'PC_NAME_MAX')
        if length < 0:
            self.skipTest('filesystem has no fixed name limit')
        invalid = 'RuntimeLibraries/newdir/' + 'b' * (length + 1)
        write_package(self.archive, {'RuntimeLibraries/good': b'new', invalid: b'bad'}, {})
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, target)
        self.assertEqual(good.read_bytes(), b'old')
        self.assertFalse((target / 'RuntimeLibraries/newdir').exists())

    def test_replacement_cannot_make_a_preserved_link_escape(self):
        target = self.root / 'target'
        libraries = target / 'RuntimeLibraries'
        (libraries / 'sub').mkdir(parents=True)
        (libraries / 'secret').write_bytes(b'inside')
        (target / 'secret').write_bytes(b'outside')
        os.symlink('sub', libraries / 'pivot')
        os.symlink('pivot/../secret', libraries / 'preserved')
        self.assertEqual((libraries / 'preserved').read_bytes(), b'inside')
        write_package(self.archive, {}, {'RuntimeLibraries/pivot': '.'})
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, target)
        self.assertEqual(os.readlink(libraries / 'pivot'), 'sub')
        self.assertEqual((libraries / 'preserved').read_bytes(), b'inside')

    def test_contained_preserved_link_follows_a_valid_package_replacement(self):
        target = self.root / 'target'
        libraries = target / 'RuntimeLibraries'
        (libraries / 'sub').mkdir(parents=True)
        (libraries / 'sub/secret').write_bytes(b'old')
        os.symlink('sub', libraries / 'pivot')
        os.symlink('pivot/secret', libraries / 'preserved')
        write_package(self.archive, {'RuntimeLibraries/new/secret': b'new'},
                      {'RuntimeLibraries/pivot': 'new'})
        package_tool.install(self.archive, target, out=lambda _: None)
        self.assertEqual((libraries / 'preserved').read_bytes(), b'new')

    @unittest.skipIf(os.geteuid() == 0, 'root bypasses directory read permissions')
    def test_export_refuses_unreadable_subtrees_without_publishing_an_archive(self):
        framework = self.home / 'RuntimeFrameworks/Foo.framework'
        framework.chmod(0)
        try:
            with self.assertRaises(package_tool.PackageError):
                package_tool.export(self.home, self.archive, 'test')
        finally:
            framework.chmod(0o755)
        self.assertFalse(self.archive.exists())

    def test_traversal_path_is_refused(self):
        manifest = {
            'schema': package_tool.SCHEMA,
            'files': [{'path': '../escape', 'size': 1,
                       'sha256': hashlib.sha256(b'x').hexdigest(), 'mode': 0o644}],
            'symlinks': [],
        }
        write_package(self.archive, {'../escape': b'x'}, {}, manifest)
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, self.root / 'target', out=lambda _l: None)
        self.assertFalse((self.root / 'escape').exists())

    def test_path_outside_runtime_roots_is_refused(self):
        manifest = {
            'schema': package_tool.SCHEMA,
            'files': [{'path': 'elsewhere/libGL.dylib', 'size': 1,
                       'sha256': hashlib.sha256(b'x').hexdigest(), 'mode': 0o644}],
            'symlinks': [],
        }
        write_package(self.archive, {'elsewhere/libGL.dylib': b'x'}, {}, manifest)
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, self.root / 'target', out=lambda _l: None)

    def test_absolute_symlink_is_refused(self):
        manifest = {
            'schema': package_tool.SCHEMA,
            'files': [],
            'symlinks': [{'path': 'RuntimeLibraries/libGL.dylib', 'target': '/etc/passwd'}],
        }
        write_package(self.archive, {}, {'RuntimeLibraries/libGL.dylib': '/etc/passwd'},
                      manifest)
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, self.root / 'target', out=lambda _l: None)

    def test_symlink_escaping_its_root_is_refused(self):
        manifest = {
            'schema': package_tool.SCHEMA,
            'files': [],
            'symlinks': [{'path': 'RuntimeLibraries/libGL.dylib',
                          'target': '../../../etc/passwd'}],
        }
        write_package(self.archive, {},
                      {'RuntimeLibraries/libGL.dylib': '../../../etc/passwd'}, manifest)
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, self.root / 'target', out=lambda _l: None)

    def test_unexpected_member_is_refused(self):
        manifest = {
            'schema': package_tool.SCHEMA,
            'files': [],
            'symlinks': [],
        }
        write_package(self.archive, {'RuntimeLibraries/extra.dylib': b'x'}, {}, manifest)
        with self.assertRaises(package_tool.PackageError):
            package_tool.install(self.archive, self.root / 'target', out=lambda _l: None)

    def test_destdir_install_and_dry_run(self):
        package_tool.export(self.home, self.archive, 'r142')
        stage = self.root / 'stage'
        lines = []
        package_tool.install(self.archive, stage, dry_run=True, out=lines.append)
        self.assertFalse((stage / 'RuntimeLibraries/libGL.dylib').exists())
        self.assertTrue(any('would install' in line for line in lines))
        package_tool.install(self.archive, stage, out=lambda _line: None)
        self.assertEqual((stage / 'RuntimeLibraries/libGL.dylib').read_bytes(),
                         b'gl-bytes')

    def test_guest_home_ignores_users_shared(self):
        prefix = self.root / 'prefix'
        home = make_home(prefix / 'Users/zero')
        (prefix / 'Users/Shared/Riot Games').mkdir(parents=True)
        self.assertEqual(package_tool.guest_home_for(prefix), home)

    def test_guest_home_rejects_ambiguity(self):
        prefix = self.root / 'prefix'
        make_home(prefix / 'Users/zero')
        make_home(prefix / 'Users/other')
        with self.assertRaises(package_tool.PackageError):
            package_tool.guest_home_for(prefix)

    def test_export_needs_both_runtime_roots(self):
        broken = self.root / 'broken'
        (broken / 'RuntimeLibraries').mkdir(parents=True)
        with self.assertRaises(package_tool.PackageError):
            package_tool.export(broken, self.root / 'broken.tar.gz', '')

    def test_inspect_reports_the_contents(self):
        package_tool.export(self.home, self.archive, 'r142')
        lines = []
        manifest = package_tool.describe(self.archive, out=lines.append)
        self.assertEqual(manifest['label'], 'r142')
        self.assertTrue(any(line.startswith('files: 3, symlinks: 2') for line in lines))


if __name__ == '__main__':
    unittest.main()
