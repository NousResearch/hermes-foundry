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
PRINCIPAL = "44444444-4444-4444-4444-444444444444"
WORKFLOW = "hermes-agent-foundry-role-sync"
PROJECT_SCOPE = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/example-rg/providers/"
    "Microsoft.CognitiveServices/accounts/example-account/projects/example-project"
)
WORKFLOW_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/example-rg/providers/"
    f"Microsoft.Logic/workflows/{WORKFLOW}"
)


class RbacAutomationRemovalTests(unittest.TestCase):
    def run_removal(self, *, installed_blueprint=BLUEPRINT, workflow_present=True, extra=()):
        workflow = {
            "id": WORKFLOW_ID,
            "identity": {"principalId": PRINCIPAL},
            "properties": {"parameters": {
                "projectScope": {"value": PROJECT_SCOPE},
                "blueprintClientId": {"value": installed_blueprint},
                "foundryUserRoleId": {"value": "53ca6127-db72-4b80-b1b0-d745d6d5456d"},
            }},
        }
        assignment = {
            "id": f"{PROJECT_SCOPE}/providers/Microsoft.Authorization/roleAssignments/{PRINCIPAL}",
            "roleDefinitionId": (
                f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/"
                "roleDefinitions/f58310d9-a9f6-439a-9e8d-f62e7b41a168"
            ),
        }

        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            calls = temp / "calls.log"
            state = temp / "workflow-state"
            state.write_text("present" if workflow_present else "absent")
            (temp / "workflow.json").write_text(json.dumps(workflow))
            (temp / "assignments.json").write_text(json.dumps([assignment]))
            fake_az = temp / "az"
            fake_az.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                printf 'az %s\\n' "$*" >> "$CALL_LOG"
                case "$*" in
                  "account show --query id --output tsv") printf '%s\\n' "$TEST_SUBSCRIPTION" ;;
                  resource\\ show*Microsoft.CognitiveServices*"--output none") exit 0 ;;
                  resource\\ show*Microsoft.Logic*"--output json")
                    if [ "$(cat "$STATE_FILE")" = present ]; then
                      cat "$FIXTURE_DIR/workflow.json"
                    else
                      exit 3
                    fi ;;
                  resource\\ list*) printf '[{"name":"hermes-agent-foundry-role-sync"}]' ;;
                  role\\ assignment\\ list*) cat "$FIXTURE_DIR/assignments.json" ;;
                  role\\ assignment\\ delete*) exit 0 ;;
                  resource\\ delete*Microsoft.Logic*) printf absent > "$STATE_FILE" ;;
                  resource\\ show*Microsoft.Logic*"--output none")
                    [ "$(cat "$STATE_FILE")" = present ] ;;
                  *) printf 'unexpected az call: %s\\n' "$*" >&2; exit 9 ;;
                esac
            """))
            fake_az.chmod(fake_az.stat().st_mode | stat.S_IXUSR)
            environment = dict(os.environ)
            environment.update({
                "PATH": f"{temp}:{environment['PATH']}",
                "CALL_LOG": str(calls),
                "STATE_FILE": str(state),
                "FIXTURE_DIR": str(temp),
                "TEST_SUBSCRIPTION": SUBSCRIPTION,
            })
            command = [
                "bash", str(ROOT / "scripts/remove-agent-rbac-automation.sh"),
                "--subscription", SUBSCRIPTION,
                "--resource-group", "example-rg",
                "--account", "example-account",
                "--project", "example-project",
                "--blueprint-client-id", BLUEPRINT,
                *extra,
            ]
            result = subprocess.run(
                command, text=True, capture_output=True, env=environment, check=False
            )
            return result, calls.read_text(), state.read_text()

    def test_default_is_read_only_verified_plan(self):
        result, calls, state = self.run_removal()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Verified removal plan", result.stdout)
        self.assertIn("PLAN ONLY", result.stdout)
        self.assertEqual("present", state)
        self.assertNotIn("role assignment delete", calls)
        self.assertNotIn("resource delete", calls)

    def test_mismatched_blueprint_aborts_before_mutation(self):
        result, calls, state = self.run_removal(
            installed_blueprint="11111111-1111-1111-1111-111111111111",
            extra=("--workflow-name", WORKFLOW, "--execute", "--confirm-workflow-name", WORKFLOW),
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("blueprintClientId does not match", result.stderr)
        self.assertEqual("present", state)
        self.assertNotIn("role assignment delete", calls)
        self.assertNotIn("resource delete", calls)

    def test_execute_requires_exact_workflow_confirmation(self):
        result, calls, state = self.run_removal(extra=("--execute",))

        self.assertEqual(2, result.returncode)
        self.assertIn("must exactly match", result.stderr)
        self.assertEqual("present", state)
        self.assertNotIn("role assignment delete", calls)
        self.assertNotIn("resource delete", calls)

    def test_unreadable_or_absent_workflow_fails_closed(self):
        result, calls, state = self.run_removal(workflow_present=False)

        self.assertEqual(1, result.returncode)
        self.assertIn("could not be read", result.stderr)
        self.assertEqual("absent", state)
        self.assertNotIn("role assignment delete", calls)
        self.assertNotIn("resource delete", calls)

    def test_confirmed_execute_removes_only_delegation_and_workflow(self):
        result, calls, state = self.run_removal(
            extra=("--execute", "--confirm-workflow-name", WORKFLOW)
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("absent", state)
        self.assertEqual(1, calls.count("role assignment delete"))
        self.assertEqual(1, calls.count("resource delete"))
        self.assertIn("Foundry User assignments were left unchanged", result.stdout)


if __name__ == "__main__":
    unittest.main()
