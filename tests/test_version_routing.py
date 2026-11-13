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


class VersionRoutingTests(unittest.TestCase):
    def run_router(
        self,
        current="7",
        expected="7",
        target="8",
        target_logic="HermesRouter",
        patch_behavior="success",
        version_states="active",
        concurrent_route="",
    ):
        def agent(version):
            return {
                "agent_endpoint": {"version_selector": {"version_selection_rules": [{
                    "agent_version": version,
                    "traffic_percentage": 100,
                    "type": "FixedRatio",
                }]}},
                "blueprint": {"client_id": BLUEPRINT},
            }

        env = {
            name: "secret-value"
            for name in (
                "AgentTenantId", "AZURE_AI_PROJECT_ENDPOINT", "AZURE_FOUNDRY_BASE_URL",
                "MODEL_DEPLOYMENT_NAME", "FALLBACK_MODEL_DEPLOYMENT_NAME", "MODEL_PRIORITY",
                "MODEL_DEPLOYMENTS_SNAPSHOT", "IdentitySignKey",
            )
        }
        env["AgentLogic"] = target_logic
        env["AgentBlueprintId"] = BLUEPRINT
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

        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            calls = temp / "calls.log"
            state = temp / "state"
            state.write_text(current)
            fixtures = temp / "fixtures"
            fixtures.mkdir()
            (fixtures / "version.json").write_text(json.dumps(version))
            (fixtures / "inheritance.json").write_text(json.dumps(inheritance))
            fake_az = temp / "az"
            fake_az.write_text(textwrap.dedent(f"""\
                #!/usr/bin/env bash
                printf 'az %s\\n' "$*" >> "$CALL_LOG"
                method=GET
                previous=""
                for argument in "$@"; do
                  if [ "$previous" = "--method" ]; then method="$argument"; fi
                  previous="$argument"
                done
                case "$*" in
                  *"/agents/hermes-agent-on-foundry/versions/{target}?api-version=v1"*)
                    python3 - "$FIXTURES/version.json" "$FIXTURES/version-count" <<'PYVER'
import json, os, pathlib, sys
count = pathlib.Path(sys.argv[2])
n = int(count.read_text()) if count.exists() else 0
states = os.environ['VERSION_STATES'].split(',')
data = json.load(open(sys.argv[1]))
data['status'] = states[min(n, len(states) - 1)]
count.write_text(str(n + 1))
if os.environ.get('CONCURRENT_ROUTE'):
    pathlib.Path(os.environ['STATE_FILE']).write_text(os.environ['CONCURRENT_ROUTE'])
print(json.dumps(data))
PYVER
                    ;;
                  *"inheritablePermissions"*) cat "$FIXTURES/inheritance.json" ;;
                  *"/agents/hermes-agent-on-foundry?api-version=v1"*)
                    if [ "$method" = "PATCH" ]; then
                      if [ "$PATCH_BEHAVIOR" = "before_apply_failure" ]; then exit 7; fi
                      printf '{target}' > "$STATE_FILE"
                      if [ "$PATCH_BEHAVIOR" = "after_apply_failure" ]; then exit 7; fi
                    else
                      python3 - "$STATE_FILE" <<'PY'
import json, sys
value = open(sys.argv[1]).read().strip()
print(json.dumps({{
    "agent_endpoint": {{"version_selector": {{"version_selection_rules": [{{
        "agent_version": value, "traffic_percentage": 100, "type": "FixedRatio"
    }}]}}}},
    "blueprint": {{"client_id": "{BLUEPRINT}"}},
}}))
PY
                    fi ;;
                  *) printf 'unexpected az call: %s\\n' "$*" >&2; exit 9 ;;
                esac
            """).lstrip())
            fake_az.chmod(fake_az.stat().st_mode | stat.S_IXUSR)
            environment = dict(os.environ)
            environment.update({
                "PATH": f"{temp}:{environment['PATH']}",
                "CALL_LOG": str(calls),
                "STATE_FILE": str(state),
                "FIXTURES": str(fixtures),
                "PATCH_BEHAVIOR": patch_behavior,
                "VERSION_STATES": version_states,
                "CONCURRENT_ROUTE": concurrent_route,
                "HERMES_READY_TIMEOUT": "0.2",
                "HERMES_READY_POLL_INTERVAL": "0.01",
            })
            result = subprocess.run([
                "bash", str(ROOT / "scripts/route-agent-version.sh"),
                "--account", "example-account",
                "--project", "example-project",
                "--agent", "hermes-agent-on-foundry",
                "--target-version", target,
                "--expect-current", expected,
            ], text=True, capture_output=True, env=environment, check=False)
            return result, calls.read_text(), state.read_text()

    def test_route_validates_then_patches_and_reads_back(self):
        result, calls, state = self.run_router()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("8", state)
        self.assertIn("routing update succeeded", result.stdout)
        self.assertEqual(1, calls.count("--method PATCH"))
        self.assertNotIn("secret-value", result.stdout + result.stderr)

    def test_stale_current_version_aborts_before_any_write(self):
        result, calls, state = self.run_router(current="9", expected="7")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("expected serving version 7", result.stderr)
        self.assertNotIn("--method PATCH", calls)
        self.assertEqual("9", state)

    def test_non_production_target_aborts_before_any_write(self):
        result, calls, state = self.run_router(target_logic="ResponsesApi")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("not using the production HermesRouter mode", result.stderr)
        self.assertNotIn("--method PATCH", calls)
        self.assertEqual("7", state)

    def test_patch_error_is_resolved_by_reading_actual_route(self):
        result, calls, state = self.run_router(patch_behavior="after_apply_failure")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("8", state)
        self.assertIn("reported an error, but read-back proves", result.stdout)
        self.assertEqual(1, calls.count("--method PATCH"))

    def test_patch_error_without_route_change_fails_without_retry(self):
        result, calls, state = self.run_router(patch_behavior="before_apply_failure")

        self.assertNotEqual(0, result.returncode)
        self.assertEqual("7", state)
        self.assertIn("Timed out waiting", result.stderr)
        self.assertEqual(1, calls.count("--method PATCH"))

    def test_waits_for_creating_version_then_routes(self):
        result, calls, state = self.run_router(version_states='creating,creating,active')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('8', state)
        self.assertEqual(1, calls.count('--method PATCH'))

    def test_terminal_version_failure_does_not_route(self):
        result, calls, state = self.run_router(version_states='failed')
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn('--method PATCH', calls)

    def test_already_routed_resume_validates_without_patch(self):
        result, calls, state = self.run_router(current='8')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn('--method PATCH', calls)
        self.assertIn('already serving', result.stdout)

    def test_route_changes_during_readiness_wait_are_not_overwritten(self):
        result, calls, state = self.run_router(concurrent_route='9')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual('9', state)
        self.assertNotIn('--method PATCH', calls)


if __name__ == "__main__":
    unittest.main()
