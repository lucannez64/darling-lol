"""Offline tests for tools/check-guest-imports.py.

Mach-O files are 16-byte headers and llvm-objdump/llvm-nm output is replayed from fixtures,
so no LLVM tools or client files are needed.
"""
import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
spec = importlib.util.spec_from_file_location('check_guest_imports', ROOT / 'tools/check-guest-imports.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

MH_EXECUTE, MH_DYLIB = 2, 6


def thin(filetype):
    return b'\xcf\xfa\xed\xfe' + struct.pack('<III', 0x01000007, 3, filetype)


def headers(*commands, chained=False):
    """llvm-objdump --private-headers text for (command, name-or-path) pairs."""
    lines = []
    for command, value in commands:
        lines += [f'Load command {len(lines)}', f'      cmd {command}', '  cmdsize 56']
        key = 'path' if command == 'LC_RPATH' else 'name'
        lines.append(f'     {key} {value} (offset 24)')
    lines += ['      cmd LC_DYLD_CHAINED_FIXUPS' if chained else '      cmd LC_DYLD_INFO_ONLY',
              '  cmdsize 16']
    return '\n'.join(lines) + '\n'


def trie(*names):
    return 'Exports trie:\n' + ''.join(f'0x00001000  {name}\n' for name in names)


def undefined(*entries):
    """llvm-nm -m -u lines for (symbol, library short name, weak)."""
    return ''.join(f"                 (undefined){' weak' if weak else ''} external {symbol} "
                   f'(from {library})\n' for symbol, library, weak in entries)


class Fixture:
    """A prefix, an SDK and a client bundle on disk, plus replayed tool output."""

    def __init__(self, root):
        self.root = Path(root)
        self.prefix = self.root / 'prefix'
        self.sdk = self.root / 'sdk'
        self.bundle = self.prefix / 'Users/Shared/Client.app'
        self.config = {'prefix': str(self.prefix), 'guest_home': '/Users/me', 'sdk': str(self.sdk)}
        self.outputs = {}

    def file(self, path, filetype=MH_DYLIB, private_headers='', exports='', nm='',
             bind='', lazy_bind=''):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(thin(filetype))
        key = str(path.resolve())
        self.outputs[key] = {'--private-headers': private_headers, '--exports-trie': exports,
                             '-u': nm, '--bind': bind, '--lazy-bind': lazy_bind, '--weak-bind': ''}
        return path

    def tool(self, *arguments):
        path = str(Path(arguments[-1]).resolve())
        option = next(a for a in arguments[1:] if a.startswith('-') and not a.startswith('--arch')
                      and a not in ('--macho', '-m'))
        return self.outputs.get(path, {}).get(option, '')

    def scan(self, bundle=None):
        with patch.object(checker, 'run_tool', side_effect=self.tool):
            return checker.scan(bundle or self.bundle, self.config)


def system(fixture, exports=('_getenv',), extra=()):
    """libSystem re-exporting libsystem_c, which defines the given symbols."""
    fixture.file(fixture.sdk / 'usr/lib/system/libsystem_c.dylib', exports=trie(*exports))
    fixture.file(fixture.sdk / 'usr/lib/libSystem.B.dylib',
                 private_headers=headers(('LC_ID_DYLIB', '/usr/lib/libSystem.B.dylib'),
                                         ('LC_REEXPORT_DYLIB', '/usr/lib/system/libsystem_c.dylib'),
                                         *extra),
                 exports=trie('_kevent_impl'))


class ImportCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def executable(self, nm, *commands, chained=True, **kwargs):
        return self.fx.file(self.fx.bundle / 'Contents/MacOS/Client', MH_EXECUTE,
                            private_headers=headers(*commands, chained=chained), nm=nm, **kwargs)

    def test_import_resolves_through_reexported_library(self):
        system(self.fx)
        self.executable(undefined(('_getenv', 'libSystem', False)),
                        ('LC_LOAD_DYLIB', '/usr/lib/libSystem.B.dylib'))
        report = self.fx.scan()
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['reached'], 1)

    def test_missing_symbol_in_a_chained_fixups_image_is_an_error(self):
        system(self.fx)
        self.executable(undefined(('_sandbox_extension_consume', 'libSystem', False)),
                        ('LC_LOAD_DYLIB', '/usr/lib/libSystem.B.dylib'))
        report = self.fx.scan()
        missing = report['errors'][0]['missing_symbols']
        self.assertEqual([m['symbol'] for m in missing], ['_sandbox_extension_consume'])
        self.assertEqual(missing[0]['library'], '/usr/lib/libSystem.B.dylib')

    def test_lazily_bound_missing_symbol_is_not_an_error(self):
        system(self.fx)
        self.executable(undefined(('_AddEventTypesToHandler', 'libSystem', False),
                                  ('_NSAppendToKillRing', 'libSystem', False)),
                        ('LC_LOAD_DYLIB', '/usr/lib/libSystem.B.dylib'), chained=False,
                        lazy_bind='__DATA __la_symbol_ptr 0x1000 libSystem _AddEventTypesToHandler\n',
                        bind='__DATA __got 0x2000 pointer 0 libSystem _NSAppendToKillRing\n')
        report = self.fx.scan()
        self.assertEqual([m['symbol'] for m in report['errors'][0]['missing_symbols']],
                         ['_NSAppendToKillRing'])
        self.assertEqual([m['symbol'] for m in report['lazy'][0]['missing_symbols']],
                         ['_AddEventTypesToHandler'])

    def test_weak_imports_weak_libraries_and_dyld_stub_binder_are_ignored(self):
        system(self.fx)
        self.executable(undefined(('_absent', 'libSystem', True), ('dyld_stub_binder', 'libSystem', False),
                                  ('_ASAuthorization', 'AuthenticationServices', False)),
                        ('LC_LOAD_DYLIB', '/usr/lib/libSystem.B.dylib'),
                        ('LC_LOAD_WEAK_DYLIB', '/System/Library/Frameworks/AuthenticationServices.framework/'
                                               'Versions/A/AuthenticationServices'))
        self.assertEqual(self.fx.scan()['errors'], [])

    def test_missing_library_is_reported(self):
        system(self.fx)
        self.executable('', ('LC_LOAD_DYLIB', '/usr/lib/libSystem.B.dylib'),
                        ('LC_LOAD_DYLIB', '/System/Library/Frameworks/QuickLookUI.framework/Versions/A/QuickLookUI'))
        report = self.fx.scan()
        self.assertEqual(report['errors'][0]['missing_libraries'],
                         ['/System/Library/Frameworks/QuickLookUI.framework/Versions/A/QuickLookUI'])

    def test_runtime_frameworks_overlay_shadows_the_sdk(self):
        system(self.fx)
        name = 'QuickLookUI.framework/Versions/A/QuickLookUI'
        self.fx.file(self.fx.sdk / 'System/Library/Frameworks' / name, exports=trie('_old'))
        self.fx.file(self.fx.prefix / 'Users/me/RuntimeFrameworks' / name, exports=trie('_QLPreviewNew'))
        self.executable(undefined(('_QLPreviewNew', 'QuickLookUI', False)),
                        ('LC_LOAD_DYLIB', '/usr/lib/libSystem.B.dylib'),
                        ('LC_LOAD_DYLIB', '/System/Library/Frameworks/' + name))
        self.assertEqual(self.fx.scan()['errors'], [])

    def test_rpath_resolves_through_the_load_chain(self):
        # Squirrel's case: the framework's own LC_RPATH (@loader_path/Libraries) has no
        # match; dyld then uses the executable's @executable_path/../Frameworks.
        system(self.fx)
        frameworks = self.fx.bundle / 'Contents/Frameworks'
        self.fx.file(frameworks / 'Mantle.framework/Versions/A/Mantle', exports=trie('_MTLModel'))
        self.fx.file(frameworks / 'Electron.framework/Versions/A/Electron',
                     private_headers=headers(('LC_RPATH', '@loader_path/Libraries'),
                                             ('LC_LOAD_DYLIB', '@rpath/Mantle.framework/Mantle'),
                                             chained=True),
                     nm=undefined(('_MTLModel', 'Mantle', False)))
        os.symlink('Versions/A/Mantle', frameworks / 'Mantle.framework/Mantle')
        self.executable('', ('LC_RPATH', '@executable_path/../Frameworks'),
                        ('LC_LOAD_DYLIB', '@rpath/Electron.framework/Versions/A/Electron'))
        report = self.fx.scan()
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['reached'], 3)

    def test_image_nothing_links_to_is_only_a_dlopen_gap(self):
        system(self.fx)
        self.executable('', ('LC_LOAD_DYLIB', '/usr/lib/libSystem.B.dylib'))
        self.fx.file(self.fx.bundle / 'Contents/Frameworks/Krisp/libkrisp.dylib',
                     private_headers=headers(('LC_LOAD_DYLIB', '/usr/lib/libSystem.B.dylib'), chained=True),
                     nm=undefined(('__ZNSt3__14__fs10filesystem8__statusE', 'libSystem', False)))
        report = self.fx.scan()
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['dlopen_only'], 1)
        self.assertTrue(report['dlopen_only_gaps'][0]['image'].endswith('libkrisp.dylib'))

    def test_exit_status_and_default_bundle_follow_the_configuration(self):
        system(self.fx)
        self.executable(undefined(('_missing', 'libSystem', False)),
                        ('LC_LOAD_DYLIB', '/usr/lib/libSystem.B.dylib'))
        config = self.fx.root / 'config.json'
        config.write_text(json.dumps(dict(self.fx.config, prefix='/nonexistent')))
        out = io.StringIO()
        with patch.dict(os.environ, {'DPREFIX': str(self.fx.prefix)}), \
                patch.object(checker, 'DEFAULT_BUNDLE', 'Users/Shared/Client.app'), \
                patch.object(checker, 'run_tool', side_effect=self.fx.tool), redirect_stdout(out):
            status = checker.main(['--config', str(config)])
        self.assertEqual(status, 1)
        self.assertIn('error: Contents/MacOS/Client: 1 symbol(s) missing from /usr/lib/libSystem.B.dylib: _missing',
                      out.getvalue())

    def test_clean_bundle_exits_zero(self):
        system(self.fx)
        self.executable(undefined(('_getenv', 'libSystem', False)),
                        ('LC_LOAD_DYLIB', '/usr/lib/libSystem.B.dylib'))
        config = self.fx.root / 'config.json'
        config.write_text(json.dumps(self.fx.config))
        out = io.StringIO()
        with patch.object(checker, 'run_tool', side_effect=self.fx.tool), redirect_stdout(out):
            status = checker.main(['--config', str(config), str(self.fx.bundle)])
        self.assertEqual(status, 0)
        self.assertIn('no missing imports in images reached from the bundle\'s executables', out.getvalue())


