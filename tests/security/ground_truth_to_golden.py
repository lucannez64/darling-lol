#!/usr/bin/env python3
"""Turn security-ground-truth artifacts into a golden matrix.

Reads the artifact directory produced by `gh run download` for
.github/workflows/security-ground-truth.yml and writes:

  - a machine-readable golden JSON (fixtures/macos-golden.json)
  - a human-readable markdown report (docs/security/GROUND_TRUTH.md)

Usage:
  ground_truth_to_golden.py ARTIFACT_DIR --json OUT.json --markdown OUT.md
                                [--run-id ID] [--date YYYY-MM-DD]

The probe's own cdhash changes on every build, so only its flags are recorded
as stable facts. Apple binary cdhashes are recorded per macOS build.
"""
import argparse
import json
import pathlib
import re
import sys

FLAG_NAMES = [
    (0x00000001, "CS_VALID"),
    (0x00000002, "CS_ADHOC"),
    (0x00000004, "CS_GET_TASK_ALLOW"),
    (0x00000008, "CS_INSTALLER"),
    (0x00000100, "CS_HARD"),
    (0x00000200, "CS_KILL"),
    (0x00000400, "CS_CHECK_EXPIRATION"),
    (0x00000800, "CS_RESTRICT"),
    (0x00001000, "CS_ENFORCEMENT"),
    (0x00002000, "CS_REQUIRE_LV"),
    (0x00004000, "CS_ENTITLEMENTS_VALIDATED"),
    (0x00010000, "CS_RUNTIME"),
    (0x00020000, "CS_LINKER_SIGNED"),
    (0x02000000, "CS_DYLD_PLATFORM"),
    (0x04000000, "CS_PLATFORM_BINARY"),
    (0x08000000, "CS_PLATFORM_PATH"),
    (0x10000000, "CS_DEBUGGED"),
    (0x20000000, "CS_SIGNED"),
    (0x40000000, "CS_DEV_CODE"),
]


def flag_names(value):
    return [name for bit, name in FLAG_NAMES if value & bit]


