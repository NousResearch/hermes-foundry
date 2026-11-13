import copy
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/gateway'))
from portal_auth import EncryptedStore, PortalAuth, PortalError

ENDPOINT = 'https://inference-api.nousresearch.com/v1'
CONTEXT = {'tenant': '11111111-1111-1111-1111-111111111111',
           'agent': '22222222-2222-2222-2222-222222222222',
           'caller': '33333333-3333-3333-3333-333333333333'}
TOKENS = {'access_token': 'alice-access', 'refresh_token': 'alice-refresh', 'expires_in': 900,
          'scope': 'inference:invoke', 'inference_base_url': ENDPOINT}
DEVICE = {'device_code': 'private-device', 'user_code': 'AAAA-BBBB', 'expires_in': 600, 'interval': 5,
          'verification_uri_complete': 'https://portal.nousresearch.com/device?user_code=AAAA-BBBB'}


class MemoryGraph:
    def __init__(self):
        self.files = {}
        self.counter = 0
    def request(self, method, path, body=None, headers=None, binary=False):
        path = path.split('?')[0].removesuffix(':/content')
        item = self.files.get(path)
        if method == 'GET':
            if not item: return 404, {}, b''
            return 200, {}, item[1] if binary else json.dumps({'eTag': item[0]}).encode()
        headers = headers or {}
        if item and headers.get('If-Match') != item[0]: return 412, {}, b''
        if not item and headers.get('If-None-Match') != '*': return 412, {}, b''
        self.counter += 1
        etag = str(self.counter)
        self.files[path] = etag, body
        return 200, {}, json.dumps({'eTag': etag}).encode()


class PortalAuthTests(unittest.TestCase):
    def setUp(self):
        self.graph = MemoryGraph()
        self.store = EncryptedStore(self.graph, 'test-key-' * 8, CONTEXT, ENDPOINT)
        self.oauth = Mock()
        self.auth = PortalAuth(self.store, ENDPOINT, self.oauth, clock=lambda: 1000)

    def test_encrypts_tokens_and_isolates_users_agents_and_tenants(self):
        self.store.save(TOKENS, '')
        encoded = next(iter(self.graph.files.values()))[1]
        self.assertNotIn(b'alice', encoded)
        self.assertNotIn(b'access_token', encoded)
        self.assertEqual(self.store.load()[0], TOKENS)
        for field in CONTEXT:
            ctx = {**CONTEXT, field: '44444444-4444-4444-4444-444444444444'}
            other = EncryptedStore(self.graph, 'test-key-' * 8, ctx, ENDPOINT)
            self.assertEqual(other.load()[0], {})
            self.graph.files[other.path.removesuffix(':/content')] = next(iter(self.graph.files.values()))
            with self.assertRaises(PortalError): other.load()

    def test_no_identity_or_encryption_key_fails_closed(self):
        for ctx in ({}, {**CONTEXT, 'caller': ''}):
            with self.assertRaises(PortalError): EncryptedStore(self.graph, 'key' * 20, ctx, ENDPOINT)
        with self.assertRaises(PortalError): EncryptedStore(self.graph, '', CONTEXT, ENDPOINT)

    def test_browser_login_approval_and_turn_credential(self):
        self.oauth.post.side_effect = [DEVICE, TOKENS]
        reply, credential = self.auth.handle('login', True)
        self.assertIn(DEVICE['verification_uri_complete'], reply)
        self.assertIsNone(credential)
        self.auth.clock = lambda: 1006
        reply, credential = self.auth.handle('status', True)
        self.assertIn('connected', reply)
        reply, credential = self.auth.handle('', True)
        self.assertIsNone(reply)
        self.assertEqual(credential['api_key'], 'alice-access')
        self.assertEqual(self.oauth.post.call_count, 2)

    def test_no_browser_code_for_noninteractive_turn(self):
        reply, credential = self.auth.handle('', False)
        self.assertIn('one-to-one', reply)
        self.oauth.post.assert_not_called()

    def test_poll_interval_pending_and_slow_down(self):
        self.oauth.post.return_value = DEVICE
        self.auth.handle('login', True)
        self.auth.handle('status', True)
        self.assertEqual(self.oauth.post.call_count, 1)
        self.auth.clock = lambda: 1006
        self.oauth.post.return_value = {'error': 'slow_down'}
        self.auth.handle('status', True)
        state, _ = self.store.load()
        self.assertEqual(state['interval'], 10)
        self.assertEqual(state['next_poll'], 1016)

    def test_refresh_consumes_old_token_before_network_and_persists_rotation(self):
        self.store.save({**TOKENS, 'expires_at': 1001}, '')
        def refresh(*args):
            self.assertEqual(args[2], {'x-nous-refresh-token': 'alice-refresh'})
            state, _ = self.store.load()
            self.assertTrue(state['busy'])
            self.assertNotIn('refresh_token', state)
            other = PortalAuth(self.store, ENDPOINT, Mock(), clock=lambda: 1000)
            self.assertIn('processed', other.handle()[0])
            other.oauth.post.assert_not_called()
            return {**TOKENS, 'access_token': 'new-access', 'refresh_token': 'new-refresh'}
        self.oauth.post.side_effect = refresh
        _, token = self.auth.handle()
        self.assertEqual(token['api_key'], 'new-access')
        self.assertEqual(self.store.load()[0]['refresh_token'], 'new-refresh')

    def test_etag_conflict_cannot_replay_refresh_token(self):
        stale = self.store.save({**TOKENS, 'expires_at': 1001}, '')
        self.store.save({'busy': True, 'started': 1000}, stale)
        self.store.load = Mock(return_value=({**TOKENS, 'expires_at': 1001}, stale))
        with self.assertRaises(PortalError): self.auth.handle()
        self.oauth.post.assert_not_called()

    def test_crash_does_not_replay_refresh_token(self):
        self.store.save({**TOKENS, 'expires_at': 1001}, '')
        self.oauth.post.side_effect = PortalError('network failure')
        with self.assertRaises(PortalError): self.auth.handle()
        self.assertTrue(self.store.load()[0]['busy'])
        self.assertIn('processed', self.auth.handle()[0])
        self.assertEqual(self.oauth.post.call_count, 1)
        self.auth.clock = lambda: 1100
        self.assertIn('one-to-one', self.auth.handle()[0])

    def test_rejects_cross_environment_token_and_external_login_link(self):
        self.oauth.post.return_value = {**DEVICE, 'verification_uri_complete': 'https://evil.test'}
        with self.assertRaises(PortalError): self.auth.handle('login', True)
        self.assertEqual(self.store.load()[0], {})
        with self.assertRaises(PortalError):
            self.auth._tokens({**TOKENS, 'inference_base_url': 'https://evil.test'})

    def test_logout_disconnects_only_current_user_and_revokes_session(self):
        self.store.save({**TOKENS, 'expires_at': 1900}, '')
        other = EncryptedStore(self.graph, 'test-key-' * 8,
                               {**CONTEXT, 'caller': '44444444-4444-4444-4444-444444444444'}, ENDPOINT)
        other.save({'access_token': 'bob'}, '')
        reply, _ = self.auth.handle('logout', True)
        self.assertIn('Signed out', reply)
        self.assertEqual(self.store.load()[0], {})
        self.assertEqual(other.load()[0]['access_token'], 'bob')
        self.oauth.post.assert_called_once_with('/api/oauth/revoke', {'token': 'alice-refresh'})
