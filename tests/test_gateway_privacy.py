import contextlib
import importlib.util
import io
import os
import pathlib
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


if __name__ == "__main__":
    unittest.main()
