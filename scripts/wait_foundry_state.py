#!/usr/bin/env python3
"""Bounded, read-only waits for asynchronous Foundry operations; never replay writes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def serving(agent):
    rules = ((agent.get('agent_endpoint') or {}).get('version_selector') or {}).get('version_selection_rules') or []
    full = [item for item in rules if item.get('traffic_percentage') == 100]
    return str(full[0].get('agent_version') or '') if len(full) == 1 else ''


def wait(read, mode, target='', expected='', timeout=600, interval=5, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    last = None
    while True:
        data = read()
        state = str(data.get('status') or '').lower() if mode == 'active' else serving(data)
        if state == ('active' if mode == 'active' else target):
            return data
        if mode == 'active' and state not in {'creating', 'provisioning', 'updating', 'pending', 'queued', 'starting'}:
            raise RuntimeError(f'Version entered non-ready state {state or "(missing)"!r}; no route was changed.')
        if mode == 'route' and state != expected:
            raise RuntimeError(f'Serving route changed unexpectedly to {state or "(ambiguous)"}; refusing to overwrite it.')
        if clock() >= deadline:
            raise RuntimeError(f'Timed out waiting for {mode}: last state {state}. Checkpoint retained; resume with ./setup.sh --continue.')
        if state != last:
            print(f'Waiting for Foundry {mode}: {state}...', file=sys.stderr, flush=True)
            last = state
        sleep(min(interval, max(0, deadline - clock())))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--mode', choices=['active', 'route'], default='active')
    parser.add_argument('--target', default='')
    parser.add_argument('--expected', default='')
    args = parser.parse_args()
    from foundry_agent_lookup import _valid_agent_url
    import re
    agent_url = re.sub(r'/versions/[0-9]+(?=\?)', '', args.url)
    if not _valid_agent_url(agent_url):
        raise ValueError('Invalid Foundry state URL')
    def read():
        result = subprocess.run(['az', 'rest', '--method', 'GET', '--resource', 'https://ai.azure.com',
            '--url', args.url, '--output', 'json'], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError('Foundry status read failed; no write was retried. Check Azure access and resume.')
        return json.loads(result.stdout)
    data = wait(read, args.mode, args.target, args.expected,
                timeout=float(os.environ.get('HERMES_READY_TIMEOUT', '600')),
                interval=float(os.environ.get('HERMES_READY_POLL_INTERVAL', '5')))
    # Records can contain environment secrets; keep them private even in temporary directories.
    output = Path(args.output)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream)
    os.chmod(output, 0o600)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        sys.exit(f'ERROR: {exc}')
