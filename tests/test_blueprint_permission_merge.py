import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts/merge_inheritable_scopes.py"
SPEC = importlib.util.spec_from_file_location("merge_inheritable_scopes", MODULE_PATH)
MERGER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MERGER)


class BlueprintPermissionMergeTests(unittest.TestCase):
    def test_preserves_existing_tenant_scopes_and_adds_missing_hermes_scopes(self):
        result = MERGER.merge(
            {
                "inheritableScopes": {
                    "@odata.type": "#microsoft.graph.enumeratedScopes",
                    "scopes": ["Custom.Scope", "Mail.Send"],
                }
            },
            ["Mail.Send", "Files.ReadWrite.All"],
        )

        self.assertEqual(
            ["Custom.Scope", "Mail.Send", "Files.ReadWrite.All"],
            result["inheritableScopes"]["scopes"],
        )

    def test_rejects_malformed_existing_scope_data(self):
        for value in (
            [],
            {"inheritableScopes": []},
            {"inheritableScopes": {"scopes": "Mail.Send"}},
            {"inheritableScopes": {"scopes": ["Mail.Send", 3]}},
            {"inheritableScopes": {"scopes": ["invalid scope"]}},
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    MERGER.merge(value, ["Mail.Send"])

    def test_reconciliation_script_uses_additive_patch_and_hides_error_body(self):
        script = (ROOT / "scripts/configure-blueprint-graph-permissions.sh").read_text()

        self.assertIn("merge_inheritable_scopes.py", script)
        self.assertIn('"@$WORK/inheritance-patch.json"', script)
        self.assertNotIn("sed -n '1,20p' \"$WORK/current-inheritance.json\"", script)


if __name__ == "__main__":
    unittest.main()
