import copy
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import configure_inference_egress as egress
import inference_settings as setup


class EgressTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(setup, 'catalog_version', return_value='2026-09-29')
        self.catalog = patcher.start()
        self.addCleanup(patcher.stop)
        self.env = {'INFERENCE_PROVIDER': 'nous', 'NOUS_MODEL': 'hermes'}

    def test_destinations_exact_and_provider_specific(self):
        self.assertEqual(egress.destinations(self.env),
                         ['inference-api.nousresearch.com', 'portal.nousresearch.com'])
        self.assertEqual(egress.destinations({'INFERENCE_PROVIDER': 'azure-foundry'}), [])

    def test_preserves_deny_and_content_policy_and_precedes_catchall(self):
        original = {'properties': {'mode': 'Blocking', 'contentFilters': [{'name': 'test'}],
            'egressPolicy': {'mode': 'Enforced', 'defaultAction': 'Deny', 'rules': [
                {'name': 'deny', 'match': {'host': '*'}, 'action': {'actionType': 'Deny'}}]}}}
        saved = copy.deepcopy(original)
        result = egress.policy_with_allows(original, egress.destinations(self.env))
        self.assertEqual(original, saved)
        props = result['properties']
        self.assertEqual(props['contentFilters'], original['properties']['contentFilters'])
        self.assertEqual(props['egressPolicy']['defaultAction'], 'Deny')
        self.assertEqual(props['egressPolicy']['rules'][0]['action']['actionType'], 'Allow')
        self.assertEqual(props['egressPolicy']['rules'][-1]['name'], 'deny')
        self.assertEqual(egress.policy_with_allows(result, egress.destinations(self.env)), result)

    def test_does_not_introduce_new_deny_for_unrestricted_deployment(self):
        result = egress.policy_with_allows({}, egress.destinations(self.env))
        self.assertEqual(result['properties']['egressPolicy']['defaultAction'], 'Allow')

    def test_policy_attachment_must_survive_api_readback(self):
        with self.assertRaises(RuntimeError):
            egress.verify({'definition': {'rai_config': {'rai_policy_name': 'expected'}}},
                          {'definition': {}})

    def test_configures_managed_firewall_and_attaches_verified_policy(self):
        account = '/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct'
        stored = {}
        def rest(method, resource, api, body=None):
            if resource.endswith('/managedNetworks'):
                return {'value': [{'id': account + '/managedNetworks/default', 'properties': {
                    'managedNetwork': {'isolationMode': 'AllowOnlyApprovedOutbound'}}}]}
            if method == 'PUT':
                stored[resource] = body
            return stored[resource]
        body = {'definition': {'environment_variables': self.env}}
        with mock.patch.object(egress, 'az', return_value=[{'name': 'acct', 'id': account}]), \
             mock.patch.object(egress, 'rest', side_effect=rest), \
             mock.patch.dict(egress.os.environ, {}, clear=True):
            egress.configure(body, 'acct')
        self.assertEqual(len([p for p in stored if '/outboundRules/' in p]), 2)
        self.assertIn(body['definition']['rai_config']['rai_policy_name'], stored)

    def test_setup_collects_choices_without_api_key_prompt(self):
        answers = iter(['nous', 'nous,azure-foundry', 'hermes', '', '', ''])
        result = setup.configure_interactively({}, lambda _: next(answers))
        self.assertEqual(result['INFERENCE_PROVIDER'], 'nous')
        self.assertEqual(result['NOUS_MODEL'], 'hermes')
        self.assertNotIn('NOUS_API_KEY', result)

    def test_setup_can_choose_foundry_without_portal_credentials(self):
        answers = iter(['azure-foundry', 'azure-foundry', '', ''])
        result = setup.configure_interactively({}, lambda _: next(answers))
        self.assertEqual(result['INFERENCE_PROVIDER'], 'azure-foundry')

    def test_readback_accepts_inherited_filters_but_rejects_changed_explicit_filters(self):
        policy = egress.policy_with_allows({}, ['inference-api.nousresearch.com'])
        actual = copy.deepcopy(policy)
        actual['properties']['contentFilters'] = [{'name': 'inherited', 'enabled': True}]
        egress.verify_policy(policy, actual)
        policy['properties']['contentFilters'] = [{'name': 'required', 'enabled': True}]
        with self.assertRaises(RuntimeError):
            egress.verify_policy(policy, actual)

    def test_setup_enter_uses_default_nous_model(self):
        answers = iter(['', '', '', '', '', ''])
        result = setup.configure_interactively({}, lambda _: next(answers))
        self.assertEqual(result['NOUS_MODEL'], 'openai/gpt-6.1-sol')

    def test_setup_uses_selected_foundry_model_and_version(self):
        answers = iter(['azure-foundry', 'azure-foundry', 'openai/custom-model', ''])
        self.catalog.return_value = '2026-10-01'
        result = setup.configure_interactively({}, lambda _: next(answers))
        self.assertEqual(result['FOUNDRY_MODEL_NAME'], 'custom-model')
        self.assertEqual(result['FOUNDRY_MODEL_VERSION'], '2026-10-01')

    def test_custom_foundry_model_never_inherits_unrelated_default_version(self):
        self.catalog.side_effect = ValueError('Model unavailable')
        with self.assertRaisesRegex(ValueError, 'Model unavailable'):
            setup.provision_defaults({'FOUNDRY_MODEL_NAME': 'custom-model'})

    def test_foundry_default_matches_verified_azure_sol_catalog(self):
        result = setup.provision_defaults({})
        self.assertEqual(result['FOUNDRY_MODEL_NAME'], 'gpt-6.1-sol')
        self.assertEqual(result['FOUNDRY_MODEL_VERSION'], '2026-09-29')

    def test_switch_saved_foundry_to_nous_repairs_allowlist_and_prompts(self):
        env = {'INFERENCE_PROVIDER': 'azure-foundry', 'INFERENCE_ALLOWED_PROVIDERS': 'azure-foundry',
               'FOUNDRY_MODEL_NAME': 'gpt-5-mini', 'FOUNDRY_MODEL_VERSION': 'old', 'AZURE_LOCATION': 'eastus2'}
        answers = iter(['nous', '', '', '', 'openai/gpt-6.1-sol'])
        prompts = []
        def ask(prompt):
            prompts.append(prompt)
            return next(answers)
        result = setup.configure_interactively(env, ask)
        self.assertEqual(result['INFERENCE_ALLOWED_PROVIDERS'], 'nous,azure-foundry')
        self.assertEqual(result['NOUS_MODEL'], 'openai/gpt-6.1-sol')
        self.assertEqual(result['FOUNDRY_MODEL_NAME'], 'gpt-6.1-sol')
        self.assertEqual(result['FOUNDRY_MODEL_VERSION'], '2026-09-29')
        self.assertFalse(any('model version' in prompt.lower() for prompt in prompts))
        self.catalog.assert_called_once()

    def test_nous_only_has_no_foundry_model_question(self):
        prompts = []
        answers = iter(['nous', 'nous', '', '', ''])
        def ask(prompt):
            prompts.append(prompt)
            return next(answers)
        setup.configure_interactively({}, ask)
        self.assertFalse(any('Foundry model' in prompt for prompt in prompts))


