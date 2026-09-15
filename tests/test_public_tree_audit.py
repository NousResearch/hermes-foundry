import importlib.util
import pathlib
import unittest


def _load_audit():
    path = pathlib.Path(__file__).parents[1] / "scripts" / "audit-public-tree.py"
    spec = importlib.util.spec_from_file_location("hfull_public_tree_audit_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicTreeAuditTests(unittest.TestCase):
    def setUp(self):
        self.audit = _load_audit()

    def test_atlassian_tokens_are_detected(self):
        value = "AT" + "ATT" + "a" * 40
        self.assertIsNotNone(self.audit.CONTENT_RULES["Atlassian token"].search(value))

    def test_private_keys_are_detected(self):
        value = "-----BEGIN " + "PRIVATE KEY-----"
        self.assertIsNotNone(self.audit.CONTENT_RULES["private key"].search(value))

    def test_real_tenant_domains_and_emails_are_detected(self):
        tenant = "company" + ".onmicrosoft.com"
        self.assertIsNotNone(self.audit.CONTENT_RULES["tenant domain"].search(tenant))
        self.assertIsNotNone(self.audit.EMAIL.search("person@" + tenant))

    def test_documentation_placeholders_and_example_addresses_are_allowed(self):
        placeholder = "<organization>" + ".onmicrosoft.com"
        self.assertIsNone(self.audit.CONTENT_RULES["tenant domain"].search(placeholder))
        self.assertEqual(self.audit.EMAIL.search("person@example.com").group(1), "example.com")

    def test_unreviewed_guid_is_not_allowlisted(self):
        value = "9f0c" + "8a01-2b7d-4c39-8a11-123456789abc"
        self.assertRegex(value, self.audit.GUID)
        self.assertNotIn(value, self.audit.ALLOWED_GUIDS)


if __name__ == "__main__":
    unittest.main()
