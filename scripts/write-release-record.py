#!/usr/bin/env python3
"""Write a tenant-neutral, secret-free record for one created hosted-agent version."""

from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import re
import tempfile


DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
VERSION = re.compile(r"^[0-9]+$")


def _image_arg(dockerfile: str, name: str) -> str:
    match = re.search(rf"^ARG {re.escape(name)}=([^\s]+)$", dockerfile, re.MULTILINE)
    if not match or "@" not in match.group(1):
        raise ValueError(f"Dockerfile has no digest-pinned {name}")
    image, digest = match.group(1).rsplit("@", 1)
    if not DIGEST.fullmatch(digest):
        raise ValueError(f"Dockerfile {name} is not pinned to a sha256 digest")
    return f"{image}@{digest}"


def write_record(
    output: pathlib.Path,
    dockerfile: pathlib.Path,
    source_commit: str,
    source_state: str,
    image_digest: str,
    previous_version: str,
    new_version: str,
) -> None:
    if not COMMIT.fullmatch(source_commit):
        raise ValueError("source commit must be a full lowercase Git object ID")
    if source_state not in {"clean", "dirty"}:
        raise ValueError("source state must be clean or dirty")
    if not DIGEST.fullmatch(image_digest):
        raise ValueError("image digest must be sha256:<64 lowercase hex characters>")
    if not VERSION.fullmatch(previous_version) or not VERSION.fullmatch(new_version):
        raise ValueError("hosted-agent versions must be numeric")

    docker_source = dockerfile.read_text()
    base_images = {
        "Hermes": _image_arg(docker_source, "HERMES_IMAGE"),
        ".NET SDK": _image_arg(docker_source, "DOTNET_SDK_IMAGE"),
        "ASP.NET runtime": _image_arg(docker_source, "DOTNET_ASPNET_IMAGE"),
    }
    created = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    lines = [
        "# Hermes Foundry release record",
        "",
        f"Created: {created}",
        "Status: Hosted-agent version created and definition-validated; traffic was not routed.",
        f"Source commit: `{source_commit}`",
        f"Source state at version creation: **{source_state}**",
        f"Combined image digest: `{image_digest}`",
        f"New hosted-agent version: `{new_version}`",
        f"Rollback candidate: `{previous_version}`",
        "",
        "## Pinned build inputs",
        "",
    ]
    lines.extend(f"- {label}: `{value}`" for label, value in base_images.items())
    lines.extend([
        "",
        "## Automated validation completed before this record",
        "",
        "- Serving-version Graph inheritable permissions were verified.",
        "- The new definition retained required environment keys, protocols, metadata, and model policy.",
        "- New-version Graph inheritable permissions were verified.",
        "- The combined image reference was resolved to the immutable digest above.",
        "",
        "## Required before routing",
        "",
        "- Verify the source state is clean; a dirty source state is release-blocking.",
        "- Test chat, a harmless Graph read/write, OneDrive create/readback, and one Routine in an isolated session.",
        "- Route with the guarded version-routing script and retain the rollback candidate.",
        "",
    ])

    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output.parent, 0o700)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output.parent, prefix=".release-", delete=False
    ) as handle:
        handle.write("\n".join(lines))
        temporary = pathlib.Path(handle.name)
    os.chmod(temporary, 0o600)
    temporary.replace(output)
    os.chmod(output, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--dockerfile", required=True, type=pathlib.Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-state", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--previous-version", required=True)
    parser.add_argument("--new-version", required=True)
    args = parser.parse_args()
    try:
        write_record(
            args.output,
            args.dockerfile,
            args.source_commit,
            args.source_state,
            args.image_digest,
            args.previous_version,
            args.new_version,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
