#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Patch-stack and runtime overlay manager for darling-lol.

Commands:
  patchkit.py status [--source DIR] [--json]
      Show per-component checkout state (clean / applied / drifted / wrong-rev
      / missing) plus patch hunk and manifest health.

  patchkit.py verify [--source DIR] [--skip-prepare] [--skip-validate]
                     [--strict-manifest] [--json]
      Validate unified diff hunk counts, git-apply numstat, manifest drift,
      scripts/validate.py, and (when --source exists) scripts/prepare.py
      --check-only.

  patchkit.py sync-manifest [--write]
      Recompute modified_files and new_files in upstream/manifest.json from
      patches/*.patch (dry-run by default).

  patchkit.py resplice <component> <file>... [--source DIR] [--write]
                       [--sync-manifest] [--context N]
      Regenerate ONLY the named file section(s) of a component's patch from
      the applied source tree without pulling in unrelated edits, verify
      byte-for-byte round-trip against the pinned upstream revision in a
      private git index, and optionally write the updated patch.

  patchkit.py overlay <status|install|rollback> ...
      Manage a checksummed ledger (build/overlay-ledger.json) of installed
      runtime overlay files with running-process and free-space guards and
      atomic rollback.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import ROOT, render  # noqa: E402

DIFF_HEADER_RE = re.compile(r'^diff --git (?:"?[abciow]/)(.+?)"? (?:"?[abciow]/)(.+?)"?$')
HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)

# Processes that indicate Darling or a guest client/game is running.
ACTIVE_PROCESS_TOKENS = (
    "darlingserver",
    "LeagueofLegends",
    "LeagueClient",
    "RiotClientServices",
    "RiotClientUx",
)


class PatchkitError(Exception):
    """Recoverable error with a clear user-facing message."""


def git_environment(index: Path | None = None) -> dict[str, str]:
    """Build a clean git environment isolated from inherited GIT_* overrides."""
    strip = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
        "GIT_INTERNAL_SUPER_PREFIX",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_NAMESPACE",
        "GIT_SHALLOW_FILE",
        "GIT_GRAFT_FILE",
        "GIT_QUARANTINE_PATH",
    )
    env = {k: v for k, v in os.environ.items() if k not in strip}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return env


def git_run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    input_data: str | bytes | None = None,
    text: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        env=env if env is not None else git_environment(),
        input=input_data,
        text=text,
        capture_output=True,
        check=check,
    )


def load_manifest(root: Path = ROOT) -> dict[str, Any]:
    path = root / "upstream/manifest.json"
    if not path.is_file():
        raise PatchkitError(f"manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(manifest: dict[str, Any], root: Path = ROOT) -> None:
    path = root / "upstream/manifest.json"
    atomic_write_text(path, json.dumps(manifest, indent=2) + "\n")


def read_text_exact(path: Path) -> str:
    """Read UTF-8 text without translating CRLF/LF line endings."""
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".patchkit-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Serialize read-modify-write sequences with an advisory flock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ---- Patch parsing and structural validation ------------------------------


@dataclass
class PatchSection:
    old_path: str
    new_path: str
    text: str
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False

    @property
    def canonical_path(self) -> str:
        return self.old_path if self.is_deleted else self.new_path


def _split_lf_keepends(text: str) -> list[str]:
    """Split text strictly on LF, preserving CRLF and control characters (\v, \f)."""
    if not text:
        return []
    parts = text.split("\n")
    out = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _split_lf_lines(text: str) -> list[str]:
    """Split text strictly on LF, stripping only a trailing CR per line."""
    lines = _split_lf_keepends(text)
    out: list[str] = []
    for ln in lines:
        if ln.endswith("\n"):
            ln = ln[:-1]
        if ln.endswith("\r"):
            ln = ln[:-1]
        out.append(ln)
    return out


def _unquote_git_path(raw: str) -> str:
    """Decode Git C-quoted path escapes to raw bytes first, then UTF-8."""
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        inner = raw[1:-1]
        buf = bytearray()
        i = 0
        simple = {
            "a": 0x07,
            "b": 0x08,
            "f": 0x0C,
            "n": 0x0A,
            "r": 0x0D,
            "t": 0x09,
            "v": 0x0B,
            "\\": ord("\\"),
            '"': ord('"'),
        }
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner):
                nxt = inner[i + 1]
                if nxt in "01234567" and i + 3 < len(inner) and all(c in "01234567" for c in inner[i + 1 : i + 4]):
                    buf.append(int(inner[i + 1 : i + 4], 8) & 0xFF)
                    i += 4
                    continue
                if nxt in simple:
                    buf.append(simple[nxt])
                    i += 2
                    continue
                buf.extend(nxt.encode("utf-8"))
                i += 2
                continue
            buf.extend(ch.encode("utf-8"))
            i += 1
        return os.fsdecode(bytes(buf))
    return raw


def _parse_diff_git_header(line: str) -> tuple[str, str]:
    """Parse 'diff --git a/... b/...' handling quoted and unquoted paths."""
    rest = line[len("diff --git ") :]
    if rest.startswith('"'):
        parts: list[str] = []
        i = 0
        while i < len(rest) and len(parts) < 2:
            if rest[i] == '"':
                j = i + 1
                while j < len(rest):
                    if rest[j] == "\\" and j + 1 < len(rest):
                        j += 2
                        continue
                    if rest[j] == '"':
                        j += 1
                        break
                    j += 1
                parts.append(_unquote_git_path(rest[i:j]))
                i = j
            else:
                j = rest.find(" ", i)
                if j < 0:
                    j = len(rest)
                parts.append(rest[i:j])
                i = j
            while i < len(rest) and rest[i] == " ":
                i += 1
        if len(parts) == 2:
            a_path = parts[0][2:] if len(parts[0]) >= 2 and parts[0][1] == "/" else parts[0]
            b_path = parts[1][2:] if len(parts[1]) >= 2 and parts[1][1] == "/" else parts[1]
            return a_path, b_path
    if len(rest) >= 2 and rest[0] in "abciow" and rest[1] == "/":
        # Handle paths containing spaces by preferring the split where old==new.
        candidates = []
        for dst_prefix in (" b/", " w/", " i/", " c/", " o/", " a/"):
            pos = 2
            while True:
                hit = rest.find(dst_prefix, pos)
                if hit == -1:
                    break
                a_cand = rest[2:hit]
                b_cand = rest[hit + len(dst_prefix) :]
                if a_cand == b_cand:
                    return a_cand, b_cand
                candidates.append((a_cand, b_cand))
                pos = hit + 1
        if candidates:
            return candidates[0]
    match = DIFF_HEADER_RE.match(line)
    if not match:
        raise PatchkitError(f"cannot parse diff header: {line!r}")
    return match.group(1), match.group(2)


def split_patch_sections(patch_text: str) -> tuple[str, list[PatchSection]]:
    """Split a unified git patch into (preamble, [PatchSection, ...])."""
    lines = _split_lf_keepends(patch_text)
    preamble_lines: list[str] = []
    raw_sections: list[list[str]] = []
    current: list[str] | None = None

    for line in lines:
        if line.startswith("diff --git "):
            if current is not None:
                raw_sections.append(current)
            current = [line]
        elif current is None:
            preamble_lines.append(line)
        else:
            current.append(line)

    if current is not None:
        raw_sections.append(current)

    sections: list[PatchSection] = []
    for chunk in raw_sections:
        header = chunk[0].rstrip("\r\n")
        old_path, new_path = _parse_diff_git_header(header)
        body = "".join(chunk)
        is_new = any(
            ln.startswith("new file mode ") or ln.rstrip("\r\n") == "--- /dev/null"
            for ln in chunk[1:8]
        )
        is_deleted = any(
            ln.startswith("deleted file mode ") or ln.rstrip("\r\n") == "+++ /dev/null"
            for ln in chunk[1:8]
        )
        is_binary = any(
            ln.startswith("GIT binary patch") or ln.startswith("Binary files ")
            for ln in chunk[1:12]
        )
        sections.append(
            PatchSection(
                old_path=old_path,
                new_path=new_path,
                text=body,
                is_new=is_new,
                is_deleted=is_deleted,
                is_binary=is_binary,
            )
        )
    return "".join(preamble_lines), sections


def verify_hunk_counts(patch_text: str, label: str = "<patch>") -> list[str]:
    """Verify every @@ -a,b +c,d @@ hunk header matches its body line counts."""
    errors: list[str] = []
    _, sections = split_patch_sections(patch_text)
    if not sections and patch_text.strip():
        return [f"{label}: non-empty patch has no 'diff --git' sections"]

    for sec in sections:
        if sec.is_binary:
            continue
        lines = _split_lf_lines(sec.text)
        i = 0
        # Advance past file header to first hunk.
        while i < len(lines) and not lines[i].startswith("@@ "):
            i += 1
        hunk_num = 0
        while i < len(lines):
            line = lines[i]
            if not line.startswith("@@ "):
                errors.append(
                    f"{label} ({sec.canonical_path}): unexpected line outside hunk: {line[:60]!r}"
                )
                i += 1
                continue
            hunk_num += 1
            match = HUNK_HEADER_RE.match(line)
            if not match:
                errors.append(
                    f"{label} ({sec.canonical_path} hunk #{hunk_num}): malformed hunk header {line!r}"
                )
                i += 1
                continue
            exp_old = int(match.group(2)) if match.group(2) is not None else 1
            exp_new = int(match.group(4)) if match.group(4) is not None else 1
            got_old = 0
            got_new = 0
            i += 1
            while i < len(lines) and not lines[i].startswith("@@ "):
                body_line = lines[i]
                if body_line.startswith("+"):
                    got_new += 1
                elif body_line.startswith("-"):
                    got_old += 1
                elif body_line.startswith(" "):
                    got_old += 1
                    got_new += 1
                elif body_line == "":
                    # If the hunk already satisfied its declared counts and only
                    # blank separator lines remain before the next hunk/section,
                    # skip them (matching git apply).
                    j = i + 1
                    while j < len(lines) and lines[j] == "":
                        j += 1
                    at_boundary = j == len(lines) or lines[j].startswith("@@ ")
                    if got_old == exp_old and got_new == exp_new and at_boundary:
                        i = j
                        break
                    # Bare empty context line (editor stripped leading space)
                    got_old += 1
                    got_new += 1
                elif body_line.startswith("\\ No newline at end of file"):
                    pass
                else:
                    errors.append(
                        f"{label} ({sec.canonical_path} hunk #{hunk_num}): invalid hunk line prefix: {body_line[:60]!r}"
                    )
                i += 1
            if got_old != exp_old or got_new != exp_new:
                errors.append(
                    f"{label} ({sec.canonical_path} hunk #{hunk_num}): "
                    f"hunk header expects (-{exp_old},+{exp_new}) but body has (-{got_old},+{got_new})"
                )
    return errors


def patch_file_inventory(patch_text: str) -> tuple[int, list[str]]:
    """Return (modified_files_count, new_files_list) matching manifest.json."""
    _, sections = split_patch_sections(patch_text)
    modified = 0
    new_files: list[str] = []
    for sec in sections:
        if sec.is_new:
            new_files.append(sec.new_path)
        else:
            modified += 1
    return modified, new_files


def manifest_drift(manifest: dict[str, Any], root: Path = ROOT) -> list[dict[str, Any]]:
    """Compare manifest modified_files/new_files against actual patches."""
    drifts: list[dict[str, Any]] = []
    for comp in manifest["components"]:
        patch_path = root / comp["patch"]
        if not patch_path.is_file():
            drifts.append(
                {"component": comp["path"], "patch": comp["patch"], "error": "patch file missing"}
            )
            continue
        actual_mod, actual_new = patch_file_inventory(read_text_exact(patch_path))
        exp_mod = comp.get("modified_files")
        exp_new = comp.get("new_files")
        issues: list[str] = []
        if exp_mod is not None and exp_mod != actual_mod:
            issues.append(f"modified_files: manifest={exp_mod} actual={actual_mod}")
        if exp_new is not None and set(exp_new) != set(actual_new):
            missing = sorted(set(actual_new) - set(exp_new))
            extra = sorted(set(exp_new) - set(actual_new))
            parts = []
            if missing:
                parts.append("missing=" + ",".join(missing))
            if extra:
                parts.append("extra=" + ",".join(extra))
            issues.append(f"new_files: {'; '.join(parts)}")
        if issues:
            drifts.append(
                {
                    "component": comp["path"],
                    "patch": comp["patch"],
                    "actual_modified": actual_mod,
                    "actual_new": actual_new,
                    "issues": issues,
                }
            )
    return drifts


def resolve_component(manifest: dict[str, Any], query: str) -> dict[str, Any]:
    """Match a component by path, leaf name, or patch filename."""
    q = query.strip().rstrip("/")
    matches = []
    for comp in manifest["components"]:
        path = comp["path"]
        patch_rel = comp["patch"]
        patch_stem = Path(patch_rel).stem
        leaf = "darling" if path == "." else Path(path).name
        if q in (path, patch_rel, Path(patch_rel).name, patch_stem, leaf):
            matches.append(comp)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(c["path"] for c in matches)
        raise PatchkitError(f"ambiguous component {query!r}; matches: {names}")
    available = ", ".join(
        f"{Path(c['patch']).stem} ({c['path']})" for c in manifest["components"]
    )
    raise PatchkitError(f"unknown component {query!r}. Known: {available}")


# ---- Component checkout status -------------------------------------------


def _expected_patched_tree(repo: Path, revision: str, patch_text: str) -> str | None:
    """Return git tree hash of applying patch_text to revision in a private index."""
    with tempfile.TemporaryDirectory(prefix="patchkit-idx-") as tmp:
        env = git_environment(Path(tmp) / "index")
        if git_run(["git", "-C", str(repo), "read-tree", revision], env=env).returncode != 0:
            return None
        res = git_run(
            ["git", "-C", str(repo), "apply", "--cached", "--whitespace=nowarn", "-"],
            env=env,
            input_data=patch_text.encode("utf-8"),
            text=False,
        )
        if res.returncode != 0:
            return None
        out = git_run(["git", "-C", str(repo), "write-tree"], env=env)
        return out.stdout.strip() if out.returncode == 0 else None


def inspect_component_status(
    comp: dict[str, Any], source: Path, root: Path = ROOT
) -> dict[str, Any]:
    """Classify one component checkout and its patch file."""
    patch_path = root / comp["patch"]
    patch_raw = read_text_exact(patch_path) if patch_path.is_file() else ""
    hunk_errors = verify_hunk_counts(patch_raw, comp["patch"]) if patch_raw else [f"{comp['patch']}: missing"]
    patch_rendered = render(patch_raw) if patch_raw else ""

    repo = (source / comp["path"]).resolve()
    info: dict[str, Any] = {
        "component": comp["path"],
        "patch": comp["patch"],
        "expected_rev": comp["revision"],
        "hunk_errors": hunk_errors,
        "state": "missing",
        "drifted_files": [],
    }
    if not (repo / ".git").exists():
        return info

    rev = git_run(["git", "-C", str(repo), "rev-parse", "HEAD"])
    if rev.returncode != 0:
        info["state"] = "error"
        info["error"] = rev.stderr.strip() or "git rev-parse HEAD failed"
        return info
    actual_rev = rev.stdout.strip()
    info["actual_rev"] = actual_rev
    if actual_rev != comp["revision"]:
        info["state"] = "wrong-rev"
        return info

    # Determine files touched by patch and files currently dirty in repo.
    _, sections = split_patch_sections(patch_rendered)
    patch_paths = {s.canonical_path for s in sections} | {
        s.old_path for s in sections if s.old_path and not s.is_new
    }

    porcelain = git_run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all", "-z"]
    )
    if porcelain.returncode != 0:
        info["state"] = "error"
        info["error"] = porcelain.stderr.strip() or "git status failed"
        return info

    dirty_paths: set[str] = set()
    if porcelain.stdout:
        entries = porcelain.stdout.split("\0")
        i = 0
        while i < len(entries):
            entry = entries[i]
            if len(entry) >= 4:
                code = entry[:2]
                rel = entry[3:]
                dirty_paths.add(rel)
                if code[0] in ("R", "C") or code[1] in ("R", "C"):
                    if i + 1 < len(entries) and entries[i + 1]:
                        dirty_paths.add(entries[i + 1])
                    i += 1
            i += 1

    if not dirty_paths:
        info["state"] = "clean"
        return info

    # Compare working tree against revision+patch in a private index.
    expected_tree = _expected_patched_tree(repo, comp["revision"], patch_rendered)
    if expected_tree is None:
        info["state"] = "patch-invalid"
        return info

    check_paths = sorted(patch_paths | dirty_paths)
    with tempfile.TemporaryDirectory(prefix="patchkit-cmp-") as tmp:
        env = git_environment(Path(tmp) / "index")
        git_run(["git", "-C", str(repo), "read-tree", expected_tree], env=env, check=True)
        # Update only the relevant paths from working tree to compare exact contents.
        existing = [p for p in check_paths if os.path.lexists(repo / p)]
        removed = [p for p in check_paths if not os.path.lexists(repo / p)]
        if existing:
            git_run(
                ["git", "-C", str(repo), "update-index", "--add", "--replace", "-z", "--stdin"],
                env=env,
                input_data=("\0".join(existing) + "\0").encode("utf-8"),
                text=False,
                check=True,
            )
        if removed:
            git_run(
                ["git", "-C", str(repo), "update-index", "--force-remove", "-z", "--stdin"],
                env=env,
                input_data=("\0".join(removed) + "\0").encode("utf-8"),
                text=False,
                check=True,
            )
        diff = git_run(
            ["git", "-C", str(repo), "diff", "--cached", "--name-only", expected_tree],
            env=env,
        )
        if diff.returncode != 0:
            info["state"] = "error"
            info["error"] = diff.stderr.strip() or "git diff --cached failed"
            return info
        drifted = [line for line in diff.stdout.splitlines() if line]
        if not drifted:
            info["state"] = "applied"
        else:
            info["state"] = "drifted"
            info["drifted_files"] = drifted
    return info


# ---- Resplice ------------------------------------------------------------


def diff_single_file_against_head(
    repo: Path, revision: str, rel_path: str, *, context: int = 3, allow_missing: bool = False
) -> str:
    """Produce a clean unified git diff for ONE file relative to revision.

    Works for modified, newly added (even untracked), or deleted files without
    touching the checkout's real git index.
    """
    full_path = repo / rel_path
    with tempfile.TemporaryDirectory(prefix="patchkit-resplice-") as tmp:
        env = git_environment(Path(tmp) / "index")
        git_run(["git", "-C", str(repo), "read-tree", revision], env=env, check=True)

        in_head = (
            git_run(
                ["git", "-C", str(repo), "cat-file", "-e", f"{revision}:{rel_path}"],
                env=env,
            ).returncode
            == 0
        )

        if os.path.lexists(full_path):
            git_run(
                ["git", "-C", str(repo), "update-index", "--add", "--replace", "--", rel_path],
                env=env,
                check=True,
            )
        elif in_head:
            git_run(
                ["git", "-C", str(repo), "update-index", "--force-remove", "--", rel_path],
                env=env,
                check=True,
            )
        elif allow_missing:
            return ""
        else:
            raise PatchkitError(f"{rel_path}: neither in {revision[:12]} nor in working tree")

        res = git_run(
            [
                "git",
                "-c",
                "diff.mnemonicPrefix=false",
                "-C",
                str(repo),
                "diff",
                "--cached",
                "--binary",
                "--no-ext-diff",
                "--no-color",
                f"-U{context}",
                revision,
                "--",
                rel_path,
            ],
            env=env,
            text=False,
        )
        if res.returncode != 0:
            raise PatchkitError(
                f"git diff failed for {rel_path}: {res.stderr.decode('utf-8', errors='replace').strip()}"
            )
        return res.stdout.decode("utf-8")


def splice_sections(
    original_patch: str, replacements: dict[str, str]
) -> tuple[str, list[str]]:
    """Replace, insert, or remove named file sections inside a unified patch.

    `replacements[path] == ""` removes the file's section from the patch.
    Returns (new_patch_text, summary_actions).
    """
    preamble, sections = split_patch_sections(original_patch)
    remaining = dict(replacements)
    out_chunks: list[str] = []
    if preamble:
        out_chunks.append(preamble)

    actions: list[str] = []
    for sec in sections:
        is_rename = (not sec.is_new) and (not sec.is_deleted) and (sec.old_path != sec.new_path)
        if is_rename and ((sec.old_path in remaining) ^ (sec.new_path in remaining)):
            raise PatchkitError(
                f"cannot resplice only one side of rename {sec.old_path} -> {sec.new_path}; "
                "pass both old and new paths"
            )
        if is_rename and sec.old_path in remaining and sec.new_path in remaining:
            for side in (sec.old_path, sec.new_path):
                new_text = remaining.pop(side)
                if new_text:
                    if not new_text.endswith("\n"):
                        new_text += "\n"
                    out_chunks.append(new_text)
                    actions.append(f"updated {side}")
                else:
                    actions.append(f"removed {side} (matches upstream)")
            continue
        key = sec.canonical_path
        if key in remaining:
            new_text = remaining.pop(key)
            if new_text:
                if not new_text.endswith("\n"):
                    new_text += "\n"
                out_chunks.append(new_text)
                actions.append(f"updated {key}")
            else:
                actions.append(f"removed {key} (matches upstream)")
        else:
            out_chunks.append(sec.text)

    for key in sorted(remaining):
        new_text = remaining[key]
        if new_text:
            if not new_text.endswith("\n"):
                new_text += "\n"
            out_chunks.append(new_text)
            actions.append(f"added {key}")
        else:
            actions.append(f"unchanged {key} (not in patch, matches upstream)")

    return "".join(out_chunks), actions


def verify_round_trip(
    repo: Path, revision: str, patch_text: str, check_files: list[str]
) -> None:
    """Apply patch_text to revision in a private index and verify check_files byte-for-byte."""
    hunk_errs = verify_hunk_counts(patch_text, "<respliced>")
    if hunk_errs:
        raise PatchkitError("hunk verification failed:\n  " + "\n  ".join(hunk_errs))

    with tempfile.TemporaryDirectory(prefix="patchkit-rt-") as tmp:
        env = git_environment(Path(tmp) / "index")
        git_run(["git", "-C", str(repo), "read-tree", revision], env=env, check=True)
        if patch_text.strip():
            apply_res = git_run(
                ["git", "-C", str(repo), "apply", "--cached", "--whitespace=nowarn", "-"],
                env=env,
                input_data=render(patch_text).encode("utf-8"),
                text=False,
            )
            if apply_res.returncode != 0:
                stderr = apply_res.stderr.decode("utf-8", errors="replace").strip()
                raise PatchkitError(
                    f"respliced patch does not apply cleanly to {revision[:12]}:\n{stderr}"
                )
        tree = git_run(["git", "-C", str(repo), "write-tree"], env=env, check=True).stdout.strip()
        for rel in check_files:
            disk_path = repo / rel
            ls_res = git_run(
                ["git", "-C", str(repo), "ls-tree", tree, "--", rel],
                env=env,
            )
            tree_mode = ls_res.stdout.split()[0] if ls_res.returncode == 0 and ls_res.stdout.strip() else None
            blob_res = git_run(
                ["git", "-C", str(repo), "cat-file", "blob", f"{tree}:{rel}"],
                env=env,
                text=False,
            )
            if disk_path.is_symlink():
                if tree_mode != "120000" or blob_res.returncode != 0:
                    raise PatchkitError(
                        f"round-trip failed: {rel} symlink mode mismatch (got {tree_mode!r})"
                    )
                expected_target = os.fsencode(os.readlink(disk_path))
                if blob_res.stdout != expected_target:
                    raise PatchkitError(f"round-trip symlink target mismatch for {rel}")
            elif os.path.lexists(disk_path):
                if blob_res.returncode != 0 or tree_mode == "120000":
                    raise PatchkitError(f"round-trip failed: {rel} missing or wrong type after patch apply")
                disk_bytes = disk_path.read_bytes()
                if blob_res.stdout != disk_bytes:
                    raise PatchkitError(
                        f"round-trip byte mismatch for {rel}: patched={len(blob_res.stdout)}B disk={len(disk_bytes)}B"
                    )
            else:
                if blob_res.returncode == 0:
                    raise PatchkitError(f"round-trip failed: deleted {rel} still exists after patch apply")


# ---- Overlay ledger ------------------------------------------------------


def running_darling_processes() -> list[tuple[int, str]]:
    """Return [(pid, cmdline)] for live Darling / client processes without signalling anything."""
    found: list[tuple[int, str]] = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return found
    my_pid = os.getpid()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        cmdline = raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
        if any(tok in cmdline for tok in ACTIVE_PROCESS_TOKENS):
            found.append((pid, cmdline))
    return found


def load_ledger(ledger_path: Path) -> dict[str, Any]:
    if not ledger_path.is_file():
        return {"schema": 1, "entries": {}}
    return json.loads(ledger_path.read_text(encoding="utf-8"))


def save_ledger(ledger_path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(ledger_path, json.dumps(data, indent=2) + "\n")


def overlay_install(
    src: Path,
    dest: Path,
    *,
    name: str | None,
    ledger_path: Path,
    backup_dir: Path,
    allow_running: bool = False,
    min_free_mb: int = 64,
    root: Path = ROOT,
    process_checker=running_darling_processes,
) -> dict[str, Any]:
    src = src.resolve()
    dest = dest.expanduser().resolve()
    if not src.is_file():
        raise PatchkitError(f"overlay source does not exist: {src}")

    if not allow_running:
        alive = process_checker()
        if alive:
            summary = ", ".join(f"pid {pid}" for pid, _ in alive[:5])
            raise PatchkitError(
                f"refusing overlay install while Darling/client processes are running ({summary}); "
                "close them first or pass --allow-running"
            )

    dest.parent.mkdir(parents=True, exist_ok=True)
    needed_bytes = src.stat().st_size * 2 + min_free_mb * 1024 * 1024
    usage = shutil.disk_usage(dest.parent)
    if usage.free < needed_bytes:
        raise PatchkitError(
            f"insufficient free space on {dest.parent}: "
            f"{usage.free // (1024 * 1024)} MiB free, need >= {needed_bytes // (1024 * 1024)} MiB"
        )

    entry_name = name or dest.name
    lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
    with _file_lock(lock_path):
        ledger = load_ledger(ledger_path)
        prev = ledger.get("entries", {}).get(entry_name)
        if prev is not None and Path(prev["target"]).resolve() != dest:
            raise PatchkitError(
                f"overlay name {entry_name!r} is already recorded for {prev['target']}; "
                "roll it back first or choose a different --name"
            )

        # Preserve original recovery state across repeated upgrades of the same target.
        if prev is not None:
            had_original = bool(prev.get("had_original"))
            backup_path = Path(prev["backup"]) if prev.get("backup") else None
        else:
            backup_path = None
            had_original = dest.exists()
            if had_original:
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                backup_path = (backup_dir / f"{entry_name}.{stamp}.bak").resolve()
                shutil.copy2(dest, backup_path)

        # Keep an immediate pre-install snapshot so we can undo dest if save_ledger fails.
        pre_install_tmp: str | None = None
        existed_before_install = dest.exists()
        if existed_before_install:
            pfd, pre_install_tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".overlay-pre-")
            os.close(pfd)
            shutil.copy2(dest, pre_install_tmp)

        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".overlay-")
        os.close(fd)
        try:
            shutil.copy2(src, tmp_name)
            os.replace(tmp_name, dest)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            if pre_install_tmp:
                try:
                    os.unlink(pre_install_tmp)
                except OSError:
                    pass
            raise

        head_res = git_run(["git", "-C", str(root), "rev-parse", "HEAD"])
        commit = head_res.stdout.strip() if head_res.returncode == 0 else "unknown"
        record = {
            "name": entry_name,
            "source": str(src),
            "target": str(dest),
            "sha256": sha256_file(dest),
            "source_commit": commit,
            "had_original": had_original,
            "backup": str(backup_path) if backup_path else None,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        ledger["entries"][entry_name] = record
        try:
            save_ledger(ledger_path, ledger)
        except BaseException:
            # Recover destination to pre-install state so disk and ledger never diverge.
            if pre_install_tmp and os.path.exists(pre_install_tmp):
                os.replace(pre_install_tmp, dest)
            elif not existed_before_install:
                dest.unlink(missing_ok=True)
            raise
        finally:
            if pre_install_tmp and os.path.exists(pre_install_tmp):
                try:
                    os.unlink(pre_install_tmp)
                except OSError:
                    pass
        return record


def overlay_rollback(
    *,
    name: str | None,
    rollback_all: bool,
    ledger_path: Path,
    allow_running: bool = False,
    process_checker=running_darling_processes,
) -> list[str]:
    if not rollback_all and not name:
        raise PatchkitError("specify an overlay name or --all")
    if not allow_running:
        alive = process_checker()
        if alive:
            summary = ", ".join(f"pid {pid}" for pid, _ in alive[:5])
            raise PatchkitError(
                f"refusing overlay rollback while Darling/client processes are running ({summary}); "
                "close them first or pass --allow-running"
            )

    lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
    with _file_lock(lock_path):
        ledger = load_ledger(ledger_path)
        entries = ledger.get("entries", {})
        targets = list(entries.keys()) if rollback_all else [name]
        restored: list[str] = []

        for key in targets:
            if key not in entries:
                raise PatchkitError(f"overlay not in ledger: {key}")
            rec = entries[key]
            dest = Path(rec["target"])
            backup = Path(rec["backup"]) if rec.get("backup") else None
            if rec.get("had_original"):
                if not backup or not backup.is_file():
                    raise PatchkitError(f"backup missing for {key}: {backup}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".overlay-rb-")
                os.close(fd)
                try:
                    shutil.copy2(backup, tmp_name)
                    os.replace(tmp_name, dest)
                except BaseException:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
                restored.append(f"restored {key} -> {dest}")
            else:
                if dest.exists():
                    dest.unlink()
                restored.append(f"removed {key} ({dest}, had no prior file)")
            del entries[key]
            # Persist after each item so a later failure never leaves restored items stale.
            save_ledger(ledger_path, ledger)

        return restored


def overlay_status(ledger_path: Path) -> list[dict[str, Any]]:
    ledger = load_ledger(ledger_path)
    rows: list[dict[str, Any]] = []
    for name, rec in sorted(ledger.get("entries", {}).items()):
        dest = Path(rec["target"])
        if not dest.exists():
            state = "missing"
        else:
            current_sha = sha256_file(dest)
            state = "ok" if current_sha == rec.get("sha256") else "modified"
        rows.append(
            {
                "name": name,
                "state": state,
                "target": rec["target"],
                "commit": (rec.get("source_commit") or "unknown")[:12],
                "sha256": (rec.get("sha256") or "")[:12],
                "backup": rec.get("backup"),
                "installed_at": rec.get("installed_at"),
            }
        )
    return rows


# ---- CLI subcommand handlers ---------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    source = args.source.resolve()
    manifest = load_manifest(root)
    results = [inspect_component_status(c, source, root) for c in manifest["components"]]
    drifts = manifest_drift(manifest, root)

    if args.json:
        print(json.dumps({"source": str(source), "components": results, "manifest_drift": drifts}, indent=2))
        return 0

    print(f"Source: {source}")
    print(f"{'component':32s} {'state':13s} {'hunks':7s} details")
    for r in results:
        hunk_col = "ok" if not r["hunk_errors"] else f"ERR({len(r['hunk_errors'])})"
        details = ""
        if r["state"] == "drifted":
            preview = ", ".join(r["drifted_files"][:4])
            extra = f" (+{len(r['drifted_files']) - 4} more)" if len(r["drifted_files"]) > 4 else ""
            details = f"drifted: {preview}{extra}"
        elif r["state"] == "wrong-rev":
            details = f"got {r.get('actual_rev', '')[:12]} want {r['expected_rev'][:12]}"
        elif r["state"] == "error":
            details = r.get("error", "git error")
        print(f"{r['component']:32s} {r['state']:13s} {hunk_col:7s} {details}")

    if drifts:
        print(f"\nManifest drift ({len(drifts)} component(s); run `patchkit.py sync-manifest --write`):")
        for d in drifts:
            detail = d.get("error") or "; ".join(d.get("issues", []))
            print(f"  {d['patch']}: {detail}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    manifest = load_manifest(root)
    errors: list[str] = []
    warnings: list[str] = []

    for comp in manifest["components"]:
        patch_path = root / comp["patch"]
        if not patch_path.is_file():
            errors.append(f"{comp['patch']}: file missing")
            continue
        text = read_text_exact(patch_path)
        errors.extend(verify_hunk_counts(text, comp["patch"]))
        numstat = git_run(["git", "apply", "--numstat", "-"], input_data=text.encode("utf-8"), text=False)
        if numstat.returncode != 0:
            stderr = numstat.stderr.decode("utf-8", errors="replace").strip()
            errors.append(f"{comp['patch']}: git apply --numstat failed: {stderr}")

    drifts = manifest_drift(manifest, root)
    for d in drifts:
        msg = f"manifest drift in {d['patch']}: " + (d.get("error") or "; ".join(d.get("issues", [])))
        if args.strict_manifest:
            errors.append(msg)
        else:
            warnings.append(msg)

    if not args.skip_validate:
        val = subprocess.run([sys.executable, str(root / "scripts/validate.py")], capture_output=True, text=True)
        if val.returncode != 0:
            errors.append(f"validate.py failed: {(val.stdout + val.stderr).strip()}")

    source = args.source.resolve()
    if not args.skip_prepare and (source / ".git").exists():
        prep = subprocess.run(
            [sys.executable, str(root / "scripts/prepare.py"), "--check-only", "--source", str(source)],
            capture_output=True,
            text=True,
        )
        if prep.returncode != 0:
            errors.append(f"prepare.py --check-only failed: {(prep.stdout + prep.stderr).strip()}")

    if args.json:
        print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings}, indent=2))
        return 0 if not errors else 1

    for w in warnings:
        print(f"WARN: {w}")
    for e in errors:
        print(f"FAIL: {e}")
    if errors:
        print(f"FAIL: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"PASS: {len(manifest['components'])} patches verified ({len(warnings)} manifest warning(s))")
    return 0


def cmd_sync_manifest(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    manifest = load_manifest(root)
    drifts = manifest_drift(manifest, root)
    if not drifts:
        print("Manifest is already in sync with patches/*.patch")
        return 0

    for d in drifts:
        detail = d.get("error") or "; ".join(d.get("issues", []))
        print(f"  {d['patch']}: {detail}")

    if not args.write:
        print(f"Dry-run: {len(drifts)} component(s) out of sync. Pass --write to update upstream/manifest.json.")
        return 1 if any("error" in d for d in drifts) else 0

    unresolved = [d for d in drifts if "error" in d]
    if unresolved:
        names = ", ".join(d["patch"] for d in unresolved)
        raise PatchkitError(f"cannot sync manifest with missing patch file(s): {names}")

    by_path = {d["component"]: d for d in drifts}
    for comp in manifest["components"]:
        upd = by_path.get(comp["path"])
        if not upd:
            continue
        if "modified_files" in comp:
            comp["modified_files"] = upd["actual_modified"]
        if "new_files" in comp:
            comp["new_files"] = upd["actual_new"]
    save_manifest(manifest, root)
    print(f"Updated upstream/manifest.json ({len(by_path)} component(s))")
    return 0


def cmd_resplice(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    source = args.source.resolve()
    manifest = load_manifest(root)
    comp = resolve_component(manifest, args.component)
    repo = (source / comp["path"]).resolve()
    if not (repo / ".git").exists():
        raise PatchkitError(f"component checkout not found: {repo}")

    actual_rev = git_run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True).stdout.strip()
    if actual_rev != comp["revision"]:
        raise PatchkitError(
            f"{comp['path']}: checkout HEAD {actual_rev[:12]} != pinned {comp['revision'][:12]}"
        )

    patch_path = root / comp["patch"]
    lock_path = patch_path.with_suffix(patch_path.suffix + ".lock")
    with _file_lock(lock_path):
        original_patch = read_text_exact(patch_path) if patch_path.is_file() else ""
        _, existing_sections = split_patch_sections(original_patch)
        patched_paths = {s.canonical_path for s in existing_sections} | {
            s.old_path for s in existing_sections if s.old_path
        }

        replacements: dict[str, str] = {}
        normalized_files: list[str] = []
        for raw_file in args.files:
            rel = raw_file.strip().removeprefix("./")
            normalized_files.append(rel)
            replacements[rel] = diff_single_file_against_head(
                repo,
                comp["revision"],
                rel,
                context=args.context,
                allow_missing=(rel in patched_paths),
            )

        new_patch, actions = splice_sections(original_patch, replacements)
        if not new_patch.strip():
            raise PatchkitError(
                f"resplice would leave {comp['patch']} empty (which validate.py rejects); "
                "patch left unchanged"
            )
        verify_round_trip(repo, comp["revision"], new_patch, normalized_files)

        for act in actions:
            print(f"  {act}")
        print(f"Round-trip verified byte-for-byte against {comp['revision'][:12]} for {len(normalized_files)} file(s).")

        if not args.write:
            print(f"Dry-run: pass --write to update {comp['patch']}")
            return 0

        atomic_write_text(patch_path, new_patch)
        print(f"Wrote {comp['patch']}")

        if args.sync_manifest:
            actual_mod, actual_new = patch_file_inventory(new_patch)
            if "modified_files" in comp:
                comp["modified_files"] = actual_mod
            if "new_files" in comp:
                comp["new_files"] = actual_new
            save_manifest(manifest, root)
            print(f"Synced upstream/manifest.json for {comp['path']} (modified={actual_mod}, new={len(actual_new)})")
    return 0


def cmd_overlay(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    ledger_path = args.ledger.resolve()
    backup_dir = args.backup_dir.resolve()

    if args.overlay_cmd == "status":
        rows = overlay_status(ledger_path)
        if args.json:
            print(json.dumps({"ledger": str(ledger_path), "entries": rows}, indent=2))
            return 0
        if not rows:
            print(f"No overlays recorded in {ledger_path}")
            return 0
        print(f"{'name':28s} {'state':9s} {'commit':12s} {'sha256':12s} target")
        for r in rows:
            print(f"{r['name']:28s} {r['state']:9s} {r['commit']:12s} {r['sha256']:12s} {r['target']}")
        return 0

    if args.overlay_cmd == "install":
        rec = overlay_install(
            args.src,
            args.dest,
            name=args.name,
            ledger_path=ledger_path,
            backup_dir=backup_dir,
            allow_running=args.allow_running,
            min_free_mb=args.min_free_mb,
            root=root,
        )
        print(f"Installed {rec['name']} -> {rec['target']} (sha256={rec['sha256'][:12]}, backup={rec['backup']})")
        return 0

    if args.overlay_cmd == "rollback":
        restored = overlay_rollback(
            name=args.name,
            rollback_all=args.all,
            ledger_path=ledger_path,
            allow_running=args.allow_running,
        )
        for line in restored:
            print(line)
        return 0

    raise PatchkitError(f"unknown overlay command: {args.overlay_cmd}")


def _positive_int(raw: str) -> int:
    try:
        val = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {raw!r}") from exc
    if val <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {val}")
    return val


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Patch-stack and overlay manager for darling-lol.")
    parser.add_argument("--root", type=Path, default=ROOT, help="darling-lol repository root")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Inspect checkout state and patch health for all components")
    p_status.add_argument("--source", type=Path, default=ROOT / "build/darling-src")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_verify = sub.add_parser("verify", help="Verify patch hunks, numstat, manifest drift, and validate.py")
    p_verify.add_argument("--source", type=Path, default=ROOT / "build/darling-src")
    p_verify.add_argument("--skip-prepare", action="store_true")
    p_verify.add_argument("--skip-validate", action="store_true")
    p_verify.add_argument("--strict-manifest", action="store_true", help="Treat manifest count drift as failure")
    p_verify.add_argument("--json", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

    p_sync = sub.add_parser("sync-manifest", help="Recompute modified_files/new_files in upstream/manifest.json")
    p_sync.add_argument("--write", action="store_true", help="Write updated manifest (default is dry-run)")
    p_sync.set_defaults(func=cmd_sync_manifest)

    p_resplice = sub.add_parser("resplice", help="Regenerate specific file sections of a component patch")
    p_resplice.add_argument("component", help="Component path, leaf name, or patch stem (e.g. darlingserver, indium)")
    p_resplice.add_argument("files", nargs="+", help="File path(s) relative to the component root")
    p_resplice.add_argument("--source", type=Path, default=ROOT / "build/darling-src")
    p_resplice.add_argument("--context", type=_positive_int, default=3, help="Unified diff context lines (default: 3)")
    p_resplice.add_argument("--write", action="store_true", help="Write the updated patch file")
    p_resplice.add_argument("--sync-manifest", action="store_true", help="Also sync upstream/manifest.json")
    p_resplice.set_defaults(func=cmd_resplice)

    p_ov = sub.add_parser("overlay", help="Checksummed runtime overlay install/rollback ledger")
    p_ov.add_argument("--ledger", type=Path, default=ROOT / "build/overlay-ledger.json")
    p_ov.add_argument("--backup-dir", type=Path, default=ROOT / "build/overlay-backups")
    ov_sub = p_ov.add_subparsers(dest="overlay_cmd", required=True)

    p_ov_st = ov_sub.add_parser("status", help="List installed overlays and verify on-disk checksums")
    p_ov_st.add_argument("--json", action="store_true")
    p_ov_st.set_defaults(func=cmd_overlay)

    p_ov_in = ov_sub.add_parser("install", help="Atomically install one overlay file with backup and ledger")
    p_ov_in.add_argument("src", type=Path)
    p_ov_in.add_argument("dest", type=Path)
    p_ov_in.add_argument("--name", default=None)
    p_ov_in.add_argument("--allow-running", action="store_true")
    p_ov_in.add_argument("--min-free-mb", type=int, default=64)
    p_ov_in.set_defaults(func=cmd_overlay)

    p_ov_rb = ov_sub.add_parser("rollback", help="Restore backed-up overlay file(s)")
    p_ov_rb.add_argument("name", nargs="?", default=None)
    p_ov_rb.add_argument("--all", action="store_true")
    p_ov_rb.add_argument("--allow-running", action="store_true")
    p_ov_rb.set_defaults(func=cmd_overlay)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PatchkitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
