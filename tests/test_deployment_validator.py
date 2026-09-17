import json
import os
import pathlib
import stat
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).parents[1]
BLUEPRINT = "33333333-3333-3333-3333-333333333333"


class DeploymentValidatorTests(unittest.TestCase):
    def run_validator(self, agent_logic="HermesRouter"):
        agent = {
            "agent_endpoint": {
                "protocols": ["activity", "responses"],
                "authorization_schemes": [{"type": "BotServiceTenant"}],
                "version_selector": {"version_selection_rules": [
                    {"agent_version": "7", "traffic_percentage": 100, "type": "FixedRatio"}
                ]},
            },
            "blueprint": {"client_id": BLUEPRINT},
        }
        env = {
            name: "secret-value"
            for name in (
                "AgentTenantId", "AgentBlueprintId", "AZURE_AI_PROJECT_ENDPOINT",
                "AZURE_FOUNDRY_BASE_URL", "MODEL_DEPLOYMENT_NAME",
                "FALLBACK_MODEL_DEPLOYMENT_NAME", "MODEL_PRIORITY",
                "MODEL_DEPLOYMENTS_SNAPSHOT", "IdentitySignKey",
            )
        }
        env["AgentLogic"] = agent_logic
        version = {
            "status": "active",
            "definition": {
                "image": "registry.example/hermes@sha256:" + "a" * 64,
                "container_protocol_versions": [
                    {"protocol": "activity_protocol", "version": "v1"},
                    {"protocol": "responses", "version": "2.0.0"},
                ],
                "environment_variables": env,
            },
        }
        inheritance = {"inheritableScopes": {"scopes": [
            "Chat.ReadWrite", "Mail.ReadWrite", "Mail.Send", "User.Read.All",
            "User.ReadBasic.All", "Files.ReadWrite.All", "Calendars.ReadWrite",
            "Sites.ReadWrite.All",
        ]}}
        bot = {"properties": {
            "msaAppId": BLUEPRINT,
            "endpoint": "https://example.test/protocols/activityProtocol?api-version=v1",
        }}

        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            calls = temp / "calls.log"
            fixtures = temp / "fixtures"
            fixtures.mkdir()
            for name, value in (
                ("agent", agent), ("version", version),
                ("inheritance", inheritance), ("bot", bot),
            ):
                (fixtures / f"{name}.json").write_text(json.dumps(value))
            fake_az = temp / "az"
            fake_az.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                printf 'az %s\\n' "$*" >> "$CALL_LOG"
                case "$*" in
                  *"/agents/hermes-agent-on-foundry?api-version=v1"*) cat "$FIXTURES/agent.json" ;;
                  *"/agents/hermes-agent-on-foundry/versions/7?api-version=v1"*) cat "$FIXTURES/version.json" ;;
                  *"inheritablePermissions"*) cat "$FIXTURES/inheritance.json" ;;
                  resource\\ show*) cat "$FIXTURES/bot.json" ;;
                  *) printf 'unexpected az call: %s\\n' "$*" >&2; exit 9 ;;
                esac
            """))
            fake_az.chmod(fake_az.stat().st_mode | stat.S_IXUSR)
            environment = dict(os.environ)
            environment.update({
                "PATH": f"{temp}:{environment['PATH']}",
                "CALL_LOG": str(calls),
                "FIXTURES": str(fixtures),
            })
            result = subprocess.run([
                "bash", str(ROOT / "scripts/validate-deployment.sh"),
                "--account", "example-account",
                "--project", "example-project",
                "--agent", "hermes-agent-on-foundry",
                "--bot-service-id", "/subscriptions/example/resourceGroups/example/providers/Microsoft.BotService/botServices/hermes",
            ], text=True, capture_output=True, env=environment, check=False)
            return result, calls.read_text()

    def test_valid_deployment_passes_without_printing_values_or_mutating(self):
        result, calls = self.run_validator()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Deployment validation passed", result.stdout)
        self.assertIn("values not printed", result.stdout)
        self.assertNotIn("secret-value", result.stdout + result.stderr)
        for mutation in ("--method POST", "--method PUT", "--method PATCH", "--method DELETE"):
            self.assertNotIn(mutation, calls)

    def test_non_production_runtime_mode_fails(self):
        result, _ = self.run_validator(agent_logic="ResponsesApi")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("not using the production HermesRouter mode", result.stderr)


if __name__ == "__main__":
    unittest.main()
