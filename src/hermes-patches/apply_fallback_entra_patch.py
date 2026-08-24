#!/usr/bin/env python3
"""Patch the pinned Hermes image so fallback entries can override Foundry auth mode.

The upstream fallback resolver passes a fallback's model and base URL into the shared provider
router, but not its ``auth_mode``. The router consequently reloads the primary model profile and can
construct an API-key client for an explicitly Entra-authenticated fallback. Keep this as a narrow,
asserted image overlay until the pinned Hermes base contains the equivalent upstream fix.
"""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(os.environ.get("HERMES_PATCH_ROOT", "/opt/hermes"))
MARKER = "fallback_auth_mode: str = None"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one source match, found {count}")
    return text.replace(old, new, 1)


def patch_auxiliary_client(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return

    text = replace_once(
        text,
        """    api_mode: Optional[str] = None,\n) -> Tuple[Optional[Any], Optional[str]]:\n""",
        """    api_mode: Optional[str] = None,\n    auth_mode: Optional[str] = None,\n) -> Tuple[Optional[Any], Optional[str]]:\n""",
        "_try_azure_foundry signature",
    )
    text = replace_once(
        text,
        """    except Exception:\n        model_cfg = {}\n\n    try:\n        runtime = _resolve_azure_foundry_runtime(\n""",
        """    except Exception:\n        model_cfg = {}\n\n    # A fallback provider is an independent profile. Honor its explicit auth mode instead of\n    # silently inheriting an API-key default from whichever config copy this resolver loaded.\n    if auth_mode:\n        model_cfg = dict(model_cfg)\n        model_cfg[\"auth_mode\"] = str(auth_mode).strip().lower()\n\n    try:\n        runtime = _resolve_azure_foundry_runtime(\n""",
        "_try_azure_foundry auth overlay",
    )
    text = replace_once(
        text,
        """def resolve_provider_client(\n    provider: str,\n    model: str = None,\n    async_mode: bool = False,\n    raw_codex: bool = False,\n    explicit_base_url: str = None,\n    explicit_api_key: str = None,\n    api_mode: str = None,\n    main_runtime: Optional[Dict[str, Any]] = None,\n    is_vision: bool = False,\n    task: Optional[str] = None,\n) -> Tuple[Optional[Any], Optional[str]]:\n""",
        """def resolve_provider_client(\n    provider: str,\n    model: str = None,\n    async_mode: bool = False,\n    raw_codex: bool = False,\n    explicit_base_url: str = None,\n    explicit_api_key: str = None,\n    api_mode: str = None,\n    main_runtime: Optional[Dict[str, Any]] = None,\n    is_vision: bool = False,\n    task: Optional[str] = None,\n    fallback_auth_mode: str = None,\n) -> Tuple[Optional[Any], Optional[str]]:\n""",
        "resolve_provider_client signature",
    )
    text = replace_once(
        text,
        """            explicit_base_url=explicit_base_url,\n            api_mode=api_mode,\n        )\n""",
        """            explicit_base_url=explicit_base_url,\n            api_mode=api_mode,\n            auth_mode=fallback_auth_mode,\n        )\n""",
        "azure resolver call",
    )
    path.write_text(text, encoding="utf-8")


def patch_fallback_activation(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if "fallback_auth_mode=(fb.get(\"auth_mode\")" not in text:
        text = replace_once(
            text,
            """            explicit_base_url=fb_base_url_hint,\n            explicit_api_key=fb_api_key_hint)\n""",
            """            explicit_base_url=fb_base_url_hint,\n            explicit_api_key=fb_api_key_hint,\n            fallback_auth_mode=(fb.get(\"auth_mode\") or None))\n""",
            "fallback provider resolver call",
        )

    credential_marker = "preserving the active Entra token provider"
    if credential_marker not in text:
        text = replace_once(
            text,
            """            return agent._try_activate_fallback(reason)  # try next in chain\n        try:\n            from hermes_cli.model_normalize import normalize_model_for_provider\n""",
            """            return agent._try_activate_fallback(reason)  # try next in chain\n\n        # The model switch is in-place. If the active Azure Foundry runtime already has a callable\n        # Entra credential, preserve it on the new client instead of replacing it with the API-key\n        # placeholder produced by legacy fallback resolution. The callable mints a fresh token per\n        # request; no JWT is copied or persisted here.\n        from agent.azure_identity_adapter import is_token_provider\n\n        active_credential = getattr(agent, \"api_key\", None)\n        if fb_provider == \"azure-foundry\" and is_token_provider(active_credential):\n            fb_client.api_key = active_credential\n            logger.info(\"Azure Foundry fallback: preserving the active Entra token provider\")\n\n        try:\n            from hermes_cli.model_normalize import normalize_model_for_provider\n""",
            "fallback credential preservation",
        )
    path.write_text(text, encoding="utf-8")


def main() -> None:
    auxiliary = ROOT / "agent" / "auxiliary_client.py"
    fallback = ROOT / "agent" / "chat_completion_helpers.py"
    patch_auxiliary_client(auxiliary)
    patch_fallback_activation(fallback)

    auxiliary_text = auxiliary.read_text(encoding="utf-8")
    fallback_text = fallback.read_text(encoding="utf-8")
    assert MARKER in auxiliary_text
    assert "auth_mode=fallback_auth_mode" in auxiliary_text
    assert "fallback_auth_mode=(fb.get(\"auth_mode\") or None)" in fallback_text
    assert "preserving the active Entra token provider" in fallback_text
    print("HERMES FALLBACK ENTRA PATCH OK")


if __name__ == "__main__":
    main()
