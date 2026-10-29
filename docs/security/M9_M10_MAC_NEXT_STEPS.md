# M9/M10: what we need from a real Mac

**Status:** local preparation is complete in PR #144, but Apple-policy and
match acceptance cannot be truthfully completed on Linux. This document is a
concrete, copy-pasteable handoff for a maintainer or contributor with access to
a real Mac. Please help us close the last honest gap in #49.

We are **not** asking you to claim success or to bypass Apple policy. We need
observed ground truth and reproducible fixtures. If a step cannot be completed
safely, stop and report what was observed; a documented gap is better than a
fabricated `notarized=true`.

## What already exists

- `tools/dc-codesign` and `mldr` verify CodeDirectory page hashes, special
  slots (requirements/XML/DER entitlements), team IDs and library validation.
- M5 seals `__TEXT`/`__LINKEDIT`; M6 requires a root-owned runtime trust store
  outside the prefix; M7 covers the mutating SIP VFS paths; M8 enforces
  task-port/debug and cross-process write policy in darlingserver.
- `tools/dc-notary.py` handles quarantine xattrs and detects the `t8lr` ticket
  trailer, but **never** marks a ticket validated.
- `tools/dc-cms-verify.py` verifies a CMS signature and signer chain against a
  **local** CA, and reports `content_binding=unverified`.
- `tools/security-soak.py` runs an enforce-mode smoke set and analyzes logs.
- Existing acceptance suite: parser 12/12, gate 11/11, signer 6/6, csops 3/3,
  LV 14/14, M5 1/1, trust store 9/9, SIP 3/3, AMFI 7/7, M9 prep 7/7,
  M10 prep 2/2.

## What we need from macOS (M9)

Please use a stock Mac with SIP enabled for the observations and keep the
commands/read-only outputs. Do not disable SIP except for the explicitly
labelled comparison runs.

### 1. Record the environment

```sh
sw_vers
uname -a
csrutil status
system_profiler SPSoftwareDataType | head
xcode-select -p
codesign --version
```

Save the output as `tests/security/fixtures/macos/env-<build>.txt`.

### 2. Probe Gatekeeper/notarization behavior

Use a **notarized and stapled** app or DMG supplied by the project (or a
publicly redistributable one), plus a copy with the ticket stripped. For each
fixture, record:

```sh
file "$FIXTURE"
xattr -l "$FIXTURE"
codesign -dvvv --entitlements :- "$FIXTURE" 2>&1
codesign --verify --deep --strict --verbose=4 "$FIXTURE"
spctl --assess --type execute --verbose=4 "$FIXTURE"
stapler validate "$FIXTURE"
```

For an app bundle, repeat `codesign -dvvv` on the main executable and inspect
`Contents/CodeResources` and any stapled ticket location. For a DMG, record
where `stapler` stores the ticket. A short note in
`tests/security/fixtures/macos/README.md` explaining exactly where the ticket
was found is the key deliverable.

Do the same once with a quarantine xattr. Work on a disposable copy so the
fixture's original quarantine state is never overwritten or removed:

```sh
quarantine_dir=$(mktemp -d)
quarantine_copy="$quarantine_dir/$(basename "$FIXTURE")"
cp -R "$FIXTURE" "$quarantine_copy"
xattr -w com.apple.quarantine '0081;00000000;Probe;00000000-0000-0000-0000-000000000000' "$quarantine_copy"
# launch/assess and record what macOS reports; then discard the copy
rm -rf "$quarantine_dir"
```

### 3. Establish CMS content binding

`dc-cms-verify.py` currently verifies the CMS signature and signer chain but
marks the signed-content binding unverified. We need to know what bytes Apple's
CMS signs for a Mach-O. Please capture a notarized (or Developer ID) binary and
run the probe in `tests/security/probe/` or send us:

- the extracted CMS DER;
- every candidate CodeDirectory/SuperBlob component;
- the exact `openssl cms -verify` command and output that succeeds, if any;
- the `codesign -dvvv` output.

Update `tools/dc-cms-verify.py` only after the observed bytes are documented.
Never replace `content_binding=unverified` with a guess.

### 4. Apple chain policy and trust cache

Record which certificates/chains `spctl` accepts and which it rejects:

```sh
security find-identity -v -p codesigning
security verify-cert -c signer.pem
spctl --assess --type execute --verbose=4 "$FIXTURE"
```

If you can provide a **redistributable, non-secret** notarized fixture and its
certificate-chain metadata, add it under `tests/security/fixtures/macos/` with
checksums and provenance. Do not commit private keys, developer credentials,
Apple-issued trust caches, or personal data.

The local test CA in `test_notary.sh` proves the verifier plumbing only. It is
**not** Apple policy and must never be described as such.

## What we need from macOS/reference hardware (M10)

### 5. Full runtime smoke and game/match soak

With the branch installed and signed (`tools/sign-darling-runtime.sh --sdk`,
then `--prefix` for overlays), run:

```sh
darling shutdown
DARLING_CODESIGN_ENFORCE=enforce ./scripts/run.sh
# launch the Riot Client and League of Legends, play a full match
```

In another terminal, capture the run and analyze it:

```sh
python3 tools/security-soak.py run --smoke --out /tmp/darling-soak
python3 tools/security-soak.py analyze /tmp/darling-soak/*.log
```

For the full match, run under `enforce`, collect the complete Darling logs and
the soak report, and record:

- macOS/Darling/kernel versions;
- `DARLING_CODESIGN_ENFORCE`, `DARLING_SIP_ENFORCE`, trust-store generation;
- every `mldr`/dyld/library-validation rejection or crash;
- whether `DARLING_MSEAL=0`/rollback is needed;
- the final result of a full match with zero security findings.

The M10 acceptance is **a full match on the reference machine with enforcement
on and zero verification failures**, not merely the smoke command. If that
cannot be run, say so explicitly; we will keep #49 open.

### 6. Rootless server path (currently blocked by trust boundary)

The server deliberately disables the trust store in a rootless user namespace:
uid 0 maps to the host user, and a guest can write the same user config path.
Do not enable or claim rootless runtime LV until the project has a host-private
trust boundary. The current automated test covers only the rootless CLI path
and key creation, not a rootless server.

## How to report back

Please open a PR or comment on #49 with:

1. environment/build matrix;
2. fixture paths/checksums and provenance (or a private handoff offer);
3. exact commands and observed outputs;
4. which `notarized`/`content_binding`/trust-cache claims are now supported;
5. remaining failures and whether they are policy or compatibility issues.

If you have a Mac and can run even one section, please do. The project has
taken this as far as it can without real Apple observations, and we would be
very grateful for a careful maintainer or contributor to help us finish the
last step honestly. Thank you.
