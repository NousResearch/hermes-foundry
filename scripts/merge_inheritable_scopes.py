#!/usr/bin/env python3
"""Build an additive Agent Identity Blueprint inheritance update."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys


SCOPE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,127}")


def merge(existing: object, required: list[str]) -> dict:
    if not isinstance(existing, dict):
        raise ValueError("current inheritance is not an object")
    inheritable = existing.get("inheritableScopes")
    if inheritable is None:
        inheritable = {}
    if not isinstance(inheritable, dict):
        raise ValueError("current inheritableScopes is not an object")
    current = inheritable.get("scopes")
    if current is None:
        current = []
    if not isinstance(current, list) or not all(isinstance(scope, str) for scope in current):
        raise ValueError("current scope list is malformed")

    ordered: list[str] = []
    seen: set[str] = set()
    for scope in [*current, *required]:
        if not SCOPE_PATTERN.fullmatch(scope):
            raise ValueError("scope name is malformed")
        if scope not in seen:
            seen.add(scope)
            ordered.append(scope)
    return {
        "inheritableScopes": {
            "@odata.type": "#microsoft.graph.enumeratedScopes",
            "scopes": ordered,
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("current", type=pathlib.Path)
    parser.add_argument("scopes", nargs="+")
    args = parser.parse_args()
    try:
        current = json.loads(args.current.read_text())
        result = merge(current, args.scopes)
    except (OSError, json.JSONDecodeError, ValueError):
        print("ERROR: current blueprint inheritance is malformed; no update was generated.", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
