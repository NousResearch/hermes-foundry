#!/usr/bin/env python3
"""Compare requested fields with Azure readback without printing secret values."""
import json
import sys


def verify(expected, actual, path='version'):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise ValueError('Version readback type mismatch at ' + path)
        for key, value in expected.items():
            if key not in actual:
                raise ValueError('Version readback missing ' + path + '.' + key)
            verify(value, actual[key], path + '.' + key)
    elif expected != actual:
        # Azure may return numeric CPU values as strings.
        if path.endswith('.cpu') and str(expected) == str(actual):
            return
        raise ValueError('Version readback mismatch at ' + path)


if __name__ == '__main__':
    try:
        expected = json.load(open(sys.argv[1]))
        # Creation-only endpoint/blueprint fields belong to the agent, not its version.
        verify({key: expected[key] for key in ('definition', 'metadata') if key in expected},
               json.load(open(sys.argv[2])))
    except ValueError as exc:
        sys.exit('ERROR: ' + str(exc))
