from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10
    tomllib = None


ROOT = Path(__file__).resolve().parents[1]


def load_abi_auditor() -> ModuleType:
    module_path = ROOT / "scripts" / "audit_appimage.py"
    spec = importlib.util.spec_from_file_location("audit_appimage", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load AppImage ABI auditor")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackagingTests(unittest.TestCase):
    def test_metadata_uses_maintained_setuptools_and_pep639_license(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        if tomllib is None:
            self.assertIn('requires = ["setuptools==82.0.1"]', text)
            self.assertIn('license = "MIT"', text)
            self.assertIn('license-files = ["LICENSE"]', text)
            return
        data = tomllib.loads(text)

        self.assertEqual(data["build-system"]["requires"], ["setuptools==82.0.1"])
        self.assertEqual(data["project"]["license"], "MIT")
        self.assertEqual(data["project"]["license-files"], ["LICENSE"])

    def test_appimage_inputs_are_immutable_and_hash_locked(self):
        script = (ROOT / "scripts" / "build_appimage.sh").read_text(encoding="utf-8")
        requirements = (ROOT / "packaging" / "appimage-requirements.txt").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("/continuous/", script)
        self.assertNotIn("pip install --upgrade", script)
        self.assertIn("python3.11.14-cp311-cp311-manylinux_2_28_x86_64", script)
        self.assertIn("89ff05124b2fcbecbd46006be5b477760", script)
        self.assertIn("appimagetool/releases/download/1.9.1", script)
        self.assertIn("ed4ce84f0d9caff66f50bcca6ff6f35", script)
        self.assertIn("--require-hashes", script)
        self.assertIn("--runtime-file", script)
        self.assertIn("SOURCE_DATE_EPOCH", script)
        self.assertIn("APPIMAGE_OFFLINE", script)
        self.assertIn("$APPDIR/python3.11.14.desktop", script)
        self.assertIn("$APPDIR/python.png", script)
        self.assertIn("$APPDIR/usr/share/icons/hicolor/256x256/apps/python.png", script)
        self.assertIn('rm -f "$APPDIR/.DirIcon"', script)
        self.assertNotIn("continuous", script)
        self.assertEqual(requirements.count("--hash=sha256:"), 3)

    def test_appimage_build_is_caller_umask_independent(self):
        script = (ROOT / "scripts" / "build_appimage.sh").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("\numask 022\n", script)
        self.assertIn("umask 022", workflow)
        self.assertIn("umask 077", workflow)

    def test_appimage_strips_build_only_python_packages(self):
        script = (ROOT / "scripts" / "build_appimage.sh").read_text(encoding="utf-8")

        for name in (
            "build",
            "certifi",
            "packaging",
            "pip",
            "pyproject_hooks",
            "sitecustomize.py",
        ):
            self.assertIn(f"$SITE_PACKAGES/{name}", script)

    def test_abi_parser_reads_needs_but_ignores_library_definitions(self):
        module = load_abi_auditor()
        sample = """
Version needs section '.gnu.version_r' contains 2 entries:
  0x0010:   Name: GLIBC_2.28  Flags: none  Version: 4
  0x0020:   Name: GLIBCXX_3.4.22  Flags: none  Version: 5
Version definition section '.gnu.version_d' contains 1 entry:
  0x001c: Rev: 1  Flags: none  Index: 2  Cnt: 1  Name: GLIBC_9.99
"""

        requirements = module.parse_version_needs(sample)

        self.assertEqual(requirements["GLIBC"], {"2.28"})
        self.assertEqual(requirements["GLIBCXX"], {"3.4.22"})
        self.assertEqual(requirements["CXXABI"], set())

    def test_abi_parser_rejects_nonnumeric_target_requirements(self):
        module = load_abi_auditor()
        sample = """
Version needs section '.gnu.version_r' contains 1 entry:
  0x0010:   Name: GLIBC_PRIVATE  Flags: none  Version: 4
"""

        with self.assertRaisesRegex(ValueError, "unsupported ABI requirement"):
            module.parse_version_needs(sample)

    def test_abi_parser_rejects_unrecognized_requirement_sections(self):
        module = load_abi_auditor()
        sample = """
Section des exigences de version '.gnu.version_r':
  0x0010:   Name: GLIBC_2.28  Flags: none  Version: 4
"""

        with self.assertRaisesRegex(ValueError, "outside version-needs section"):
            module.parse_version_needs(sample)

    def test_abi_audit_rejects_zero_elf_files(self):
        module = load_abi_auditor()
        requirements = {kind: set() for kind in module.KINDS}

        with self.assertRaisesRegex(RuntimeError, "no ELF files"):
            module.validate_audit(0, requirements)

    def test_abi_audit_rejects_missing_glibc_requirements(self):
        module = load_abi_auditor()
        requirements = {kind: set() for kind in module.KINDS}
        requirements["GLIBCXX"].add("3.4.22")

        with self.assertRaisesRegex(RuntimeError, "no GLIBC requirements"):
            module.validate_audit(350, requirements)

    def test_abi_auditor_forces_c_locale(self):
        module = load_abi_auditor()

        environment = module.readelf_environment()
        self.assertEqual(environment["LC_ALL"], "C")
        self.assertEqual(environment["LANG"], "C")

    def test_appstream_does_not_claim_an_absent_binary(self):
        metadata = (
            ROOT / "packaging" / "io.github.BearHuddleston.codexbar_kde.appdata.xml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("<binary>codexbar-kde</binary>", metadata)

    def test_appimage_ships_reviewed_licensing_evidence(self):
        notice_path = ROOT / "THIRD_PARTY_NOTICES.md"
        review_path = ROOT / "docs" / "appimage-licensing.md"
        sbom_path = ROOT / "packaging" / "CodexBar_KDE-x86_64.spdx.json"
        for path in (notice_path, review_path, sbom_path):
            self.assertTrue(path.is_file(), f"missing licensing evidence: {path}")

        notice = notice_path.read_text(encoding="utf-8")
        self.assertIn("PyQt6 6.7.1 — GPL v3", notice)
        self.assertIn("Qt 6.7.1", notice)
        self.assertIn("PyQt6-sip 13.8.0", notice)
        self.assertIn("Python 3.11.14", notice)
        self.assertIn("Corresponding source", notice)

        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        self.assertEqual(sbom["spdxVersion"], "SPDX-2.3")
        packages = {package["name"]: package for package in sbom["packages"]}
        self.assertEqual(packages["PyQt6"]["licenseDeclared"], "GPL-3.0-only")
        self.assertIn("GPL-3.0-only", packages["Qt"]["licenseDeclared"])
        self.assertEqual(packages["CPython"]["licenseDeclared"], "Python-2.0")
        self.assertEqual(
            packages["PyQt6-sip"]["licenseDeclared"], "LicenseRef-PyQt6-sip"
        )

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("GPL/commercial terms", readme)
        self.assertIn("THIRD_PARTY_NOTICES.md", readme)

        script = (ROOT / "scripts" / "build_appimage.sh").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        for name in ("THIRD_PARTY_NOTICES.md", "CodexBar_KDE-x86_64.spdx.json"):
            self.assertIn(name, script)
            self.assertIn(name, workflow)
        self.assertIn("GPL-3.0-and-LGPL-3.0.txt", script)
        self.assertIn("PyQt6-sip.txt", script)
        self.assertIn("PyQt6_Qt6-6.7.1.dist-info/LICENSE", script)
        self.assertIn("lib/python3.11/LICENSE.txt", script)
        self.assertIn("spdx-tools==0.8.5", workflow)
        self.assertIn("pyspdxtools --infile", workflow)

    def test_ci_actions_are_node24_native_and_sha_pinned(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        pins = {
            "actions/checkout": (
                "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
                "v7.0.0",
            ),
            "actions/setup-python": (
                "ece7cb06caefa5fff74198d8649806c4678c61a1",
                "v6.3.0",
            ),
            "actions/upload-artifact": (
                "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                "v7.0.1",
            ),
        }
        for action, (commit, version) in pins.items():
            self.assertIn(f"uses: {action}@{commit} # {version}", workflow)
            self.assertNotIn(f"uses: {action}@v", workflow)

    def test_ci_checks_committed_whitespace_changes(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("BASE_SHA: ${{ github.event.pull_request.base.sha }}", workflow)
        self.assertIn('git diff --check "$BASE_SHA...HEAD"', workflow)
        self.assertIn("git show --check --format= HEAD", workflow)
        self.assertNotIn("- run: git diff --check\n", workflow)

    def test_ci_covers_supported_python_versions_and_reproducible_appimage(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
            self.assertIn(f'"{version}"', workflow)
        self.assertIn("libegl1", workflow)
        self.assertIn("libgl1", workflow)
        self.assertIn("libxkbcommon-x11-0", workflow)
        self.assertIn("libxcb-cursor0", workflow)
        self.assertIn("scripts/build_appimage.sh", workflow)
        self.assertIn("--test-render", workflow)
        self.assertIn("--codexbar-bin /tmp/fake-codexbar", workflow)
        self.assertIn("APPIMAGE_OFFLINE: 1", workflow)
        self.assertIn("cmp ", workflow)
        self.assertIn("scripts/audit_appimage.py", workflow)


if __name__ == "__main__":
    unittest.main()