class CatalogTests(unittest.TestCase):
    def test_catalog_selects_default_supported_version_in_selected_subscription(self):
        import json
        def model(version, default=False, sku='GlobalStandard'):
            return {'model': {'name': 'gpt-6.1-sol', 'format': 'OpenAI', 'version': version,
                              'isDefaultVersion': default, 'skus': [{'name': sku}]}}
        response = mock.Mock(returncode=0, stdout=json.dumps([
            model('2026-09-29', True), model('2026-10-01'), model('2099', True, 'Other')]))
        with mock.patch.object(setup.subprocess, 'run', return_value=response) as run:
            version = setup.catalog_version({'FOUNDRY_MODEL_NAME': 'gpt-6.1-sol',
                'AZURE_LOCATION': 'eastus2', 'AZURE_SUBSCRIPTION_ID': 'selected'})
        self.assertEqual(version, '2026-09-29')
        self.assertIn('selected', run.call_args.args[0])

    def test_empty_catalog_fails_with_actionable_error(self):
        with mock.patch.object(setup.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='[]')):
            with self.assertRaisesRegex(ValueError, 'Choose another model or region'):
                setup.catalog_version({'FOUNDRY_MODEL_NAME': 'missing', 'AZURE_LOCATION': 'eastus2'})

class ModelInputTests(unittest.TestCase):
    def test_portal_adds_only_unambiguous_prefixes_and_normalizes_lists(self):
        result = setup.normalize_portal_models({
            'NOUS_MODEL': ' gpt-6.1-sol ',
            'NOUS_MODELS': 'gpt-5-mini, openai/gpt-5-mini, o3, anthropic/claude-example, hermes'})
        self.assertEqual(result['NOUS_MODEL'], 'openai/gpt-6.1-sol')
        self.assertEqual(result['NOUS_MODELS'], 'openai/gpt-5-mini,openai/o3,anthropic/claude-example,hermes')

    def test_foundry_accepts_bare_and_prefixed_but_rejects_other_providers(self):
        for name in ['gpt-6.1-sol', 'openai/gpt-6.1-sol', ' OpenAI/gpt-6.1-sol ']:
            self.assertEqual(setup.normalize_model(name), 'gpt-6.1-sol')
        with self.assertRaises(ValueError):
            setup.normalize_model('anthropic/claude-example')

    def test_every_model_prompt_has_three_examples_and_normalizes_input(self):
        answers = iter(['nous', '', 'gpt-6.1-sol', 'gpt-5-mini, gpt-4.1', 'openai/gpt-6.1-sol', ''])
        with mock.patch('builtins.print') as output:
            result = setup.configure_interactively({}, lambda _: next(answers), resolver=lambda _: '2026-09-29')
        guidance = [call.args[0] for call in output.call_args_list if 'examples:' in call.args[0]]
        self.assertEqual(len(guidance), 3)
        for message in guidance:
            for name in ['gpt-6.1-sol', 'gpt-5-mini', 'gpt-4.1']:
                self.assertIn(name, message)
        self.assertEqual(result['NOUS_MODEL'], 'openai/gpt-6.1-sol')
        self.assertEqual(result['NOUS_MODELS'], 'openai/gpt-5-mini,openai/gpt-4.1')
        self.assertEqual(result['FOUNDRY_MODEL_NAME'], 'gpt-6.1-sol')
