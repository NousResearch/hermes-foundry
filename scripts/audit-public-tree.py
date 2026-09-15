#!/usr/bin/env python3
"""Fail when tracked files contain private deployment artifacts or common secrets."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]

# These are public Microsoft application/role IDs or generated .NET project identifiers. Keeping
# the list explicit makes every newly introduced GUID require review while avoiding false alarms for
# identifiers that are part of the portable source tree.
ALLOWED_GUIDS = {
    "00000000-0000-0000-0000-000000000000",
    "00000003-0000-0000-c000-000000000000",  # Microsoft Graph
    "11111111-1111-1111-1111-111111111111",  # isolated test tenant fixture
    "22222222-2222-2222-2222-222222222222",  # isolated test blueprint fixture
    "33333333-3333-3333-3333-333333333333",  # isolated test Agent Identity fixture
    "44444444-4444-4444-4444-444444444444",  # isolated test Agent User fixture
    "036de73a-2484-fa6d-3988-3d7db76eb74e",  # .NET project ID
    "53ca6127-db72-4b80-b1b0-d745d6d5456d",  # Azure AI/Foundry User
    "5a807f24-c9de-44ee-a3a7-329e88a00ffc",  # Agent 365 tooling audience
    "6d3109d3-6ff5-438e-aa23-cddd3703b4cb",  # local .NET user-secrets namespace, no secret value
    "7f951dda-4ed3-4680-a7ca-43fe172d538d",  # Cognitive Services OpenAI User
    "a62a6059-f7fb-4d65-a242-68cc4925c04a",  # .NET solution ID
    "a97b65f3-24c7-4388-baec-2e87135dc908",  # Cognitive Services User
    "b24988ac-6180-42a0-ab88-20f7382dd24c",  # Contributor
    "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1",  # Agent 365 first-party tooling audience
    "fae04ec0-301f-11d3-bf4b-00c04f79efbc",  # .NET C# project type
    "f58310d9-a9f6-439a-9e8d-f62e7b41a168",  # Role Based Access Control Administrator
}

FORBIDDEN_PARTS = {
    ".azure",
    ".private",
    "deployment-output",
    "internal-notes",
    "meeting-notes",
    "private",
    "research",
    "transcripts",
}
FORBIDDEN_SUFFIXES = {
    ".bak", ".cer", ".crt", ".db", ".doc", ".docx", ".key", ".log", ".p12",
    ".pdf", ".pem", ".pfx", ".sqlite", ".tar", ".zip",
}

GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)
CONTENT_RULES = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Atlassian token": re.compile(r"\bATATT[A-Za-z0-9_+=-]{20,}"),
    "JWT/access token": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
    "Azure storage key": re.compile(r"(?:AccountKey|SharedAccessSignature)\s*=", re.IGNORECASE),
    "SAS signature": re.compile(r"[?&]sig=[A-Za-z0-9%+/=_-]{16,}", re.IGNORECASE),
    "local home path": re.compile(
        r"(?:/" + r"Users/[^/<>{}\s]+|[A-Z]:\\" + r"Users\\[^\\<>\s]+)"
    ),
    "tenant domain": re.compile(r"\b(?!<organization>)[A-Za-z0-9.-]+\.onmicrosoft\.com\b", re.IGNORECASE),
}


def tracked_files() -> list[pathlib.Path]:
    # Include non-ignored untracked files as well. This catches an accidental private artifact
    # before it is staged, while CI naturally sees the same committed tree.
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    ).decode("utf-8", "surrogateescape")
    return [ROOT / name for name in output.split("\0") if name]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        lowered_parts = {part.lower() for part in relative.parts}
        if lowered_parts & FORBIDDEN_PARTS:
            findings.append(f"{relative}: private/generated directory is tracked")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(f"{relative}: private/binary artifact type is tracked")

        try:
            raw = path.read_bytes()
        except OSError as exc:
            findings.append(f"{relative}: could not read tracked file: {exc}")
            continue
        if b"\x00" in raw:
            continue
        text = raw.decode("utf-8", "replace")
        for label, pattern in CONTENT_RULES.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: possible {label}")
        for match in EMAIL.finditer(text):
            if match.group(1).lower() not in {"example.com", "example.org", "example.net"}:
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: non-example email address")
        for match in GUID.finditer(text):
            if match.group(0).lower() not in ALLOWED_GUIDS:
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: unreviewed GUID")

    if findings:
        print("Public-tree audit failed:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print(f"Public-tree audit passed ({len(tracked_files())} tracked/candidate files checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
