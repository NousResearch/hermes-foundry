"""Provider configuration and per-turn, per-user browser OAuth credentials."""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ENDPOINT = 'https://inference-api.nousresearch.com/v1'
PROVIDERS = {'nous', 'azure-foundry'}


def settings(env=None):
    env = os.environ if env is None else env
    provider = env.get('INFERENCE_PROVIDER', 'azure-foundry').strip().lower()
    allowed = {x.strip().lower() for x in env.get('INFERENCE_ALLOWED_PROVIDERS', provider).split(',') if x.strip()}
    if not allowed or not allowed <= PROVIDERS or provider not in allowed:
        raise ValueError('INFERENCE_ALLOWED_PROVIDERS must contain the default and only nous or azure-foundry')
    endpoint = env.get('NOUS_INFERENCE_BASE_URL', DEFAULT_ENDPOINT).rstrip('/')
    url = urllib.parse.urlsplit(endpoint)
    if (url.scheme != 'https' or url.hostname not in {
            'inference-api.nousresearch.com', 'stg-inference-api.nousresearch.com'}
            or url.port not in (None, 443) or url.username or url.password
            or url.path != '/v1' or url.query or url.fragment):
        raise ValueError('NOUS_INFERENCE_BASE_URL must be an approved Nous HTTPS inference endpoint ending in /v1')
    models = list(dict.fromkeys(x.strip() for x in (
        env.get('NOUS_MODEL', '') + ',' + env.get('NOUS_MODELS', '')).split(',') if x.strip()))
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', x) for x in models):
        raise ValueError('Invalid Nous model identifier')
    if 'nous' in allowed:
        if not models:
            raise ValueError('Set NOUS_MODEL to a model available to your portal account')
        if endpoint != DEFAULT_ENDPOINT:
            raise ValueError('Per-user browser sign-in currently supports the production Nous portal only')
    return provider, allowed, endpoint, models


def model_configuration(env=None):
    env = os.environ if env is None else env
    provider, allowed, endpoint, models = settings(env)
    fallback = []
    if 'azure-foundry' in allowed:
        for key in ('MODEL_DEPLOYMENT_NAME', 'FALLBACK_MODEL_DEPLOYMENT_NAME', 'AZURE_FOUNDRY_BASE_URL'):
            if not env.get(key):
                raise ValueError(key + ' is required when Foundry inference is enabled')
    if provider == 'nous':
        model = {'default': models[0], 'provider': 'nous', 'base_url': endpoint}
        # No provider-crossing failover: billing and data routing remain explicit.
        fallback = [{'provider': 'nous', 'model': m, 'base_url': endpoint} for m in models[1:]]
    else:
        model = {'default': env['MODEL_DEPLOYMENT_NAME'], 'provider': 'azure-foundry',
                 'base_url': env['AZURE_FOUNDRY_BASE_URL'], 'auth_mode': 'entra_id'}
        priority = env.get('MODEL_PRIORITY', env['MODEL_DEPLOYMENT_NAME'] + ',' + env['FALLBACK_MODEL_DEPLOYMENT_NAME'])
        for name in dict.fromkeys(priority.split(',') + [env['FALLBACK_MODEL_DEPLOYMENT_NAME']]):
            name = name.strip()
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', name):
                raise ValueError('Invalid Foundry model deployment name')
            fallback.append({'provider': 'azure-foundry', 'model': name,
                             'base_url': env['AZURE_FOUNDRY_BASE_URL'], 'auth_mode': 'entra_id'})
    # Automatic fallback remains empty: a conversation can switch providers independently of
    # the startup default. A global fallback chain would send switched turns to the wrong account.
    return {'model': model, 'fallback_providers': fallback if len(allowed) == 1 else []}


def resolve_credentials():
    """Resolve only the current authenticated caller's ephemeral OAuth credential."""
    _, allowed, endpoint, _ = settings()
    if 'nous' not in allowed:
        raise ValueError('Nous inference is not enabled by the administrator')
    key = os.environ.get('GATEWAY_KEY', '')
    if not key:
        raise RuntimeError('Per-user portal authentication is unavailable')
    request = urllib.request.Request('http://127.0.0.1:8080/v1/portal-token',
                                     headers={'Authorization': 'Bearer ' + key})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
            result = json.loads(response.read(32768))
        if (result.get('base_url') != endpoint or result.get('provider') != 'nous'
                or not result.get('api_key') or result.get('expires_at', 0) <= time.time()):
            raise ValueError()
        return result
    except urllib.error.HTTPError as error:
        error.close()
        raise RuntimeError('Sign in to your own portal account with /portal login in Teams.') from None
    except Exception:
        raise RuntimeError('Sign in to your own portal account with /portal login in Teams.') from None


if __name__ == '__main__':
    try:
        config = model_configuration()
        for name, value in config.items():
            print(name + ': ' + json.dumps(value))
    except ValueError as error:
        raise SystemExit(str(error))
