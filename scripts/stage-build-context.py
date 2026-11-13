#!/usr/bin/env python3
"""Stage only reviewed build inputs, rejecting symlinks and missing files."""
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def stage(destination):
    destination = pathlib.Path(destination)
    if any(destination.iterdir()):
        raise ValueError("Build staging directory must be empty")
    files = json.loads((ROOT / "config/build-context.json").read_text())
    for name in files:
        relative = pathlib.PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid build input path")
        source = ROOT / name
        if any((ROOT / part).is_symlink() for part in [relative, *relative.parents]):
            raise ValueError("Symlink build input refused: " + name)
        if not source.is_file():
            raise ValueError("Missing build input: " + name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(source.stat().st_mode & 0o777)
    # Defense in depth: the staging context has the same explicit Docker allowlist.
    shutil.copyfile(ROOT / ".dockerignore", destination / ".dockerignore")


if __name__ == "__main__":
    stage(sys.argv[1])
