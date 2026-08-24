#!/usr/bin/env python3
"""Add required annotations arrays to replayed Responses output text.

The pinned Hermes Responses adapter rebuilds assistant-history ``output_text`` parts without the
``annotations`` member. Microsoft Foundry's project-scoped OpenAI endpoint requires that member,
even when it is an empty list, so the second turn in a conversation fails before any tool can run.
Keep this as a narrow, asserted image overlay until the pinned Hermes base includes the fix.
"""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(os.environ.get("HERMES_PATCH_ROOT", "/opt/hermes"))
MARKER = "Foundry requires annotations on replayed output_text"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one source match, found {count}")
    return text.replace(old, new, 1)


def patch_adapter(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return

    text = replace_once(
        text,
        '''                normalized_content.append({"type": "output_text", "text": sanitize_text(text)})
''',
        '''                # Foundry requires annotations on replayed output_text, including when empty.
                normalized_content.append({
                    "type": "output_text",
                    "text": sanitize_text(text),
                    "annotations": [],
                })
''',
        "message-item output_text preflight",
    )
    text = replace_once(
        text,
        '''                        validated.append({"type": text_type, "text": sanitize_text(text)})
''',
        '''                        text_part = {"type": text_type, "text": sanitize_text(text)}
                        if text_type == "output_text":
                            text_part["annotations"] = []
                        validated.append(text_part)
''',
        "role-message output_text preflight",
    )
    text = replace_once(
        text,
        '''                            validated.append({"type": text_type, "text": sanitize_text(part)})
''',
        '''                            text_part = {"type": text_type, "text": sanitize_text(part)}
                            if text_type == "output_text":
                                text_part["annotations"] = []
                            validated.append(text_part)
''',
        "string-part output_text preflight",
    )
    path.write_text(text, encoding="utf-8")


def main() -> None:
    adapter = ROOT / "agent" / "codex_responses_adapter.py"
    patch_adapter(adapter)

    patched = adapter.read_text(encoding="utf-8")
    assert MARKER in patched
    assert '"annotations": []' in patched
    assert patched.count('text_part["annotations"] = []') == 2
    print("HERMES FOUNDRY ANNOTATIONS PATCH OK")


if __name__ == "__main__":
    main()
