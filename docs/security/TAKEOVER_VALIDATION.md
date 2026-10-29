# PR #144 continuation and validation

The continuation retains Luiz Fernando (@obrientatsuya)'s commits from
[PR #144](https://github.com/lucannez64/darling-lol/pull/144), merges current
main, and adds separate fixes. The intended result remains the M0–M8
cooperative enforcement prototype and M9/M10 preparation tools. Issue #49 stays
open for Apple trust/notarization and production soak acceptance.

## Failures reproduced and corrected

- A successful `mseal` on a file-backed MAP_PRIVATE mapping did not stop a
  subsequent `pwrite` to that inode from changing mapped bytes. mldr and dyld
  now verify and map a write/grow/shrink-sealed memfd snapshot in `enforce` or
  M5 mode. M5 additionally replaces read-only segments with anonymous snapshots
  before sealing. This avoids mutable backing and MADV_DONTNEED reloading it.
  Descriptor errors fail closed, source offsets are preserved, short I/O and
  EINTR are handled, and each image is limited to 2 GiB. There is an additional
  full-image copy and memory cost; no performance claim is made.
- A learned protected inode could be opened through an existing hardlink under
  `/usr/local`: the path exception returned before checking the inode registry.
  The shared policy now checks the registry first. New local files under
  `/usr/local` still work. Both the host probe and guest fixture include this
  alias, as well as an alias under `/tmp`.
- XML substring matching treated commented or nested keys as boolean
  entitlements. Both runtime consumers now share a bounded libxml2 parser that
  reads direct root-dictionary keys and requires an actual `<true/>`. Duplicate
  queried keys, malformed XML and custom entities deny privileges. Comments do
  not grant policy, and nested keys cannot override the top-level value.
- The SIP guest probe had its enabled/disabled expectations reversed, so it
  rejected correct behavior. Those expectations now agree with csrctl output.
- The generated minimal dylib lacked LC_ID_DYLIB, making llvm-lipo skip the
  runtime signing checks. The fixture now has that load command.

## Fresh verification in this continuation

The prepared source tree was used as a read-only source of the pinned files
and configured compiler flags. Patched sources, generated RPC headers, objects
and binaries were built in isolated temporary directories; no runtime was
installed or re-signed.

- Host policy regressions: five tests compile the shipped helpers and exercise
  real file mutation, immutable descriptors, short/interrupted I/O, hardlinks,
  malformed/commented/nested entitlements, mprotect, MAP_FIXED, munmap and
  MADV_DONTNEED. This machine supports mseal, so that probe runs without a skip.
- Native mldr builds and links with the regenerated RPC interface, OpenSSL and
  libxml2. The loader gate checks run against that new binary.
- Server verifier, trust-store, process, call and server translation units, the
  guest SIP/mman/fcntl units, and both changed dyld translation units are
  syntax-compiled with their configured target flags and patched headers.
- The changed SIP and M5 guest probes are syntax-compiled against the pinned
  Darling SDK. M5 now checks that mutation of the original main-image and
  dylib files leaves their mapped code bytes unchanged, as well as mprotect
  and MAP_FIXED rejection.
- The standalone parser cross-checks against checksum-verified rcodesign
  0.29.0; the runtime signing tests and local test-CA/quarantine/ticket checks
  run on temporary fixtures. Parser fuzz smoke uses ASan/UBSan for 30 seconds.
- All component patches are checked against their pinned bases, source-package
  validation runs, and the complete Python suite runs. Exact results are
  recorded in the continuation PR description.

## Remaining acceptance and limits

The rebuilt guest runtime has not been installed or booted. Guest csops,
library validation, AMFI/debug, M5 and SIP integration must be rerun together
on a newly built runtime and disposable prefix. The earlier reference-host
counts in the implementation plan and source PR belong to the original author;
they are not new integration results for this continuation.

M5 remains opt-in and best-effort when page sealing fails; failures are logged,
but there is no fallback registry on kernels without mseal. Snapshot creation
is fail-closed when requested. JIT pages are outside M5, and sealed images
cannot be unmapped on dlclose. This is cooperative enforcement: raw Linux
syscalls bypass guest wrapper policy. The SIP registry still learns inodes
only when protected paths are inspected, is bounded, and cannot prevent a
symlink swap between path checking and the actual syscall. Rootless mode has
no authenticated trust-store boundary. DER entitlement policy, Apple CMS/
Developer ID trust, notarization acceptance and a full match soak remain open.
The default signing mode stays off; this continuation does not establish
production security or close #49.

## Follow-up review fixes

The first continuation review added the following regressions and corrections:

- Cross-process signing-state updates use the existing host-credential debug
  authorization. The UID/GID RPC still updates the emulated audit token, but
  never copies a requested UID into the server's authorization credential.
- A constructor-set fork marker survives parent death. Initial checkin permits
  registration once only for an initial process; fork children keep inherited
  state even if unverified. Exec clears all old signing state and blobs before
  reopening registration. Compiled tests exercise the real transition and debug
  authorization methods with explicit credential/registration scenarios.
- Both rename operands reject ancestors of protected roots. The learned inode
  registry is explicitly process-local; exec/independent processes do not share
  it. Host regressions check `/private`, `/private/var`, `/Library` and `/`.
- Primary and alternate CodeDirectories are validated and the strongest
  supported hash is selected: SHA-384, SHA-256, truncated SHA-256, then SHA-1.
  Dual-digest rcodesign fixtures check the selected cdhash and page hashes;
  even a malformed unselected directory is rejected. Duplicate slots or hash
  types are rejected.
- Prefix overlays require explicit `--trust`; automatic SDK trust stays scoped
  to the SDK when SDK and prefix roots are processed together. Key creation
  starts under umask 077, which is restored on success and failure.
- Ground-truth rendering no longer infers flags, empty entitlements or SIP
  state from missing results. Existing macOS 14 observations remain historical;
  new collection uses macOS 15, and collection actions use pinned commit SHAs.

The unrelated framework sections visible in monolithic patch files are
unchanged from main. Comparing per-upstream-file patch sections shows that
`darling.patch` changes only mldr, its elfcalls interface and the new security
helpers. This continuation does not reintroduce the audio/graphics/framework
implementation work already present on main.
