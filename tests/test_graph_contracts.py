import importlib.util
import io
import json
import os
import pathlib
import sys
import types
import unittest
import urllib.error
from unittest import mock


class _FakeFastMCP:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self, *args, **kwargs):
        return lambda fn: fn


class _Response:
    def __init__(self, body=b"{}", status=200, headers=None):
        self._body = body
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _http_error(code, body=b'{"error":{"code":"failure"}}', headers=None):
    return urllib.error.HTTPError(
        "https://graph.microsoft.com/v1.0/test",
        code,
        "failure",
        headers or {},
        io.BytesIO(body),
    )


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
        path = pathlib.Path(__file__).parents[1] / "src" / "sidecar" / "agent365_mcp.py"
        spec = importlib.util.spec_from_file_location("hfull_graph_contract_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class GraphContractTests(unittest.TestCase):
    def setUp(self):
        self.authority = mock.patch.dict(os.environ, {"AGENT_CALLER_TIER": "owner"})
        self.authority.start()
        self.addCleanup(self.authority.stop)
        self.m = _load_sidecar()
        guard = mock.patch.object(self.m, "_access_request", return_value={"result": True})
        guard.start()
        self.addCleanup(guard.stop)

    def test_sidecar_never_reuses_token_after_turn_authority_disappears(self):
        self.m._TOKEN_URL = "http://127.0.0.1:8088"
        self.m._TOKEN_KEY = ""
        missing = _http_error(404, b'{"error":"no agent turn authority seen yet"}')
        with mock.patch.object(self.m.urllib.request, "urlopen", side_effect=[
            _Response(b'{"token":"turn-token"}'),
            missing,
        ]) as urlopen:
            self.assertEqual(self.m._token(), "turn-token")
            self.assertEqual(self.m._token(), "")

        self.assertEqual(urlopen.call_count, 2)
        self.assertTrue(missing.closed)

    def test_get_honors_numeric_retry_after_and_recovers(self):
        with mock.patch.object(self.m, "_token", return_value="token"), \
             mock.patch.object(self.m, "_graph_open", side_effect=[
                 _http_error(429, headers={"Retry-After": "3"}),
                 _Response(b'{"value":[]}'),
             ]) as urlopen, \
             mock.patch.object(self.m.time, "sleep") as sleep:
            result = self.m._graph("GET", "me/drive/root/children")

        self.assertEqual(json.loads(result), {"value": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(3.0)

    def test_retry_after_is_capped_to_protect_turn_deadline(self):
        self.assertEqual(self.m._retry_delay({"Retry-After": "600"}, 0), 20)

    def test_absolute_graph_escape_hatch_cannot_exfiltrate_bearer_token(self):
        with mock.patch.object(self.m, "_token", return_value="sensitive-token"), \
             mock.patch.object(self.m, "_graph_open") as urlopen:
            result = self.m.graph_query("https://attacker.example/collect")

        self.assertIn("unsafe Graph path refused", result)
        self.assertIn("Nothing was sent", result)
        urlopen.assert_not_called()

    def test_absolute_graph_url_accepts_only_https_v1_endpoint(self):
        accepted = self.m._graph_url(
            "https://graph.microsoft.com/v1.0/me?$select=id")
        self.assertEqual(
            "https://graph.microsoft.com/v1.0/me?$select=id", accepted)
        for value in (
            "http://graph.microsoft.com/v1.0/me",
            "https://graph.microsoft.com.evil.example/v1.0/me",
            "https://graph.microsoft.com/beta/me",
            "//attacker.example/collect",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.m._graph_url(value)

    def test_get_retries_connection_timeout_with_bounded_backoff(self):
        with mock.patch.object(self.m, "_token", return_value="token"), \
             mock.patch.object(self.m, "_graph_open", side_effect=[
                 TimeoutError("timed out"),
                 _Response(b'{"id":"ok"}'),
             ]) as urlopen, \
             mock.patch.object(self.m.time, "sleep") as sleep:
            result = self.m._graph("GET", "me")

        self.assertEqual(json.loads(result), {"id": "ok"})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_mutating_post_is_not_replayed_after_transient_response(self):
        with mock.patch.object(self.m, "_token", return_value="token"), \
             mock.patch.object(self.m, "_graph_open",
                               side_effect=_http_error(503)) as urlopen, \
             mock.patch.object(self.m.time, "sleep") as sleep:
            result = self.m._graph("POST", "me/sendMail", {"message": {}})

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("not automatically retried", result)
        self.assertIn("could duplicate", result)

    def test_401_is_system_fault_and_not_retried(self):
        with mock.patch.object(self.m, "_token", return_value="token"), \
             mock.patch.object(self.m, "_graph_open",
                               side_effect=_http_error(401)) as urlopen:
            result = self.m._graph("GET", "me")

        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("System fault, not a user problem", result)

    def test_403_explains_both_permission_layers(self):
        error = _http_error(403)
        with mock.patch.object(self.m, "_token", return_value="token"), \
             mock.patch.object(self.m, "_graph_open",
                               side_effect=error) as urlopen:
            result = self.m._graph("GET", "me/messages")

        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(error.closed)
        self.assertIn("consent grant", result)
        self.assertIn("inheritablePermissions", result)

    def test_drive_404_has_truthful_terminal_state(self):
        error = _http_error(404, b'{"error":{"code":"itemNotFound"}}')
        with mock.patch.object(self.m, "_token", return_value="token"), \
             mock.patch.object(self.m, "_graph_open", side_effect=error):
            result = self.m._graph("PUT", "me/drive/root:/report.txt:/content", "hello",
                                   raw_body=True)

        self.assertIn("NOTHING was created or changed", result)

    def test_conflict_and_malformed_error_bodies_are_returned_without_crashing(self):
        with mock.patch.object(self.m, "_token", return_value="token"), \
             mock.patch.object(self.m, "_graph_open",
                               side_effect=_http_error(409, b"not-json")):
            result = self.m._graph("POST", "me/events", {"subject": "test"})

        self.assertEqual(result, "ERROR 409 from Graph: not-json")

    def test_locked_docx_upload_retries_four_times_then_reports_unchanged(self):
        errors = [_http_error(423) for _ in range(4)]
        with mock.patch.object(self.m, "_token", return_value="token"), \
             mock.patch.object(self.m, "_graph_open", side_effect=errors) as urlopen, \
             mock.patch.object(self.m.time, "sleep") as sleep:
            result = self.m._graph_put_bytes("me/drive/items/item/content", b"docx")

        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])
        self.assertIn("remained locked after four bounded attempts", result)
        self.assertIn("Nothing was overwritten", result)

    def test_etag_conflict_is_never_retried(self):
        with mock.patch.object(self.m, "_token", return_value="token"), \
             mock.patch.object(self.m, "_graph_open",
                               side_effect=_http_error(412)) as urlopen:
            result = self.m._graph_put_bytes(
                "me/drive/items/item/content", b"docx", if_match='"etag"'
            )

        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("Nothing was overwritten", result)


if __name__ == "__main__":
    unittest.main()
