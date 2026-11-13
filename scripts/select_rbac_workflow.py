#!/usr/bin/env python3
"""Choose a project/blueprint-specific Logic App, retaining matching legacy installs."""
import argparse
import hashlib
import json
import subprocess
import sys

LEGACY = 'hermes-agent-foundry-role-sync'


def scoped_name(project, blueprint):
    suffix = hashlib.sha256((project.lower() + '\0' + blueprint.lower()).encode()).hexdigest()[:16]
    return LEGACY + '-' + suffix


def select(project, blueprint, inventory, read):
    if not isinstance(inventory, list) or any(not isinstance(item, dict) for item in inventory):
        raise ValueError('Invalid Logic App inventory; refusing to guess its owner.')
    boundary = project.lower().find('/providers/')
    if boundary < 0:
        raise ValueError('Expected a complete project resource ID.')
    prefix = project[:boundary] + '/providers/Microsoft.Logic/workflows/'
    names = {str(item.get('name', '')).lower() for item in inventory}
    # Retain its principal, original cutoff, and role assignment if the legacy target matches.
    if LEGACY in names:
        data = read(prefix + LEGACY)
        parameters = (data.get('properties') or {}).get('parameters') or {}
        def value(key):
            return str((parameters.get(key) or {}).get('value') or '').lower()
        if value('projectScope') == project.lower() and value('blueprintClientId') == blueprint.lower():
            return LEGACY
        print('The legacy Logic App belongs to another target; using a separate scoped workflow.', file=sys.stderr)
    return scoped_name(project, blueprint)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-scope', required=True)
    parser.add_argument('--blueprint-id', required=True)
    parser.add_argument('--inventory', required=True)
    args = parser.parse_args()
    def read(resource):
        result = subprocess.run(['az', 'resource', 'show', '--ids', resource, '--output', 'json'], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Existing Logic App could not be read; refusing to guess its owner.')
        return json.loads(result.stdout)
    print(select(args.project_scope, args.blueprint_id, json.load(open(args.inventory)), read))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        sys.exit('ERROR: ' + str(exc))
