#!/usr/bin/env python3
"""Check every Mach-O image in a client bundle for imports the guest runtime does not provide.

When a client update links a new symbol, dyld stops at the first one it cannot bind, so
finding them one launch at a time is slow. This walks the bundle the way dyld would and
lists every gap at once:

- Libraries resolve as in the launcher's guest: DYLD_FRAMEWORK_PATH (<guest home>/
  RuntimeFrameworks) by framework partial path, DYLD_LIBRARY_PATH (<guest home>/
  RuntimeLibraries) by leaf name, then the install path in the prefix and in the SDK
  (riot_overlay_audit.resolve, shared with the overlay audit).
- @rpath uses the LC_RPATHs of the image and of every image above it in the load chain,
  each expanded relative to the image that declares it.
- An import bound to library L is satisfied when L, or a library L re-exports
  (LC_REEXPORT_DYLIB, recursively), exports the symbol. Weak imports, weak libraries,
  flat-namespace lookups and dyld_stub_binder (provided by dyld itself) are skipped.

How a gap is reported depends on when dyld would hit it:

- error: an image reached through load commands from an executable in the bundle needs a
  missing library, or a symbol dyld binds at load. Images with LC_DYLD_CHAINED_FIXUPS
  (built for macOS 12 or later) bind every import at load, so any missing symbol stops
  the client before its first window.
- lazy: an image with LC_DYLD_INFO binds __la_symbol_ptr entries on first call. A missing
  lazily bound symbol only fails if the client calls it; a working client often has some.
- dlopen only: images nothing links to (plug-ins and SDKs the client opens with dlopen,
  such as voice-chat libraries) only matter when the client loads them.

Only errors make the exit status non-zero.

Run-time gaps, such as an Objective-C selector a stub does not implement, are out of reach
of a static import check.

    python3 tools/check-guest-imports.py
    python3 tools/check-guest-imports.py "$HOME/.darling/Users/Shared/Riot Games/Riot Client.app"
    python3 tools/check-guest-imports.py --json report.json <bundle>
"""
import argparse
import json
import os
import re
import struct
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import riot_overlay_audit as audit

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUNDLE = 'Users/Shared/Riot Games/Riot Client.app'
IGNORED_SYMBOLS = {'dyld_stub_binder'}
DEPENDENCY_COMMANDS = {'LC_LOAD_DYLIB', 'LC_LOAD_WEAK_DYLIB', 'LC_REEXPORT_DYLIB',
                       'LC_LOAD_UPWARD_DYLIB', 'LC_LAZY_LOAD_DYLIB'}
MH_EXECUTE = 2
CPU_TYPE_X86_64 = 0x01000007


