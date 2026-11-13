import json
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/hermes-patches'))
import foundry_inference as inference
spec = importlib.util.spec_from_file_location('inference_settings_test', ROOT / 'scripts/inference_settings.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


class InferenceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.env = {'INFERENCE_PROVIDER': 'nous', 'NOUS_MODEL': 'NousResearch/Hermes-test'}

    def test_portal_only_config_needs_no_foundry_model_or_token(self):
        config = inference.model_configuration(self.env)
        self.assertEqual(config['model']['provider'], 'nous')
        self.assertEqual(config['model']['base_url'], inference.DEFAULT_ENDPOINT)
        self.assertNotIn('api_key', config['model'])
        self.assertEqual(config['fallback_providers'], [])

    def test_multi_provider_never_implicitly_crosses_billing_accounts(self):
        config = inference.model_configuration({**self.env,
            'INFERENCE_ALLOWED_PROVIDERS': 'nous,azure-foundry',
            'MODEL_DEPLOYMENT_NAME': 'primary', 'FALLBACK_MODEL_DEPLOYMENT_NAME': 'fallback',
            'AZURE_FOUNDRY_BASE_URL': 'https://example.services.ai.azure.com/openai/v1'})
        self.assertEqual(config['fallback_providers'], [])

    def test_invalid_provider_or_endpoint_fails_closed(self):
        for override in ({'INFERENCE_PROVIDER': 'untrusted'}, {'NOUS_MODEL': ''},
                         {'NOUS_INFERENCE_BASE_URL': 'https://untrusted.example/v1'}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                inference.settings({**self.env, **override})

    def test_resolver_reads_current_caller_without_cross_turn_cache(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = [json.dumps({'provider': 'nous', 'base_url': inference.DEFAULT_ENDPOINT,
            'api_key': user, 'expires_at': 9999999999}).encode() for user in ('alice', 'bob')]
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.dict(os.environ, {**self.env, 'GATEWAY_KEY': 'internal-test'}, clear=True), \
             mock.patch.object(inference.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(inference.resolve_credentials()['api_key'], 'alice')
            self.assertEqual(inference.resolve_credentials()['api_key'], 'bob')
        self.assertEqual(opener.open.call_count, 2)

    def test_missing_authenticated_turn_never_uses_shared_secret(self):
        with mock.patch.dict(os.environ, {**self.env, 'NOUS_API_KEY_FILE': '/tmp/unused'}, clear=True):
            with self.assertRaises(RuntimeError): inference.resolve_credentials()

    def test_new_install_defaults_to_nous_and_upgrades_preserve_foundry(self):
        with mock.patch.dict(os.environ, {k: v for k, v in self.env.items() if k != 'INFERENCE_PROVIDER'}, clear=True):
            self.assertEqual(deployment.apply({}, new_install=True)['INFERENCE_PROVIDER'], 'nous')
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(deployment.apply({})['INFERENCE_PROVIDER'], 'azure-foundry')

    def test_deployment_propagates_only_references_not_credentials(self):
        with mock.patch.dict(os.environ, {**self.env, 'NOUS_API_KEY': 'synthetic-never-propagated'}, clear=True):
            config = deployment.apply({})
            self.assertNotIn('NOUS_API_KEY', config)
            self.assertNotIn('NOUS_API_KEY_FILE', config)
            self.assertNotIn('NOUS_API_KEY_SECRET_URI', config)

    def test_upgrade_preserves_options_unless_explicitly_changed(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(deployment.apply(dict(self.env)), self.env)
