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


class PublicationFailureHandlingTests(unittest.TestCase):
    def run_publisher(self, mode):
        agent = {
            "blueprint": {"client_id": BLUEPRINT},
            "agent_endpoint": {
                "protocols": ["activity", "responses"],
                "authorization_schemes": [{"type": "BotServiceTenant"}],
                "version_selector": {"version_selection_rules": [
                    {"agent_version": "1", "traffic_percentage": 100}
                ]},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            (temp / "agent.json").write_text(json.dumps(agent))
            fake_az = temp / "az"
            fake_az.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                case "$*" in
                  rest*) cat "$FIXTURE_DIR/agent.json" ;;
                  account\\ get-access-token*) printf '%s\\n' fake-token ;;
                  *) printf 'unexpected az call: %s\\n' "$*" >&2; exit 9 ;;
                esac
            """))
            fake_curl = temp / "curl"
            fake_curl.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                output=""
                while (($#)); do
                  if [ "$1" = --output ]; then output="$2"; shift 2; else shift; fi
                done
                case "$CURL_MODE" in
                  transport) exit 28 ;;
                  duplicate)
                    printf '%s' '{"error":{"code":"UserError","message":"version already exists"}}' > "$output"
                    printf 400 ;;
                  server)
                    printf '%s' '{"error":{"code":"InternalError","message":"private-tenant-value"}}' > "$output"
                    printf 500 ;;
                  success)
                    printf '%s' '{"status":"submitted"}' > "$output"
                    printf 200 ;;
                  *) exit 9 ;;
                esac
            """))
            for executable in (fake_az, fake_curl):
                executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            environment = dict(os.environ)
            environment.update({
                "PATH": f"{temp}:{environment['PATH']}",
                "FIXTURE_DIR": str(temp),
                "CURL_MODE": mode,
                "ACCOUNT": "example-account",
                "PROJECT": "example-project",
                "AGENT": "example-agent",
            })
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/publish-autopilot-blueprint.sh")],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            return result

    def test_transport_failure_reports_uncertain_outcome_without_retry(self):
        result = self.run_publisher("transport")

        self.assertEqual(1, result.returncode)
        self.assertIn("outcome may be unknown", result.stderr)
        self.assertIn("pending-agent requests", result.stderr)

    def test_server_error_does_not_print_response_body(self):
        result = self.run_publisher("server")

        self.assertEqual(1, result.returncode)
        self.assertIn("HTTP 500", result.stderr)
        self.assertNotIn("private-tenant-value", result.stderr)
        self.assertNotIn("private-tenant-value", result.stdout)

    def test_only_structured_version_conflict_is_idempotent_success(self):
        result = self.run_publisher("duplicate")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("existing request was left unchanged", result.stdout)

    def test_successful_publication_is_reported(self):
        result = self.run_publisher("success")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Developer publication completed", result.stdout)


if __name__ == "__main__":
    unittest.main()