def parse_codesign(text):
    sections = {}
    current = None
    for line in text.splitlines():
        if line.startswith("### "):
            current = line[4:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    binaries = []
    for path, lines in sections.items():
        if not path.startswith("/"):
            continue
        body = "\n".join(lines)
        if "No such file or directory" in body:
            continue
        entry = {"path": path}
        m = re.search(r"^CDHash=(\w+)", body, re.M)
        if m:
            entry["cdhash"] = m.group(1)
        m = re.search(r"^Platform identifier=(\d+)", body, re.M)
        if m:
            entry["platform"] = int(m.group(1))
        m = re.search(r"^CodeDirectory v=(\w+)", body, re.M)
        if m:
            entry["cd_version"] = "0x" + m.group(1)
        m = re.search(r"flags=(0x[0-9a-f]+)", body)
        if m:
            entry["cd_flags"] = m.group(1)
        m = re.search(r"^Hash type=(\w+)", body, re.M)
        if m:
            entry["hash_type"] = m.group(1)
        m = re.search(r"^TeamIdentifier=(.+)$", body, re.M)
        if m:
            team = m.group(1).strip()
            entry["teamid"] = None if team == "not set" else team
        keys = [k.strip() for k in re.findall(r"^\s*\[Key\]\s+(.+)$", body, re.M)]
        if keys:
            entry["entitlement_keys"] = keys
        if len(entry) > 1:
            binaries.append(entry)
    return binaries


def parse_env(artifact_dir):
    probe_files = sorted(artifact_dir.glob("ground-truth-*/ground-truth-*.json"))
    envs = []
    for probe_path in probe_files:
        runner = probe_path.parent.name.replace("ground-truth-", "")
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        sign_txt = probe_path.parent / f"codesign-{runner}.txt"
        if not sign_txt.is_file():
            raise SystemExit(f"missing codesign report: {sign_txt}")
        text = sign_txt.read_text(encoding="utf-8", errors="replace")

        sw = {}
        m = re.search(r"ProductVersion:\s*(\S+)", text)
        if m:
            sw["product"] = m.group(1)
        m = re.search(r"BuildVersion:\s*(\S+)", text)
        if m:
            sw["build"] = m.group(1)
        m = re.search(r"System Integrity Protection status:\s*(\w+)", text)
        sip = m.group(1) if m else "unknown"

        self_status = probe["self"]["status"]
        child_query = probe.get("child_query", {})
        child_status = child_query.get("status", {})
        self_ok = self_status.get("rc") == 0
        child_ok = child_status.get("rc") == 0
        flags = int(self_status["flags"], 16) if self_ok else None
        child_flags = int(child_status["flags"], 16) if child_ok else None
        entitlements = probe["self"]["entitlements"]

        envs.append(
            {
                "runner": runner,
                "macos": {
                    "product": sw.get("product"),
                    "build": sw.get("build"),
                    "darwin": probe["uname"]["release"],
                    "arch": probe["uname"]["machine"],
                },
                "sip": sip,
                "csr": {
                    "config": probe["csr"]["config"],
                    "check_task_for_pid": probe["csr_check"]["task_for_pid"],
                    "check_debugger": probe["csr_check"]["debugger"],
                },
                "probe": {
                    "status_rc": self_status.get("rc"),
                    "status_errno": self_status.get("errno", 0),
                    "status_flags": self_status.get("flags") if self_ok else None,
                    "flag_names": flag_names(flags) if self_ok else [],
                    "teamid": {
                        "rc": probe["self"]["teamid"]["rc"],
                        "errno": probe["self"]["teamid"]["errno"],
                    },
                    "entitlements": {
                        "rc": entitlements.get("rc"),
                        "errno": entitlements.get("errno", 0),
                        "length": entitlements.get("length"),
                    },
                    "entitlements_length": entitlements.get("length"),
                    "pidoffset": probe["self"]["pidoffset"]["value"],
                    "child_fork_errno": child_query.get("fork_errno"),
                    "child_status_rc": child_status.get("rc"),
                    "child_status_errno": child_status.get("errno", 0),
                    "child_status_flags": child_status.get("flags") if child_ok else None,
                    "child_matches_self": (flags == child_flags) if self_ok and child_ok else None,
                },
                "binaries": parse_codesign(text),
            }
        )
    return sort_envs(envs)


def sort_envs(envs):
    return sorted(envs, key=lambda e: e["runner"])


def render_markdown(golden):
    out = []
    out.append("# macOS ground truth")
    out.append("")
    out.append(
        "Collected by `.github/workflows/security-ground-truth.yml` running "
        "`tests/security/probe/ground_truth.c` on real GitHub macOS runners. "
        "The probe is read-only: it calls `csops`/`csrctl` and runs "
        "`codesign -dvvv`, never signs or modifies anything."
    )
    out.append("")
    if golden.get("source", {}).get("run_id"):
        out.append(f"Source run: `{golden['source']['run_id']}`.")
        out.append("")
    out.append("> Caveat: runner VMs boot with SIP disabled (`csrutil status`), so")
    out.append("> these values describe a CI VM, not a stock Mac. Flags and ABI")
    out.append("> shapes are still representative; SIP enforcement is not.")
    out.append("")

    out.append("## Environments")
    out.append("")
    out.append("| Runner | macOS | Build | Darwin | Arch | SIP | CSR config |")
    out.append("|---|---|---|---|---|---|---|")
    for e in golden["environments"]:
        out.append(
            f"| {e['runner']} | {e['macos']['product']} | {e['macos']['build']} | "
            f"{e['macos']['darwin']} | {e['macos']['arch']} | {e['sip']} | "
            f"`{e['csr']['config']}` |"
        )
    out.append("")

    out.append("## Probe observations")
    out.append("")
    out.append("| Runner | `CS_OPS_STATUS` | Decoded flags | teamid | entitlements | child query |")
    out.append("|---|---|---|---|---|---|")
    for e in golden["environments"]:
        p = e["probe"]
        team = f"rc={p['teamid']['rc']} errno={p['teamid']['errno']}"
        ent = p.get("entitlements", {})
        if ent.get("rc") == 0 and ent.get("length") is not None:
            ents = f"{ent['length']} bytes"
        elif ent:
            ents = f"rc={ent.get('rc')} errno={ent.get('errno', 0)}"
        elif p.get("entitlements_length") is not None:
            ents = f"{p['entitlements_length']} bytes"
        else:
            ents = "unavailable"
        status_failed = p.get("status_rc") not in (None, 0)
        if status_failed:
            status = f"unavailable (rc={p['status_rc']} errno={p.get('status_errno', 0)})"
            decoded = "(unavailable)"
        else:
            status = f"`{p.get('status_flags')}`"
            decoded = ", ".join(p.get("flag_names") or []) or "(none)"
        if p.get("child_matches_self") is None:
            child = "unavailable"
            if p.get("child_status_rc") not in (None, 0):
                child += f" (rc={p['child_status_rc']} errno={p.get('child_status_errno', 0)})"
            elif p.get("child_fork_errno") is not None:
                child += f" (fork errno={p['child_fork_errno']})"
        else:
            child = "matches self" if p["child_matches_self"] else "differs"
        out.append(
            f"| {e['runner']} | {status} | {decoded} | {team} | {ents} | {child} |"
        )
    out.append("")
    out.append(
        "A linker-signed binary with no team therefore reports "
        "`CS_VALID|CS_LINKER_SIGNED|CS_DYLD_PLATFORM|CS_SIGNED` and `ENOENT` for "
        "`CS_OPS_TEAMID`. An empty entitlements blob returns success with length 0."
    )
    out.append("")

    for e in golden["environments"]:
        out.append(f"## Platform binaries — {e['runner']}")
        out.append("")
        out.append("| Path | CDHash | Platform | Team ID | Entitlements |")
        out.append("|---|---|---|---|---|")
        for b in e["binaries"]:
            keys = b.get("entitlement_keys", [])
            ents = f"{len(keys)} keys" if keys else "none"
            team = b.get("teamid") or "not set"
            out.append(
                f"| `{b['path']}` | `{b.get('cdhash', '?')}` | "
                f"{b.get('platform', '?')} | {team} | {ents} |"
            )
        out.append("")
        for b in e["binaries"]:
            keys = b.get("entitlement_keys", [])
            if not keys:
                continue
            out.append(f"`{b['path']}` entitlements:")
            out.append("")
            for k in keys:
                out.append(f"- `{k}`")
            out.append("")

    out.append("## What this establishes")
    out.append("")
    out.append("- Exact `csops` status flags for a locally built, linker-signed binary.")
    out.append("- `CS_OPS_TEAMID` failure mode (`-1`/`ENOENT`) when no team exists.")
    out.append("- Empty-entitlements semantics.")
    out.append("- Cross-process `csops` works when SIP allows `task_for_pid`.")
    out.append("- Per-macOS-build cdhashes and platform identifiers for Apple binaries.")
    out.append("- Real entitlement keys from `amfid` and friends.")
    out.append("")
    out.append("## What it does not establish")
    out.append("")
    out.append("- SIP enforcement: the runners boot with SIP disabled.")
    out.append("- Hardened-runtime behaviour of a notarized third-party app.")
    out.append("- Notarization tickets (no public notarized fixture here).")
    out.append("- Intel behaviour: the matrix is arm64 only.")
    out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("artifacts", type=pathlib.Path)
    ap.add_argument("--json", type=pathlib.Path, required=True)
    ap.add_argument("--markdown", type=pathlib.Path, required=True)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--date", default=None)
    args = ap.parse_args()

    envs = parse_env(args.artifacts)
    if not envs:
        sys.exit("no ground-truth artifacts found")

    golden = {
        "schema": 1,
        "source": {
            "workflow": "security-ground-truth.yml",
            "run_id": args.run_id,
            "date": args.date,
        },
        "environments": envs,
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(golden, indent=2) + "\n", encoding="utf-8")
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render_markdown(golden), encoding="utf-8")
    print(f"wrote {args.json} and {args.markdown} ({len(envs)} environments)")


if __name__ == "__main__":
    main()
