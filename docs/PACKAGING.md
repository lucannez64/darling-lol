# Prebuilt runtime overlays

Compiling the patched Darling runtime needs the full toolchain. Installing the
result does not. `tools/runtime-package.py` packs the per-user overlay trees
that the build installs:

    <guest-home>/RuntimeLibraries
    <guest-home>/RuntimeFrameworks

into one deterministic archive with a hashed manifest, and installs that
archive into another prefix or into a staging directory for a distribution
package. It never includes Riot's game files, the Darling base runtime under
`/usr/local`, or the prefix identity (serial, MLB, UUID, ROM).

## Export on a machine that built the runtime

```sh
python3 tools/runtime-package.py export \
  --prefix "$HOME/.darling" \
  --output darling-lol-runtime-r$(date +%Y%m%d).tar.gz \
  --label "Riot Client 142 runtime"
```

`--guest-home` overrides the guest home when the prefix holds more than one
`Users/*` directory. The export is byte-for-byte reproducible for the same
input: entries are sorted, ownership is fixed at root and timestamps come from
`SOURCE_DATE_EPOCH` (default 0), so a package build can set it.

## Inspect and install elsewhere

```sh
python3 tools/runtime-package.py inspect --package darling-lol-runtime.tar.gz
python3 tools/runtime-package.py install \
  --package darling-lol-runtime.tar.gz \
  --prefix "$HOME/.darling"
```

For a freshly initialized guest home that has no runtime overlays yet, add
`--guest-home "$HOME/.darling/Users/<guest-user>"` to the install command,
using the existing guest home. Automatic home selection only finds homes
that already contain runtime overlays.

`--destdir DIR` writes below `DIR` instead of the guest home, which is what a
package build stages into `$pkgdir`. `--dry-run` lists the entries without
writing.

## Safety

The archive is untrusted input:

- every path must stay inside `RuntimeLibraries` or `RuntimeFrameworks`, with no
  absolute paths, `..`, or backslashes;
- every symlink must be relative and must resolve inside its own runtime root;
- every file hash is checked against the manifest before anything is moved into
  place, using a staging directory inside the destination;
- the installer refuses to replace a directory, refuses a symlinked destination
  root, and refuses symlinked ancestors below that root. Caller-selected parents
  above the destination root may themselves be symlinks.

A checksum mismatch, an unexpected member, or an escaping link aborts the
install and leaves the existing overlay untouched. All entries and destination
collisions are checked before replacement begins. Each replacement is atomic;
an I/O failure during replacement can still leave a partially updated set.

## What this covers

This is the packaging piece that needs no root and no rebuild: a distribution
or a maintainer can ship the overlay archive, and a user installs it into their
own prefix. The Darling base runtime (installed under `/usr/local/libexec`) and
the Riot Client install still follow their own setup steps; the Nix output in
[`NIX.md`](NIX.md) already provides a system-wide base runtime.
