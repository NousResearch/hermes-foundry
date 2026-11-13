import os, tempfile, json, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from pathlib import Path
home = tempfile.mkdtemp()
os.environ.update(HERMES_HOME=home, INFERENCE_PROVIDER='nous', INFERENCE_ALLOWED_PROVIDERS='nous,azure-foundry',
                  NOUS_MODEL='Hermes-test', GATEWAY_KEY='synthetic-internal-key',
                  AZURE_FOUNDRY_BASE_URL='https://example.services.ai.azure.com/api/projects/test/openai/v1')
Path(home+'/config.yaml').write_text('model: {provider: nous, default: Hermes-test}\n')
current_user = 'alice-access'
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        assert self.path == '/v1/portal-token'
        assert self.headers['Authorization'] == 'Bearer synthetic-internal-key'
        self.send_response(200 if current_user else 403)
        self.end_headers()
        self.wfile.write(json.dumps({'provider': 'nous', 'base_url': 'https://inference-api.nousresearch.com/v1',
            'api_key': current_user, 'expires_at': time.time()+900, 'source': 'foundry-user-oauth'}).encode())
server = ThreadingHTTPServer(('127.0.0.1', 8080), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    from hermes_cli.auth import resolve_nous_runtime_credentials, AuthError
    from gateway.platforms.api_server import _resolve_request_runtime_agent_kwargs
    assert resolve_nous_runtime_credentials()['api_key'] == 'alice-access'
    nous = _resolve_request_runtime_agent_kwargs('nous', target_model='Hermes-test')
    assert nous['api_key'] == 'alice-access'
    with patch('agent.azure_identity_adapter.build_token_provider', return_value=lambda: 'synthetic-entra'):
        azure = _resolve_request_runtime_agent_kwargs('azure-foundry', target_model='gpt-test')
    assert azure['provider'] == 'azure-foundry' and callable(azure['api_key'])
    assert azure['base_url'] == os.environ['AZURE_FOUNDRY_BASE_URL']
    current_user = 'bob-access'
    assert _resolve_request_runtime_agent_kwargs('nous', target_model='Hermes-test')['api_key'] == 'bob-access'
    current_user = None
    # Exercise the actual agent factory: choosing Foundry must work BEFORE Nous sign-in.
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.config import PlatformConfig
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    with patch('agent.azure_identity_adapter.build_token_provider', return_value=lambda: 'synthetic-entra'), \
         patch('run_agent.AIAgent') as constructor:
        adapter._create_agent(requested_provider='azure-foundry', requested_model='gpt-test')
        assert constructor.call_args.kwargs['provider'] == 'azure-foundry'
        assert callable(constructor.call_args.kwargs['api_key'])
    try: resolve_nous_runtime_credentials()
    except AuthError: pass
    else: raise AssertionError('Anonymous turn reused another user credential')
    print('Pinned Hermes provider dispatch: Alice -> Foundry -> Bob; anonymous caller denied')
finally:
    server.shutdown(); server.server_close(); thread.join()
