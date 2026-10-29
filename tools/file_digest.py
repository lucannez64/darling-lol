"""SHA-256 of a file without hashlib.file_digest().

hashlib.file_digest() is Python 3.11+; Ubuntu 22.04 ships Python 3.10, where the
installers failed only after the build had finished (issue #137). This reads in
chunks, so the memory use does not depend on the file size.
"""
import hashlib


def sha256(path):
    checksum = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1 << 20), b''):
            checksum.update(chunk)
    return checksum.hexdigest()
