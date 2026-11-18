# KeyPair attestation status in Riot Client 143 (macOS build)

Findings from static analysis of the shipped `RiotGamesApi` x86_64 slice and
from runtime observation of the unmodified client under Darling. This note
only documents what the code does. It contains no client modifications and
no guidance for changing any outcome.

## Scope and artifacts

| Artifact | Value |
|---|---|
| Build | Riot Client `143.0.8.5295` (launcher CL 5295, release `07AAF191150F5E09`) |
| Analyzed image | `RiotGamesApi` x86_64, sha256 `a094db97d1d5d4aa…` |
| Comparison image | `141.0.7.5129` framework, x86_64 sha256 `68ae2d3ec20ff26f…` |
| Evidence bundles | `build/keychain-compat/darling.json` (probe), e2e logs under `build/e2e-rc143/` (local run outputs, not tracked) |
| Disassembly | rizin; all addresses below are x86_64 file vaddrs in the 143 image |

Runtime corroborating logs: the issue #31 report bundle (TR region, Darling)
and a fresh-prefix end-to-end boot on this machine (EUW, no login).

## 1. The status decision

The client's keypair/attestation state machine starts in
`OnKeyPairConfigUpdate` (`0x17a2f50`). It calls the status function
`0x17a7720`, whose decision — reconstructed from the disassembly — is:

```c
status = 0;                                  // "unknown"

if (state->platform_gates_ok) {             // 0x888/0x889/0x88a/0x88d/0x88c
    if (state->early_attestation_ran) {     // byte 0x780 == 1   ← checked FIRST
        status = 2 - state->early_result;    // byte 0x760: 1 = attested
        goto store;
    }
    // fallback: consult the crypto provider
    if (state->provider == NULL) { status = 2; goto store; }
    if (state->provider->vt[0x68]() == 0     // platform-capable: hardcoded TRUE
        && !state->flag_0x7c8)
        { status = 2; goto store; }
    if (state->cfg_attestation == "bypass")  // string at +0x870
        { status = 1; goto store; }          // logged: "Attestation bypass configured"
    if (state->flags_0x960_962_961_all_set)
        { status = 2; goto store; }
    r = state->provider->vt[0x60]();          // ← the new RC 143 consult
    status = 2 - r;                           // helper 0x17a76d0
}
store:
    out->status = status;                    // 1 = supported, 2 = not supported
```

The caller logs `Attestation not supported` whenever `status == 2`
(`0x17a3f78` in the 143 image; identical compare in the 141 image at
`0x17cedb8`). The reason code at `out+4` uses the same result byte.

## 2. The provider's capability methods are constants

The provider object at `state->0x868` is created by
`factory 0x105a940 → env 0x117c3b0` with **no platform check anywhere in
construction** (the env creator returns success unconditionally). The
vtable at `0x02f21080` contains:

| slot | address | body |
|---|---|---|
| `+0x10` | `0x105aa40` | returns the curve name `"secp256r1"` (real software crypto) |
| `+0x20` | `0x105aaa0` | algorithm dispatch ("Requested algorithm not supported…") |
| `+0x60` | `0x105bd70` | **`xor eax,eax; ret` — always 0** |
| `+0x68` | `0x105bd80` | `mov al,1; ret` — always 1 |
| `+0x78` | `0x105bd90` | "Not implemented for this platform" |

So in this build the fallback consult `vt[0x60]` always reports the device
attestation capability as **unavailable**, on every machine.

## 3. The early-attestation path is a closed loop

The only code that sets the early flags (`0x760` result, `0x780` ran-flag;
writers at `0x17a9996`/`0x17a99c0`/`0x17a99f8`/`0x17a9a46`) is
`TryEarlyAttestation` (`0x17a9060`). Its entry:

```c
TryEarlyAttestation(out, state, tag):
    status = computeStatus(state);            // 0x17a9092 — same function as §1
    if (status == 11 || status == 12) { out->status = 1; return; }
    if (status != 1) {                        // 0x17a9191
        emit "KeyPair Account is not supported on this device "
             "or plugin is not initialized";
        return;                               // never sets 0x780/0x760
    }
    ... credentials (0x17a81e0, "keypair_credentials"),
        keystore (0x17a8ac0), provider crypto (vt[0x70]) ...
    state->early_result = result;             // 0x760
    state->early_attestation_ran = 1;          // 0x780
```

