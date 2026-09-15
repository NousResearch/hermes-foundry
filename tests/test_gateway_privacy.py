import contextlib
import importlib.util
import io
import json
import os
import pathlib
import threading
import time
import unittest
from unittest import mock


def _load_gateway():
    path = pathlib.Path(__file__).parents[1] / "src" / "gateway" / "app.py"
    spec = importlib.util.spec_from_file_location("hermes_gateway_privacy_test", path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"HERMES_URL": "http://127.0.0.1:8000"}):
        spec.loader.exec_module(module)
    return module


class GatewayPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = _load_gateway()

    def test_redactor_removes_credentials_tenant_and_user_identifiers(self):
        jwt = "eyJ" + "header.payload.signature"
        guid = "9f0c" + "8a01-2b7d-4c39-8a11-123456789abc"
        tenant = "company" + ".onmicrosoft.com"
        text = (
            f"user=person@{tenant} id={guid} bearer={jwt} "
            "url=https://example.test/file?sig=secret-value&x=1"
        )

        redacted = self.gateway._redact_log_message(text)

        self.assertNotIn("person@", redacted)
        self.assertNotIn(tenant, redacted)
        self.assertNotIn(guid, redacted)
        self.assertNotIn(jwt, redacted)
        self.assertNotIn("secret-value", redacted)
        self.assertIn("<email>", redacted)
        self.assertIn("<token>", redacted)
        self.assertIn("sig=<redacted>", redacted)

    def test_log_ships_only_the_redacted_message(self):
        output = io.StringIO()
        shipped = []
        with mock.patch.object(self.gateway, "_ship", side_effect=shipped.append):
            with contextlib.redirect_stdout(output):
                self.gateway.log("contact", "person@example.com")

        self.assertEqual(["contact <email>"], shipped)
        self.assertEqual("contact <email>", output.getvalue().strip())

    def test_chat_turns_are_serialized_while_other_posts_are_not(self):
        active = 0
        maximum = 0
        state_lock = threading.Lock()

        class Probe(self.gateway.Handler):
            def _do_POST(probe_self):
                nonlocal active, maximum
                with state_lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with state_lock:
                    active -= 1

        first = object.__new__(Probe)
        second = object.__new__(Probe)
        first.path = second.path = "/v1/chat/completions"
        threads = [threading.Thread(target=item.do_POST) for item in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(1, maximum)

    def test_turn_authority_is_erased_after_chat_finishes(self):
        class Probe(self.gateway.Handler):
            def _do_POST(probe_self):
                self.gateway._agent_token.update(
                    token="short-lived", upn="person@example.com",
                    tier="owner", routine_capability="signed")

        handler = object.__new__(Probe)
        handler.path = "/v1/chat/completions"
        handler.do_POST()

        self.assertEqual(
            {
                "token": "", "upn": "", "tier": "outsider",
                "routine_capability": "", "correlation": "",
            },
            self.gateway._agent_token,
        )

    def test_log_lines_receive_current_turn_correlation_without_identity(self):
        self.gateway._agent_token["correlation"] = "safe-correlation"
        output = io.StringIO()
        try:
            with mock.patch.object(self.gateway, "_ship"):
                with contextlib.redirect_stdout(output):
                    self.gateway.log("tool completed")
        finally:
            self.gateway._clear_turn_authority()

        self.assertEqual(
            "tool completed | correlation=safe-correlation",
            output.getvalue().strip(),
        )

    def test_dependency_health_reports_only_component_booleans(self):
        with mock.patch.object(
            self.gateway, "_dependency_socket", side_effect=[True, False]
        ):
            health = self.gateway._dependency_health()

        self.assertEqual({
            "ok": False,
            "components": {
                "gateway": True,
                "hermes": True,
                "m365_sidecar": False,
            },
        }, health)
        rendered = str(health)
        self.assertNotIn(self.gateway.HERMES_URL, rendered)
        if self.gateway.GATEWAY_KEY:
            self.assertNotIn(self.gateway.GATEWAY_KEY, rendered)

    def test_deep_health_returns_503_until_all_dependencies_are_reachable(self):
        handler = object.__new__(self.gateway.Handler)
        handler.path = "/healthz"
        sent = []
        handler._send = lambda status, body=b"", **kwargs: sent.append((status, body))

        with mock.patch.object(self.gateway, "_dependency_health", return_value={
            "ok": False,
            "components": {"gateway": True, "hermes": False, "m365_sidecar": True},
        }):
            handler.do_GET()

        self.assertEqual(503, sent[0][0])
        self.assertEqual(False, __import__("json").loads(sent[0][1])["ok"])

    def test_shallow_root_health_remains_available_during_cold_start(self):
        handler = object.__new__(self.gateway.Handler)
        handler.path = "/"
        sent = []
        handler._send = lambda status, body=b"", **kwargs: sent.append((status, body))

        with mock.patch.object(self.gateway, "_dependency_health") as deep:
            handler.do_GET()

        self.assertEqual((200, b'{"ok":true}'), sent[0])
        deep.assert_not_called()

    def test_edge_log_accepts_only_prehashed_session_tag(self):
        handler = object.__new__(self.gateway.Handler)
        handler.path = "/v1/edge-log"
        handler._authorised = lambda: True
        handler._body = lambda: json.dumps({
            "stage": "test",
            "detail": "structural",
            "session": "raw-platform-session",
            "session_tag": "123456789abc",
        }).encode()
        sent = []
        handler._send = lambda status, body=b"", **kwargs: sent.append(status)

        with mock.patch.object(self.gateway, "log") as log:
            handler._do_POST()

        self.assertEqual([204], sent)
        rendered = str(log.call_args)
        self.assertIn("session_tag=123456789abc", rendered)
        self.assertNotIn("raw-platform-session", rendered)


if __name__ == "__main__":
    unittest.main()
