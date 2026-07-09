#!/usr/bin/env python3
"""Audit undefined ABI version requirements in a final AppImage."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

KINDS = ("GLIBC", "GLIBCXX", "CXXABI")
DEFAULT_CEILINGS = {
    "GLIBC": "2.28",
    "GLIBCXX": "3.4.22",
    "CXXABI": "1.3.11",
}
_NAME_RE = re.compile(r"\bName: (GLIBCXX|GLIBC|CXXABI)_(\d+(?:\.\d+)+)\b")


def version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def parse_version_needs(output: str) -> dict[str, set[str]]:
    """Return only requirements from readelf's version-needs section."""
    requirements: dict[str, set[str]] = {kind: set() for kind in KINDS}
    in_needs = False
    for line in output.splitlines():
        if line.startswith("Version needs section"):
            in_needs = True
            continue
        if line.startswith("Version definition section") or line.startswith(
            "Version symbols section"
        ):
            in_needs = False
            continue
        if not in_needs:
            continue
        for kind, version in _NAME_RE.findall(line):
            requirements[kind].add(version)
    return requirements


def _readelf_requirements(path: Path) -> dict[str, set[str]] | None:
    header = subprocess.run(
        ["readelf", "-h", os.fspath(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if header.returncode:
        return None
    result = subprocess.run(
        ["readelf", "--version-info", "--wide", os.fspath(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"readelf failed for {path}: {result.stderr.strip()}")
    return parse_version_needs(result.stdout)


def audit_appimage(path: Path) -> tuple[int, dict[str, set[str]]]:
    if shutil.which("readelf") is None:
        raise RuntimeError("readelf is required for AppImage ABI auditing")
    path = path.resolve()
    requirements: dict[str, set[str]] = {kind: set() for kind in KINDS}
    elf_count = 0
    with tempfile.TemporaryDirectory(prefix="codexbar-appimage-audit-") as temporary:
        subprocess.run(
            [os.fspath(path), "--appimage-extract"],
            cwd=temporary,
            stdout=subprocess.DEVNULL,
            check=True,
        )
        candidates = [path]
        extracted = Path(temporary) / "squashfs-root"
        candidates.extend(item for item in extracted.rglob("*") if item.is_file())
        for candidate in candidates:
            found = _readelf_requirements(candidate)
            if found is None:
                continue
            elf_count += 1
            for kind in KINDS:
                requirements[kind].update(found[kind])
    return elf_count, requirements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("appimage", type=Path)
    for kind, ceiling in DEFAULT_CEILINGS.items():
        parser.add_argument(
            f"--max-{kind.lower()}",
            default=ceiling,
            metavar="VERSION",
        )
    args = parser.parse_args()
    ceilings = {
        kind: getattr(args, f"max_{kind.lower()}") for kind in DEFAULT_CEILINGS
    }

    try:
        elf_count, requirements = audit_appimage(args.appimage)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.error(str(error))

    failed = False
    print(f"ELF files audited: {elf_count}")
    for kind in KINDS:
        maximum = max(requirements[kind], key=version_key, default="none")
        ceiling = ceilings[kind]
        print(f"{kind}: {maximum} (ceiling {ceiling})")
        if maximum != "none" and version_key(maximum) > version_key(ceiling):
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