def macho_filetype(path):
    """Mach-O filetype of the x86_64 image in path (thin or fat), or None."""
    try:
        with open(path, 'rb') as handle:
            head = handle.read(4096)
            if head[:4] in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
                wide = head[3] == 0xbf
                count = struct.unpack_from('>I', head, 4)[0]
                size = 32 if wide else 20
                for index in range(min(count, (len(head) - 8) // size)):
                    entry = 8 + index * size
                    cputype = struct.unpack_from('>I', head, entry)[0]
                    offset = (struct.unpack_from('>Q', head, entry + 8)[0] if wide
                              else struct.unpack_from('>I', head, entry + 8)[0])
                    if cputype == CPU_TYPE_X86_64:
                        handle.seek(offset)
                        return thin_filetype(handle.read(16))
                return None
            return thin_filetype(head[:16])
    except OSError:
        return None


def thin_filetype(head):
    if len(head) < 16 or head[:4] != b'\xcf\xfa\xed\xfe':
        return None
    if struct.unpack_from('<I', head, 4)[0] != CPU_TYPE_X86_64:
        return None
    return struct.unpack_from('<I', head, 12)[0]


def load_commands(path):
    """([(command, install name)], [rpath]) of the x86_64 image, in load-command order."""
    text = audit.tool('llvm-objdump', '--macho', '--private-headers', '--arch=x86_64', str(path))
    dylibs, rpaths, command = [], [], None
    for line in text.splitlines():
        line = line.strip()
        match = re.match(r'cmd (LC_\w+)$', line)
        if match:
            command = match.group(1)
            continue
        match = re.match(r'name (.+) \(offset \d+\)$', line)
        if match and command in DEPENDENCY_COMMANDS:
            dylibs.append((command, match.group(1)))
        match = re.match(r'path (.+) \(offset \d+\)$', line)
        if match and command == 'LC_RPATH':
            rpaths.append(match.group(1))
    return dylibs, rpaths


def imports(path):
    """[(symbol, library short name, weak)] of the x86_64 image, from llvm-nm -m."""
    out = []
    text = audit.tool('llvm-nm', '-m', '-u', '--arch=x86_64', str(path))
    for line in text.splitlines():
        match = re.search(r'\(undefined\)(.*?)external (\S+) \(from (.+)\)\s*$', line)
        if match:
            out.append((match.group(2), match.group(3), 'weak' in match.group(1)))
    return out


def bind_kinds(path):
    """(chained, symbols bound at load, symbols bound lazily) for the x86_64 image.

    With LC_DYLD_CHAINED_FIXUPS every import is bound at load and the two sets are empty."""
    headers = audit.tool('llvm-objdump', '--macho', '--private-headers', '--arch=x86_64', str(path))
    if re.search(r'cmd LC_DYLD_CHAINED_FIXUPS$', headers, re.M):
        return True, frozenset(), frozenset()

    def symbols(option):
        found = set()
        text = audit.tool('llvm-objdump', '--macho', option, '--arch=x86_64', str(path))
        for line in text.splitlines():
            names = [token for token in line.split() if token.startswith('_')]
            if names:
                found.add(names[-1])
        return frozenset(found)

    return False, symbols('--bind') | symbols('--weak-bind'), symbols('--lazy-bind')


def short_name(install):
    """The name llvm-nm prints for a library: libSystem.B.dylib -> libSystem,
    libc++.1.dylib -> libc++, .../Electron Framework -> Electron Framework."""
    return os.path.basename(install).split('.')[0]


def expand(rpaths, declaring_image, executable):
    """LC_RPATH entries as absolute paths: @loader_path is the declaring image's folder."""
    out = []
    for rpath in rpaths:
        if rpath.startswith('@loader_path'):
            rpath = str(Path(declaring_image).parent) + rpath[len('@loader_path'):]
        elif rpath.startswith('@executable_path'):
            if executable is None:
                continue
            rpath = str(Path(executable).parent) + rpath[len('@executable_path'):]
        out.append(os.path.normpath(rpath))
    return out


class Checker:
    def __init__(self, config):
        self.config = config

    def resolve(self, install, origin, rpaths, executable):
        config = dict(self.config, game=str(executable or origin))
        return audit.resolve(install, str(origin), rpaths, config)

    @lru_cache(maxsize=None)
    def exports(self, path):
        """Every name path exports, following LC_REEXPORT_DYLIB recursively."""
        names = set()
        text = audit.tool('llvm-objdump', '--macho', '--exports-trie', '--arch=x86_64', str(path))
        for line in text.splitlines():
            for token in line.split():
                if token.startswith('_') or token.startswith('.objc'):
                    names.add(token)
                    break
        dylibs, rpaths = load_commands(path)
        own = expand(rpaths, path, None)
        for command, install in dylibs:
            if command == 'LC_REEXPORT_DYLIB':
                target = self.resolve(install, path, own, None)
                if target is not None and target != path:
                    names |= self.exports(target)
        return frozenset(names)

    def check_image(self, image, rpaths, executable):
        """(missing libraries, missing symbols, lazily bound missing symbols, resolved
        dependencies) for one image."""
        dylibs, own = load_commands(image)
        chain = expand(own, image, executable) + list(rpaths)
        libraries, symbols, lazy, resolved, by_name = [], [], [], {}, {}
        chained, eager, lazily = None, frozenset(), frozenset()
        for command, install in dylibs:
            target = self.resolve(install, image, chain, executable)
            by_name.setdefault(short_name(install), (command, install, target))
            if target is None:
                if command != 'LC_LOAD_WEAK_DYLIB':
                    libraries.append(install)
                continue
            resolved[target] = chain
        for symbol, library, weak in imports(image):
            if weak or symbol in IGNORED_SYMBOLS or library in ('dynamically looked up',
                                                               'flat-namespace'):
                continue
            entry = by_name.get(library)
            if entry is None:
                symbols.append((symbol, library, None))
                continue
            command, install, target = entry
            if target is None:
                continue  # reported as a missing library (or a weak one)
            if symbol not in self.exports(target):
                if chained is None:
                    chained, eager, lazily = bind_kinds(image)
                only_lazy = not chained and symbol in lazily and symbol not in eager
                (lazy if only_lazy else symbols).append((symbol, install, str(target)))
        return libraries, symbols, lazy, resolved


def images_in(bundle):
    """{path: filetype} for every x86_64 Mach-O file in the bundle (symlinks skipped)."""
    found = {}
    for folder, _, files in os.walk(bundle):
        for name in files:
            path = Path(folder) / name
            if path.is_symlink():
                continue
            kind = macho_filetype(path)
            if kind is not None:
                found[path.resolve()] = kind
    return found


def scan(bundle, config):
    """Report dict: images reached from the bundle's executables, and the others."""
    bundle = Path(bundle).resolve()
    checker = Checker(config)
    images = images_in(bundle)
    executables = sorted(path for path, kind in images.items() if kind == MH_EXECUTE)
    report = {'bundle': str(bundle), 'executables': [str(p) for p in executables],
              'errors': [], 'lazy': [], 'dlopen_only_gaps': []}
    checked = set()

    def entry(image, libraries, symbols):
        return {'image': str(image), 'missing_libraries': libraries,
                'missing_symbols': [{'symbol': s, 'library': l, 'resolved': r}
                                    for s, l, r in symbols]}

    def visit(image, rpaths, executable, reached):
        libraries, symbols, lazy, resolved = checker.check_image(image, rpaths, executable)
        if not reached:
            if libraries or symbols or lazy:
                report['dlopen_only_gaps'].append(entry(image, libraries, symbols + lazy))
            return resolved
        if libraries or symbols:
            report['errors'].append(entry(image, libraries, symbols))
        if lazy:
            report['lazy'].append(entry(image, [], lazy))
        return resolved

    for executable in executables:
        queue = [(executable, [])]
        while queue:
            image, rpaths = queue.pop(0)
            if image in checked:
                continue
            checked.add(image)
            for target, chain in visit(image, rpaths, executable, True).items():
                if target in images and target not in checked:
                    queue.append((target, chain))
    for image in sorted(set(images) - checked):
        visit(image, [], None, False)
    report['reached'] = len(checked)
    report['dlopen_only'] = len(images) - len(checked)
    return report


def configuration(path):
    try:
        config = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise SystemExit(f'{path} is missing or unreadable; run scripts/configure.py first')
    if os.environ.get('DPREFIX'):
        config['prefix'] = os.environ['DPREFIX']
    return config


def print_report(report, bundle):
    def rel(path):
        try:
            return str(Path(path).relative_to(bundle))
        except ValueError:
            return path

    print(f"checked {report['reached'] + report['dlopen_only']} images: {report['reached']} "
          f"reached from {len(report['executables'])} executable(s), "
          f"{report['dlopen_only']} only loadable with dlopen")
    for level, entries in (('error', report['errors']),
                           ('lazy (fails only if called)', report['lazy']),
                           ('dlopen only', report['dlopen_only_gaps'])):
        for entry in entries:
            for library in entry['missing_libraries']:
                print(f"{level}: {rel(entry['image'])}: library not found: {library}")
            grouped = {}
            for item in entry['missing_symbols']:
                grouped.setdefault(item['library'], []).append(item['symbol'])
            for library, names in grouped.items():
                print(f"{level}: {rel(entry['image'])}: {len(names)} symbol(s) missing from "
                      f"{library}: {' '.join(sorted(names))}")
    if not report['errors']:
        print('no missing imports in images the client loads at startup')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('bundle', nargs='?', type=Path,
                        help=f'bundle to check (default: <prefix>/{DEFAULT_BUNDLE})')
    parser.add_argument('--config', type=Path, default=ROOT / 'build/config.json',
                        help='configuration with prefix, guest_home and sdk')
    parser.add_argument('--json', type=Path, metavar='FILE', help='also write the report as JSON')
    arguments = parser.parse_args(argv)
    config = configuration(arguments.config)
    bundle = arguments.bundle or Path(config['prefix']) / DEFAULT_BUNDLE
    if not Path(bundle).is_dir():
        raise SystemExit(f'{bundle} is not a directory')
    report = scan(bundle, config)
    if arguments.json:
        arguments.json.write_text(json.dumps(report, indent=1) + '\n')
    print_report(report, Path(bundle).resolve())
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
