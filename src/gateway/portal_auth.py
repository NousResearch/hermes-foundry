"""Caller-bound Nous browser OAuth. No deployment-wide portal credential.

Encrypted records live outside Hermes memory. ETag compare-and-swap consumes each
rotating credential BEFORE contacting OAuth; an ambiguous crash requires sign-in
again rather than replaying a refresh token across concurrent hosted sandboxes.
"""
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PORTAL = 'https://portal.nousresearch.com'
CLIENT_ID = 'hermes-cli'
MAX_BYTES = 65536


class PortalError(RuntimeError):
    pass


class EncryptedStore:
    def __init__(self, transport, key, context, endpoint):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if not key or len(key) < 32:
            raise PortalError('Portal credential storage is not configured.')
        try:
            binding = {k: str(uuid.UUID(context[k])) for k in ('tenant', 'agent', 'caller')}
        except (ValueError, KeyError, TypeError):
            raise PortalError('Portal login requires a verified Microsoft 365 user identity.') from None
        self.aad = json.dumps({**binding, 'endpoint': endpoint, 'version': 1}, sort_keys=True).encode()
        self.cipher = AESGCM(hashlib.sha256(b'hermes-portal-oauth-v1\0' + key.encode()).digest())
        self.io = transport
        name = 'hermes-portal-credentials-' + hashlib.sha256(self.aad).hexdigest() + '.json'
        self.path = 'me/drive/special/approot:/' + name + ':/content'

    def load(self):
        status, _, data = self.io.request('GET', self.path.removesuffix(':/content') + '?$select=eTag')
        if status == 404:
            return {}, ''
        if status != 200:
            raise PortalError('Portal session storage is unavailable. Please retry.')
        try:
            etag = json.loads(data)['eTag']
            status, _, data = self.io.request('GET', self.path, binary=True)
            if status != 200 or len(data) > MAX_BYTES or not etag:
                raise ValueError()
            encrypted = base64.b64decode(json.loads(data)['encrypted'], validate=True)
            state = json.loads(self.cipher.decrypt(encrypted[:12], encrypted[12:], self.aad))
            if not isinstance(state, dict):
                raise ValueError()
            return state, etag
        except Exception:
            raise PortalError('Portal session could not be verified. Contact the administrator.') from None

    def save(self, state, etag):
        nonce = os.urandom(12)
        encrypted = nonce + self.cipher.encrypt(nonce, json.dumps(state).encode(), self.aad)
        body = json.dumps({'encrypted': base64.b64encode(encrypted).decode()}).encode()
        if len(body) > MAX_BYTES:
            raise PortalError('Portal session exceeds its storage limit.')
        path = self.path if etag else self.path + '?@microsoft.graph.conflictBehavior=fail'
        headers = {'If-Match': etag} if etag else {'If-None-Match': '*'}
        status, _, data = self.io.request('PUT', path, body, headers)
        if status not in (200, 201):
            raise PortalError('Your portal session changed in another request. Please retry.')
        try:
            return json.loads(data)['eTag']
        except Exception:
            # Never send a rotating credential when we cannot condition its final write.
            raise PortalError('Portal session storage did not confirm the update. Please retry.') from None


class OAuthClient:
    def post(self, path, fields, headers=None):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        request = urllib.request.Request(PORTAL + path,
            data=urllib.parse.urlencode({'client_id': CLIENT_ID, **fields}).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded', **(headers or {})})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
                data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read(MAX_BYTES))
                code = data.get('error')
            except Exception:
                code = None
            finally:
                exc.close()
            if code in {'authorization_pending', 'slow_down', 'access_denied', 'expired_token', 'invalid_grant'}:
                return {'error': code}
            raise PortalError('Portal sign-in request failed. Please retry.') from None
        except Exception:
            raise PortalError('Portal sign-in service is unavailable. Please retry.') from None


