import asyncio
import email.message
import io
import os
import socket
import ssl
import sys
import unittest
import urllib.error
import urllib.request
import urllib.response
from unittest import mock
from test_graph_contracts import _load_sidecar, _Response, _http_error

m = _load_sidecar()
import secure_http as http


class TransportTests(unittest.TestCase):
    def test_authenticated_graph_transport_does_not_follow_redirect(self):
        seen = []
        class FakeHTTPS(urllib.request.HTTPSHandler):
            def https_open(self, req):
                seen.append(req)
                headers = email.message.Message()
                headers['Location'] = 'https://download.example.com/content'
                response = urllib.response.addinfourl(io.BytesIO(b''), headers, req.full_url, 302)
                response.msg = 'Found'
                return response
        real_builder = urllib.request.build_opener
        with mock.patch.object(http.urllib.request, 'build_opener',
                               side_effect=lambda *handlers: real_builder(FakeHTTPS(), *handlers)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                http.graph_open(urllib.request.Request('https://graph.microsoft.com/v1.0/me',
                                headers={'Authorization': 'Bearer synthetic'}))
            caught.exception.close()
        self.assertEqual(len(seen), 1)

    def test_generic_and_binary_downloads_use_credential_free_transport(self):
        for binary in (False, True):
            error = _http_error(302, headers={'Location': 'https://example.sharepoint.com/content'})
            with mock.patch.object(m, '_caller_tier', return_value='owner'), \
                 mock.patch.object(m, '_reserved_access_path', return_value=False), \
                 mock.patch.object(m, '_token', return_value='synthetic'), \
                 mock.patch.object(m, '_graph_open', side_effect=error), \
                 mock.patch.object(m, '_graph_download', return_value=_Response(b'report')) as download:
                if binary:
                    self.assertEqual(m._graph_bytes('GET', 'me/drive/root:/report:/content'), (b'report', None))
                else:
                    self.assertEqual(m.graph_query('me/drive/root:/report:/content'), 'report')
            self.assertTrue(error.closed)
            self.assertEqual(download.call_args.args, ('https://example.sharepoint.com/content',))

    def test_graph_write_and_noncontent_redirects_are_not_followed(self):
        for method, path in [('POST', 'me/sendMail'), ('GET', 'me/messages')]:
            with mock.patch.object(m, '_caller_tier', return_value='owner'), \
                 mock.patch.object(m, '_token', return_value='synthetic'), \
                 mock.patch.object(m, '_graph_open', side_effect=_http_error(302)), \
                 mock.patch.object(m, '_graph_download') as download:
                self.assertIn('redirect refused', m._graph(method, path))
                download.assert_not_called()

    def test_download_destination_rejected_before_request(self):
        for url in ('https://evil.example/file', 'http://example.sharepoint.com/file',
                    'https://example.sharepoint.com:8443/file'):
            with mock.patch.object(http.socket, 'getaddrinfo') as dns:
                with self.assertRaises(ValueError):
                    http.graph_download(url)
                dns.assert_not_called()

    def test_download_request_has_no_authorization(self):
        with mock.patch.object(http, 'public_open', return_value='response') as opened:
            self.assertEqual(http.graph_download('https://example.sharepoint.com/file'), 'response')
        self.assertEqual(opened.call_args.args, ('https://example.sharepoint.com/file',))

    def test_connect_uses_validated_numeric_address_and_original_tls_name(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]
        context = mock.MagicMock()
        with mock.patch.object(http.socket, 'getaddrinfo', side_effect=[answer, AssertionError('second DNS lookup')]) as dns, \
             mock.patch.object(http.socket, 'socket') as sock:
            connection = http.PinnedHTTPSConnection('example.com', timeout=4, context=context)
            connection.connect()
            sock.return_value.connect.assert_called_once_with(('1.1.1.1', 443))
            context.wrap_socket.assert_called_once_with(sock.return_value, server_hostname='example.com')
            self.assertEqual(dns.call_count, 1)

    def test_private_or_mixed_dns_answers_never_open_socket(self):
        for addresses in (['127.0.0.1'], ['1.1.1.1', '10.0.0.1'], ['169.254.169.254']):
            answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in addresses]
            with mock.patch.object(http.socket, 'getaddrinfo', return_value=answer), \
                 mock.patch.object(http.socket, 'socket') as sock:
                with self.assertRaises(ValueError):
                    http.PinnedHTTPSConnection('example.com', timeout=1).connect()
                sock.assert_not_called()

    def test_tls_failure_closes_socket_and_does_not_disable_verification(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]
        context = mock.MagicMock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError('synthetic failure')
        with mock.patch.object(http.socket, 'getaddrinfo', return_value=answer), \
             mock.patch.object(http.socket, 'socket') as sock:
            with self.assertRaises(ssl.SSLCertVerificationError):
                http.PinnedHTTPSConnection('example.com', timeout=1, context=context).connect()
            sock.return_value.close.assert_called_once()

    def test_dns_rebinding_between_validation_and_connect_is_rejected(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with mock.patch.object(http.socket, 'getaddrinfo', side_effect=[public, private]), \
             mock.patch.object(http.socket, 'socket') as sock:
            http.public_https_url('https://example.com/')
            with self.assertRaises(ValueError):
                http.PinnedHTTPSConnection('example.com', timeout=1).connect()
            sock.assert_not_called()

    def test_public_fetch_does_not_use_environment_proxies(self):
        with mock.patch.object(http, 'public_https_url'), \
             mock.patch.object(http.urllib.request, 'build_opener') as builder:
            http.public_open('https://example.com')
            handlers = builder.call_args.args
            proxy = next(h for h in handlers if isinstance(h, urllib.request.ProxyHandler))
            self.assertEqual(proxy.proxies, {})
            self.assertTrue(any(isinstance(h, http.PinnedHTTPSHandler) for h in handlers))

    def test_public_redirect_drops_cross_origin_credentials(self):
        req = urllib.request.Request('https://example.com/a',
            headers={'Authorization': 'synthetic', 'Cookie': 'synthetic'})
        with mock.patch.object(http, 'public_https_url', side_effect=lambda url: url):
            redirected = http.PublicRedirectHandler().redirect_request(
                req, None, 302, 'Found', {}, 'https://example.org/b')
        self.assertIsNone(redirected.get_header('Authorization'))
        self.assertIsNone(redirected.get_header('Cookie'))


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_tools_fail_closed_before_provisioning(self):
        with mock.patch.dict(os.environ, {'BROWSER_PUBLIC_EGRESS_ENFORCED': ''}), \
             mock.patch.object(m, '_caller_tier', return_value='owner'), \
             mock.patch.object(m, '_browser_cdp_url') as provision:
            for tool in (m.browse_url, m.screenshot_url):
                self.assertIn('browser disabled', await tool('https://example.com'))
            provision.assert_not_called()

    async def test_browser_context_is_isolated_and_checks_all_requests(self):
        browser = mock.MagicMock()
        ctx = mock.MagicMock()
        ctx.route = mock.AsyncMock()
        ctx.route_web_socket = mock.AsyncMock()
        browser.new_context = mock.AsyncMock(return_value=ctx)
        await m._public_browser_context(browser)
        browser.new_context.assert_awaited_once_with(service_workers='block', accept_downloads=False)
        handler = ctx.route.call_args.args[1]
        for allowed in (False, True):
            route = mock.MagicMock()
            route.request.url = 'https://example.com/subrequest'
            route.abort = mock.AsyncMock()
            route.continue_ = mock.AsyncMock()
            with mock.patch.object(m, '_public_https_url', side_effect=None if allowed else ValueError('private')):
                await handler(route)
            self.assertEqual(route.continue_.await_count, int(allowed))
            self.assertEqual(route.abort.await_count, int(not allowed))
        ws = mock.MagicMock(close=mock.AsyncMock())
        await ctx.route_web_socket.call_args.args[1](ws)
        ws.close.assert_awaited_once()
