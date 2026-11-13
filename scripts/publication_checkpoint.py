#!/usr/bin/env python3
"""Do not resubmit an uncertain non-idempotent publication on setup resume."""
import hashlib
import json
import os
from pathlib import Path
import sys
from reinstall_agent import save


def checkpoint(path, action):
    record = json.loads(path.read_text()) if path.exists() else {}
    if action == 'begin':
        if record.get('state') == 'complete':
            return 'complete'
        if record.get('state') == 'submitted':
            raise RuntimeError('The previous publication outcome is uncertain. Inspect the Microsoft 365 pending request before retrying; setup will not submit it twice. Checkpoint: ' + str(path))
        save(path, {'state': 'submitted'})
        return 'submit'
    save(path, {'state': action})
    return action


if __name__ == '__main__':
    root = os.environ.get('HERMES_PUBLICATION_CHECKPOINT_DIR')
    if not root:
        print('submit' if sys.argv[1] == 'begin' else sys.argv[1])
    else:
        key = hashlib.sha256('\0'.join(sys.argv[2:]).encode()).hexdigest()
        try:
            print(checkpoint(Path(root) / (key + '.json'), sys.argv[1]))
        except RuntimeError as exc:
            sys.exit('ERROR: ' + str(exc))
