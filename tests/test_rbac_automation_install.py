import json
import os
import pathlib
import stat
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).parents[1]
SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"
BLUEPRINT = "33333333-3333-3333-3333-333333333333"
WORKFLOW = "hermes-agent-foundry-role-sync"
PROJECT_SCOPE = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/example-rg/providers/"
    "Microsoft.CognitiveServices/accounts/example-account/projects/example-project"
)
WORKFLOW_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/example-rg/providers/"
    f"Microsoft.Logic/workflows/{WORKFLOW}"
)


class RbacAutomationInstallTests(unittest.TestCase):
    def run_installer(
        self, *, list_failure=False, provider_failure=False, installed_blueprint=BLUEPRINT, explicit=False, empty=False
    ):
        workflow = {
            "id": WORKFLOW_ID,
            "name": WORKFLOW,
            "identity": {"type": "SystemAssigned", "principalId": "a" * 36},
            "properties": {"parameters": {
                "projectScope": {"value": PROJECT_SCOPE},
                "blueprintClientId": {"value": installed_blueprint},
                "foundryUserRoleId": {"value": "53ca6127-db72-4b80-b1b0-d745d6d5456d"},
                "notBefore": {"value": "2026-09-01T00:00:00Z"},
            }},
        }
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            calls = temp / "calls.log"
            (temp / "workflows.json").write_text(json.dumps([] if empty else [{"id": WORKFLOW_ID, "name": WORKFLOW}]))
            (temp / "workflow.json").write_text(json.dumps(workflow))
            fake_az = temp / "az"
            fake_az.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                printf 'az %s\\n' "$*" >> "$CALL_LOG"
                case "$*" in
                  "account show") exit 0 ;;
                  "provider show --namespace Microsoft.Logic --query registrationState -o tsv")
                    if [ "$PROVIDER_FAILURE" = true ]; then exit 3; fi
                    printf Registered ;;
                  "account show --query id -o tsv") printf '%s\\n' "$TEST_SUBSCRIPTION" ;;
                  resource\\ show*Microsoft.CognitiveServices*) exit 0 ;;
                  resource\\ list*)
                    if [ "$LIST_FAILURE" = true ]; then exit 3; fi
                    cat "$FIXTURE_DIR/workflows.json" ;;
                  resource\\ show*Microsoft.Logic*) cat "$FIXTURE_DIR/workflow.json" ;;
                  *) printf 'unexpected az call: %s\\n' "$*" >&2; exit 9 ;;
                esac
            """))
            fake_az.chmod(fake_az.stat().st_mode | stat.S_IXUSR)
            environment = dict(os.environ)
            environment.update({
                "PATH": f"{temp}:{environment['PATH']}",
                "CALL_LOG": str(calls),
                "FIXTURE_DIR": str(temp),
                "LIST_FAILURE": "true" if list_failure else "false",
                "PROVIDER_FAILURE": "true" if provider_failure else "false",
                "TEST_SUBSCRIPTION": SUBSCRIPTION,
            })
            command = [
                "bash", str(ROOT / "scripts/install-agent-rbac-automation.sh"),
                "--resource-group", "example-rg",
                "--account", "example-account",
                "--project", "example-project",
                "--blueprint-client-id", BLUEPRINT,
                "--location", "eastus2",
            ]
            if explicit:
                command += ["--workflow-name", WORKFLOW]
            result = subprocess.run(
                command, text=True, capture_output=True, env=environment, check=False
            )
            return result, calls.read_text()

    def test_unreadable_workflow_collection_stops_before_deployment(self):
        result, calls = self.run_installer(list_failure=True)

        self.assertEqual(1, result.returncode)
        self.assertIn("could not be listed", result.stderr)
        self.assertNotIn("deployment group create", calls)

    def test_unreadable_provider_state_stops_before_registration_or_deployment(self):
        result, calls = self.run_installer(provider_failure=True)

        self.assertEqual(1, result.returncode)
        self.assertIn("registration state could not be read", result.stderr)
        self.assertNotIn("provider register", calls)
        self.assertNotIn("deployment group create", calls)

    def test_existing_workflow_for_other_blueprint_stops_before_deployment(self):
        result, calls = self.run_installer(
            installed_blueprint="11111111-1111-1111-1111-111111111111", explicit=True
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("different Agent Identity Blueprint", result.stderr)
        self.assertNotIn("deployment group create", calls)

    def test_default_collision_uses_scoped_workflow(self):
        result, calls = self.run_installer(installed_blueprint="11111111-1111-1111-1111-111111111111")
        self.assertIn("using a separate scoped workflow", result.stderr)
        self.assertIn("deployment group create", calls)
        self.assertIn("workflowName=" + WORKFLOW + "-", calls)

    def test_fresh_tenant_uses_scoped_workflow(self):
        result, calls = self.run_installer(empty=True)
        self.assertIn("deployment group create", calls)
        self.assertIn("workflowName=" + WORKFLOW + "-", calls)

    def test_matching_workflow_preserves_original_cutoff_on_redeployment(self):
        result, calls = self.run_installer()

        # The fake Azure CLI deliberately rejects the deployment; reaching it proves validation
        # accepted the exact existing target and carried its original boundary forward.
        self.assertNotEqual(0, result.returncode)
        self.assertIn("deployment group create", calls)
        self.assertIn("notBefore=2026-09-01T00:00:00Z", calls)


if __name__ == "__main__":
    unittest.main()
