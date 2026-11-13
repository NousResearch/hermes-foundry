"""Public HTTPS transport with DNS validation bound to the actual socket connection.

No environment proxies: a proxy could resolve the hostname again or reach private networks.
TLS still verifies the original hostname and uses it for SNI.
"""
import http.client
import ipaddress
import socket
import ssl
import urllib.parse
import urllib.request


def public_addresses(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("host resolves to a private, local, or reserved address")
    return addresses


def public_https_url(url):
    value = (url or "").strip()
    if not value or any(ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("URL is empty or contains invalid characters")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme.lower() != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise ValueError("URL must be public HTTPS on the default port")
    try:
        public_addresses(parsed.hostname)
    except socket.gaierror as exc:
        raise ValueError("host could not be resolved") from exc
    return value


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        if self._tunnel_host:
            raise ValueError("HTTPS tunnels are not supported")
        if self.port != 443:
            raise ValueError("Only HTTPS port 443 is allowed")
        # Resolve once, validate ALL answers, then connect directly to a numeric sockaddr.
        # Never call create_connection(host), which would perform a second DNS lookup.
        addresses = public_addresses(self.host)
        last_error = None
        for family, kind, proto, _, address in addresses:
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as exc:
                sock.close()
                last_error = exc
        raise last_error or OSError("No usable public address")


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        # Validate syntax as well as addresses on every redirect/request.
        public_https_url(request.full_url)
        return self.do_open(PinnedHTTPSConnection, request, context=ssl.create_default_context())


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_https_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            for name in ("Authorization", "Cookie", "Proxy-Authorization"):
                redirected.remove_header(name)
        return redirected


def public_open(request, timeout=45):
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    public_https_url(url)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), PinnedHTTPSHandler(), PublicRedirectHandler()
    ).open(request, timeout=timeout)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def graph_open(request, timeout=60):
    """Authenticated Graph calls never follow redirects, including write redirects."""
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def graph_download(url, timeout=120):
    """Follow a Graph-issued download link without sending the Graph credential."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if not (host.endswith(".sharepoint.com") or host.endswith(".files.1drv.com")):
        raise ValueError("Unexpected Graph download destination")
    # Each hop is public HTTPS and the request contains no Graph headers.
    return public_open(url, timeout=timeout)
