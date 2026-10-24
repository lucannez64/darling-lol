# macOS ground truth

Collected by `.github/workflows/security-ground-truth.yml` running `tests/security/probe/ground_truth.c` on real GitHub macOS runners. The probe is read-only: it calls `csops`/`csrctl` and runs `codesign -dvvv`, never signs or modifies anything.

Source run: `40124165591`.

> Caveat: runner VMs boot with SIP disabled (`csrutil status`), so
> these values describe a CI VM, not a stock Mac. Flags and ABI
> shapes are still representative; SIP enforcement is not.

## Environments

| Runner | macOS | Build | Darwin | Arch | SIP | CSR config |
|---|---|---|---|---|---|---|
| macos-14 | 14.8.9 | 23J631 | 23.6.0 | arm64 | disabled | `0x0000006f` |
| macos-15 | 15.7.9 | 24G830 | 24.6.0 | arm64 | disabled | `0x0000006f` |

## Probe observations

| Runner | `CS_OPS_STATUS` | Decoded flags | teamid | entitlements | child query |
|---|---|---|---|---|---|
| macos-14 | `0x22020001` | CS_VALID, CS_LINKER_SIGNED, CS_DYLD_PLATFORM, CS_SIGNED | rc=-1 errno=2 | 0 bytes | matches self |
| macos-15 | `0x22020001` | CS_VALID, CS_LINKER_SIGNED, CS_DYLD_PLATFORM, CS_SIGNED | rc=-1 errno=2 | 0 bytes | matches self |

A linker-signed binary with no team therefore reports `CS_VALID|CS_LINKER_SIGNED|CS_DYLD_PLATFORM|CS_SIGNED` and `ENOENT` for `CS_OPS_TEAMID`. An empty entitlements blob returns success with length 0.

## Platform binaries — macos-14

| Path | CDHash | Platform | Team ID | Entitlements |
|---|---|---|---|---|
| `/bin/ls` | `df6d7e0863cb3037b01ca57547481aaf3aa81293` | 15 | not set | none |
| `/usr/bin/true` | `fd06104a5e4ab5c5275fea8defcf241d890d6764` | 15 | not set | none |
| `/usr/lib/dyld` | `9b1f7f11b41c273d184f6aa9ccc85497e2988299` | 15 | not set | none |
| `/usr/bin/security` | `ee745a08e05d0b4b6a1a31838be485e71c07ac6e` | 15 | not set | 2 keys |
| `/usr/sbin/spctl` | `656143560504407d4d542f959958cda36077e843` | 15 | not set | 1 keys |
| `/usr/libexec/amfid` | `a1ab080e99109cd31bd6f00d6ff0a5abb88cc1e4` | 15 | not set | 6 keys |
| `/usr/libexec/trustd` | `fab03735005b30e4f6646102e3c44afc3d69c34a` | 15 | not set | 13 keys |

`/usr/bin/security` entitlements:

- `com.apple.authorization.smartcard.override`
- `com.apple.private.CoreAuthentication.SPI`

`/usr/sbin/spctl` entitlements:

- `com.apple.private.iokit.nvram-csr`

`/usr/libexec/amfid` entitlements:

- `com.apple.private.amfi.developer-mode-control`
- `com.apple.private.iokit.nvram-read-access`
- `com.apple.private.iokit.nvram-write-access`
- `com.apple.private.security.storage.amfid`
- `com.apple.private.tcc.allow`
- `com.apple.security.exception.iokit-user-client-class`

`/usr/libexec/trustd` entitlements:

- `application-identifier`
- `com.apple.application-identifier`
- `com.apple.private.amfi.version-restriction`
- `com.apple.private.assets.accessible-asset-types`
- `com.apple.private.keychain.certificates`
- `com.apple.private.necp.match`
- `com.apple.private.network.delegation-whitelist`
- `com.apple.private.network.socket-delegate`
- `com.apple.private.security.storage.SFAnalytics`
- `com.apple.private.security.storage.trustd`
- `com.apple.private.security.storage.trustd-private`
- `com.apple.private.tcc.manager.check-by-audit-token`
- `com.apple.private.trustd.FileHelp`

## Platform binaries — macos-15

| Path | CDHash | Platform | Team ID | Entitlements |
|---|---|---|---|---|
| `/bin/ls` | `72efae0707353da45c87a019a5ddd40bd3dc59f5` | 16 | not set | none |
| `/usr/bin/true` | `b14712f93da0dd2af5296acdd82ceb164026a5a7` | 16 | not set | none |
| `/usr/lib/dyld` | `b3d1812132e80c85347ca357a3b1c4ba2badf258` | 16 | not set | none |
| `/usr/bin/security` | `09f808b5a2a72cbc15f2a6058829557ec9c39362` | 16 | not set | 3 keys |
| `/usr/sbin/spctl` | `116e663773751f656061ca4d7a63405dd91cb2f8` | 16 | not set | 1 keys |
| `/usr/libexec/amfid` | `a2f13f7d01ca432ed73360e6932a7c28b73aa4b6` | 16 | not set | 7 keys |
| `/usr/libexec/trustd` | `a5f41fd75285dce4236dfe3fb8fd872cea58dcb5` | 16 | not set | 14 keys |

`/usr/bin/security` entitlements:

- `com.apple.authorization.smartcard.override`
- `com.apple.private.CoreAuthentication.SPI`
- `com.apple.private.platformsso.security`

`/usr/sbin/spctl` entitlements:

- `com.apple.private.iokit.nvram-csr`

`/usr/libexec/amfid` entitlements:

- `com.apple.developer.hardened-process`
- `com.apple.private.amfi.developer-mode-control`
- `com.apple.private.iokit.nvram-read-access`
- `com.apple.private.iokit.nvram-write-access`
- `com.apple.private.security.storage.amfid`
- `com.apple.private.tcc.allow`
- `com.apple.security.exception.iokit-user-client-class`

`/usr/libexec/trustd` entitlements:

- `application-identifier`
- `com.apple.application-identifier`
- `com.apple.private.amfi.version-restriction`
- `com.apple.private.assets.accessible-asset-types`
- `com.apple.private.assets.bypass-asset-types-check`
- `com.apple.private.keychain.certificates`
- `com.apple.private.necp.match`
- `com.apple.private.network.delegation-whitelist`
- `com.apple.private.network.socket-delegate`
- `com.apple.private.security.storage.SFAnalytics`
- `com.apple.private.security.storage.trustd`
- `com.apple.private.security.storage.trustd-private`
- `com.apple.private.tcc.manager.check-by-audit-token`
- `com.apple.private.trustd.FileHelp`

## What this establishes

- Exact `csops` status flags for a locally built, linker-signed binary.
- `CS_OPS_TEAMID` failure mode (`-1`/`ENOENT`) when no team exists.
- Empty-entitlements semantics.
- Cross-process `csops` works when SIP allows `task_for_pid`.
- Per-macOS-build cdhashes and platform identifiers for Apple binaries.
- Real entitlement keys from `amfid` and friends.

## What it does not establish

- SIP enforcement: the runners boot with SIP disabled.
- Hardened-runtime behaviour of a notarized third-party app.
- Notarization tickets (no public notarized fixture here).
- Intel behaviour: the matrix is arm64 only.
