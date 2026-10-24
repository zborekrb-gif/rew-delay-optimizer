#!/usr/bin/env python3
"""Build the v20.31 release strictly from RELEASE_ALLOWLIST_v20_31.txt."""
from __future__ import annotations

import argparse
import hashlib
import shutil
import zipfile
from pathlib import Path


EXCLUDED_NAMES = {
    ".coverage",
    "MANIFEST_SHA256.txt",
    "REWDO_STARTUP_DIAGNOSTIC.log",
    "SHA256SUMS.txt",
}
EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "real_run_work",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_allowlist(path: Path) -> list[str]:
    patterns = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not patterns:
        raise ValueError("Release allowlist is empty.")
    return patterns


def allowed_files(root: Path, allowlist: Path) -> list[Path]:
    selected: set[Path] = set()
    for pattern in read_allowlist(allowlist):
        for candidate in root.glob(pattern):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            relative = candidate.relative_to(root)
            if candidate.name in EXCLUDED_NAMES or any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            selected.add(relative)
    if not selected:
        raise ValueError("Release allowlist selected no files.")
    return sorted(selected, key=lambda path: path.as_posix())


def build(root: Path, staging: Path, archive: Path, allowlist: Path) -> tuple[int, str]:
    if staging.exists():
        raise FileExistsError(f"Staging already exists: {staging}")
    if archive.exists():
        raise FileExistsError(f"Archive already exists: {archive}")
    staging.mkdir(parents=True)

    relative_files = allowed_files(root, allowlist)
    for relative in relative_files:
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)

    manifest_lines = [f"{sha256(staging / relative)}  {relative.as_posix()}" for relative in relative_files]
    manifest = staging / "SHA256SUMS.txt"
    manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8", newline="\n")

    for line in manifest_lines:
        expected, relative_text = line.split("  ", 1)
        if sha256(staging / relative_text) != expected:
            raise RuntimeError(f"Manifest verification failed: {relative_text}")

    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as handle:
        for path in sorted(staging.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_file():
                handle.write(path, Path(staging.name) / path.relative_to(staging))

    return len(relative_files) + 1, sha256(archive)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, default=None)
    args = parser.parse_args()
    root = args.root.resolve()
    allowlist = (args.allowlist or root / "RELEASE_ALLOWLIST_v20_31.txt").resolve()
    count, archive_hash = build(root, args.staging.resolve(), args.archive.resolve(), allowlist)
    print(f"Release files including manifest: {count}")
    print(f"Archive SHA-256: {archive_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
