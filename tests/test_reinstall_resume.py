import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import reinstall_agent as reinstall
import wait_foundry_state as waiting


class WaitTests(unittest.TestCase):
    def test_transitions_and_timeout_are_bounded_without_writes(self):
        states = iter([{'status': 'creating'}, {'status': 'active'}])
        self.assertEqual('active', waiting.wait(lambda: next(states), 'active', sleep=lambda _: None)['status'])
        with self.assertRaisesRegex(RuntimeError, 'Timed out'):
            waiting.wait(lambda: {'status': 'creating'}, 'active', timeout=0)
        with self.assertRaisesRegex(RuntimeError, 'non-ready'):
            waiting.wait(lambda: {'status': 'failed'}, 'active')


class ReinstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(TENANT_ID='tenant', SUBSCRIPTION_ID='sub', ACCOUNT='acct', PROJECT='proj',
                        AGENT='agent', BLUEPRINT_ID='blueprint', REG='reg',
                        HERMES_DEPLOY_RUN_ID='11111111-1111-1111-1111-111111111111')
        self.calls = []
        self.version_calls = 0
        self.route_failure = False
        self.version_failure = False
        self.image = 'reg.azurecr.io/hermes-full-foundry@sha256:' + 'a' * 64
        self.reads = []
        for name, replacement in [('ROOT', self.root), ('fingerprint', lambda _: 'source'),
                                  ('execute', self.execute), ('az_json', self.az)]:
            p = patch.object(reinstall, name, replacement); p.start(); self.addCleanup(p.stop)

    def az(self, *args):
        self.reads.append(args)
        if args[0] == 'acr': return {'digest': 'sha256:' + 'a' * 64}
        if '/versions?' in args[-1]:
            return {'data': [{'version': '4', 'definition': {'image': self.image}}]}
        return {'blueprint': {'client_id': 'blueprint'}, 'agent_endpoint': {'version_selector': {
            'version_selection_rules': [{'agent_version': '3', 'traffic_percentage': 100}]}}}

    def execute(self, args, env):
        self.calls.append(args[0])
        if args[0] == './new-version.sh':
            self.version_calls += 1
            path = Path(env['HERMES_VERSION_RESULT_FILE'])
            reinstall.save(path, dict(previous='3', version='4', image=self.image, verified=not self.version_failure))
            if self.version_failure: raise subprocess.CalledProcessError(1, args)
        if args[0].endswith('route-agent-version.sh') and self.route_failure:
            raise subprocess.CalledProcessError(1, args)

    def test_route_failure_resume_does_not_rebuild_or_recreate(self):
        self.route_failure = True
        with self.assertRaises(subprocess.CalledProcessError): reinstall.reinstall(self.env)
        self.route_failure = False
        reinstall.reinstall(self.env)
        self.assertEqual(1, self.calls.count('./build.sh'))
        self.assertEqual(1, self.version_calls)
        self.assertEqual(2, self.calls.count('./scripts/route-agent-version.sh'))

    def test_readiness_failure_resumes_existing_version_validation(self):
        self.version_failure = True
        with self.assertRaises(subprocess.CalledProcessError): reinstall.reinstall(self.env)
        self.version_failure = False
        reinstall.reinstall(self.env)
        self.assertEqual(1, self.calls.count('./build.sh'))
        self.assertEqual(2, self.version_calls)
        result = next(self.root.glob('deployment-output/reinstalls/*.version.json'))
        self.assertTrue(json.loads(result.read_text())['verified'])

    def test_changed_inputs_do_not_reuse_old_release(self):
        reinstall.reinstall(self.env)
        with patch.object(reinstall, 'fingerprint', return_value='different'):
            with self.assertRaisesRegex(RuntimeError, 'inputs or target changed'):
                reinstall.reinstall(self.env)

    def test_lost_post_response_is_reconciled_by_image(self):
        self.route_failure = True
        with self.assertRaises(subprocess.CalledProcessError): reinstall.reinstall(self.env)
        result = next(self.root.glob('deployment-output/reinstalls/*.version.json'))
        result.unlink()
        reinstall.save(result.with_suffix(result.suffix + '.pending'), {'creation_requested': True})
        self.route_failure = False
        reinstall.reinstall(self.env)
        self.assertTrue(any('/versions?' in args[-1] for args in self.reads))
        self.assertEqual(1, self.calls.count('./build.sh'))

class PublicationCheckpointTests(unittest.TestCase):
    def test_completed_publication_skips_and_unknown_does_not_repost(self):
        from publication_checkpoint import checkpoint
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'publication.json'
            self.assertEqual('submit', checkpoint(path, 'begin'))
            with self.assertRaisesRegex(RuntimeError, 'uncertain'):
                checkpoint(path, 'begin')
            checkpoint(path, 'complete')
            self.assertEqual('complete', checkpoint(path, 'begin'))

    def test_definitive_rejection_allows_corrected_retry(self):
        from publication_checkpoint import checkpoint
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'publication.json'
            checkpoint(path, 'begin')
            checkpoint(path, 'rejected')
            self.assertEqual('submit', checkpoint(path, 'begin'))

class ReadbackTests(unittest.TestCase):
    def test_detects_image_protocol_and_environment_changes_without_printing_values(self):
        from verify_hosted_version import verify
        expected = {'definition': {'image': 'expected', 'environment_variables': {'secret': 'hidden'}}}
        verify(expected, dict(expected, status='active'))
        for actual in [{'definition': {'image': 'wrong'}}, {'definition': {'image': 'expected', 'environment_variables': {'secret': 'wrong'}}}]:
            with self.assertRaises(ValueError) as error:
                verify(expected, actual)
            self.assertNotIn('hidden', str(error.exception))

    def test_eventual_egress_readback_repeats_only_reads(self):
        from configure_inference_egress import wait_readback
        values = iter([{}, {'ready': True}])
        def validate(value):
            if not value.get('ready'): raise RuntimeError('stale')
        self.assertEqual({'ready': True}, wait_readback(lambda: next(values), validate, sleep=lambda _: None))
