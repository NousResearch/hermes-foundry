#!/usr/bin/env python3
"""Run the Python suite and reject skipped tests in release CI."""

from __future__ import annotations

import pathlib
import sys
import unittest


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("\nERROR: release tests may not be skipped:", file=sys.stderr)
        for test, reason in result.skipped:
            print(f"  {test}: {reason}", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

