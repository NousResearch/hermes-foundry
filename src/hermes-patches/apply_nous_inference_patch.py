#!/usr/bin/env python3
"""Add per-user Nous OAuth credentials to the pinned runtime; assert the exact patch site."""
import ast
import os
from pathlib import Path

root = Path(os.environ.get('HERMES_PATCH_ROOT', '/opt/hermes'))
path = root / 'hermes_cli/auth.py'
source = path.read_text()
marker = 'from hermes_cli.foundry_inference import resolve_credentials'
if marker not in source:
    matches = [n for n in ast.parse(source).body
               if isinstance(n, ast.FunctionDef) and n.name == 'resolve_nous_runtime_credentials']
    if len(matches) != 1:
        raise RuntimeError('Expected one Nous runtime credential resolver')
    node = matches[0]
    first = node.body[1] if ast.get_docstring(node) else node.body[0]
    lines = source.splitlines(keepends=True)
    lines.insert(first.lineno - 1, '''    # Fetch only the current verified caller's access token from the gateway.
    from hermes_cli.foundry_inference import resolve_credentials
    try:
        return resolve_credentials()
    except (ValueError, RuntimeError) as error:
        raise AuthError(str(error), provider="nous", relogin_required=False) from None

''')
    result = ''.join(lines)
    ast.parse(result)
    path.write_text(result)

# The Foundry provider must retain its own Entra profile when the startup default is Nous.
path = root / 'hermes_cli/runtime_provider.py'
source = path.read_text()
marker = '# Foundry hosted inference profile isolation'
if marker not in source:
    nodes = [n for n in ast.parse(source).body
             if isinstance(n, ast.FunctionDef) and n.name == '_resolve_azure_foundry_runtime']
    if len(nodes) != 1:
        raise RuntimeError('Expected one Foundry runtime resolver')
    first = nodes[0].body[1]
    lines = source.splitlines(keepends=True)
    lines.insert(first.lineno - 1, '''    # Foundry hosted inference profile isolation
    from hermes_cli.foundry_inference import settings
    if "azure-foundry" not in settings()[1]:
        raise AuthError("Foundry inference is not enabled by the administrator", provider="azure-foundry")
    model_cfg = dict(model_cfg, provider="azure-foundry", auth_mode="entra_id",
                     base_url=os.environ.get("AZURE_FOUNDRY_BASE_URL", ""))
    explicit_api_key = None
    explicit_base_url = os.environ.get("AZURE_FOUNDRY_BASE_URL", "")

''')
    result = ''.join(lines)
    old = 'state = auth_mod.get_provider_auth_state("nous") or {}'
    if result.count(old) != 1:
        raise RuntimeError('Expected one explicit Nous auth state read')
    result = result.replace(old, 'state = {}  # Hosted Nous credentials come only from the current caller OAuth session.')
    ast.parse(result)
    path.write_text(result)

# Explicit request providers must not first authenticate the startup default.
# Otherwise a user choosing Foundry is blocked by an unsigned-in Nous default.
path = root / 'gateway/platforms/api_server.py'
source = path.read_text()
marker = '# Foundry caller-scoped explicit provider bootstrap'
if marker not in source:
    nodes = [n for n in ast.walk(ast.parse(source))
             if isinstance(n, ast.FunctionDef) and n.name == '_create_agent']
    if len(nodes) != 1:
        raise RuntimeError('Expected one native API agent factory')
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    block = ''.join(lines[node.lineno - 1:node.end_lineno])
    old = '            runtime_kwargs = _resolve_runtime_agent_kwargs()'
    if block.count(old) != 1:
        raise RuntimeError('Expected one native API startup credential resolution')
    block = block.replace(old, '''            # Foundry caller-scoped explicit provider bootstrap
            if requested_provider:
                runtime_kwargs = _resolve_request_runtime_agent_kwargs(
                    requested_provider, target_model=requested_model)
            else:
                runtime_kwargs = _resolve_runtime_agent_kwargs()''')
    result = ''.join(lines[:node.lineno - 1]) + block + ''.join(lines[node.end_lineno:])
    ast.parse(result)
    path.write_text(result)
