#!/usr/bin/env python3
"""Resume one forced reinstall without replaying completed builds or version creation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def az_json(*args):
    result = subprocess.run(['az', *args, '-o', 'json'], capture_output=True, text=True, cwd=ROOT)
    if result.returncode:
        raise RuntimeError('Azure read failed while reconciling reinstall; no creation was retried.')
    return json.loads(result.stdout)


def execute(args, env):
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def fingerprint(env):
    digest = hashlib.sha256()
    paths = json.loads((ROOT / 'config/build-context.json').read_text())
    for name in sorted(set(paths + ['.dockerignore'])):
        digest.update(name.encode() + b'\0' + (ROOT / name).read_bytes())
    for key in ['INFERENCE_PROVIDER', 'INFERENCE_ALLOWED_PROVIDERS', 'NOUS_MODEL', 'NOUS_MODELS',
                'NOUS_INFERENCE_BASE_URL', 'PRIMARY_MODEL', 'FALLBACK_MODEL', 'MODEL_PRIORITY', 'RAI_POLICY_ID']:
        digest.update(key.encode() + b'=' + env.get(key, '').encode() + b'\0')
    return digest.hexdigest()


def reinstall(env):
    from wait_foundry_state import serving
    target = {key: env[key] for key in ['TENANT_ID', 'SUBSCRIPTION_ID', 'ACCOUNT', 'PROJECT', 'AGENT', 'BLUEPRINT_ID', 'REG']}
    run_id = str(uuid.UUID(env.get('HERMES_DEPLOY_RUN_ID') or str(uuid.uuid4())))
    scope = hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()[:16]
    path = ROOT / 'deployment-output/reinstalls' / f'{run_id}-{scope}.json'
    result_path = path.with_suffix('.version.json')
    source = fingerprint(env)
    state = json.loads(path.read_text()) if path.exists() else dict(target=target, source=source, tag='reinstall-' + run_id)
    if state['target'] != target or state['source'] != source:
        raise RuntimeError('Build inputs or target changed during this reinstall. Use ./deploy.sh --force to start a new release; completed work remains recorded.')
    save(path, state)
    base = f"https://{env['ACCOUNT']}.services.ai.azure.com/api/projects/{env['PROJECT']}/agents/{env['AGENT']}"
    def read(url):
        return az_json('rest', '--method', 'GET', '--resource', 'https://ai.azure.com', '--url', url)
    if not state.get('built'):
        execute(['./build.sh', state['tag']], dict(env, HERMES_BUILD_SKIP_LOCAL_CONFIG='true'))
        state['built'] = True
        save(path, state)
    if not state.get('version_started'):
        agent = read(base + '?api-version=v1')
        state['previous'] = serving(agent)
        if not state['previous'].isdigit():
            raise RuntimeError('Expected one numeric serving version before reinstall.')
        if (agent.get('blueprint') or {}).get('client_id', '').lower() != env['BLUEPRINT_ID'].lower():
            raise RuntimeError('Agent blueprint changed before reinstall.')
        digest = az_json('acr', 'repository', 'show', '--name', env['REG'], '--image', 'hermes-full-foundry:' + state['tag'])['digest']
        state['image'] = env['REG'] + '.azurecr.io/hermes-full-foundry@' + digest
        state['version_started'] = True
        save(path, state)
    else:
        print('Resuming saved reinstall: checking the existing image/version before any new writes.', flush=True)
        if not result_path.exists() and result_path.with_suffix(result_path.suffix + '.pending').exists():
            # A lost POST response must be reconciled, never blindly replayed.
            matches = []
            url = base + '/versions?api-version=v1'
            seen_pages = set()
            while url:
                if url in seen_pages or len(seen_pages) >= 20:
                    raise RuntimeError('Version listing did not terminate; no POST was replayed.')
                seen_pages.add(url)
                data = read(url)
                for version in data.get('data', data.get('value', [])):
                    if (version.get('definition') or {}).get('image') == state['image'] and str(version.get('version', '')).isdigit() and int(version['version']) > int(state['previous']):
                        matches.append(str(version['version']))
                url = data.get('nextLink')
                if data.get('has_more') and not url:
                    raise RuntimeError('Version listing is incomplete; inspect the creation outcome before retrying. No POST was replayed.')
                if url and not url.startswith(base + '/versions?'):
                    raise RuntimeError('Unexpected version pagination URL.')
            if len(matches) != 1:
                raise RuntimeError('Previous version-creation outcome is uncertain; no POST was replayed. Inspect versions before starting a new --force run.')
            save(result_path, dict(previous=state['previous'], version=matches[0], image=state['image'], verified=False))
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    if not result.get('verified'):
        execute(['./new-version.sh', state['tag']], dict(env, HERMES_DEPLOY_SKIP_LOCAL_CONFIG='true',
                HERMES_VERSION_RESULT_FILE=str(result_path)))
        result = json.loads(result_path.read_text())
    if result.get('image') != state['image'] or result.get('previous') != state['previous'] or not result.get('verified'):
        raise RuntimeError('Reinstall version result does not match its saved image and rollback version.')
    execute(['./scripts/route-agent-version.sh', '--account', env['ACCOUNT'], '--project', env['PROJECT'],
             '--agent', env['AGENT'], '--target-version', result['version'], '--expect-current', result['previous']], env)
    state['routed'] = result['version']
    save(path, state)
    print(f"Reinstalled application version {result['version']} (previous version {result['previous']} retained for rollback).", flush=True)


if __name__ == '__main__':
    try:
        reinstall(dict(os.environ))
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(f'ERROR: {exc}')
