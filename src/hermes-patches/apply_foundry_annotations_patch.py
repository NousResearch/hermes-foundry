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
    patch_reasoning(adapter)

    patched = adapter.read_text(encoding="utf-8")
    assert MARKER in patched
    assert '"annotations": []' in patched
    assert patched.count('text_part["annotations"] = []') == 2
    print("HERMES FOUNDRY ANNOTATIONS PATCH OK")


def patch_reasoning(path: Path) -> None:
    """Foundry requires reasoning.id even for self-contained store=False replay.

    Live regression: the original response replays with 200; deleting only its reasoning ID
    returns 400 invalid_payload. This overlay is for our Foundry-only image. Do not apply it to
    upstream Hermes' other providers, whose replay contracts differ.
    """
    text = path.read_text(encoding="utf-8")
    marker = "Foundry reasoning replay must retain its original id"
    if marker in text:
        return
    text = replace_once(text,
        'if k not in ("id", "_issuer_kind")',
        '# Foundry retains the original reasoning ID; only internal metadata is removed.\n'
        '                                if k != "_issuer_kind"',
        "reasoning history ID retention")
    text = replace_once(text,
        '                summary = item.get("summary")\n',
        '                # Foundry reasoning replay must retain its original id, even with store=False.\n'
        '                # Legacy records without an ID cannot be replayed; never invent an ID.\n'
        '                if not isinstance(item_id, str) or not item_id:\n'
        '                    continue\n'
        '                reasoning_item["id"] = item_id\n'
        '                summary = item.get("summary")\n',
        "reasoning preflight ID retention")
    # Remove upstream comments that describe the other provider's incompatible contract.
    text = text.replace('Strip the "id" field — with store=False the',
                        'Upstream strips IDs because its non-Foundry')
    text = text.replace('Do NOT include the "id" in the outgoing item — with',
                        'Upstream omits IDs in outgoing items with')
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
