"""Propagate non-secret provider settings into a Foundry version definition."""
import os
import json
import subprocess
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/hermes-patches'))
from foundry_inference import settings

KEYS = ('INFERENCE_PROVIDER', 'INFERENCE_ALLOWED_PROVIDERS', 'NOUS_MODEL', 'NOUS_MODELS',
        'NOUS_INFERENCE_BASE_URL')
PROVISION_KEYS = ('FOUNDRY_MODEL_NAME', 'FOUNDRY_MODEL_VERSION')
SETUP_KEYS = KEYS + PROVISION_KEYS + ('AZURE_LOCATION', 'AZURE_SUBSCRIPTION_ID')


def apply(env, *, new_install=False):
    env.pop('NOUS_API_KEY_FILE', None)
    env.pop('NOUS_API_KEY_SECRET_URI', None)
    for key in KEYS:
        if key in os.environ:
            if os.environ[key]:
                env[key] = os.environ[key]
            else:
                env.pop(key, None)
    if new_install:
        env.setdefault('INFERENCE_PROVIDER', 'nous')
        env.setdefault('INFERENCE_ALLOWED_PROVIDERS', 'nous,azure-foundry'
                       if env['INFERENCE_PROVIDER'] == 'nous' else env['INFERENCE_PROVIDER'])
    else:
        env.setdefault('INFERENCE_PROVIDER', 'azure-foundry')
    normalize_portal_models(env)
    settings(env)  # Validate the complete merged policy before sending it to Azure.
    return env


# These illustrate identifier syntax; availability depends on the provider/account.
PORTAL_MODEL_HELP = ('Use provider/model; examples: openai/gpt-6.1-sol, '
                     'openai/gpt-5-mini, openai/gpt-4.1. Bare GPT and o-series names '
                     'automatically receive openai/. Other IDs are preserved; use the exact portal ID.')
FOUNDRY_MODEL_HELP = ('Use an OpenAI catalog name; examples: gpt-6.1-sol, gpt-5-mini, gpt-4.1. '
                      'You can also paste openai/model; setup removes openai/. '
                      'Availability is checked in your Azure region.')


def normalize_portal_models(env):
    for key in ('NOUS_MODEL', 'NOUS_MODELS'):
        if not env.get(key):
            continue
        models = []
        for model in env[key].split(','):
            model = model.strip()
            if not model:
                continue
            if '/' not in model and re.match(r'^(?:gpt-|o[0-9](?:-|$))', model, re.IGNORECASE):
                model = 'openai/' + model
            elif model.lower().startswith('openai/'):
                model = 'openai/' + model.split('/', 1)[1]
            if model not in models:
                models.append(model)
        env[key] = ','.join(models)
    return env


def normalize_model(name):
    name = name.strip()
    if name.lower().startswith('openai/'):
        name = name.split('/', 1)[1]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', name):
        raise ValueError('Enter an OpenAI model name, optionally prefixed with openai/')
    return name


def catalog_version(env):
    name = env['FOUNDRY_MODEL_NAME']
    location = env['AZURE_LOCATION']
    command = ['az', 'cognitiveservices', 'model', 'list', '--location', location, '-o', 'json']
    if env.get('AZURE_SUBSCRIPTION_ID'):
        command += ['--subscription', env['AZURE_SUBSCRIPTION_ID']]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f'Could not read the Azure model catalog in {location}. Check Azure login and subscription access, then retry.')
    candidates = [entry['model'] for entry in json.loads(result.stdout)
                  if entry.get('model', {}).get('name') == name
                  and entry['model'].get('format') == 'OpenAI'
                  and any(sku.get('name') == 'GlobalStandard' for sku in entry['model'].get('skus', []))
                  and entry['model'].get('version')]
    if not candidates:
        raise ValueError(f'Azure does not list {name} with GlobalStandard support in {location}. Choose another model or region.')
    preferred = [model for model in candidates if model.get('isDefaultVersion')] or candidates
    return max(model['version'] for model in preferred)


def provision_defaults(env, resolver=None):
    env['FOUNDRY_MODEL_NAME'] = normalize_model(env.get('FOUNDRY_MODEL_NAME') or 'gpt-6.1-sol')
    env.setdefault('AZURE_LOCATION', 'eastus2')
    if not env.get('FOUNDRY_MODEL_VERSION'):
        env['FOUNDRY_MODEL_VERSION'] = (resolver or catalog_version)(env)
        print(f"Azure model: {env['FOUNDRY_MODEL_NAME']} (version {env['FOUNDRY_MODEL_VERSION']}, {env['AZURE_LOCATION']}).")
    return env


