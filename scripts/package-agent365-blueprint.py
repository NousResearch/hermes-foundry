#!/usr/bin/env python3
"""Create the four-file Agent 365 package for manual admin-center upload.

The output contains tenant-specific blueprint metadata, so it is written only below the
gitignored deployment-output directory. It contains no credential or access token.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "deployment" / "agent365"
DEFAULT_OUTPUT = ROOT / "deployment-output"
GUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG file")
    return struct.unpack(">II", header[16:24])


def render_json(path: Path, replacements: dict[str, str]) -> dict:
    text = path.read_text(encoding="utf-8")
    for old, new in replacements.items():
        text = text.replace(old, new)
    return json.loads(text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Package Hermes Agent on Foundry for manual Microsoft 365 admin upload."
    )
    parser.add_argument("--blueprint-id", required=True, help="Foundry-created blueprint client ID")
    parser.add_argument("--color-icon", default=TEMPLATES / "color.png", type=Path, help="192x192 PNG; defaults to bundled Nous girl icon")
    parser.add_argument("--outline-icon", default=TEMPLATES / "outline.png", type=Path, help="32x32 PNG; defaults to bundled Nous girl outline")
    parser.add_argument("--version", default="1.0.0", help="Three-part package version")
    parser.add_argument("--display-name", default="Hermes Agent on Foundry")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    if not GUID.fullmatch(args.blueprint_id):
        parser.error("--blueprint-id must be a GUID")
    if not VERSION.fullmatch(args.version):
        parser.error("--version must use three numeric parts, for example 1.0.0")
    if not 1 <= len(args.display_name) <= 30:
        parser.error("--display-name must contain 1-30 characters for the Teams manifest")

    for path, expected in ((args.color_icon, (192, 192)), (args.outline_icon, (32, 32))):
        if not path.is_file():
            parser.error(f"icon not found: {path}")
        try:
            actual = png_dimensions(path)
        except ValueError as exc:
            parser.error(str(exc))
        if actual != expected:
            parser.error(f"{path} is {actual[0]}x{actual[1]}; expected {expected[0]}x{expected[1]}")

    output = (args.output_dir or DEFAULT_OUTPUT / "hermes-agent-on-foundry").resolve()
    output.mkdir(parents=True, exist_ok=True)
    package_dir = output / "manifest"
    package_dir.mkdir(parents=True, exist_ok=True)

    template_id = str(uuid.uuid4())
    replacements = {
        "__BLUEPRINT_ID__": args.blueprint_id,
        "__TEMPLATE_ID__": template_id,
        "__APP_VERSION__": args.version,
    }
    manifest = render_json(TEMPLATES / "manifest.template.json", replacements)
    manifest["name"] = {"short": args.display_name, "full": args.display_name}
    agent_user = render_json(
        TEMPLATES / "agenticUserTemplateManifest.template.json", replacements
    )

    (package_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (package_dir / "agenticUserTemplateManifest.json").write_text(
        json.dumps(agent_user, indent=2) + "\n", encoding="utf-8"
    )
    shutil.copyfile(args.color_icon, package_dir / "color.png")
    shutil.copyfile(args.outline_icon, package_dir / "outline.png")

    zip_path = output / "Hermes-Agent-on-Foundry.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in (
            "manifest.json",
            "agenticUserTemplateManifest.json",
            "color.png",
            "outline.png",
        ):
            archive.write(package_dir / name, arcname=name)

    print(f"Package created: {zip_path}")
    print("Contains: manifest.json, agenticUserTemplateManifest.json, color.png, outline.png")
    print("No client secret, certificate, access token, tenant ID, or user address is included.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
