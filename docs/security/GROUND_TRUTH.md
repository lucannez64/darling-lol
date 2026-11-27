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

## What a process sees of itself

A second probe, `tests/security/probe/self_view.c`, records what a process can read about itself: its memory regions, dyld state, `proc_pidinfo` and `task_info` answers, a few filesystem paths and the names of some environment variables. It describes its own process; of other processes it reads only the name and path of its parent and of pids 0 and 1, the number of pids the system lists and whether 0 and 1 are among them. It prints nothing that names a machine or a user: fixed system locations are printed in full, any other path only as its kind (`<home>`, `<application>`, `<tmp>`, `<other>`). The same file builds against an old SDK, so it can run unchanged inside Darling and the two JSON outputs can be diffed.

The workflow runs it natively (arm64) and, under Rosetta, as x86_64; it also stores `sysctl -a` with the host name and per-boot identifiers blanked.

Run: `45925926141` (macOS 15.8.2, 24H232, `macos-15` runner).

| Observation | arm64 | x86_64 under Rosetta |
|---|---|---|
| `mach_vm_region_recurse` counts accepted | 12, 16, 17, 19; 20 is answered as 19; 0 is `KERN_INVALID_ARGUMENT` | same |
| words written | exactly the count asked for | same |
| past the last region | `KERN_INVALID_ADDRESS` | same |
| main executable `__TEXT` (long structure) | protection `r-x`, maximum `r-x`, inheritance copy, `share_mode` `SM_COW`, `external_pager` 1, `shadow_depth` 1, `ref_count` 4, `pages_resident` 2, hashed `object_id` | same shape |
| regions in a scanner-style walk | 52 | 366 |
| executable regions | 3, all inside a dyld image, 2 of them in the shared cache | 22: 4 in an image, 12 in the shared cache, 8 in neither (the translation runtime) |
| share modes seen | `SM_COW`, `SM_PRIVATE`, `SM_EMPTY`, `SM_SHARED`, `SM_TRUESHARED` | same |
| `user_tag` on anonymous memory | malloc tags (1, 2, 7, 11, 12), stack (30), others | same kind |
| `mach_vm_region` flavors `BASIC_INFO_64`, `EXTENDED_INFO`, `TOP_INFO`; `vm_region_64`; `vm_region_recurse_64` | all succeed | all succeed |
| dyld shared cache | UUID, range and file path present; `_dyld_shared_cache_contains_path("/usr/lib/libSystem.B.dylib")` true while that path does not exist on disk | same |
| `dyld_all_image_infos` | version 17, `dyldVersion` `1286.12`, cache base and slide non-zero | same |
| `PROC_PIDTBSDINFO` | flags `0x404010` (`PROC_FLAG_LP64` set), start time set, status 2 | `0x2404010` |
| `PROC_PIDREGIONINFO`, `PROC_PIDREGIONPATHINFO`, `PROC_PIDREGIONPATHINFO2`, `PROC_PIDVNODEPATHINFO`, `proc_pid_rusage` | succeed; `PROC_PIDREGIONPATHINFO3` is `EINVAL` | same |
| `kern.proc.pid` | `P_LP64` set, `p_starttime` set | same |
| `kern.procargs2` | the environment follows argv | same |
| threads | `task_threads` count equals `pti_threadnum` (1) | equal (2) |
| `task_info` | every flavor tried succeeds; all eight audit-token words non-zero; `phys_footprint` non-zero | same |
| `/proc`, `/sys`, `/run`, `/dev/shm`, `/Volumes/SystemRoot` | absent | absent |
| `/System/Library/Extensions`, `/System/Volumes/Preboot`, `/System/Volumes/Data`, `/System/Cryptexes` | present | present |
| `statfs("/")` | `apfs` | `apfs` |

What this establishes: the structure sizes and return codes of the region calls, what real region records contain, which `proc_pidinfo` and `task_info` flavors answer, and the dyld shared-cache state a current macOS reports. What it does not: the runner is an arm64 VM, so the x86_64 column is a translated process (its eight executable regions outside any image belong to the translation runtime) and hardware values in the `sysctl` file are the VM's, not a Mac's.
