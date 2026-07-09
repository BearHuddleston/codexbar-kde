from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10
    tomllib = None


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_metadata_is_accepted_by_legacy_setuptools_license_schema(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        if tomllib is None:
            self.assertIn('requires = ["setuptools==68.0.0"]', text)
            self.assertIn('license = { file = "LICENSE" }', text)
            self.assertNotIn("license-files", text)
            return
        data = tomllib.loads(text)

        self.assertEqual(data["build-system"]["requires"], ["setuptools==68.0.0"])
        self.assertEqual(data["project"]["license"], {"file": "LICENSE"})
        self.assertNotIn("license-files", data["project"])

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
        self.assertIn('rm -f "$APPDIR/.DirIcon"', script)
        self.assertNotIn("continuous", script)
        self.assertEqual(requirements.count("--hash=sha256:"), 3)

    def test_abi_parser_reads_needs_but_ignores_library_definitions(self):
        module_path = ROOT / "scripts" / "audit_appimage.py"
        self.assertTrue(module_path.exists(), "missing AppImage ABI auditor")
        spec = importlib.util.spec_from_file_location("audit_appimage", module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
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
        self.assertIn("APPIMAGE_OFFLINE: 1", workflow)
        self.assertIn("cmp ", workflow)
        self.assertIn("scripts/audit_appimage.py", workflow)


if __name__ == "__main__":
    unittest.main()
