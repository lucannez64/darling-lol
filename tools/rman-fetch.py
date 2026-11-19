#!/usr/bin/env python3
"""Download one Riot release (RMAN manifest) from Riot's public patch CDN.

Usage: rman-fetch.py MANIFEST_URL OUT_DIR [--filter REGEX] [--list]
"""
import argparse
import os
import re
import struct
import sys
import urllib.request
from pathlib import Path

try:
    from compression import zstd as _zstd

    def unzstd(data):
        return _zstd.decompress(data)
except ImportError:
    import zstandard

    def unzstd(data):
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=1 << 30)

UA = {'User-Agent': 'Mozilla/5.0'}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        return r.read()


class Table:
    """Minimal flatbuffers table reader."""

    def __init__(self, buf, pos):
        self.buf, self.pos = buf, pos
        vt = pos - struct.unpack_from('<i', buf, pos)[0]
        self.vt_len = struct.unpack_from('<H', buf, vt)[0]
        self.vt = vt

    def _off(self, field):
        o = 4 + 2 * field
        if o >= self.vt_len:
            return 0
        return struct.unpack_from('<H', self.buf, self.vt + o)[0]

    def scalar(self, field, fmt, default=0):
        o = self._off(field)
        return struct.unpack_from('<' + fmt, self.buf, self.pos + o)[0] if o else default

    def _indirect(self, field):
        o = self._off(field)
        if not o:
            return None
        p = self.pos + o
        return p + struct.unpack_from('<I', self.buf, p)[0]

    def string(self, field):
        p = self._indirect(field)
        if p is None:
            return ''
        n = struct.unpack_from('<I', self.buf, p)[0]
        return self.buf[p + 4:p + 4 + n].decode()

    def vector(self, field):
        p = self._indirect(field)
        if p is None:
            return 0, 0
        return p + 4, struct.unpack_from('<I', self.buf, p)[0]

    def tables(self, field):
        start, n = self.vector(field)
        for i in range(n):
            p = start + 4 * i
            yield Table(self.buf, p + struct.unpack_from('<I', self.buf, p)[0])

    def u64s(self, field):
        start, n = self.vector(field)
        return list(struct.unpack_from(f'<{n}Q', self.buf, start)) if n else []


def parse(raw):
    if raw[:4] != b'RMAN':
        raise SystemExit('not an RMAN manifest')
    offset, length = struct.unpack_from('<II', raw, 8)
    body = unzstd(raw[offset:offset + length])
    root = Table(body, struct.unpack_from('<I', body, 0)[0])
    chunks = {}
    for bundle in root.tables(0):
        bid, pos = bundle.scalar(0, 'Q'), 0
        for chunk in bundle.tables(1):
            csize, usize = chunk.scalar(1, 'I'), chunk.scalar(2, 'I')
            chunks[chunk.scalar(0, 'Q')] = (bid, pos, csize, usize)
            pos += csize
    dirs = {d.scalar(0, 'Q'): (d.scalar(1, 'Q'), d.string(2)) for d in root.tables(3)}

    def dirpath(did):
        parts = []
        while did and did in dirs:
            did, name = dirs[did]
            if name:
                parts.append(name)
        return '/'.join(reversed(parts))

    files = []
    for f in root.tables(2):
        d = dirpath(f.scalar(1, 'Q'))
        name = f.string(3)
        files.append({'path': f'{d}/{name}' if d else name, 'size': f.scalar(2, 'I'),
                      'chunks': f.u64s(7), 'symlink': f.string(9), 'perm': f.scalar(12, 'B')})
    return files, chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('manifest')
    ap.add_argument('out')
    ap.add_argument('--filter', default='')
    ap.add_argument('--list', action='store_true')
    args = ap.parse_args()
    cdn = args.manifest.split('/channels/')[0]
    files, chunks = parse(get(args.manifest))
    rx = re.compile(args.filter)
    files = [f for f in files if rx.search(f['path'])]
    if args.list:
        for f in files:
            print(f"{f['size']:>12} {f['path']}")
        return
    needed = sorted({chunks[c][0] for f in files for c in f['chunks']})
    out = Path(args.out)
    cache = out.parent / (out.name + '.bundles')
    cache.mkdir(parents=True, exist_ok=True)
    for i, bid in enumerate(needed, 1):
        p = cache / f'{bid:016X}.bundle'
        if not p.exists():
            p.write_bytes(get(f'{cdn}/channels/public/bundles/{bid:016X}.bundle'))
        print(f'bundle {i}/{len(needed)}', file=sys.stderr, end='\r')
    print(file=sys.stderr)
    opened = {}
    for f in files:
        dest = out / f['path']
        dest.parent.mkdir(parents=True, exist_ok=True)
        if f['symlink']:
            if dest.is_symlink() or dest.exists():
                dest.unlink()
            os.symlink(f['symlink'], dest)
            continue
        with open(dest, 'wb') as w:
            for c in f['chunks']:
                bid, pos, csize, usize = chunks[c]
                if bid not in opened:
                    opened[bid] = (cache / f'{bid:016X}.bundle').read_bytes()
                data = unzstd(opened[bid][pos:pos + csize])
                if len(data) != usize:
                    raise SystemExit(f"{f['path']}: chunk {c:016X} size mismatch")
                w.write(data)
        if dest.stat().st_size != f['size']:
            raise SystemExit(f"{f['path']}: size {dest.stat().st_size} != {f['size']}")
        if f['perm'] & 1 or dest.suffix == '' and 'MacOS' in f['path']:
            dest.chmod(0o755)
    print(f'{len(files)} files written to {out}')


if __name__ == '__main__':
    main()
