# Code signing, SIP and AMFI: implementation plan

Status: **implementation in progress**. Companion to issue #49 and the draft
tracking PR #50 (which carries `docs/security/SIP_AMFI_ENFORCEMENT.md`). This
document is the engineering plan: it names the code seams, the data model, the
phases, the acceptance tests and the honest limits. M0–M8 have prototypes on
this branch, each behind its acceptance tests. M9 (Gatekeeper/notarization)
and M10 (production default + soak) now have local preparation tools and
smoke tests, but their real acceptance remains open because it requires a real
macOS/notarization corpus and a full reference-machine match test; they are
not claimed as fully implemented.

Author: fork maintainers (`obrientatsuya/darling-lol`).

The continuation of [PR #144](https://github.com/lucannez64/darling-lol/pull/144)
preserves the original commits and adds the fixes and validation described in
[TAKEOVER_VALIDATION.md](TAKEOVER_VALIDATION.md). Test counts below are the
original author's reference runs unless that validation document says they were
repeated. They do not establish production readiness.

---

## 1. Scope and non-goals

**In scope**

- Verify Mach-O code signatures (ad-hoc, Developer ID, Apple platform) on disk
  and at load time.
- Give every guest task a real, task-owned signing state that `csops(2)`
  reports truthfully.
- Enforce signing at `exec` and `dlopen`, with hardened-runtime and library
  validation semantics.
- Protect running code pages against later modification.
- Enforce entitlements that Darling can actually enforce (`get-task-allow`,
  JIT, library validation, dyld environment).
- Provide a Darwin-shaped, **local** trust store with a real authentication
  chain (not Apple's keys, and never presented as such).
- Implement real SIP configuration and protected-path enforcement inside the
  emulation layer, plus task-port / debug restrictions.
- Keep the existing "never fabricate state" principle: if a check did not run,
  the answer is an error or zero flags, never `CS_VALID`.

**Out of scope (explicit)**

- Spoofing Apple identities, Apple-signed trust caches, or notarization
  tickets. A local pin list is not an Apple trust cache.
- Making any third-party anti-cheat or DRM system "trust" the environment.
  This work is about OS compatibility fidelity and correctness; it makes no
  claim about detection outcomes and must not be sold as an anti-detection
  feature.
- CPUID trapping / hardware identity (tracked separately in
  `docs/reviews/pr41-cpuid-security-validation.md`).
- Kernel-grade isolation. See §2.

---

## 2. The honest security model

Darling is not a kernel. A guest Mach-O process shares the Linux kernel with
Darling and can issue raw Linux syscalls (`syscall` instruction) without going
through `libsystem_kernel`. Therefore:

1. Everything in this plan is **cooperative enforcement**. It gives
   well-behaved macOS software the security semantics it expects; it cannot
   contain a hostile guest the way XNU contains a hostile process.
2. "Kernel-enforced SIP" in Darling means "enforced in the one syscall path
   that conforming software uses, with no user-space override". We will say
   exactly that in docs and code comments, and never claim XNU equivalence.
3. Any acceptance test that could pass by fabrication (e.g. simply returning
   `CS_VALID`) is invalid by construction. Every positive test needs a
   corresponding negative (tampered/unsigned/wrong-entitlement) test.

This is consistent with the project's ban-risk stance: real enforcement is
compatibility work. It is not, and will not be described as, a way to satisfy
or evade an anti-cheat.

---

## 3. How macOS does it (ground truth we are matching)

### 3.1 `exec`

1. Kernel resolves the binary, reads `LC_CODE_SIGNATURE`.
2. `amfi`/`amfid` validates the `CS_SuperBlob`: `CodeDirectory` page hashes,
   CMS signature chain, designated requirement, entitlements, notarization
   ticket (Gatekeeper policy is consulted for quarantined files).
3. On success the kernel attaches a `cs_blob` to the `proc`/`task`: cdhash,
   team ID, platform-binary flag, entitlements blob, flags (`CS_VALID`,
   `CS_HARD`, `CS_KILL`, `CS_RESTRICT`, `CS_REQUIRE_LV`, `CS_RUNTIME`, …).
4. On failure the exec is denied (or the process is killed later, depending on
   `CS_HARD`/`CS_KILL`).

### 3.2 `dlopen`

`dyld` asks the kernel/`amfid` to validate the image, then enforces library
validation: a loaded image must share the main executable's team ID or be an
Apple platform binary, unless the main binary carries
`com.apple.security.cs.disable-library-validation`.

### 3.3 `csops(2)`

`csops(pid, op, buf, size)` exposes the task's signing state:
`CS_OPS_STATUS` (flags), `CS_OPS_CDHASH` (20-byte cdhash),
`CS_OPS_ENTITLEMENTS_BLOB` (XML plist), `CS_OPS_TEAMID`, `CS_OPS_BLOB`,
`CS_OPS_IDENTITY`, plus setters (`MARKINVALID`, `MARKHARD`, `MARKKILL`,
`MARKRESTRICT`, `SET_STATUS`, `CLEARINSTALLER`, `CLEAR_LINKAGE`). The `audit
token` variant resolves the target through a task port.

> The numeric values of these operations are ABI. Take them from the pinned
> XNU `bsd/sys/codesign.h` at the revision in `upstream/manifest.json`; do not
> hardcode numbers in Darling sources.

### 3.4 AMFI

`AppleMobileFileIntegrity` is a kernel component plus the `amfid` user-space
daemon. Guest code reaches it through `__mac_syscall("AMFI", call, arg)`
(policy name `"AMFI"`). Calls cover device/proc state, team IDs, and
per-process flags. Unknown calls must fail, not succeed.

### 3.5 SIP

`csrctl(2)` reports the active configuration (`CSR_ALLOW_*` bits:
`UNTRUSTED_KEXTS 0x1`, `UNRESTRICTED_FS 0x2`, `TASK_FOR_PID 0x4`,
`KERNEL_DEBUGGER 0x8`, `APPLE_INTERNAL 0x10`, `UNRESTRICTED_DTRACE 0x20`,
`UNRESTRICTED_NVRAM 0x40`, `DEVICE_CONFIGURATION 0x80`, `ANY_RECOVERY_OS 0x100`,
`UNAPPROVED_KEXTS 0x200`, `EXECUTABLE_POLICY_OVERRIDE 0x400`,
`UNAUTHENTICATED_ROOT 0x800`). The VFS then denies protected-path mutations
(`/System`, `/bin`, `/sbin`, `/usr` except `/usr/local`, `/private/etc`,
`/private/var/db` selected stores, …) and privileged operations
(`task_for_pid`, debugger attach, NVRAM writes, kext loads).

### 3.6 Gatekeeper / notarization

`syspolicyd` + `trustd` evaluate quarantine (`com.apple.quarantine` xattr),
signature validity and the stapled notarization ticket. This is the least
documented layer; the exact ticket container and the private `SecAssessment`
surface must be confirmed against a real macOS install before implementation
(see §12).

---

## 4. Current state audit

Measured on `main` at the time of writing (paths relative to
`build/darling-src/src/external/xnu/darling/src/libsystem_kernel/emulation/`):

| # | Seam | Current behavior | Problem |
|---|---|---|---|
| 1 | `src/xnu_syscall/bsd/impl/misc/csops.c` | `CS_OPS_STATUS` returns `CS_VALID` unconditionally; `BLOB`/`ENTITLEMENTS_BLOB`/`IDENTITY` zero-fill; everything else `ENOSYS` | Fabricates validity; no task state |
| 2 | `src/xnu_syscall/bsd/impl/misc/mac_syscall.c` | `Seatbelt` and `AMFI` policies print and return `0` | Fabricates AMFI success |
| 3 | `src/xnu_syscall/bsd/impl/misc/csrctl.c` | `CHECK` returns 0; `GET_ACTIVE_CONFIG` returns `CSR_VALID_FLAGS` | Reports SIP fully disabled and every request allowed |
| 4 | `src/startup/mldr/mldr.c` | Parses/maps Mach-O; no `LC_CODE_SIGNATURE` verification | No load gate |
| 5 | `Security.framework` (`libsecurity_codesigning`) | Vendored Apple implementation is present (`StaticCode.cpp`, `codedirectory.cpp`, `reqparser.cpp`) | Not wired into exec/dlopen or task state |
| 6 | `darlingserver` `duct-tape/xnu/osfmk/arm/trustcache.c` | XNU trust-cache code present, arm64-oriented | No authenticated store for x86-64 guests; unused |
| 7 | Build | `build/src/tools/codesign` is produced by cctools-port | Not used by the install/runtime steps |

Note: PR #48 already makes several of these *truthful* (errors instead of fake
success) on its branch. This plan assumes that lands first; the audit above is
the `main` baseline.

---

## 5. Target architecture

```
            +-----------------------------------------------+
 guest      | mldr (exec/dlopen)                            |
            |   parse LC_CODE_SIGNATURE                     |
            |   verify -> dc_cs_blob                        |
            |   set per-process state                       |
            +---------------------+-------------------------+
                                  | RPC (pid -> state)
            +---------------------v-------------------------+
            | darlingserver: dc_cs_state per guest process  |
            +---------------------+-------------------------+
                                  |
            +---------------------v-------------------------+
            | libsystem_kernel                              |
            |   csops.c      -> reads state (no fabrication) |
            |   mac_syscall  -> AMFI dispatch / fail closed  |
            |   csrctl.c     -> real CSR config              |
            |   VFS wrappers -> SIP path policy              |
            +---------------------+-------------------------+
                                  |
            +---------------------v-------------------------+
            | dc_codesign (new lib)                         |
            |   SuperBlob/CodeDirectory parser (bounded)    |
            |   page hashes, special slots, cdhash          |
            |   entitlements (XML + DER)                    |
            |   CMS via vendored libsecurity_codesigning    |
            |   policy engine (off|warn|enforce)            |
            +-----------------------------------------------+
```

Design rules:

- **One verifier.** `mldr`, `csops`, the CLI and the build-time signer all call
  the same `dc_codesign` API. No second implementation of cdhash math.
- **The loader is the only writer** of a task's signing state. `csops` is a
  reader (same process: in-process state; other process: `darlingserver`).
- **Policy lives in one file**, `$prefix/etc/darling/security.plist`, with an
  environment override for tests (`DARLING_SECURITY_POLICY=off|warn|enforce`).
- **Fail closed in `enforce`**, and log a machine-readable reason in `warn`.

---

## 6. Data model

New public header `dc_codesign.h` (names illustrative):

```c
#define DC_CDHASH_LEN 20
#define DC_TEAMID_LEN 64

struct dc_cs_blob {
    uint8_t  cdhash[DC_CDHASH_LEN];
    uint8_t  full_hash[32];        /* SHA-256 of CodeDirectory, not truncated */
    char     teamid[DC_TEAMID_LEN];
    uint32_t flags;                /* CS_VALID | CS_ADHOC | CS_RUNTIME | ... */
    uint8_t *entitlements;         /* XML plist, NUL-terminated */
    size_t   entitlements_len;
    uint8_t *der_entitlements;     /* CSMAGIC_EMBEDDED_DER_ENTITLEMENTS */
    size_t   der_entitlements_len;
    bool     platform_binary;
    bool     notarized;            /* only if a real ticket was validated */
    uint32_t hash_type;            /* 1=SHA1 2=SHA256 3=SHA256-trunc 4=SHA384 */
    uint32_t page_size;            /* 1 << pageSize, 4096 or 16384 */
    uint64_t code_limit;
};

struct dc_cs_state {
    bool     verified;             /* a verifier ran for this task */
    bool     valid;                /* verification succeeded */
    uint32_t flags;                /* reported by CS_OPS_STATUS */
    struct dc_cs_blob blob;
    char     image_path[PATH_MAX];
    uint64_t image_dev, image_ino, image_size, image_mtime;
};
```

`struct dc_cs_state` is owned by `darlingserver` per guest pid. In-process,
`libsystem_kernel` keeps a pointer/copy set by `mldr` before `main`, so
`csops(getpid(), ...)` is a local read.

Flags map 1:1 to `CS_*` from `bsd/sys/codesign.h`. `CS_VALID` is set **only**
when `state.valid && state.verified`.

---

## 7. Build-system prerequisites

The native `mldr` and `darlingserver` targets require OpenSSL and libxml2
headers/libraries. libxml2 parses entitlement policy; the standalone
`dc-codesign` parser still needs only OpenSSL. For example, Debian/Ubuntu hosts
need `libssl-dev libxml2-dev pkg-config` as well as the existing build toolchain.
A 32-bit mldr build needs the corresponding 32-bit crypto and XML libraries.

Enforcement is impossible while the runtime itself is unsigned. Order matters:

1. Add a post-build signing step that ad-hoc signs every Mach-O installed by
   Darling (`darlingserver`, `mldr`, `launchd`, `dyld`, frameworks, helpers)
   using the in-tree `codesign -s -`. Do it at `cmake --install` time and in
   `scripts/install-runtime.sh` for per-user overlays.
   **Tool (this branch):** `tools/sign-darling-runtime.sh` walks a tree,
   detects thin Mach-O by magic, signs with `rcodesign`, verifies every result
   with `dc-codesign`, skips already-signed files (idempotent), refuses to run
   while `darlingserver` is alive, and supports `--dry-run`, `--force`,
   `--sdk`, `--prefix`. Relocatable objects (`MH_OBJECT`) are skipped. A fat
   binary whose only slice is x86_64 is thinned in place with
   `llvm-lipo -thin x86_64` and then signed; `--no-thin` opts out and
   multi-architecture fat binaries are skipped. Final installed SDK: 1275
   Mach-O, 1274 thin loadable, 1 object; signing verified 1274, skipped 1
   object, 0 failed. `tests/security/test_sign_runtime.sh` covers thinning,
   idempotence, multi-architecture rejection and `--no-thin`: 6/6 passing.
   `sw_vers` (which loads CoreFoundation) and Python 2.7 still run.
   Confirmed: Darling executes an ad-hoc signed copy of `/usr/bin/true`
   (`mldr: codesign ok` and exit 0), so the loader accepts re-signed images.
2. Ship entitlement plists per component under
   `tools/entitlements/<component>.plist`; the signer applies them.
3. Record the resulting cdhashes in the local trust store at install time
   (§M6). Re-provision after every rebuild; stale entries must be rejected.
4. Make signing deterministic (no timestamps in ad-hoc signatures) so rebuilds
   are reproducible and cache keys stay stable.

Acceptance: `codesign -dvvv` on every installed binary shows a cdhash; the
verifier accepts all of them and rejects a byte-flipped copy.

---

## 8. Phases

Each phase lands behind a policy flag, has a positive and a negative
acceptance test, and is revertible by config. Estimates are person-weeks for
one experienced engineer, including tests.

### M0 — Ground truth and fixtures (1 w)

Owning a Mac is **not** a prerequisite. Three independent sources, documented
in `tests/security/fixtures/README.md`:

- **Constants and layouts** come from the XNU sources already pinned in
  `upstream/manifest.json` and vendored under `build/darling-src/src/external/xnu`
  (`bsd/sys/codesign.h`, `bsd/sys/csr.h`, `osfmk/kern/cs_blobs.h`). They are
  authoritative for the pinned revision; never copy numbers from blog posts.
- **Parser truth** is cross-checked on Linux with `rcodesign` (apple-codesign,
  an independent implementation): generate ad-hoc and self-signed fixtures,
  compare cdhash and blob layout against `rcodesign compute-code-hashes`.
- **Behavioural truth** comes from
  `.github/workflows/security-ground-truth.yml`, which builds and runs
  `tests/security/probe/ground_truth.c` on GitHub's arm64 macOS runners
  (`macos-14`, `macos-15`) and uploads JSON artifacts. Intel hosts can run the
  same probe manually (the GitHub Intel labels are retired or persistently
  queued). The probe is read-only: `csops` status/cdhash/teamid/entitlements,
  a forked child query (cross-process semantics), and the `csrctl`
  configuration.
- **Policy truth** (Developer ID chains, notarization) uses public signed
  releases fetched by pinned URL + SHA-256 at test time; never vendored.
- **Volunteer Mac** fallback: issue #31 has a contributor offering a real Mac;
  a read-only probe run is enough (no game, no account, no mutation).
- Save summaries to `docs/security/GROUND_TRUTH.md` with provenance, macOS
  version and runner image.
- **First collection:** run `40124165591` (macos-14 = 14.8.9/23J631,
  macos-15 = 15.7.9/24G830, both arm64). Result: linker-signed probes report
  `CS_VALID|CS_LINKER_SIGNED|CS_DYLD_PLATFORM|CS_SIGNED`, `CS_OPS_TEAMID` is
  `-1/ENOENT`, an empty entitlements blob returns success with length 0, and
  cross-process `csops` matches self. Platform-binary cdhashes and `amfid`/
  `trustd` entitlement keys are recorded in `tests/security/fixtures/macos-golden.json`.
- Deliverable: fixture generator, probe, CI artifacts, GROUND_TRUTH.md.

### M1 — `dc_codesign` parser (2 w)

- Implement bounded parsing of `CS_SuperBlob`: magic/endianness/length checks,
  `CS_BlobIndex` bounds, `CodeDirectory` v0..v0x20400+ fields, special slots
  (negative indices), page hashes, `codeLimit` vs file size, `hashType`.
- Compute cdhash exactly as XNU does (hash of `CodeDirectory` up to
  `cdLength`, truncated to 20 bytes) and the full SHA-256.
- Parse XML entitlements and DER entitlements (fail closed on malformed).
- Reuse `libsecurity_codesigning` for CMS/requirement evaluation rather than
  reimplementing crypto policy; wrap it behind `dc_verify_cms()`.
- Fuzz targets: truncated blob, length wrap, slot out of range, page size 0/31,
  hash type unknown, `codeLimit` beyond EOF, duplicate blob types.
- Acceptance: parser accepts valid macOS fixtures and the untruncated fuzz
  seeds, and rejects malformed or truncated seeds; `dc_codesign_selftest`
  runs in CI.
- **Prototype (this branch):** `tools/dc-codesign/` implements the bounded
  parser and a CLI (`info|verify|cdhash|entitlements|blobs`). `make test`
  cross-checks SHA-1/SHA-256 of the CodeDirectory, the effective cdhash, page
  hashes, tamper rejection, entitlements and malformed inputs against
  `rcodesign` on a real Mach-O.
- **Fuzzing (this branch):** `make fuzz` builds a libFuzzer harness with
  ASan/UBSan; `./fuzz/run.sh 60` seeds it from real signatures and truncations.
  Reference run: 7,046,007 executions in 61 s, no findings. Still open: a
  short fuzz smoke job in CI and the macOS-derived fixture matrix.

### M2 — Loader gate in `mldr` (2 w)

- Hook after `LC_CODE_SIGNATURE` is read and before segments are mapped.
- Modes: `off` (today), `warn` (log `mldr: codesign warning: <path>: <reason>`,
  continue), `enforce` (abort exec with `ENOEXEC`, or kill per
  `CS_HARD`/`CS_KILL`).
- Verify from the same fd used for mapping (no path re-open) to avoid TOCTOU;
  record `dev/ino/size/mtime`.
- Cache by cdhash; never cache by path alone.
- Acceptance: signed image loads; tampered page aborts under `enforce` and
  only logs under `warn`; unsigned image aborts; missing `LC_CODE_SIGNATURE`
  behaves like unsigned (not like valid).
- **Prototype (this branch):** `src/startup/mldr/mldr.c` calls a gate before
  `load()`; the parser is vendored from `tools/dc-codesign/` by the patch.
  Policy: `DARLING_CODESIGN_ENFORCE=off|warn|enforce` (default off), verbose
  success logging with `DARLING_CODESIGN_VERBOSE=1`. An unknown policy fails
  closed. `tests/security/test_mldr_gate.sh` covers off/warn/enforce, unsigned,
  signed, tampered, unknown policy and parser drift: 11/11 passing.
  Confirmed inside the real runtime: `darling shell /usr/bin/true` with `warn`
  logs `bash`, `path_helper` and `true` as unsigned, so the gate sits on the
  actual exec path. Cost: ~4 ms to verify a 4.8 MB dylib including process
  startup; no measurable change in `darling shell` startup.
  `codesign_gate()` opens the main image once with `O_NOFOLLOW|O_CLOEXEC`,
  verifies it, and hands the same descriptor to `load()`; the main image is
  never reopened by path. The M2 single-fd criterion is satisfied for the main
  executable. If the gate cannot even open the file in `off`/`warn`, the
  loader may still fall back to a path open, but that path was not approved
  by the verifier and the state remains invalid.
- Risk: breaks the runtime if M0/M7 prerequisites are skipped. Ship with
  `off` default and a documented `warn` soak period.

### M3 — Task-owned state + truthful `csops` (2 w)

- Replace `csops.c` stub: read `dc_cs_state`; implement `STATUS`, `CDHASH`,
  `ENTITLEMENTS_BLOB`, `TEAMID`, `BLOB`, `IDENTITY`, `PIDPATH`, `PIDOFFSET`.
- Setters (`MARKINVALID`, `MARKHARD`, `MARKKILL`, `MARKRESTRICT`,
  `SET_STATUS`, `CLEARINSTALLER`) require the same privilege as macOS (root or
  `get-task-allow`); otherwise `EPERM`.
- Cross-process queries: `darlingserver` RPC keyed by pid, with a generation
  counter to avoid stale pid reuse.
- Acceptance: `csops` reflects the loader's real result; a process whose image
  failed verification reports zero flags; pid reuse cannot return a previous
  process's blob; entitlements blob byte-identical to the fixture.
- **Prototype (this branch):** `mldr` fills a packed `struct dc_cs_state` and
  exposes it to `libsystem_kernel` through `struct elf_calls`; after checkin it
  registers the state (including the SuperBlob and entitlements) with
  `darlingserver`. Three RPCs (`set_cs_state`, `get_cs_state`,
  `update_cs_state`) were appended to the generated call list so existing call
  numbers stay stable. `csops.c` implements `STATUS`, `CDHASH`,
  `ENTITLEMENTS_BLOB`, `TEAMID`, `BLOB`, `IDENTITY`, `PIDPATH` (4),
  `PIDOFFSET`, the setters and the `audittoken` variant. `off` still verifies
  and records state; it only suppresses logging and rejection, so `CS_VALID`
  is never fabricated. `tests/security/test_csops.sh` covers signed, unsigned
  and entitled images plus cross-process and dead-pid behaviour: 3/3 passing;
  `test_mldr_gate.sh` remains 11/11.
  Observed: ad-hoc signed `STATUS=0x20000003`, unsigned `STATUS=0`, CDHASH
  matches `dc-codesign`, ad-hoc `TEAMID` is `ENOENT`, empty entitlements are
  an 8-byte zero token, non-empty entitlements include their 8-byte
  `CS_GenericBlob` header, `get-task-allow` sets `0x4`, a forked child inherits
  its parent's state for the same image (with a fresh server generation), and
  a dead pid returns `ESRCH`.
  Registration is one-shot per checkin/exec: the server rejects a later
  `set_cs_state`, so `MARKINVALID` or a root flag clear cannot be undone by
  replaying the RPC.
  Known limits: only the main image is verified by mldr (M4 covers `dlopen`);
  there is no MACF layer, so setter policy is self/root/`get-task-allow`;
  `exec` derives a new state and never inherits across images.
  `update_cs_state` may clear flags or mark invalid, never restore validity
  (`valid=1` is ignored); a cross-process update additionally requires caller
  root or target `get-task-allow`. Only the one-shot `set_cs_state` after
  checkin/exec can establish a new valid state. The local
  generation is not refreshed in a forked child until exec, so audittoken
  generation checks are deferred until a real pidversion mapping exists.
- Risk: Chromium probes may change behavior once they see truth. Audit call
  sites (`tools/riot-mvg-dependency-trace.c` is a starting point) and keep the
  `warn` mode for a release cycle.

### M4 — `dlopen`, library validation, hardened runtime (3 w)

- Verify every image handed to `dlopen`/`dyld`; enforce `CS_REQUIRE_LV`:
  same `teamid` as the main executable, or Apple platform binary, unless
  `com.apple.security.cs.disable-library-validation`.
- Enforce `CS_RUNTIME` dyld environment rules (strip `DYLD_*` unless
  `com.apple.security.cs.allow-dyld-environment-variables`).
- Enforce `get-task-allow` for debugger attach (§M8).
- Acceptance: unsigned dylib into a hardened process is refused; same-team
  dylib loads; `disable-library-validation` flips the result; `DYLD_INSERT_*`
  is ignored under hardened runtime and honored when entitled.
- Risk: Darling's own runtime libraries are loaded from per-user overrides;
  they must be signed and either share the main team ID or carry the
  entitlement. Publish a migration note.
- **Prototype (this branch):** dyld's existing `F_ADDFILESIGS_RETURN` /
  `F_CHECK_LV` fcntl path is wired to a new `verify_cs_image` RPC. The server
  maps the file descriptor, parses the slice at the supplied fat offset with
  the same `dc_codesign` parser, verifies page hashes and caches by
  `(dev, ino, size, mtime, offset)`. A hardened-runtime (`CS_RUNTIME`) main
  executable enables library validation by default, unless it carries
  `disable-library-validation`; `CS_OPS_CLEAR_LV` still clears the effective
  policy. Library validation allows a target when the main executable does not
  require it, has `disable-library-validation`, shares its team ID, is a
  platform image, or both sides are ad-hoc with no team ID. Darling's own
  SDK/prefix libraries must be present in the M6 local trust store; the old
  broad runtime-path
  exemption has been removed.
  Hardened-runtime handling is in dyld: `DYLD_INSERT_LIBRARIES` is ignored
  unless the process carries `allow-dyld-environment-variables`; Darling's
  root/library path variables remain available so the runtime can load its
  own overlays.
  `tests/security/test_lv_runtime.sh` covers unsigned refusal (including a
  runtime-only main), ad-hoc and same-team loads, different-team refusal,
  `disable-library-validation`, `DYLD_INSERT_LIBRARIES` with and without the
  entitlement, and `CS_OPS_CLEAR_LV` followed by a different-team load: 16/16
  passing.
  Known limits: CMS/Developer ID chains are not verified yet (M1 open item),
  and `DYLD_*` path/root variables remain usable in hardened processes to keep
  Darling's runtime loadable. The signer populates the runtime cdhashes into
  the M6 store at install time.

### M5 — Running-page integrity (3 w)

- Seal `__TEXT`/`__LINKEDIT` (or the signed ranges) after mapping:
  `mprotect(PROT_READ)` plus `mseal()` where available (Linux ≥ 6.10; probe at
  startup and fall back to `mprotect` + `mmap`/`mprotect` interception).
- Deny later `mprotect(PROT_WRITE)`, `mmap(MAP_FIXED)` over sealed ranges,
  `ptrace`/`process_vm_writev` cross-process writes, and writes through
  `/proc/<pid>/mem` by unprivileged peers.
- Options: verify page hashes eagerly at load (simple, slower) or lazily on
  first exec of a page (needs per-page dirty tracking). Start eager for
  correctness, measure, then optimize.
- Acceptance: writing to a sealed text page fails (`EPERM`/`SIGSEGV`), the
  unmodified image runs, and a page modified before sealing is detected.
- Risk: JIT and self-modifying runtimes (Chromium V8) legitimately need
  `MAP_JIT`; `allow-jit` must gate an explicit, audited exception.
- **Prototype (this branch):** under signing `enforce` or `DARLING_MSEAL=1`,
  mldr and dyld first copy each source image into a private memfd, then apply
  write/grow/shrink/seal seals. Verification and mapping use that same immutable
  descriptor, so changing or truncating the original inode cannot change the
  verified bytes. Snapshot errors reject the image; the current size limit is
  2 GiB per image. This adds a full image copy and memory cost at load time.
  For M5, read-only `__TEXT`/`__LINKEDIT` mappings are additionally replaced
  with anonymous snapshots before `mseal(2)`. dyld uses a private
  `__darling_mseal_range()` helper. Anonymous backing prevents
  `MADV_DONTNEED` from returning to mutable file contents. The kernel rejects
  later `mprotect`, `munmap` and `mmap(MAP_FIXED)` over sealed ranges on Linux
  ≥ 6.10.
  `tests/security/test_m5_seal.sh` checks the main image and a `dlopen()`ed
  dylib: both reject `mprotect(PROT_WRITE)` and `mmap(MAP_FIXED)`: 1/1 passing
  on the 7.3 test host. Sealing is opt-in: set `DARLING_MSEAL=1` (or `on`,
  `true`, `yes`) for the main image and dyld-loaded images; unset or any other
  value leaves sealing disabled without a rebuild.
  Known limits: there is no registry fallback on kernels without `mseal`;
  sealing errors are logged but do not currently abort the image (best-effort
  prototype, not fail-closed); JIT allocations are not sealed; `dlclose()`
  cannot unmap a sealed image (the mapping is intentionally left behind); page
  hashes remain eager from M0–M1; cross-process write checks beyond the M8
  debug entitlement gate are still pending.

### M6 — Local authenticated trust store (2 w)

- Store: a root-owned trust directory outside the writable prefix
  (`/etc/darling`) with a versioned header, a monotonically increasing
  generation, and a signature by a **Darling-local** key created at install
  time (Ed25519), not Apple's. Rootless mode has no authenticated store
  boundary yet and the server disables the store there.
- CLI: `dc-trustcache default-store|init|sync|list|add|remove|verify`; adding
  requires root-equivalent local policy, or ownership of the rootless config
  directory.
- Entries: 20-byte cdhash + flags, matching the shape used by the vendored
  `trustcache.c`, with an explicit header field
  `issuer = "darling-local"` so nothing can mistake it for Apple's.
- Acceptance: entry added by the local key is accepted; entry forged without
  the key is rejected; generation rollback is rejected; deleting the signature
  fails closed.
- Non-goal restated: this is **not** an Apple trust cache and must never be
  reported as one.
- **Prototype (this branch):** `tools/dc-trustcache.py` supports
  `default-store|init|sync|list|add|remove|verify`; `sync` accepts a batch of
  cdhashes and signs the versioned store (`DTC1`, issuer `darling-local`,
  generation, 20-byte cdhash + flags) with Ed25519. The default trust
  directory used by the server is `/etc/darling` for a rootful install; the
  CLI can prepare a user-config store for rootless testing, but the server
  disables it because uid 0 maps to a host user that a guest can impersonate.
  The private and public keys live beside the store. Private keys are created
  under umask 077, restored afterward. `tools/sign-darling-runtime.sh --sdk`
  records SDK cdhashes through `sync`; `--prefix` and other overlay roots need
  an explicit `--trust` to enter the store, even with `--sdk --prefix`,
  and `scripts/install-runtime.sh` invokes the signer.
  `darlingserver/src/trustcache.cpp` verifies the signature with the public
  key, requires the trust directory to be outside the prefix and to be
  non-group/world-writable, fails closed on malformed/missing signatures, and
  rejects lower generations using both an in-memory cache and a persisted
  `.lastgen` anchor. `cs_verify.cpp` requires a trust-store entry for Darling
  runtime libraries; the broad M4 runtime-path fallback is removed.
  `tests/security/test_trustcache.sh` covers outside-prefix placement,
  rootful add/rollback/tamper/missing-signature, runtime population, and a
  rootless CLI path/key initialization: 9/9 passing.
  Known limits: this is not Apple's trust cache and no Apple chain is
  validated; a root-equivalent attacker can still replace both key and store;
  rootless mode has no authenticated store boundary: uid 0 maps to the host
  user, so the server returns no trust directory and rootless runtime LV is
  not considered supported until a host-private boundary exists; `.lastgen` is
  a local rollback anchor, not a hardware monotonic counter; the full
  rootless-server path is not exercised by the automated test (only CLI
  selection/key creation); the acceptance test restarts the namespace after
  store mutations.

### M7 — SIP configuration and path enforcement (4 w)

- `csrctl`: return the configured bits; `SET_ACTIVE_CONFIG` requires
  root-equivalent local policy and logs the change. `CHECK` answers for a
  concrete operation, not "yes".
- Define the protected set once, in `dc_sip_paths.c`:
  `/System`, `/bin`, `/sbin`, `/usr` (except `/usr/local`), `/private/etc`,
  `/private/var/db` (selected), `/Library/Apple`, plus the emulated equivalents
  under the prefix. Support the `rootless` redirect to `/System/Volumes/Data`
  semantics if Darling models it.
- Hook the guest VFS wrappers used by `open(O_CREAT|O_TRUNC|O_WRONLY)`,
  `rename`, `renameat`, `unlink`, `rmdir`, `link`, `symlink`, `mkdir`,
  `chmod`, `chown`, `utimes`, `setxattr`, `mmap(PROT_WRITE)`, `truncate`.
  Enforce after canonicalization (resolve symlinks, `..`, and the prefix
  mapping) and re-check the final path — never only the user-supplied string.
- Privileged operations: deny `task_for_pid` on non-entitled targets, debugger
  attach, writes to NVRAM equivalents, and kext-style loads when the
  corresponding `CSR_ALLOW_*` bit is clear.
- Acceptance: under SIP on, every mutation of a protected path fails with
  `EPERM`; the same operation under `/usr/local` succeeds; symlink and `..`
  escapes fail; hardlink escapes require decisions on the opened file's
  device/inode and are deferred until that check exists; turning the bit on in
  config flips behavior without a rebuild; `csrctl(GET)` matches the file.
- Risk: path canonicalization bugs are the classic escape. Test with symlink
  chains, `..`, bind-mount-like prefix mappings, and case-insensitive hosts.
- **Prototype (this branch):** `DARLING_SIP_ENFORCE=1/on` is read by `mldr`
  into `dc_cs_state.sip_enabled` for the process (and inherited on fork).
  `csrctl(GET_ACTIVE_CONFIG)` reports `0` when enforcing and `CSR_VALID_FLAGS`
  when off; `CHECK` returns `EPERM` for every allow bit while enforcing;
  `SET_ACTIVE_CONFIG` is not implemented. `openat` (write/create/truncate/
  append), `unlinkat`, `mkdirat`, `renameat`, `fchmodat`/`fchmod`,
  `fchownat`/`fchown`/`lchown`, `utimes`/`futimes`, `setxattr`/`fsetxattr`,
  `truncate`/`ftruncate`, `linkat`, `symlinkat`, `rmdir` and writable
  shared (`MAP_SHARED`) file-backed `mmap` call a shared path/fd policy. The
  policy allows
  `/usr/local` and rejects resolved `/System`, `/bin`, `/sbin`, `/usr`,
  `/private/etc`, `/private/var/db` and `/Library/Apple` paths. Protected
  inodes are recorded by `(dev, ino)` and writable fd/path operations compare
  against that set, so an outside hardlink to a protected file is refused once
  the protected inode has been observed in the same process, including aliases
  under `/usr/local`. Fork children inherit that process-local registry;
  independently started or exec'd processes have their own registry.
  The path exception is applied only after the learned inode check. Renaming
  either an ancestor of a protected root or a protected path is also denied.
  `tests/security/test_sip.sh` covers
  off/on, protected writes, `/usr/local`, `..`, symlink, the mutating wrappers,
  writable shared (`MAP_SHARED`) `mmap` and a seeded hardlink escape: 3/3
  passing.
  Known limits: policy is a per-process environment flag, not a prefix
  `security.plist` or root-only `csrctl` setter; the inode registry has 4096
  slots and only learns a protected inode when a protected path is inspected,
  so a pre-existing hardlink used without touching its protected path is not
  retroactively discovered; when the registry is full, non-`/usr/local` writes
  fail closed for the life of the process; the host path is mapped back through
  `vchroot_unexpand()` and checked as a guest absolute path, but the check
  still runs before the syscall, so a racing symlink swap is not prevented;
  fallback is conservative if vchroot is uninitialized; rootless redirects are
  not modeled.

### M8 — Debug/injection restrictions and AMFI dispatch (2 w)

- Replace the blanket `mac_syscall` success with a dispatcher: known AMFI
  calls answer from real state; unknown calls return `ENOTSUP`. Seatbelt
  policy keeps its own truthful answers.
- Enforce `get-task-allow` for `ptrace`, `task_for_pid`, `process_vm_*`,
  `mach_vm_write` on another task, and `DYLD_INSERT_LIBRARIES` inheritance.
- Acceptance: entitled process can be debugged; non-entitled cannot; unknown
  AMFI call is an error (not success).
- Risk: Chromium's sandbox probes previously depended on the fake success;
  the `warn` mode and an audit list are mandatory.
- **Prototype (this branch):** AMFI call 95
  (`CDHASH_IN_TRUSTCACHE`) now queries the M6 local trust store through a
  `trustcache_contains` RPC and writes a truthful result; unknown AMFI calls
  return `ENOTSUP` instead of fabricated success. Under
  `DARLING_CODESIGN_ENFORCE=enforce`, `get-task-allow` gates
  `ptrace(PT_ATTACHEXC)` and `task_for_pid` (self and true root exempt; root
  means both the host and guest uids are 0); `task_name_for_pid` allows
  same-uid inspection or an entitled target. The target's own signing
  policy is authoritative, so a caller cannot opt out of an enforced target by
  launching itself with `off`/`warn`. The same policy is enforced
  in darlingserver by a task hook and in `Process::writeMemory`, so `mach_vm_write`
  and the underlying `process_vm_writev` path cannot bypass it; a private
  `csops` probe lets the test call the task-port RPC directly without the
  guest-side check. `tests/security/test_amfi.sh` now covers unknown AMFI,
  non-entitled ptrace, direct-RPC deny/allow, task_name deny/allow, and
  `mach_vm_write` deny/allow: 7/7 passing.
  Known limits: the Seatbelt policy still soft-succeeds to keep Chromium's
  probes alive and is not a real sandbox; raw Linux syscalls can bypass the
  cooperative server path entirely; call 95 is the only AMFI call currently
  implemented; the positive test obtains a task port and performs a memory
  write but does not run a full interactive debugger session; debug authority
  combines host-verified uid with the guest-visible uid so a guest cannot
  obtain root authority merely by requesting uid 0 in a rootless namespace.

### M9 — Gatekeeper / notarization compatibility (2–4 w, research-heavy)

**Status: local preparation implemented; Apple acceptance still blocked.**
The required real-macOS probes, Apple trust material and notarization fixtures
are not available here. No `notarized` bit is set by this branch.

- `tools/dc-notary.py` implements `com.apple.quarantine` get/set/clear and
  detects the appended `t8lr` ticket trailer used by the vendored Security
  code. A detected ticket is reported as `present=true`, `validated=false` and
  `notarized=false`.
- `tools/dc-cms-verify.py` extracts the Mach-O CMS blob and verifies its CMS
  signature against a caller-supplied **local test CA**, including signer
  chain verification. It deliberately reports `content_binding=unverified`
  until real-macOS ground truth establishes Apple's signed-content semantics.
  It never claims Apple Developer ID or Apple notarization trust.
- `tests/security/test_notary.sh` exercises quarantine round-trip, ticket
  detection, the permanent `notarized=false`, local-CA CMS success and
  unrelated-CA rejection: 7/7 passing.
- Real acceptance still requires a genuine notarized Apple fixture, the
  ticket/`syspolicyd` surface probes, and Apple chain policy. Hard rule: if no
  real ticket was validated, `notarized` is false. No heuristic "looks
  notarized". The concrete macOS handoff is in
  `docs/security/M9_M10_MAC_NEXT_STEPS.md`.

### M10 — Default switch and soak (2 w)

**Status: local harness implemented; production soak still blocked.** No
production default change or full match soak has been run. The current branch
remains an opt-in prototype.

- `tools/security-soak.py` runs representative commands under
  `DARLING_CODESIGN_ENFORCE=enforce`, writes a JSON/log report, and scans for
  signature, library-validation, crash and signing-state failures. `analyze`
  can audit existing logs. `tests/security/test_m10_soak.sh` runs the smoke
  harness and checks the analyzer against a synthetic failure: 2/2 passing.
- The full game/match soak and production default switch remain the real M10
  acceptance gate and require the reference machine.
- Update `docs/`, `README` and the launcher diagnostics; keep `warn` one flag
  away.
- Replace callers that relied on fabricated success (audit list from M3/M8).
- Acceptance: full match test on the reference machine with enforcement on
  and zero verification failures in the logs; documented rollback works.

**Total: ~24–30 person-weeks.** M0–M8 have prototypes and acceptance tests on
this branch; M9/M10 now have local preparation tools and smoke tests, but
their real Apple-policy/match acceptance still requires a reference machine.

---

## 9. Test strategy

```
tests/security/
  fixtures/            signed corpus + manifest (provenance, redaction)
  fuzz/                libFuzzer seeds for the parser
  test_parser.py       cdhash/hash-type/slot cases
  test_mldr_gate.sh    exec: good/bad/unsigned/tampered, warn vs enforce
  test_csops.sh        status/cdhash/entitlements/teamid, pid reuse
  test_lv_runtime.sh   library validation, dyld env, get-task-allow
  test_m5_seal.sh      mseal/mprotect/mmap negative cases
  test_trustcache.sh   local signed entries, forged entry, rollback
  test_sip.sh          protected paths, symlink/.. escapes (hardlinks deferred)
  test_amfi.sh         unknown calls, get-task-allow ptrace denial
  test_notary.sh       quarantine, ticket presence, local-CA CMS
  test_m10_soak.sh     enforce smoke harness + log analyzer
```

Rules:

- Every test has a negative twin. A phase is done only when both pass.
- Tests run without Apple credentials: use a local test CA and our `codesign`.
- CI runs the parser + fixture suite on every PR; loader/SIP suites run on the
  self-hosted machine because they need the runtime.
- Keep a `tests/security/README.md` mapping test → requirement (#49 matrix).

---

## 10. Performance

- Hash each image once at load; cache the `dc_cs_blob` by cdhash and by
  `(dev, ino, size, mtime)` for the on-disk check.
- Page hashing is O(size); budget ≤ 8 ms per 10 MB image on the reference
  machine, measured. If eager hashing is too slow for large frameworks, switch
  to lazy verification with a dirty-page bitmap.
- No hashing on `csops` reads; the state is precomputed.
- SIP path checks add two `realpath`-class operations per protected syscall;
  cache canonicalized prefixes, not user paths.

---

## 11. Security review checklist (per phase)

- [ ] Fails closed: parse error, I/O error, missing file, short read → deny.
- [ ] No fabrication: nothing sets `CS_VALID`/`notarized` without a verifier.
- [ ] TOCTOU: verify and map the same fd; re-check `fstat` identity.
- [ ] Path canonicalization: symlinks, `..`, case folding, prefix mapping,
      trailing slashes, NUL/embedded separators. Hardlink decisions must use
      the opened file's device/inode; path checks alone are insufficient.
- [ ] pid reuse: generation counter with the state.
- [ ] fork/exec inheritance: `fork` inherits the verified state for the same
      image and obtains a fresh server generation; `exec` derives state from
      its own image and never carries the parent state across.
- [ ] Cross-process writes: `ptrace`, `process_vm_writev`, `/proc/pid/mem`.
- [ ] No new attack surface: parser bounds-checked, no unbounded allocation,
      no recursion on attacker-controlled depth.
- [ ] Logging: reasons are stable identifiers (for tests), paths redacted when
      they may contain user data.
- [ ] Rollback: config flag restores previous behavior without a rebuild.

---

## 12. Open questions (must be answered on real macOS)

1. ~~Exact `csops` op numbers~~ **Resolved** from the pinned
   `bsd/sys/codesign.h`: `STATUS 0`, `MARKINVALID 1`, `MARKHARD 2`,
   `MARKKILL 3`, (`PIDPATH 4`, kernel-private), `CDHASH 5`, `PIDOFFSET 6`,
   `ENTITLEMENTS_BLOB 7`, `MARKRESTRICT 8`, `SET_STATUS 9`, `BLOB 10`,
   `IDENTITY 11`, `CLEARINSTALLER 12`, `CLEARPLATFORM 13`, `TEAMID 14`,
   `CLEAR_LV 15`. Flags from `osfmk/kern/cs_blobs.h` (`CS_VALID 0x1` …
   `CS_LINKER_SIGNED 0x20000`). Blob-getter size semantics are partially
   resolved: an empty entitlements blob returns `rc=0` with no data
   (`docs/security/GROUND_TRUTH.md`); the truncation behaviour for a non-empty
   blob still needs a probe with a real entitlement set.
2. AMFI `mac_syscall` call numbers and payload layouts; which ones are
   observable from user space.
3. Trust-cache module format and whether the vendored `trustcache.c` is
   reusable on x86-64 guests.
4. Notarization ticket location and the minimal `syspolicyd` surface needed
   for a truthful `notarized` bit.
5. SIP protected-path list per macOS version (it changes), and how rootless
   redirects are modeled in Darling's prefix.
6. Which CoreFoundation/Security APIs the League client actually calls, so we
   can bound the surface instead of implementing all of Gatekeeper.

Until each is answered, the corresponding item stays "not implemented" in the
requirement matrix.

---

## 13. Rollout and rollback

| Stage | Default | Rollback |
|---|---|---|
| M2 loader gate | `off`, then `warn` | config to `off` |
| M3 csops | on (truthful state) | state reports zero flags (old behavior was worse) |
| M4 library validation | `warn` | config to `off` |
| M5 page sealing | opt-in via `DARLING_MSEAL=1`; not on by default | unset `DARLING_MSEAL` |
| M6 trust cache | store populated by signing/install; required for runtime LV | **recovery, not rollback:** restore the store and re-run the signer. Removing it disables runtime LV; there is no no-rebuild M6 opt-out. |
| M7 SIP | `off` (`DARLING_SIP_ENFORCE`) | unset or set to `off` |
| M8 AMFI/debug | unknown AMFI `ENOTSUP`; debug gate active under `enforce` | use `off`/`warn` or self/root |
| M10 default | `enforce` after soak | config to `warn` |

Nothing in this plan requires a rebuild to turn off; that is a hard
requirement for field debugging.

---

## 14. References

- Issue #49 — remaining work and requirement list.
- `docs/security/M9_M10_MAC_NEXT_STEPS.md` — the Mac/reference-machine handoff.
- PR #50 / `docs/security/SIP_AMFI_ENFORCEMENT.md` — short design note.
- `docs/reviews/pr41-cpuid-security-validation.md` — current truthful-API state.
- Apple: Code Signing Guide; System Integrity Protection Guide; trust caches
  (Platform Security Guide).
- XNU: `bsd/sys/codesign.h`, `bsd/sys/csr.h`, `bsd/kern/kern_cs.c`,
  `osfmk/kern/cs_blobs.h`, `osfmk/kern/task.c` (cs_blob attachment),
  `osfmk/arm/trustcache.c`.
- Apple open source: `Security` (`libsecurity_codesigning`), `dyld`.
- Darling: `src/startup/mldr/mldr.c`,
  `src/external/xnu/darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl/misc/{csops,mac_syscall,csrctl}.c`,
  `src/external/darlingserver/duct-tape/`, `build/src/tools/codesign`.
- Ground truth without a Mac: `tests/security/fixtures/README.md`,
  `tests/security/probe/ground_truth.c`,
  `.github/workflows/security-ground-truth.yml`,
  `tests/security/ground_truth_to_golden.py`,
  `docs/security/GROUND_TRUTH.md`,
  `tests/security/fixtures/macos-golden.json`.
