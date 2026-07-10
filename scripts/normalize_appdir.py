#!/usr/bin/env python3
"""Normalize an AppDir for reproducible AppImage packing."""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import stat
from pathlib import Path


def _prune_removed_entry_points(record: Path) -> None:
    """Remove host-path-dependent console scripts already stripped from the image."""
    with record.open(encoding="utf-8", newline="") as handle:
        rows = [
            row
            for row in csv.reader(handle)
            if not row or not row[0].replace("\\", "/").startswith("../../bin/")
        ]
    with record.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)


def normalize_appdir(root: Path, epoch: int) -> None:
    """Normalize cache files, wheel records, modes, and timestamps below *root*."""
    if not root.is_dir():
        raise ValueError(f"AppDir is not a directory: {root}")
    if epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must not be negative")

    for cache in root.rglob("__pycache__"):
        if cache.is_dir():
            shutil.rmtree(cache)
    for bytecode in (*root.rglob("*.pyc"), *root.rglob("*.pyo")):
        bytecode.unlink(missing_ok=True)
    for record in root.rglob("*.dist-info/RECORD"):
        _prune_removed_entry_points(record)

    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            os.utime(path, (epoch, epoch), follow_symlinks=False)
            continue
        current_mode = path.stat().st_mode
        if path.is_dir():
            mode = 0o755
        elif path.is_file():
            mode = 0o755 if current_mode & stat.S_IXUSR else 0o644
        else:
            continue
        path.chmod(mode)
        os.utime(path, (epoch, epoch))

    root.chmod(0o755)
    os.utime(root, (epoch, epoch))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("appdir", type=Path)
    parser.add_argument("epoch", type=int)
    args = parser.parse_args()
    normalize_appdir(args.appdir, args.epoch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