class PortalAuth:
    def __init__(self, store, endpoint, oauth=None, clock=time.time):
        self.store, self.endpoint = store, endpoint
        self.oauth, self.clock = oauth or OAuthClient(), clock

    def _tokens(self, data):
        if (data.get('error') or not data.get('access_token') or not data.get('refresh_token')
                or data.get('inference_base_url', self.endpoint).rstrip('/') != self.endpoint
                or 'inference:invoke' not in data.get('scope', 'inference:invoke').split()):
            raise PortalError('Portal authorization was not completed. Run /portal login again.')
        ttl = int(data.get('expires_in', 0))
        if not 0 < ttl <= 86400:
            raise PortalError('Portal returned an invalid token lifetime.')
        return {'access_token': data['access_token'], 'refresh_token': data['refresh_token'],
                'expires_at': self.clock() + ttl}

    @staticmethod
    def _link(state):
        return ('Sign in to your own Nous portal account in your browser:\n' + state['url'] +
                '\nCode: ' + state['user_code'] +
                '\nAfter approving, send /portal status here. This connects only your account for this agent.')

    def handle(self, command='', interactive=False):
        """Return (direct reply or None, ephemeral inference credential or None)."""
        state, etag = self.store.load()
        now = self.clock()
        if command == 'logout':
            self.store.save({}, etag)
            if state.get('refresh_token'):
                try:
                    self.oauth.post('/api/oauth/revoke', {'token': state['refresh_token']})
                except PortalError:
                    return 'Signed out from this agent. Portal revocation could not be confirmed; revoke the session in the portal too.', None
            return 'Signed out from this agent. In-flight requests may finish with their short-lived token.', None
        if state.get('busy'):
            if now - state.get('started', now) < 90:
                return 'Portal sign-in is being processed. Please retry shortly.', None
            # The last process may have consumed a token before it stopped. Never replay it.
            state = {}
        if state.get('pending'):
            if state['expires_at'] <= now:
                self.store.save({}, etag)
                return 'Portal sign-in expired. Send /portal login to start again.', None
            if now < state['next_poll']:
                return 'Finish browser sign-in, then send /portal status after a few seconds.', None
            claim = self.store.save({'busy': True, 'started': now}, etag)
            data = self.oauth.post('/api/oauth/token', {
                'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': state['device_code']})
            if data.get('error') in ('authorization_pending', 'slow_down'):
                state['interval'] += 5 if data['error'] == 'slow_down' else 0
                state['next_poll'] = now + state['interval']
                self.store.save(state, claim)
                return 'Waiting for your browser approval. Then send /portal status.', None
            try:
                state = self._tokens(data)
            except PortalError:
                self.store.save({}, claim)
                raise
            etag = self.store.save(state, claim)
        if state.get('access_token'):
            if state['expires_at'] <= now + 120:
                claim = self.store.save({'busy': True, 'started': now}, etag)
                data = self.oauth.post('/api/oauth/token', {'grant_type': 'refresh_token'},
                                       {'x-nous-refresh-token': state['refresh_token']})
                try:
                    state = self._tokens(data)
                except PortalError:
                    self.store.save({}, claim)
                    raise
                self.store.save(state, claim)
            if command:
                return 'Your Nous portal account is connected to this agent. Use /portal logout to disconnect.', None
            return None, {'provider': 'nous', 'base_url': self.endpoint,
                          'api_key': state['access_token'], 'source': 'foundry-user-oauth',
                          'expires_at': state['expires_at']}
        if command == 'status':
            return 'You are not signed in. Send /portal login in a one-to-one Teams chat.', None
        if not interactive:
            return 'Connect your own Nous portal account with /portal login in a one-to-one Teams chat with this agent.', None
        data = self.oauth.post('/api/oauth/device/code', {'scope': 'inference:invoke'})
        try:
            url = urllib.parse.urlsplit(data['verification_uri_complete'])
            if url.scheme != 'https' or url.hostname != 'portal.nousresearch.com' or url.username or url.password or url.port not in (None, 443):
                raise ValueError()
            state = {'pending': True, 'device_code': data['device_code'],
                     'url': data['verification_uri_complete'], 'user_code': data['user_code'],
                     'expires_at': now + min(1800, max(1, int(data['expires_in']))),
                     'interval': max(1, int(data['interval']))}
            state['next_poll'] = now + state['interval']
        except Exception:
            raise PortalError('Portal returned an invalid sign-in request.') from None
        self.store.save(state, etag)
        return self._link(state), None
