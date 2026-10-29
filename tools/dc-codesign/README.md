# dc-codesign

Prototype of the phase-M1 parser from
[`docs/security/SIP_AMFI_IMPLEMENTATION_PLAN.md`](../../docs/security/SIP_AMFI_IMPLEMENTATION_PLAN.md).

It reads a thin 64-bit Mach-O, finds `LC_CODE_SIGNATURE`, parses the
`CS_SuperBlob` and the `CodeDirectory`, verifies every code-page hash, and
reports the effective cdhash exactly as `csops(CS_OPS_CDHASH)` would.

## Build

```sh
make            # needs OpenSSL headers (libcrypto)
```

## Usage

```sh
./dc-codesign info         path/to/binary     # key=value dump
./dc-codesign verify       path/to/binary     # parse + page hashes
./dc-codesign cdhash       path/to/binary     # 20-byte cdhash, hex
./dc-codesign entitlements path/to/binary     # XML entitlements
./dc-codesign blobs        path/to/binary     # SuperBlob slot listing
```

Exit codes: `0` ok, `1` parse/verification failure, `2` usage/IO error.

## What is checked

- Mach-O magic/endianness (64-bit LE/BE; fat binaries are rejected).
- Load-command bounds, exactly one `LC_CODE_SIGNATURE`.
- SuperBlob magic/length/count and every blob `offset + length`.
- CodeDirectory version-gated field sizes (v0x20100 … v0x20500).
- `hashOffset`/`nSpecialSlots`/`nCodeSlots` arithmetic without overflow.
- hash type ↔ hash size consistency; identifier/team-id NUL termination.
- `codeLimit` (32- and 64-bit forms) against the file size, and
  `nCodeSlots == ceil(codeLimit / pageSize)`.
- cdhash = H(CodeDirectory blob) truncated to 20 bytes, per hash type.
- Every page hash, including the short last page.
- Special slots `-2` (requirements), `-5` (XML entitlements) and `-7` (DER
  entitlements): each present blob is hashed in full with the CodeDirectory
  hash type and compared with its slot; an absent blob must have a zero slot.
- XML and DER entitlements are located, slot-verified and returned, not
  interpreted.

## What is *not* checked yet

- CMS/PKI chains, designated requirements, notarization, Apple policy.
- Info.plist, resource-dir and application special slots — they need the
  bundle, not just the Mach-O.
- Fat/universal binaries.
- Fat Mach-O slices and `LC_CODE_SIGNATURE` in non-first slices.

Those belong to M4/M9 in the plan and are deliberately not claimed here.

## Tests

```sh
make test
# or, with an explicit input:
./test.sh /path/to/some.dylib
```

`test.sh` cross-checks against [`rcodesign`](https://github.com/indygreg/apple-platform-rs),
an independent implementation: SHA-1/SHA-256 of the CodeDirectory, effective
cdhash, page verification, tamper rejection, entitlements round-trip and
slot tamper rejection,
self-signed team id, truncated and non-Mach-O inputs.

If no Mach-O is passed and no Darling build tree is present, the script reports
`SKIP` instead of failing.

## Fuzzing

```sh
make fuzz                 # clang -fsanitize=fuzzer,address,undefined
./fuzz/run.sh 60          # builds a corpus from a signed Mach-O, runs 60s
./fuzz/run.sh 300 path/to/binary
```

`fuzz/run.sh` seeds the corpus with an unsigned Mach-O, ad-hoc and SHA-1
signatures, an entitlements signature, truncations and arbitrary bytes, then
runs the parser under ASan/UBSan. The harness also verifies page hashes and
touches both ends of every located blob, so OOB reads in either path abort.

Reference run (Ryzen 7 5800H-class, 12 threads): 7,046,007 executions in 61 s,
115k exec/s, 480 new corpus units, no findings.

CI (`.github/workflows/dc-codesign.yml`) runs `make test` and a 30 s fuzz
smoke on every change under `tools/dc-codesign/`. When no Darling build tree is
present, both scripts generate a minimal signable Mach-O with
`minimal_macho.py`.

## Shared runtime policy helpers

`dc_entitlements.h` interprets direct keys of the root plist dictionary for
mldr and darlingserver. Only an actual `<true/>` grants a boolean entitlement;
comments, nested dictionaries, duplicate policy keys, malformed XML and custom
entities grant nothing. It uses libxml2 with network/DTD loading and entity
substitution disabled. DER policy interpretation remains unimplemented.

`dc_image_snapshot.h` creates an immutable memfd for verification and mapping;
`dc_mseal.h` replaces read-only mappings with anonymous snapshots before
sealing. Their runtime copies are checked byte-for-byte by
`tests/test_security_host.py`, which also compiles and exercises the policies
with real Linux files, hardlinks and memory mappings:

```sh
# Additional dependencies: libxml2 development files and pkg-config.
python3 -m unittest discover -s tests -p test_security_host.py
```

These host checks do not require an installed Darling runtime. The mseal probe
skips on kernels that return ENOSYS. See the takeover validation report for
remaining guest and production acceptance work.
