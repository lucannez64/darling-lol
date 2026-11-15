"""Hash the Iridium sources for the translator identity that keys cached shaders.

Indium keeps translated shaders on disk under Iridium::translatorIdentity(),
which starts with IRIDIUM_SOURCE_HASH. Iridium refuses to compile without that
definition, so every build of it outside CMake passes definition() from here.
The value is computed from the sources being compiled, never read from a cached
CMake flags file: a stale hash would let a translator fix go unnoticed behind
shaders cached by the old translator. The steps match the Indium CMakeLists.txt.
"""
import hashlib
from pathlib import Path

FOLDERS = ('include/iridium', 'private-include/iridium', 'src/iridium')
PREFIX = '-DIRIDIUM_SOURCE_HASH='


def source_hash(indium):
    """SHA-256 over the sorted 'relative path:SHA-256 of the file' lines."""
    indium = Path(indium)
    names = sorted(path.relative_to(indium).as_posix() for folder in FOLDERS
                   for path in (indium / folder).rglob('*') if path.is_file())
    if not names:
        raise SystemExit(f'No Iridium sources found to hash under {indium}')
    lines = ''.join(f'{name}:{hashlib.sha256((indium / name).read_bytes()).hexdigest()}\n'
                    for name in names)
    return hashlib.sha256(lines.encode()).hexdigest()


def definition(indium):
    """The compiler argument that gives Iridium the hash of these sources."""
    return f'{PREFIX}"{source_hash(indium)}"'


def replace_definition(defines, indium):
    """The defines of a CMake flags file, with its hash replaced by the current one."""
    return [value for value in defines if not value.startswith(PREFIX)] + [definition(indium)]
