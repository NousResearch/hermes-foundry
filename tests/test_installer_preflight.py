import os
import pathlib
import stat
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).parents[1]
TENANT = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"


class InstallerPreflightTests(unittest.TestCase):
    def run_preflight(self, provider_state="Registered"):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            calls = root / "calls.log"
            az = root / "az"
            az.write_text(textwrap.dedent(f"""\
                #!/usr/bin/env bash
                printf 'az %s\\n' "$*" >> "$CALL_LOG"
                case "$*" in
                  "account show --query tenantId --output tsv") printf '%s\\n' "$TEST_TENANT" ;;
                  "account show --query id --output tsv") printf '%s\\n' "$TEST_SUBSCRIPTION" ;;
                  account\\ get-access-token*) exit 0 ;;
                  provider\\ show*) printf '%s\\n' "{provider_state}" ;;
                  account\\ list-locations*) printf '%s\\n' eastus2 ;;
                  "bicep version") printf '%s\\n' 'Bicep CLI version 1.0' ;;
                  *) printf 'unexpected az call: %s\\n' "$*" >&2; exit 9 ;;
                esac
            """))
            azd = root / "azd"
            azd.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                printf 'azd %s\\n' "$*" >> "$CALL_LOG"
                [[ "$1 $2" == "auth token" ]]
            """))
            az.chmod(az.stat().st_mode | stat.S_IXUSR)
            azd.chmod(azd.stat().st_mode | stat.S_IXUSR)
            env = dict(os.environ)
            env.update({
                "PATH": f"{root}:{env['PATH']}",
                "CALL_LOG": str(calls),
                "TEST_TENANT": TENANT,
                "TEST_SUBSCRIPTION": SUBSCRIPTION,
            })
            result = subprocess.run(
                [
                    "bash", str(ROOT / "scripts/validate-installation.sh"),
                    "--tenant", TENANT,
                    "--subscription", SUBSCRIPTION,
                    "--location", "East US 2",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            return result, calls.read_text()

    def test_success_is_read_only_and_checks_both_token_audiences(self):
        result, calls = self.run_preflight()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Preflight passed. No resources were changed.", result.stdout)
        self.assertIn("--resource https://management.azure.com/", calls)
        self.assertIn("--resource https://ai.azure.com", calls)
        for mutation in (" register", " create", " set", " deployment"):
            self.assertNotIn(mutation, calls)

    def test_missing_provider_fails_without_registering_it(self):
        result, calls = self.run_preflight(provider_state="NotRegistered")

        self.assertEqual(1, result.returncode)
        self.assertIn("Microsoft.CognitiveServices is not registered", result.stderr)
        self.assertIn("No resources were changed", result.stderr)
        self.assertNotIn("provider register", calls)


if __name__ == "__main__":
    unittest.main()
