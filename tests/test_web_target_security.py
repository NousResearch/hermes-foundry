import importlib.util
import pathlib
import socket
import sys
import types
import unittest
from unittest import mock


class _FakeFastMCP:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self, *args, **kwargs):
        return lambda fn: fn


def _load_sidecar():
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = _FakeFastMCP
    server = types.ModuleType("mcp.server")
    server.fastmcp = fastmcp
    mcp = types.ModuleType("mcp")
    mcp.server = server
    with mock.patch.dict(sys.modules, {
        "mcp": mcp,
        "mcp.server": server,
        "mcp.server.fastmcp": fastmcp,
    }):
        path = pathlib.Path(__file__).parents[1] / "src/sidecar/agent365_mcp.py"
        spec = importlib.util.spec_from_file_location("hfull_web_security_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class WebTargetSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load_sidecar()

    def resolve_as(self, *addresses):
        return mock.patch.object(
            self.m.socket,
            "getaddrinfo",
            return_value=[
                (socket.AF_INET6 if ":" in address else socket.AF_INET,
                 socket.SOCK_STREAM, 6, "", (address, 443))
                for address in addresses
            ],
        )

    def test_public_https_target_is_accepted(self):
        with self.resolve_as("203.0.113.10"):
            # Documentation ranges are not globally routable and must not be used as a success
            # fixture. Use a real globally classified address without contacting it.
            with self.assertRaises(ValueError):
                self.m._public_https_url("https://example.com/report")
        with self.resolve_as("1.1.1.1"):
            self.assertEqual(
                "https://example.com/report",
                self.m._public_https_url("https://example.com/report"),
            )

    def test_private_ipv4_ipv6_and_mixed_dns_are_refused(self):
        for addresses in (("127.0.0.1",), ("169.254.169.254",), ("::1",),
                          ("1.1.1.1", "10.0.0.8")):
            with self.subTest(addresses=addresses), self.resolve_as(*addresses):
                with self.assertRaisesRegex(ValueError, "private, local, or reserved"):
                    self.m._public_https_url("https://example.com/")

    def test_credentials_http_custom_ports_and_unresolved_hosts_are_refused(self):
        for value in (
            "http://example.com/",
            "https://user:password@example.com/",
            "https://example.com:8443/",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.m._public_https_url(value)
        with mock.patch.object(self.m.socket, "getaddrinfo", side_effect=socket.gaierror()):
            with self.assertRaisesRegex(ValueError, "could not be resolved"):
                self.m._public_https_url("https://missing.example/")

    def test_redirect_handler_revalidates_destination_before_following(self):
        handler = self.m._PublicHttpsRedirectHandler()
        request = self.m.urllib.request.Request("https://example.com/")
        with self.resolve_as("10.0.0.8"):
            with self.assertRaises(ValueError):
                handler.redirect_request(
                    request, None, 302, "Found", {}, "https://internal.example/secret"
                )


if __name__ == "__main__":
    unittest.main()
