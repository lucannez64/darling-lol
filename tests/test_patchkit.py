# SPDX-License-Identifier: GPL-3.0-only
"""Host-only unit tests for scripts/patchkit.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import patchkit  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    env = patchkit.git_environment()
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.invalid"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
    res = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return res.stdout


def _make_fake_repo_and_project(base: Path) -> tuple[Path, Path, str]:
    """Create a mini darling-lol project root and a pinned upstream git repo."""
    project = base / "project"
    (project / "patches").mkdir(parents=True)
    (project / "upstream").mkdir(parents=True)
    (project / "scripts").mkdir(parents=True)

    source = base / "source"
    comp_repo = source / "src/external/demo"
    comp_repo.mkdir(parents=True)
    _git(comp_repo, "init")
    (comp_repo / "alpha.c").write_text("int alpha(void) {\n    return 1;\n}\n", encoding="utf-8")
    (comp_repo / "beta.c").write_text("int beta(void) {\n    return 2;\n}\n", encoding="utf-8")
    (comp_repo / "unrelated.c").write_text("int u(void) { return 0; }\n", encoding="utf-8")
    _git(comp_repo, "add", ".")
    _git(comp_repo, "commit", "-m", "upstream base")
    rev = _git(comp_repo, "rev-parse", "HEAD").strip()

    # Create initial patch touching alpha.c and beta.c
    (comp_repo / "alpha.c").write_text("int alpha(void) {\n    return 10;\n}\n", encoding="utf-8")
    (comp_repo / "beta.c").write_text("int beta(void) {\n    return 20;\n}\n", encoding="utf-8")
    initial_patch = _git(comp_repo, "diff", rev)
    (project / "patches/demo.patch").write_text(initial_patch, encoding="utf-8")

    manifest = {
        "schema": 1,
        "upstream": "https://example.invalid/darling.git",
        "revision": rev,
        "components": [
            {
                "path": "src/external/demo",
                "revision": rev,
                "repository": "https://example.invalid/demo.git",
                "patch": "patches/demo.patch",
                "modified_files": 2,
                "new_files": [],
            }
        ],
    }
    (project / "upstream/manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return project, source, rev


class PatchParsingTests(unittest.TestCase):
    def test_verify_hunk_counts_accepts_valid_patch(self) -> None:
        patch = (
            "diff --git a/foo.c b/foo.c\n"
            "index 1111111..2222222 100644\n"
            "--- a/foo.c\n"
            "+++ b/foo.c\n"
            "@@ -1,3 +1,4 @@\n"
            " int main(void) {\n"
            "-    return 0;\n"
            "+    int x = 1;\n"
            "+    return x;\n"
            " }\n"
        )
        self.assertEqual(patchkit.verify_hunk_counts(patch, "foo.patch"), [])

    def test_verify_hunk_counts_catches_dropped_line(self) -> None:
        bad_patch = (
            "diff --git a/foo.c b/foo.c\n"
            "index 1111111..2222222 100644\n"
            "--- a/foo.c\n"
            "+++ b/foo.c\n"
            "@@ -1,3 +1,4 @@\n"
            " int main(void) {\n"
            "-    return 0;\n"
            "+    int x = 1;\n"
            "+    return x;\n"
        )
        errs = patchkit.verify_hunk_counts(bad_patch, "bad.patch")
        self.assertEqual(len(errs), 1)
        self.assertIn("(-3,+4)", errs[0])
        self.assertIn("(-2,+3)", errs[0])

    def test_verify_hunk_counts_handles_no_newline_marker(self) -> None:
        patch = (
            "diff --git a/a.txt b/a.txt\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "\\ No newline at end of file\n"
            "+new\n"
            "\\ No newline at end of file\n"
        )
        self.assertEqual(patchkit.verify_hunk_counts(patch, "a.patch"), [])

    def test_inventory_splits_modified_and_new_files(self) -> None:
        patch = (
            "diff --git a/old.c b/old.c\n"
            "--- a/old.c\n"
            "+++ b/old.c\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "diff --git a/sub/brand_new.h b/sub/brand_new.h\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/sub/brand_new.h\n"
            "@@ -0,0 +1 @@\n"
            "+#pragma once\n"
        )
        mod, new_f = patchkit.patch_file_inventory(patch)
        self.assertEqual(mod, 1)
        self.assertEqual(new_f, ["sub/brand_new.h"])

    def test_parses_mnemonic_diff_prefix(self) -> None:
        patch = (
            "diff --git i/src/sandbox.h w/src/sandbox.h\n"
            "--- i/src/sandbox.h\n"
            "+++ w/src/sandbox.h\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        _, sections = patchkit.split_patch_sections(patch)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].canonical_path, "src/sandbox.h")
        self.assertEqual(patchkit.verify_hunk_counts(patch, "mnemonic.patch"), [])

    def test_verify_hunk_counts_allows_blank_separator_between_sections(self) -> None:
        patch = (
            "diff --git a/a.c b/a.c\n"
            "--- a/a.c\n"
            "+++ b/a.c\n"
            "@@ -1,2 +1,2 @@\n"
            " int a;\n"
            "-int b;\n"
            "+int c;\n"
            "\n"
            "diff --git a/d.c b/d.c\n"
            "--- a/d.c\n"
            "+++ b/d.c\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
        )
        self.assertEqual(patchkit.verify_hunk_counts(patch, "sep.patch"), [])

    def test_unquote_utf8_and_formfeed_in_hunk(self) -> None:
        self.assertEqual(patchkit._unquote_git_path('"a/caf\\303\\251.h"'), "a/café.h")
        patch = (
            "diff --git a/ff.c b/ff.c\n"
            "--- a/ff.c\n"
            "+++ b/ff.c\n"
            "@@ -1,2 +1,2 @@\n"
            " \x0c/* page break */\n"
            "-int a = 1;\n"
            "+int a = 2;\n"
        )
        self.assertEqual(patchkit.verify_hunk_counts(patch, "ff.patch"), [])


class StatusAndRespliceTests(unittest.TestCase):
    def test_status_classifies_applied_drifted_and_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, source, rev = _make_fake_repo_and_project(Path(tmp))
            manifest = patchkit.load_manifest(project)
            comp = manifest["components"][0]
            comp_repo = source / comp["path"]

            st_applied = patchkit.inspect_component_status(comp, source, project)
            self.assertEqual(st_applied["state"], "applied")
            self.assertEqual(st_applied["drifted_files"], [])

            # Introduce drift in unrelated.c
            (comp_repo / "unrelated.c").write_text("int u(void) { return 99; }\n", encoding="utf-8")
            st_drift = patchkit.inspect_component_status(comp, source, project)
            self.assertEqual(st_drift["state"], "drifted")
            self.assertEqual(st_drift["drifted_files"], ["unrelated.c"])

            # Reset to clean upstream
            _git(comp_repo, "checkout", "--", ".")
            st_clean = patchkit.inspect_component_status(comp, source, project)
            self.assertEqual(st_clean["state"], "clean")

    def test_resplice_updates_only_requested_file_and_ignores_unrelated_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, source, rev = _make_fake_repo_and_project(Path(tmp))
            comp_repo = source / "src/external/demo"

            # Edit alpha.c (wanted), add extra.h (wanted), and edit unrelated.c (NOT wanted)
            (comp_repo / "alpha.c").write_text("int alpha(void) {\n    return 42;\n}\n", encoding="utf-8")
            (comp_repo / "extra.h").write_text("#define DEMO_EXTRA 1\n", encoding="utf-8")
            (comp_repo / "unrelated.c").write_text("int u(void) { return 999; }\n", encoding="utf-8")

            rc = patchkit.main(
                [
                    "--root",
                    str(project),
                    "resplice",
                    "demo",
                    "alpha.c",
                    "extra.h",
                    "--source",
                    str(source),
                    "--write",
                    "--sync-manifest",
                ]
            )
            self.assertEqual(rc, 0)

            updated_patch = (project / "patches/demo.patch").read_text(encoding="utf-8")
            self.assertIn("return 42;", updated_patch)
            self.assertIn("DEMO_EXTRA", updated_patch)
            self.assertIn("return 20;", updated_patch)  # beta.c preserved untouched
            self.assertNotIn("unrelated.c", updated_patch)
            self.assertNotIn("return 999;", updated_patch)

            # Manifest synced
            m = patchkit.load_manifest(project)
            self.assertEqual(m["components"][0]["modified_files"], 2)
            self.assertEqual(m["components"][0]["new_files"], ["extra.h"])

            # Real git index of comp_repo stayed untouched (extra.h still untracked)
            status = _git(comp_repo, "status", "--porcelain")
            self.assertIn("?? extra.h", status)

    def test_resplice_removes_reverted_file_from_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, source, _rev = _make_fake_repo_and_project(Path(tmp))
            comp_repo = source / "src/external/demo"
            _git(comp_repo, "checkout", "--", "beta.c")

            rc = patchkit.main(
                [
                    "--root",
                    str(project),
                    "resplice",
                    "demo",
                    "beta.c",
                    "--source",
                    str(source),
                    "--write",
                    "--sync-manifest",
                ]
            )
            self.assertEqual(rc, 0)
            updated_patch = (project / "patches/demo.patch").read_text(encoding="utf-8")
            self.assertNotIn("beta.c", updated_patch)
            self.assertIn("alpha.c", updated_patch)
            m = patchkit.load_manifest(project)
            self.assertEqual(m["components"][0]["modified_files"], 1)

    def test_resplice_preserves_crlf_and_rejects_empty_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, source, _rev = _make_fake_repo_and_project(Path(tmp))
            comp_repo = source / "src/external/demo"

            # Inject CRLF into beta.c section of patch and verify resplicing alpha.c keeps it intact.
            patch_file = project / "patches/demo.patch"
            raw = patch_file.read_bytes()
            raw_crlf = raw.replace(b"return 20;\n", b"return 20;\r\n")
            patch_file.write_bytes(raw_crlf)

            (comp_repo / "alpha.c").write_text("int alpha(void) {\n    return 77;\n}\n", encoding="utf-8")
            (comp_repo / ".dotfile").write_text("dot=1\n", encoding="utf-8")
            rc = patchkit.main(
                [
                    "--root",
                    str(project),
                    "resplice",
                    "demo",
                    "alpha.c",
                    ".dotfile",
                    "--source",
                    str(source),
                    "--write",
                ]
            )
            self.assertEqual(rc, 0)
            out_bytes = patch_file.read_bytes()
            self.assertIn(b"return 20;\r\n", out_bytes)
            self.assertIn(b"diff --git a/.dotfile b/.dotfile", out_bytes)

            # Reverting every patched file must refuse to write an empty patch.
            _git(comp_repo, "checkout", "--", "alpha.c", "beta.c")
            (comp_repo / ".dotfile").unlink()
            with self.assertRaises(patchkit.PatchkitError) as ctx:
                patchkit.cmd_resplice(
                    patchkit.build_parser().parse_args(
                        [
                            "--root",
                            str(project),
                            "resplice",
                            "demo",
                            "alpha.c",
                            "beta.c",
                            ".dotfile",
                            "--source",
                            str(source),
                            "--write",
                        ]
                    )
                )
            self.assertIn("empty", str(ctx.exception))
            self.assertEqual(patch_file.read_bytes(), out_bytes)


class OverlayLedgerTests(unittest.TestCase):
    def test_install_blocks_when_darling_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            src = base / "new.dylib"
            dest = base / "installed/libdemo.dylib"
            src.write_bytes(b"new-overlay")
            with self.assertRaises(patchkit.PatchkitError) as ctx:
                patchkit.overlay_install(
                    src,
                    dest,
                    name="libdemo",
                    ledger_path=base / "ledger.json",
                    backup_dir=base / "backups",
                    allow_running=False,
                    min_free_mb=0,
                    process_checker=lambda: [(1234, "/usr/bin/darlingserver")],
                )
            self.assertIn("running", str(ctx.exception))
            self.assertFalse(dest.exists())

    def test_install_status_and_rollback_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ledger = base / "ledger.json"
            backups = base / "backups"

            dest1 = base / "prefix/libone.dylib"
            dest1.parent.mkdir(parents=True)
            dest1.write_bytes(b"original-bytes")
            src1 = base / "build/libone.dylib"
            src1.parent.mkdir(parents=True)
            src1.write_bytes(b"patched-bytes")

            dest2 = base / "prefix/libtwo.dylib"  # brand new, no prior file
            src2 = base / "build/libtwo.dylib"
            src2.write_bytes(b"brand-new-overlay")

            rec1 = patchkit.overlay_install(
                src1, dest1, name="libone", ledger_path=ledger, backup_dir=backups,
                min_free_mb=0, process_checker=lambda: [],
            )
            self.assertEqual(dest1.read_bytes(), b"patched-bytes")
            self.assertTrue(rec1["had_original"])

            rec2 = patchkit.overlay_install(
                src2, dest2, name="libtwo", ledger_path=ledger, backup_dir=backups,
                min_free_mb=0, process_checker=lambda: [],
            )
            self.assertFalse(rec2["had_original"])

            rows = {r["name"]: r for r in patchkit.overlay_status(ledger)}
            self.assertEqual(rows["libone"]["state"], "ok")
            self.assertEqual(rows["libtwo"]["state"], "ok")

            # Tamper with dest1 and verify status catches it
            dest1.write_bytes(b"tampered")
            rows2 = {r["name"]: r for r in patchkit.overlay_status(ledger)}
            self.assertEqual(rows2["libone"]["state"], "modified")

            # Roll back both
            restored = patchkit.overlay_rollback(
                name=None, rollback_all=True, ledger_path=ledger, process_checker=lambda: []
            )
            self.assertEqual(len(restored), 2)
            self.assertEqual(dest1.read_bytes(), b"original-bytes")
            self.assertFalse(dest2.exists())
            self.assertEqual(patchkit.overlay_status(ledger), [])

    def test_reinstall_same_target_preserves_original_backup_and_blocks_name_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ledger = base / "ledger.json"
            backups = base / "backups"

            dest = base / "prefix/libone.dylib"
            dest.parent.mkdir(parents=True)
            dest.write_bytes(b"v0-original")

            v1 = base / "v1.dylib"
            v2 = base / "v2.dylib"
            v1.write_bytes(b"v1-patched")
            v2.write_bytes(b"v2-patched")

            patchkit.overlay_install(
                v1, dest, name="libone", ledger_path=ledger, backup_dir=backups,
                min_free_mb=0, process_checker=lambda: [],
            )
            patchkit.overlay_install(
                v2, dest, name="libone", ledger_path=ledger, backup_dir=backups,
                min_free_mb=0, process_checker=lambda: [],
            )
            self.assertEqual(dest.read_bytes(), b"v2-patched")

            # Reusing the same name for a different target must fail.
            other_dest = base / "prefix/libother.dylib"
            with self.assertRaises(patchkit.PatchkitError):
                patchkit.overlay_install(
                    v2, other_dest, name="libone", ledger_path=ledger, backup_dir=backups,
                    min_free_mb=0, process_checker=lambda: [],
                )

            # Rolling back restores v0-original, NOT v1-patched.
            patchkit.overlay_rollback(
                name="libone", rollback_all=False, ledger_path=ledger, process_checker=lambda: []
            )
            self.assertEqual(dest.read_bytes(), b"v0-original")


if __name__ == "__main__":
    unittest.main()
