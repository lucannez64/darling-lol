# Code signing, SIP and AMFI enforcement

Status: **in progress / design** — this document tracks the work that turns
Darling's currently *truthful but unenforced* security responses into real
enforcement. It is the roadmap behind the tracking issue and the draft pull
request that carries it. Nothing here claims to be implemented until the
acceptance test for that item exists and passes.

## Where we are

The runtime no longer fabricates security results (see
`docs/reviews/pr41-cpuid-security-validation.md`):

- `csops(CS_OPS_STATUS)` returns `CS_VALID` only when something actually
  validated the image. Today that is **never**, so it returns zero flags.
- Unsupported signing queries and setters return errors instead of success.
- `__mac_syscall("AMFI", ...)` clears its output and returns `ENOTSUP`.
- `__csrctl` reports SIP as disabled with validated inputs.
- `tools/verify-darling-code-signature.py` performs a bounded, read-only
  Mach-O seal/CMS check **on disk**.

Those changes removed false positives. They did **not** add enforcement, and the
privacy/safety boundary is deliberate:

> This is a draft foundation, not a full SIP/AMFI implementation. It does not
> enforce signing at exec/dlopen, protect running pages, implement Apple's
> certificate/entitlement/notarization policy, load an authenticated Apple
> trust cache, or provide kernel-enforced SIP filesystem/debug restrictions.
> The standalone verifier is not a launch gate and does not establish a task's
> signing state. Callers that depended on fabricated signing or no-op AMFI
> success may behave differently; the production runtime has not been replaced.
> Full enforcement prerequisites are documented instead of claimed as
> implemented.

## Principles

1. **Fail closed.** A gate blocks on error; it never degrades to "allow" because
   a check could not run.
2. **No fabricated state.** A task is signed only if a verification step set
   that state for that task. A file on disk is not a live task.
3. **Opt-in, then default.** New gates land behind an explicit policy
   (env/config) with enforcement off by default until the acceptance test and a
   match test pass.
4. **One acceptance test per item.** An item moves to *done* only when its
   named test exists and passes, including a negative (tamper) case.
5. **Documented gaps stay documented.** Nothing is checked off while it is only
   partially true.

## Requirement matrix

| # | Requirement | Today | Primary seam | Acceptance test |
|---|---|---|---|---|
| 1 | Launch gate over the on-disk snapshot | verifier is a standalone tool | `tools/verify-darling-code-signature.py`, `scripts/run.sh`, `tools/run-riot-client.sh` | gate test: tampered image refuses to launch under enforcement |
| 2 | Task-owned signing state (`CS_VALID`) | always zero | `mldr.c`, `csops.c` | probe: status reflects the loader's result, not a filename |
| 3 | Enforce verification at exec / dlopen | not done | `patches/darling.patch` → `src/startup/mldr/mldr.c` | gate test: bad image aborts exec; good image loads |
| 4 | Protect running pages (page integrity) | not done | xnu VM mapping/seal, `mmap`/write paths | test: writing a sealed page fails; unchanged image runs |
| 5 | Apple certificate/entitlement/notarization policy | not done | `patches/security.patch` → `libsecurity_codesigning/lib/{StaticCode.cpp,reqparser.cpp}` | test: requirement/OID/EKU/revocation/notarization cases |
| 6 | Authenticated Apple trust cache | local pin list only | `mac_syscall.c` AMFI, trust-cache source | test: authenticated entry accepted, forged entry rejected |
| 7 | Kernel-enforced SIP filesystem paths | disabled flag only | `csrctl.c`, xnu VFS (rename/link/mmap/metadata) | test: protected path operations denied under SIP |
| 8 | Debug/injection restrictions, privileged-op mediation | not done | xnu task/port + privilege paths | test: unauthorized debug/inject denied |
| 9 | Migrate callers that relied on fabricated success | unknown | call sites of `csops`/AMFI/Security | audit + regression run |

## Staged plan

- **Stage 0 (this PR).** Land this document and the tracking issue. No behavior
  change.
- **Stage 1 — Snapshot launch gate.** Wire the verifier into the host launch
  path behind `DARLING_CODESIGN_ENFORCE`; refuse to start when the target fails
  policy. *Honest limit:* gates the bytes on disk, not the loaded image; TOCTOU
  remains. Covers item 1.
- **Stage 2 — Task signing state.** Give `csops` a per-task state that a
  verification step sets; `CS_OPS_STATUS` reports it. Still no enforcement
  unless Stage 3 lands. Covers item 2.
- **Stage 3 — Loader gate (exec/dlopen).** Verify in `mldr` before mapping;
  abort the image on failure; set the task state from the same result; cover
  `dlopen`. Covers items 3 and 5 (partial).
- **Stage 4 — Page integrity.** Seal code pages against later writes/maps;
  handle fork/exec and library loads. Covers item 4.
- **Stage 5 — Apple policy.** Certificate OIDs/EKUs, designated requirements,
  revocation, timestamping, notarization, resource envelopes. Covers item 5.
- **Stage 6 — Authenticated trust cache.** Load real, authenticated entries with
  the correct CDHash/generation. Covers item 6.
- **Stage 7 — SIP enforcement.** Protected-path semantics for rename/link/mmap/
  metadata and root/host access; privileged-operation mediation; debug/injection
  restrictions; a defined policy root and administrator model. Covers items 7-8.
- **Stage 8 — Production switch.** Replace the fabricated-success callers, run a
  full match with enforcement on, and only then consider defaults. Covers item 9.

## Non-goals and honesty constraints

- A local SHA256 pin list is **not** Apple's signed trust cache and must never be
  reported as such.
- A host-side wrapper is **not** kernel-enforced SIP; Stage 1 must say so.
- The standalone verifier is not a task's signing state; only the loader can set
  that.
- CPUID trapping remains out of scope here (see the CPUID section of
  `docs/reviews/pr41-cpuid-security-validation.md`).

## References

- `docs/reviews/pr41-cpuid-security-validation.md` — what the current code does.
- `tools/verify-darling-code-signature.py` — the snapshot verifier.
- `upstream/manifest.json` — pinned revisions and per-component patch scopes.
- Apple: [Code Signing Guide](https://developer.apple.com/library/archive/documentation/Security/Conceptual/CodeSigningGuide/AboutCS/AboutCS.html),
  [System Integrity Protection](https://developer.apple.com/library/archive/documentation/Security/Conceptual/System_Integrity_Protection_Guide/FileSystemProtections/FileSystemProtections.html),
  [trust caches](https://support.apple.com/guide/security-pdf/trust-caches-sec7d38fbf97/web).