def configure_interactively(env, ask=input, locked_keys=(), resolver=None):
    """Collect non-secret deployment choices; never request an API key itself."""
    def prompt(key, label, default=''):
        if key in locked_keys and env.get(key):
            return
        current = env.get(key) or default
        answer = ask(f"{label} [{current}]: ").strip()
        if answer or current:
            env[key] = answer or current

    prompt('INFERENCE_PROVIDER', 'Default inference provider (nous / azure-foundry)', 'nous')
    env['INFERENCE_PROVIDER'] = env['INFERENCE_PROVIDER'].strip().lower()
    if env['INFERENCE_PROVIDER'] not in {'nous', 'azure-foundry'}:
        raise ValueError('Choose nous or azure-foundry as the default inference provider')
    if 'INFERENCE_ALLOWED_PROVIDERS' not in locked_keys:
        allowed = [p.strip().lower() for p in env.get('INFERENCE_ALLOWED_PROVIDERS', '').split(',') if p.strip()]
        if allowed and env['INFERENCE_PROVIDER'] not in allowed:
            env['INFERENCE_ALLOWED_PROVIDERS'] = ','.join([env['INFERENCE_PROVIDER']] + allowed)
    prompt('INFERENCE_ALLOWED_PROVIDERS',
           'Providers users may select in chat (examples: nous; azure-foundry; nous,azure-foundry)',
           'nous,azure-foundry' if env['INFERENCE_PROVIDER'] == 'nous' else 'azure-foundry')
    allowed = {p.strip().lower() for p in env['INFERENCE_ALLOWED_PROVIDERS'].split(',') if p.strip()}
    if not allowed <= {'nous', 'azure-foundry'} or env['INFERENCE_PROVIDER'] not in allowed:
        raise ValueError('Selectable providers must include the default provider and contain only nous or azure-foundry')
    if 'nous' in {p.strip().lower() for p in env['INFERENCE_ALLOWED_PROVIDERS'].split(',')}:
        print(PORTAL_MODEL_HELP)
        prompt('NOUS_MODEL', 'Default Nous portal model ID', 'openai/gpt-6.1-sol')
        print(PORTAL_MODEL_HELP + ' Separate additional IDs with commas; leave blank for none.')
        prompt('NOUS_MODELS', 'Additional permitted Nous model IDs (comma-separated, optional)')
        normalize_portal_models(env)
        print(f"Portal model: {env['NOUS_MODEL']}")
        if env.get('NOUS_MODELS'):
            print(f"Additional portal models: {env['NOUS_MODELS']}")
        env.pop('NOUS_API_KEY_FILE', None)
        env.pop('NOUS_API_KEY_SECRET_URI', None)
        print('Each user signs in to their own Nous account in Teams with /portal login. No shared portal credential is needed.')
    if 'azure-foundry' in {p.strip().lower() for p in env['INFERENCE_ALLOWED_PROVIDERS'].split(',')}:
        previous = env.get('FOUNDRY_MODEL_NAME', 'gpt-6.1-sol')
        print(FOUNDRY_MODEL_HELP)
        prompt('FOUNDRY_MODEL_NAME', 'Foundry model to deploy (OpenAI catalog model name)', 'gpt-6.1-sol')
        env['FOUNDRY_MODEL_NAME'] = normalize_model(env['FOUNDRY_MODEL_NAME'])
        if env['FOUNDRY_MODEL_NAME'] != normalize_model(previous) and 'FOUNDRY_MODEL_VERSION' not in locked_keys:
            env.pop('FOUNDRY_MODEL_VERSION', None)
    if not env.get('AZURE_LOCATION'):
        prompt('AZURE_LOCATION', 'Azure deployment region', 'eastus2')
    provision_defaults(env, resolver=resolver)
    settings(env)
    return env


def main():
    import json
    import subprocess
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--save-azd', action='store_true')
    parser.add_argument('--interactive', action='store_true')
    parser.add_argument('--validate-new', action='store_true')
    args = parser.parse_args()
    saved = {}
    if args.save_azd:
        result = subprocess.run(['azd', 'env', 'get-values', '--output', 'json'],
                                capture_output=True, text=True, check=True)
        saved = {k: v for k, v in json.loads(result.stdout).items() if k in SETUP_KEYS}
    if (os.environ.get('FOUNDRY_MODEL_NAME')
            and os.environ['FOUNDRY_MODEL_NAME'] != saved.get('FOUNDRY_MODEL_NAME', 'gpt-6.1-sol')
            and not os.environ.get('FOUNDRY_MODEL_VERSION')):
        saved.pop('FOUNDRY_MODEL_VERSION', None)
    if args.interactive and sys.stdin.isatty():
        for key in SETUP_KEYS:
            if key in os.environ:
                saved[key] = os.environ[key]
        config = configure_interactively(saved, locked_keys=set(os.environ) & set(SETUP_KEYS))
    else:
        config = apply(saved, new_install=True)
    if args.save_azd:
        for key in PROVISION_KEYS:
            if os.environ.get(key): config[key] = os.environ[key]
        provision_defaults(config)
        for key in SETUP_KEYS:
            if key in config:
                subprocess.run(['azd', 'env', 'set', key, config[key]], check=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(f'ERROR: {exc}')
