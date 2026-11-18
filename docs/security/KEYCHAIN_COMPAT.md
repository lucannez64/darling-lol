# Darling software Keychain compatibility

## Environment and baseline

The x86-64 probe used Darling revision `60ba801decee7a00782f74f6be4c8ffb013f79ff`, the project overlay, and the SDK at `/usr/local/libexec/darling`. Each run used a fresh `DPREFIX`. The probe creates only synthetic items.

`llvm-nm` found the following exports in both the installed and build-tree Security binaries. The public API for signing is `SecKeyCreateSignature`.

| Export | Installed baseline behavior |
|---|---|
| `SecItemAdd`, `SecItemCopyMatching`, `SecItemUpdate`, `SecItemDelete` | Implemented entry points. Data-protection queries go to the modern securityd XPC path. The private prefix could not reach that service. |
| `SecKeyCreateRandomKey` | P-256 generation reached the `ccec_generate_key_fips` stub and failed with `errSecParam` (-50). A Secure Enclave request crashed. |
| `SecKeyCreateSignature` | Exported. The modern EC descriptor calls the `ccec_sign` stub. |
| `SecKeyVerifySignature` | Exported. The EC descriptor has a corecrypto verification implementation. The baseline could not produce a software key and signature for this sequence. |
| `SecKeyCopyExternalRepresentation`, `SecKeyCreateWithData` | Exported, with EC descriptor code for external byte representations. |

The standalone smoke probe returned `errSecNotAvailable` (-25291) for data-protection add, fetch, and delete. Its legacy generic-password add returned -60008, and legacy fetch and delete returned `errSecItemNotFound` (-25300). The first acceptance run captured the create process's SIGSEGV at the Secure Enclave call. The reopen process recorded 17 failed assertions because the items were absent. Evidence is under `build/keychain-compat/baseline/` and `build/keychain-compat/exports.txt`. The `build/` paths in this note are local run outputs and are not tracked; `tools/run-keychain-compat.py` regenerates them.

## Implemented behavior

- `SecItem.cpp` routes `kSecUseDataProtectionKeychain = true` queries for `kSecClassGenericPassword` and software P-256 `kSecClassKey` items to `DarlingKeychain.m`. The store supports add, attribute matching, update, and delete. It returns password data and reconstructs `SecKeyRef` values. Duplicate adds return `errSecDuplicateItem` (-25299). Absent items return `errSecItemNotFound` (-25300).
- `SecECKey.m` keeps the vendored SecKey descriptors and uses `DarlingP256.c` for P-256 generation, import validation, and X9.62 DER ECDSA. Public export is the 65-byte uncompressed X9.63 point. Private export appends the 32-byte scalar. The existing Security algorithm adapters handle message hashing. The backend uses Darling's bundled OpenSSL `crypto098` target. A standalone probe passed 25 generation and sign/verify cycles with this library. The installed LibreSSL variants returned invalid points in the same probe. The `cli_gui_common` component now installs the crypto dependency alongside Security.
- `SecKey.m` routes explicitly permanent software keys to the same store and returns `errSecNotAvailable` for the Secure Enclave token. Permanent generation reaches the store only when the parameters also set `kSecUseDataProtectionKeychain`; `kSecAttrIsPermanent` alone keeps the stock path. When the private half of a permanent pair cannot be stored, the public half is removed again.
- The store claims a query only when it can answer it. `kSecAttrAccessGroup` is stored, is part of the item identity, and limits find, update and delete; an item stored without a group is reached only by queries that name no group. A search for `kSecAttrSynchronizableAny` places no constraint. An access-control object with an unconditional default and no application password is reduced to its protection class. Anything else, such as RSA keys, persistent references, synchronizable items, or passcode, biometry and application-password constraints, is left to the stock Security path and keeps its previous status (`errSecNotAvailable` in a private prefix). A malformed supported query, such as a match limit on delete or a match-all search that requests password data, returns `errSecParam` as on macOS.

The store is a versioned binary property list at `$DPREFIX/Users/<guest-user>/Library/Keychains/Darling/items-v1.plist`. Passwords use service, account and access group as their identity. Keys use the key class, type, size, application label, application tag and access group. Capability attributes such as `kSecAttrCanSign` are stored as the caller supplies them. The store uses a private directory, owner-only regular files, a separate advisory lock, and atomic file replacement. It stores password data and exported keys in plaintext. Accessibility values are matching metadata. The supported path uses local files and guest user permissions. Legacy Keychain queries, constrained access-control objects, and synchronization remain outside this compatibility surface.

The source changes are recorded in `patches/security.patch`, `patches/darling.patch`, and `upstream/manifest.json`.

## Validation

```sh
cmake --build build/darling-src/build --target Security_x86_64 --parallel 2
tools/run-keychain-compat.py
python3 scripts/prepare.py --check-only
```

The runner stages the rebuilt Security dylib for the private guest process and checks the path reported by `dladdr`. The installed SDK can be measured with `--installed`. The final report is `build/keychain-compat/darling.json`, with separate create and reopen logs.

| Case | Result |
|---|---|
| Software P-256 generation | Pass for After First Unlock and When Unlocked |
| Add and fetch the key by attributes | Pass for both keys |
| Generic-password CRUD | Pass, including duplicate and missing-item statuses |
| Sign, verify, export and reconstruct | Pass for both keys. Signatures fail verification against a different message. |
| Process restart | Pass. A second process recovered the same two public-key fingerprints and the updated password, then deleted all three items and checked that they were absent. |
| Secure Enclave token | Pass. Returns `errSecNotAvailable` (-25291). |
| Client query forms | Pass. Permanent generation through `kSecPrivateKeyAttrs`, generation with an unconstrained access-control object, access group, `kSecAttrSynchronizableAny`, and `ThisDeviceOnly` protection work. Persistent-reference and RSA queries keep the stock status. |

The create process passed 46 assertions, and the reopen process passed 17. The runner's cross-process fingerprint check passed. Total failures were **0**. Darling leaves an empty root-owned overlay work directory under `/tmp/darling-keychain-*` after each run; the runner removes everything else.

| Regression probe | Installed baseline | Patched Security |
|---|---|---|
| Foundation compatibility | 0 failures | 0 failures, identical report |
| Identity consistency | 0 anomalies | 0 anomalies, identical report |
| Signature check | 18 observed statuses | All 18 identical. Basic validation of the synthetic ad-hoc signed fixture returned 0 in both runs. |

The signature runner normally opens the live prefix. For this comparison it ran in a fresh private prefix against `/bin/ls`, `/bin/echo`, and a synthetic signed Mach-O. The before and after data and the private-prefix invocation harness are under `build/keychain-compat/`.

A fresh-prefix boot of Riot Client `143.0.8.5295` with this Security build reached the login UX and logged no `DesktopKeystore:` lines, because the client's attestation gate closes before any keystore call (see `ATTESTATION_143_FINDINGS.md`, section 6). A logged-in session and a match with this build are **pending, needs user run**.