The runner requires the very status it is the only one allowed to produce.
With `vt[0x60] == 0` and no remote `attestation` bypass key (§4), no unmodified entry point (config update, the async
`AttestationStart` operation, the `status == 1` continuation) can reach the
attestation work: every path funnels into the same status gate.

## 4. The gate is remote-configurable — the `"attestation"` key

`OnKeyPairConfigUpdate` applies a settings dictionary delivered by the
SDK config service — the same channel that serves
`keystone.keypair_player.deferred_report_public_jwk_in_telemetry`,
visible in every client log. Applied keys (`0x17a2f50`):

```
report_public_jwk_in_telemetry          → telemetry flag
report_hardware_supported_curves_in_telemetry → telemetry flag
attestation                             → state->cfg_attestation (+0x870)
```

and `cfg_attestation` is the exact string compared against `"bypass"` in
§1. In other words:

```c
if (platform_gates_ok && !early_attestation_ran && provider != NULL
        && (provider->vt[0x68]() || flag_0x7c8)
        && remoteConfig["attestation"] == "bypass")
    status = 1;      // logged as "Attestation bypass configured"
```

Whether that key is served to a given population is Riot's server-side
choice and is not observable from the binary. In every observed Darling
log (issue #31 bundle and the fresh-prefix e2e boot) the keypair config
carries only `deferred_report_public_jwk_in_telemetry`; no `attestation`
key arrives, and the initial check reports "not supported" from the first
login onward. A client that *does* receive the key shows a distinct,
observable signature: the `Attestation bypass configured` line followed
by attestation activity (`AttestationStart`/`Attestation succesfully
done` or `Local attestation failed`). No such signature has been reported
from any macOS client on this build.

## 5. What changed from 141 to 143

The 141 image contains an unconditional capability-flag store in its init:

```
0x17cd3d9:  mov dword [state+0x8a8], 0x01000000   ; → flag 0x8ab := 1
```

and its status function gates "supported" on that flag (plus the same
provider `vt[0x68]` consult). RC 143 removed the static flag and replaced
it with the live `vt[0x60]` consult plus the remote `attestation` config
override, in the same release that removed the
`/keypair-account/v1/external-attestation` API surface.

```text
141: supported  = flag_0x8ab (set to 1 in-binary at init)
143: supported  = platform gates pass, and then in order:
                  the early result if early attestation ran,
                  else (provider present) cfg_attestation=="bypass"
                  or provider->vt[0x60]()        ← 0 in the shipped image
```

## 6. Runtime corroboration

- Fresh-prefix e2e boot of the unmodified, self-updated `143.0.8.5295`
  under Darling (rebuilt Security/Keychain active, `dladdr`-verified in
  the keychain probe): client reaches config fetch over TLS, foundation
  handoff, login UX — and logs the same `OnKeyPairConfigUpdate:
  Attestation not supported` (`attestationState:
  InitialCheckAttestationSupportFailed`, `keypairSupported: false`) with
  zero `DesktopKeystore:`/`TryEarlyAttestation` lines. The early path
  never ran: its own status re-check turned it away before any keystore
  call — identical to what any client without the remote key would do.
- Both issue #31 reports played normally for ~48 h (≈43 and ≈20 matches)
  with the gate closed from first login; the later
  `UNSUPPORTED_THIRD_PARTY_TOOLS` warning is a separate Vanguard
  environment detection (delivered via the `gaps-mailbox` penalty
  notification) and is not an attestation verdict. Login and gameplay
  proceed through the normal non-keypair RSO path in all observed cases.

## 7. Conclusion for issue #31

In the shipped macOS `143.0.8.5295`, `Attestation not supported` is the
default state of the keypair attestation gate for any client that does
not receive the remote `attestation` config key — including real Macs.
It is not a Darling-specific regression, and nothing in the observed
Darling environment changes the verdict: the deciding consult is a
constant in the image, and the re-enable path is a server-side setting.
The message is cosmetic for login and gameplay: sessions authorize via
the standard RSO flow (`keypairSupported: false` in the session
lifecycle telemetry), as both issue #31 report bundles show. The
fresh-prefix e2e boot stopped at the login UX, so it supports only the
attestation state, not the authorization path.

The falsifier for this model is a real-Mac login log on this exact
release that shows no attestation lines at all, or attestation activity
without the `Attestation bypass configured` line: either would contradict
the "not supported" default. A log with the `Attestation bypass
configured` signature would not contradict the model; it would confirm
that the remote key is served to some population.