class ToolFailureTests(unittest.TestCase):
    def test_failed_llvm_run_stops_the_check_instead_of_reading_as_clean(self):
        # An LLVM tool that starts and fails prints nothing on stdout; reading that as
        # "no imports" would report a clean bundle.
        failed = checker.subprocess.CompletedProcess(['llvm-nm'], 1, '', 'llvm-nm: error: bad file')
        with patch.object(checker.llvm_tools, 'require', return_value='/usr/bin/llvm-nm'), \
                patch.object(checker.subprocess, 'run', return_value=failed):
            with self.assertRaises(SystemExit) as raised:
                checker.imports('/bundle/Client')
        self.assertIn('llvm-nm: error: bad file', str(raised.exception))

    def test_successful_llvm_run_returns_stdout(self):
        done = checker.subprocess.CompletedProcess(['llvm-nm'], 0, 'output\n', '')
        with patch.object(checker.llvm_tools, 'require', return_value='/usr/bin/llvm-nm'), \
                patch.object(checker.subprocess, 'run', return_value=done) as run:
            self.assertEqual(checker.run_tool('llvm-nm', '-u', 'x'), 'output\n')
        self.assertEqual(run.call_args.args[0], ('/usr/bin/llvm-nm', '-u', 'x'))


class MachoTests(unittest.TestCase):
    def test_thin_and_fat_x86_64_filetypes(self):
        with tempfile.TemporaryDirectory() as tmp:
            thin_file = Path(tmp) / 'thin'
            thin_file.write_bytes(thin(MH_EXECUTE))
            fat_file = Path(tmp) / 'fat'
            arm = struct.pack('>IIIII', 0x0100000c, 0, 4096, 16, 12)
            intel = struct.pack('>IIIII', 0x01000007, 3, 8192, 16, 12)
            body = bytearray(b'\xca\xfe\xba\xbe' + struct.pack('>I', 2) + arm + intel)
            body += b'\0' * (8192 - len(body)) + thin(MH_DYLIB)
            fat_file.write_bytes(bytes(body))
            text_file = Path(tmp) / 'text'
            text_file.write_text('#!/bin/sh\n')
            self.assertEqual(checker.macho_filetype(thin_file), MH_EXECUTE)
            self.assertEqual(checker.macho_filetype(fat_file), MH_DYLIB)
            self.assertIsNone(checker.macho_filetype(text_file))

    def test_short_name_matches_llvm_nm(self):
        self.assertEqual(checker.short_name('/usr/lib/libSystem.B.dylib'), 'libSystem')
        self.assertEqual(checker.short_name('/usr/lib/libc++.1.dylib'), 'libc++')
        self.assertEqual(checker.short_name('@rpath/Electron Framework.framework/Electron Framework'),
                         'Electron Framework')


if __name__ == '__main__':
    unittest.main()
